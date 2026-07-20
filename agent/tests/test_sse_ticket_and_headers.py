"""Regression tests for SSE tickets and HTTP transport hardening."""

from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import api_server
from src.api import security


def _remote_client() -> TestClient:
    return TestClient(api_server.app, client=("203.0.113.10", 50000))


@pytest.fixture(autouse=True)
def configured_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_AUTH_KEY", "ticket-test-secret")
    monkeypatch.setattr(api_server, "_API_KEY", "ticket-test-secret")
    security._SSE_TICKETS.clear()
    security._SSE_TICKET_EXPIRIES.clear()


def test_remote_client_mints_ticket_with_bearer_and_ticket_is_single_use() -> None:
    client = _remote_client()

    assert client.post("/auth/sse-ticket").status_code == 401
    response = client.post("/auth/sse-ticket", headers={"Authorization": "Bearer ticket-test-secret"})

    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert isinstance(ticket, str) and ticket
    assert security._consume_sse_ticket(ticket) is True
    assert security._consume_sse_ticket(ticket) is False


def test_remote_event_stream_rejects_api_key_query_and_accepts_single_use_ticket() -> None:
    app = FastAPI()

    @app.get("/events", dependencies=[Depends(security.require_event_stream_auth)])
    async def events() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app, client=("203.0.113.10", 50000))
    rejected = client.get("/events?api_key=ticket-test-secret")
    assert rejected.status_code == 401

    ticket = security._mint_sse_ticket()
    accepted = client.get(f"/events?ticket={ticket}")
    replayed = client.get(f"/events?ticket={ticket}")
    assert accepted.status_code == 200
    assert replayed.status_code == 401


def test_sse_ticket_store_rejects_mints_at_its_fixed_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "_MAX_SSE_TICKETS", 2, raising=False)

    security._mint_sse_ticket()
    security._mint_sse_ticket()

    with pytest.raises(security.SSETicketCapacityError):
        security._mint_sse_ticket()


def test_consumed_tickets_still_bound_ticket_creation_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mint-and-consume churn cannot grow the expiry index without a limit."""
    monkeypatch.setattr(security, "_MAX_SSE_TICKETS", 2, raising=False)

    first = security._mint_sse_ticket()
    second = security._mint_sse_ticket()
    assert security._consume_sse_ticket(first) is True
    assert security._consume_sse_ticket(second) is True

    with pytest.raises(security.SSETicketCapacityError):
        security._mint_sse_ticket()


def test_expired_sse_ticket_cannot_be_consumed_and_releases_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr(security.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(security, "_MAX_SSE_TICKETS", 1, raising=False)
    ticket = security._mint_sse_ticket()

    now["value"] += security._SSE_TICKET_TTL_SECONDS + 1

    assert security._consume_sse_ticket(ticket) is False
    assert security._mint_sse_ticket()


def test_security_response_headers_are_added() -> None:
    response = _remote_client().get("/health", headers={"Authorization": "Bearer ticket-test-secret"})

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" not in response.headers
    assert "default-src 'self'" in response.headers["content-security-policy-report-only"]


def test_uvicorn_filter_redacts_ticket_and_api_key_query_values() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %s',
        ("203.0.113.10:50000", "GET", "/events?ticket=super-secret&api_key=also-secret", "1.1", 200),
        None,
    )

    assert security._UvicornQuerySecretRedactionFilter().filter(record) is True
    rendered = record.getMessage()
    assert "super-secret" not in rendered
    assert "also-secret" not in rendered
    assert "[redacted]" in rendered


def test_uvicorn_filter_removes_all_query_values_from_news_api_subroute_access_logs() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %s',
        ("203.0.113.10:50000", "GET", "/news-api/snapshot?llm=sk-canary&foo=bar", "1.1", 422),
        None,
    )

    assert security._UvicornQuerySecretRedactionFilter().filter(record) is True
    rendered = record.getMessage()
    assert "sk-canary" not in rendered
    assert "foo=bar" not in rendered
    assert "/news-api/snapshot HTTP/1.1" in rendered
