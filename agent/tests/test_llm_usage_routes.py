"""HTTP contract tests for the global LLM usage aggregation endpoint."""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from src.api import llm_usage_routes, state
from src.session.models import Attempt, Message, Session


_TEST_CLIENT_SUPPORTS_CLIENT = "client" in inspect.signature(TestClient).parameters

if not _TEST_CLIENT_SUPPORTS_CLIENT:

    @api_server.app.middleware("http")
    async def _compat_test_client_scope(request, call_next):
        host = request.headers.get("x-test-client-host")
        if host:
            request.scope["client"] = (host, 50000)
        return await call_next(request)


def _remote_client() -> TestClient:
    """Return a TestClient that simulates a non-loopback caller."""
    if not _TEST_CLIENT_SUPPORTS_CLIENT:
        return TestClient(api_server.app, headers={"X-Test-Client-Host": "203.0.113.10"})
    return TestClient(api_server.app, client=("203.0.113.10", 50000))


def _local_client(*, raise_server_exceptions: bool = True) -> TestClient:
    """Return a TestClient that simulates a loopback caller."""
    if not _TEST_CLIENT_SUPPORTS_CLIENT:
        return TestClient(
            api_server.app,
            headers={"X-Test-Client-Host": "127.0.0.1"},
            raise_server_exceptions=raise_server_exceptions,
        )
    return TestClient(
        api_server.app,
        client=("127.0.0.1", 50000),
        raise_server_exceptions=raise_server_exceptions,
    )


def _usage(total_tokens: int) -> dict[str, object]:
    return {
        "provider": "openai",
        "model": "gpt-test",
        "totals": {
            "input_tokens": total_tokens - 2,
            "output_tokens": 2,
            "total_tokens": total_tokens,
            "calls": 1,
        },
        "per_iteration": [
            {
                "iter": 1,
                "input_tokens": total_tokens - 2,
                "output_tokens": 2,
                "total_tokens": total_tokens,
            }
        ],
        "api_key": "api-key-sentinel",
        "login_token": "login-token-sentinel",
        "prompt": "attempt-prompt-sentinel",
        "response": "model-response-sentinel",
    }


@pytest.fixture(autouse=True)
def isolated_usage_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the lazy session singleton at test-only persisted data."""
    monkeypatch.setattr(api_server, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(api_server, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(api_server, "_session_service", None)
    monkeypatch.setattr(state, "_session_service", None)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")


@pytest.fixture
def usage_data() -> None:
    """Create two session-linked usage artifacts with sensitive sentinels."""
    service = api_server._get_session_service()
    assert service is not None

    for session_id, title, run_id, total_tokens, occurred_at in [
        ("alpha-session", "Alpha research", "run-alpha", 10, "2026-07-01T10:00:00Z"),
        ("beta-session", "title-sentinel", "run-beta", 20, "2026-07-02T10:00:00Z"),
    ]:
        service.store.create_session(
            Session(
                session_id=session_id,
                title=title,
                created_at=occurred_at,
                updated_at=occurred_at,
                config={"api_key": "api-key-sentinel", "login_token": "login-token-sentinel"},
            )
        )
        service.store.create_attempt(
            Attempt(
                attempt_id=f"attempt-{run_id}",
                session_id=session_id,
                run_dir=str(api_server.RUNS_DIR / run_id),
                prompt="attempt-prompt-sentinel",
                created_at=occurred_at,
            )
        )
        service.store.append_message(
            Message(
                session_id=session_id,
                role="assistant",
                content="message-body-sentinel",
                created_at=occurred_at,
                metadata={"run_id": run_id},
            )
        )
        run_dir = api_server.RUNS_DIR / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "llm_usage.json").write_text(json.dumps(_usage(total_tokens)), encoding="utf-8")


def test_local_usage_response_has_strict_safe_aggregate_contract(usage_data: None) -> None:
    response = _local_client().get(
        "/usage/llm",
        params={"timezone": "UTC", "page": 1, "page_size": 1},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "generated_at",
        "timezone",
        "period",
        "totals",
        "trend",
        "breakdown",
        "sessions",
    }
    assert set(body["period"]) == {"start_at", "end_at"}
    assert body["totals"]["total_tokens"] == 30
    assert body["totals"]["sessions"] == 2
    for secret in (
        "api-key-sentinel",
        "login-token-sentinel",
        "message-body-sentinel",
        "attempt-prompt-sentinel",
        "model-response-sentinel",
    ):
        assert secret not in response.text
    row = body["sessions"]["items"][0]
    assert row["title"] == "title-sentinel"
    assert set(row) == {"session_id", "title", "last_run_at", "totals", "runs"}
    assert set(row["runs"][0]) == {"run_id", "occurred_at", "provider", "model", "totals"}


def test_remote_usage_requires_correct_bearer_key(usage_data: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_AUTH_KEY", "secret")
    monkeypatch.setattr(api_server, "_API_KEY", "secret")
    client = _remote_client()

    assert _local_client().get("/usage/llm").status_code == 200
    assert client.get("/usage/llm").status_code == 401
    assert client.get("/usage/llm", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/usage/llm", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_search_and_pagination_only_change_session_page(usage_data: None) -> None:
    client = _local_client()
    all_rows = client.get("/usage/llm", params={"timezone": "UTC", "page_size": 1}).json()
    second_page = client.get("/usage/llm", params={"timezone": "UTC", "page": 2, "page_size": 1}).json()
    filtered = client.get(
        "/usage/llm",
        params={"timezone": "UTC", "query": "alpha", "page_size": 1},
    ).json()

    for body in (second_page, filtered):
        assert body["totals"] == all_rows["totals"]
        assert body["trend"] == all_rows["trend"]
        assert body["breakdown"] == all_rows["breakdown"]
        assert body["totals"]["sessions"] == 2
    assert all_rows["sessions"]["items"] != second_page["sessions"]["items"]
    assert filtered["sessions"]["total_items"] == 1
    assert all_rows["sessions"]["total_items"] == 2


def test_usage_offloads_file_aggregation_from_request_thread(
    usage_data: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_thread_ids: list[int] = []
    aggregate_thread_ids: list[int] = []
    original_get_session_service = api_server._get_session_service
    original_aggregate = llm_usage_routes.LLMUsageAggregationService.aggregate

    def tracked_get_session_service():
        request_thread_ids.append(threading.get_ident())
        return original_get_session_service()

    def tracked_aggregate(self, **kwargs):
        aggregate_thread_ids.append(threading.get_ident())
        return original_aggregate(self, **kwargs)

    monkeypatch.setattr(api_server, "_get_session_service", tracked_get_session_service)
    monkeypatch.setattr(llm_usage_routes.LLMUsageAggregationService, "aggregate", tracked_aggregate)

    response = _local_client().get("/usage/llm")

    assert response.status_code == 200
    assert len(request_thread_ids) == 1
    assert len(aggregate_thread_ids) == 1
    assert aggregate_thread_ids[0] != request_thread_ids[0]


def test_usage_returns_501_when_session_runtime_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_server, "_get_session_service", lambda: None)

    response = _local_client().get("/usage/llm")

    assert response.status_code == 501
    assert response.json() == {"detail": "Session runtime not enabled"}


def test_usage_normalizes_offset_period_boundaries_to_utc(usage_data: None) -> None:
    response = _local_client().get(
        "/usage/llm",
        params={
            "start_at": "2026-07-01T18:00:00+08:00",
            "end_at": "2026-07-02T18:00:00+08:00",
        },
    )

    assert response.status_code == 200
    assert response.json()["period"] == {
        "start_at": "2026-07-01T10:00:00Z",
        "end_at": "2026-07-02T10:00:00Z",
    }


@pytest.mark.parametrize(
    "params",
    [
        {"timezone": "../UTC"},
        {"timezone": "/etc/passwd"},
        {"start_at": "9999-12-31T23:59:59-23:59"},
        {"end_at": "0001-01-01T00:00:00+23:59"},
    ],
)
def test_usage_converts_timezone_and_datetime_library_errors_to_400(
    usage_data: None,
    params: dict[str, object],
) -> None:
    response = _local_client(raise_server_exceptions=False).get("/usage/llm", params=params)

    assert response.status_code == 400


@pytest.mark.parametrize("timezone_name", ["A" * 256, "A" * 1024, "A" * 5000])
def test_usage_rejects_overlong_timezone_names_as_bad_requests(
    usage_data: None,
    timezone_name: str,
) -> None:
    response = _local_client(raise_server_exceptions=False).get(
        "/usage/llm",
        params={"timezone": timezone_name},
    )

    assert response.status_code == 400


def test_usage_converts_timezone_os_errors_to_bad_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os_error(_: str) -> None:
        raise OSError("File name too long")

    monkeypatch.setattr(llm_usage_routes, "ZoneInfo", raise_os_error)

    response = _local_client(raise_server_exceptions=False).get(
        "/usage/llm",
        params={"timezone": "UTC"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid timezone"}


@pytest.mark.parametrize(
    "params",
    [
        {"timezone": "Not/AZone"},
        {"start_at": "not-a-date"},
        {"start_at": "2026-07-01T00:00:00"},
        {"start_at": "2026-07-02T00:00:00Z", "end_at": "2026-07-02T00:00:00Z"},
        {"start_at": "2026-07-03T00:00:00Z", "end_at": "2026-07-02T00:00:00Z"},
        {"page": 0},
        {"page_size": 101},
        {"timezone": ""},
        {"page": "abc"},
        {"page_size": "nan"},
        {"query": "x" * 501},
    ],
)
def test_usage_rejects_invalid_query_parameters(usage_data: None, params: dict[str, object]) -> None:
    response = _local_client().get("/usage/llm", params=params)

    assert response.status_code == 400
