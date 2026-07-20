"""Contract tests for the authenticated investment-news HTTP boundary."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.news.models import RefreshAcceptedResponse, RefreshStatus, SnapshotResponse


class FakeNewsCoordinator:
    """In-memory coordinator that cannot construct feeds or an LLM."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.task_id = UUID("00000000-0000-0000-0000-000000000001")
        self.start_calls = 0
        self.close_calls = 0
        self._started = False

    async def start(self) -> RefreshAcceptedResponse:
        self.start_calls += 1
        reused = self._started and not self.gate.is_set()
        if not reused:
            self._started = True
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
    return TestClient(app)


def test_snapshot_returns_unavailable_envelope_without_starting_refresh(
    client: TestClient, coordinator: FakeNewsCoordinator
) -> None:
    response = client.get("/news-api/snapshot")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["snapshot"] is None
    assert coordinator.start_calls == 0


def test_refresh_returns_202_and_reuses_task(client: TestClient) -> None:
    first = client.post("/news-api/refresh")
    second = client.post("/news-api/refresh")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["reused"] is True
    assert second.json()["task_id"] == first.json()["task_id"]


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


def test_coordinator_close_is_called_by_api_shutdown_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    import api_server

    coordinator = FakeNewsCoordinator()
    monkeypatch.setattr(api_server, "news_coordinator", coordinator)

    asyncio.run(api_server._close_news_coordinator())

    assert coordinator.close_calls == 1


def test_api_server_imports_with_scheduled_204_route() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import api_server"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
