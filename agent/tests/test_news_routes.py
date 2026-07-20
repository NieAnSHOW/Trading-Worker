"""Contract tests for the authenticated investment-news HTTP boundary."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.news.coordinator import NewsRefreshCoordinator
from src.news.models import RefreshAcceptedResponse, RefreshStatus, SnapshotResponse
from src.news.storage import SnapshotStorageError


class FakeNewsCoordinator:
    """In-memory coordinator that cannot construct feeds or an LLM."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.running = asyncio.Event()
        self.task_id = UUID("00000000-0000-0000-0000-000000000001")
        self.start_calls = 0
        self.background_starts = 0
        self.close_calls = 0
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> RefreshAcceptedResponse:
        self.start_calls += 1
        reused = self._task is not None and not self._task.done()
        if not reused:
            self._task = asyncio.create_task(self._refresh())
            await self.running.wait()
        status = RefreshStatus(
            state="fetching",
            task_id=self.task_id,
            started_at=datetime.now(timezone.utc),
        )
        return RefreshAcceptedResponse(task_id=self.task_id, reused=reused, status=status)

    async def status(self) -> RefreshStatus:
        return RefreshStatus(state="idle")

    async def snapshot_response(self) -> SnapshotResponse:
        return SnapshotResponse(
            available=False,
            stale=False,
            snapshot=None,
            refresh=RefreshStatus(state="idle"),
        )

    async def close(self) -> None:
        self.close_calls += 1
        self.gate.set()
        if self._task is not None:
            await self._task

    async def _refresh(self) -> None:
        self.background_starts += 1
        self.running.set()
        await self.gate.wait()


@pytest.fixture()
def coordinator() -> FakeNewsCoordinator:
    return FakeNewsCoordinator()


@pytest.fixture()
def client(coordinator: FakeNewsCoordinator) -> TestClient:
    from src.api.news_routes import register_news_routes

    app = FastAPI()

    async def allow_request() -> None:
        return None

    register_news_routes(app, require_auth=allow_request, coordinator=coordinator)

    @app.on_event("shutdown")
    async def close_coordinator() -> None:
        await coordinator.close()

    with TestClient(app) as test_client:
        yield test_client


def test_snapshot_returns_unavailable_envelope_without_starting_refresh(
    client: TestClient, coordinator: FakeNewsCoordinator
) -> None:
    response = client.get("/news-api/snapshot")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["snapshot"] is None
    assert coordinator.start_calls == 0


def test_refresh_returns_202_and_reuses_the_task_awaiting_its_gate(client: TestClient, coordinator: FakeNewsCoordinator) -> None:
    first = client.post("/news-api/refresh")
    second = client.post("/news-api/refresh")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["reused"] is True
    assert second.json()["task_id"] == first.json()["task_id"]
    assert coordinator.running.is_set()
    assert coordinator.gate.is_set() is False
    assert coordinator.start_calls == 2
    assert coordinator.background_starts == 1


def test_refresh_status_does_not_start_work(client: TestClient, coordinator: FakeNewsCoordinator) -> None:
    response = client.get("/news-api/refresh/status")

    assert response.status_code == 200
    assert response.json()["state"] == "idle"
    assert coordinator.start_calls == 0


def test_news_routes_reject_body_and_query_parameters(client: TestClient) -> None:
    body = client.post("/news-api/refresh", json={"feed_url": "https://example.test/feed"})
    query = client.get("/news-api/snapshot?track_id=ai&llm=secret")
    get_body = client.request("GET", "/news-api/refresh/status", content=b"track_id=ai")

    assert body.status_code == 422
    assert query.status_code == 422
    assert get_body.status_code == 422
    assert body.headers["content-type"].startswith("application/json")
    assert "https://example.test/feed" not in body.text
    assert "secret" not in query.text


def test_snapshot_corruption_returns_a_redacted_envelope_without_logging_canary(caplog: pytest.LogCaptureFixture) -> None:
    class CorruptStore:
        def read(self) -> None:
            raise SnapshotStorageError("snapshot_corrupt", "snapshot-canary-do-not-disclose")

        def write(self, snapshot: object) -> None:
            raise AssertionError("snapshot reads must not write")

    coordinator = NewsRefreshCoordinator(store=CorruptStore())
    app = FastAPI()

    async def allow_request() -> None:
        return None

    from src.api.news_routes import register_news_routes

    register_news_routes(app, require_auth=allow_request, coordinator=coordinator)
    with TestClient(app) as test_client:
        response = test_client.get("/news-api/snapshot")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "stale": False,
        "snapshot": None,
        "refresh": {
            "state": "idle",
            "task_id": None,
            "started_at": None,
            "completed_at": None,
            "processed_endpoints": 0,
            "successful_endpoints": 0,
            "failed_endpoints": 0,
            "processed_tracks": 0,
            "total_endpoints": 106,
            "total_tracks": 12,
            "error": None,
        },
        "error": {"code": "snapshot_corrupt", "message": "news snapshot is corrupt"},
    }
    assert "snapshot-canary-do-not-disclose" not in response.text
    assert "snapshot-canary-do-not-disclose" not in caplog.text


def test_api_server_registers_and_closes_the_same_news_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    import api_server
    import src.api.news_routes as news_routes
    import src.news.coordinator as coordinator_module

    coordinator = FakeNewsCoordinator()
    registrations: list[object] = []

    def get_coordinator() -> FakeNewsCoordinator:
        return coordinator

    real_register = news_routes.register_news_routes

    def register_spy(app: FastAPI, require_auth: object, coordinator: object) -> None:
        registrations.append(coordinator)
        real_register(app, require_auth=require_auth, coordinator=coordinator)  # type: ignore[arg-type]

    monkeypatch.setattr(coordinator_module, "get_news_coordinator", get_coordinator)
    monkeypatch.setattr(news_routes, "register_news_routes", register_spy)
    monkeypatch.delitem(sys.modules, "api_server")

    reloaded_api_server = importlib.import_module("api_server")
    with TestClient(reloaded_api_server.app):
        pass

    assert registrations == [coordinator]
    assert coordinator.close_calls == 1
    assert reloaded_api_server.news_coordinator is coordinator
    monkeypatch.setitem(sys.modules, "api_server", api_server)


def test_api_server_imports_with_scheduled_204_route() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import api_server"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
