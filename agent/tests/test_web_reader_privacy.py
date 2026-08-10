"""Regression tests for read_url third-party (Jina) hardening.

Network is mocked; no live r.jina.ai calls. Asserts: HTTP errors surface
the upstream status + body for debugging; a cached snapshot is surfaced
via `cached: true`; `no_cache=True` sends the x-no-cache header while
the default path is byte-identical (no extra header).
"""

from __future__ import annotations

import json

import pytest

import src.tools.web_reader_tool as wr
from src.tools.web_reader_tool import read_url

URL = "https://example.com/page"


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def captured(monkeypatch):
    box = {}

    def fake_get(url, headers=None, timeout=None):
        box["url"] = url
        box["headers"] = headers or {}
        box["timeout"] = timeout
        r = box["resp"]
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(wr.requests, "get", fake_get)
    return box


def test_http_error_surfaces_status_and_body(captured):
    captured["resp"] = _Resp(500, "ParamValidationError: bad input")
    out = json.loads(read_url(URL))
    assert out["status"] == "error"
    assert "500" in out["error"]
    assert "ParamValidationError: bad input" in out["error"]


def test_exception_error_surfaces_exc_text(captured):
    captured["resp"] = RuntimeError("boom: connect failed (10.0.0.1)")
    out = json.loads(read_url(URL))
    assert out["status"] == "error"
    assert "boom: connect failed" in out["error"]


def test_cached_snapshot_is_flagged(captured):
    captured["resp"] = _Resp(200, "Title: X\n\nWarning: This is a cached snapshot\n\nbody")
    out = json.loads(read_url(URL))
    assert out["status"] == "ok"
    assert out.get("cached") is True


def test_fresh_response_has_no_cached_key(captured):
    captured["resp"] = _Resp(200, "Title: X\n\nlive body content")
    out = json.loads(read_url(URL))
    assert out["status"] == "ok"
    assert "cached" not in out  # additive: absent on the normal path


def test_no_cache_header_opt_in_only(captured):
    captured["resp"] = _Resp(200, "Title: X\n\nbody")
    read_url(URL)  # default
    assert "x-no-cache" not in {k.lower() for k in captured["headers"]}
    read_url(URL, no_cache=True)
    assert captured["headers"].get("x-no-cache") == "true"


def test_reader_uses_bounded_connect_and_read_timeouts(captured):
    """The reader bounds connect, read, and total request duration."""
    captured["resp"] = _Resp(200, "Title: X\n\nbody")

    read_url(URL)

    timeout = captured["timeout"]
    assert timeout.connect_timeout == 5
    assert timeout.read_timeout == 25
    assert timeout.total == 30


def _search_ok(self, query: str, max_results: int) -> str:
    assert max_results == 5
    return json.dumps(
        {
            "status": "ok",
            "query": query,
            "backends": "qihu_fallback",
            "results": [
                {
                    "title": "Dollar outlook after Fed policy meeting",
                    "url": "https://example.com/coverage",
                    "snippet": "Analysts expect higher-for-longer rates.",
                }
            ],
        }
    )


def test_jina_abuse_restriction_returns_search_summary(captured, monkeypatch):
    captured["resp"] = _Resp(403, "AbuseAlleviationError: blocked")
    monkeypatch.setattr(wr.WebSearchTool, "execute", _search_ok)
    url = "https://www.reuters.com/world/africa/dollar-outlook-2026-05-27/?tracking=secret#section"

    payload = json.loads(wr.read_url(url))

    assert payload["status"] == "ok"
    assert payload["access_level"] == "summary_only"
    assert payload["url"] == url
    assert payload["source_url"] == url
    assert payload["fallback"]["reason"] == "source_access_denied"
    assert payload["fallback"]["upstream_status"] == 403
    assert payload["fallback"]["backends"] == "qihu_fallback"
    assert payload["fallback"]["results"][0]["url"] == "https://example.com/coverage"
    assert "search-result summaries, not article text" in payload["content"]
    assert "AbuseAlleviationError" not in payload["content"]
    assert "tracking" not in payload["fallback"]["query"]
    assert "secret" not in payload["fallback"]["query"]
    assert "#section" not in payload["fallback"]["query"]
    assert "site:www.reuters.com" in payload["fallback"]["query"]


@pytest.mark.parametrize("status_code", [429, 451])
def test_access_restriction_statuses_return_search_summary(captured, monkeypatch, status_code):
    captured["resp"] = _Resp(status_code, "blocked")
    calls = []

    def search(self, **kwargs):
        calls.append(kwargs)
        return _search_ok(self, **kwargs)

    monkeypatch.setattr(wr.WebSearchTool, "execute", search)

    payload = json.loads(wr.read_url(URL))

    assert len(calls) == 1
    assert payload["access_level"] == "summary_only"
    assert payload["fallback"]["upstream_status"] == status_code


def test_fallback_search_error_returns_stable_access_denied_error(captured, monkeypatch):
    captured["resp"] = _Resp(403, "blocked")
    malicious_error = "backend leaked token=secret; internal host=10.0.0.1"
    monkeypatch.setattr(
        wr.WebSearchTool,
        "execute",
        lambda self, **kwargs: json.dumps({"status": "error", "error": malicious_error}),
    )
    url = "https://example.com/blocked/article"

    payload = json.loads(wr.read_url(url))

    assert payload == {
        "status": "error",
        "error_code": "source_access_denied",
        "error": "source access denied and summary search failed: search unavailable",
        "source_url": url,
        "upstream_status": 403,
        "suggested_query": "blocked article site:example.com",
    }
    assert malicious_error not in payload["error"]


def test_ordinary_captcha_text_does_not_trigger_fallback(captured, monkeypatch):
    captured["resp"] = _Resp(500, "This article explains how captcha systems work.")

    def fail_search(*args, **kwargs):
        raise AssertionError("ordinary captcha text should not trigger fallback")

    monkeypatch.setattr(wr.WebSearchTool, "execute", fail_search)

    payload = json.loads(wr.read_url(URL))

    assert payload["error"] == (
        "remote reader returned HTTP 500: This article explains how captcha systems work."
    )


def test_explicit_captcha_challenge_triggers_fallback(captured, monkeypatch):
    captured["resp"] = _Resp(500, "Please complete the captcha verification to continue.")
    calls = []

    def search(self, **kwargs):
        calls.append(kwargs)
        return _search_ok(self, **kwargs)

    monkeypatch.setattr(wr.WebSearchTool, "execute", search)

    payload = json.loads(read_url("https://example.com/blocked/article"))

    assert len(calls) == 1
    assert calls[0]["max_results"] == 5
    assert payload["status"] == "ok"
    assert payload["access_level"] == "summary_only"


def test_long_fallback_query_preserves_site_clause():
    path = "/".join(f"very-long-path-segment-{index}" for index in range(40))

    query = wr._fallback_query(f"https://example.com/{path}")

    assert len(query) <= wr._FALLBACK_QUERY_MAX_LENGTH
    assert query.endswith("site:example.com")


def test_long_hostname_fallback_query_preserves_complete_site_clause():
    hostname = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))
    query = wr._fallback_query(f"https://{hostname}/" + "very-long-path-" * 40)

    assert query.endswith(f"site:{hostname}")
    assert len(query) <= wr._FALLBACK_QUERY_MAX_LENGTH


def test_empty_fallback_search_returns_stable_access_denied_error(captured, monkeypatch):
    captured["resp"] = _Resp(403, "blocked")
    monkeypatch.setattr(
        wr.WebSearchTool,
        "execute",
        lambda self, **kwargs: json.dumps({"status": "ok", "results": []}),
    )

    payload = json.loads(wr.read_url("https://example.com/blocked/article"))

    assert payload["status"] == "error"
    assert payload["error_code"] == "source_access_denied"
    assert payload["error"] == "source access denied and summary search returned no results"


def test_non_eligible_http_failure_does_not_search(captured, monkeypatch):
    captured["resp"] = _Resp(500, "server error")

    def fail_search(*args, **kwargs):
        raise AssertionError("restricted-source fallback should not run")

    monkeypatch.setattr(wr.WebSearchTool, "execute", fail_search)

    payload = json.loads(wr.read_url(URL))

    assert payload["error"] == "remote reader returned HTTP 500: server error"


def test_full_content_response_does_not_search(captured, monkeypatch):
    captured["resp"] = _Resp(200, "Title: X\n\nfull article content")

    def fail_search(*args, **kwargs):
        raise AssertionError("successful reader response should not search")

    monkeypatch.setattr(wr.WebSearchTool, "execute", fail_search)

    payload = json.loads(wr.read_url(URL))

    assert payload["status"] == "ok"
    assert "access_level" not in payload
    assert "source_url" not in payload
    assert "fallback" not in payload
