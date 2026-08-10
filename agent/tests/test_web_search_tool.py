"""Tests for WebSearchTool: CN-first default, opt-in overseas engines, fallback.

Default behavior (no env): the CN chain (360 → sogou → cn.bing) runs directly
and overseas ddgs engines are never contacted. Setting VIBE_TRADING_SEARCH_BACKENDS
opts into the overseas ddgs path. All tests mock ddgs.DDGS and the CN scrapers
so no network calls are made.
"""
import json
import logging
import sys
from contextlib import contextmanager
from types import ModuleType

import pytest

from src.tools.web_search_tool import WebSearchTool


def _make_ddgs_module(text_impl):
    """Build a fake ``ddgs`` module whose DDGS().text delegates to text_impl."""
    module = ModuleType("ddgs")

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results=5, **kwargs):
            return text_impl(query, max_results=max_results, **kwargs)

    module.DDGS = FakeDDGS
    return module


@contextmanager
def _patch_ddgs(monkeypatch, text_impl):
    monkeypatch.setitem(sys.modules, "ddgs", _make_ddgs_module(text_impl))
    yield


@pytest.fixture(autouse=True)
def _clear_backend_env(monkeypatch):
    monkeypatch.delenv("VIBE_TRADING_SEARCH_BACKENDS", raising=False)
    monkeypatch.delenv("VIBE_TRADING_SEARCH_BING_FALLBACK", raising=False)
    monkeypatch.delenv("ALIYUN_IQS_API_KEY", raising=False)


# --- Overseas ddgs path (opt-in via VIBE_TRADING_SEARCH_BACKENDS) ---


def test_overseas_returns_results_and_passes_backend_list(monkeypatch):
    """With BACKENDS set, ddgs runs and the engine list is forwarded."""
    monkeypatch.setenv("VIBE_TRADING_SEARCH_BACKENDS", "duckduckgo, google, bing, brave, mojeek, yahoo")
    seen = {}

    def text_impl(query, max_results, **kwargs):
        seen.update(kwargs)
        return [{"title": "T1", "href": "http://a", "body": "snippet1"}]

    with _patch_ddgs(monkeypatch, text_impl):
        out = json.loads(WebSearchTool().execute(query="nvidia"))

    assert out["status"] == "ok"
    assert out["results"][0] == {"title": "T1", "url": "http://a", "snippet": "snippet1"}
    assert seen.get("backend") == "duckduckgo, google, bing, brave, mojeek, yahoo"


def test_env_overrides_backends(monkeypatch):
    """VIBE_TRADING_SEARCH_BACKENDS overrides the default engine list."""
    monkeypatch.setenv("VIBE_TRADING_SEARCH_BACKENDS", "google, bing")
    seen = {}

    def text_impl(query, max_results, **kwargs):
        seen.update(kwargs)
        return [{"title": "T", "href": "http://x", "body": "b"}]

    with _patch_ddgs(monkeypatch, text_impl):
        out = json.loads(WebSearchTool().execute(query="aapl"))

    assert out["status"] == "ok"
    assert seen.get("backend") == "google, bing"


def test_overseas_retries_transient_failure_then_succeeds(monkeypatch):
    """A transient exception is retried (with backoff) and a later attempt wins."""
    monkeypatch.setenv("VIBE_TRADING_SEARCH_BACKENDS", "duckduckgo")
    monkeypatch.setattr("src.tools.web_search_tool.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def text_impl(query, max_results, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Ratelimit 202")
        return [{"title": "ok", "href": "http://ok", "body": "b"}]

    with _patch_ddgs(monkeypatch, text_impl):
        out = json.loads(WebSearchTool().execute(query="msft"))

    assert out["status"] == "ok"
    assert calls["n"] == 2


def test_overseas_no_results_is_ok_empty(monkeypatch):
    """ddgs raising 'No results found.' yields an ok+empty envelope, not error."""
    monkeypatch.setenv("VIBE_TRADING_SEARCH_BACKENDS", "duckduckgo")
    monkeypatch.setattr("src.tools.web_search_tool.time.sleep", lambda *_: None)

    def text_impl(query, max_results, **kwargs):
        raise RuntimeError("No results found.")

    with _patch_ddgs(monkeypatch, text_impl):
        out = json.loads(WebSearchTool().execute(query="zzzz-no-such-thing"))

    assert out["status"] == "ok"
    assert out["results"] == []
    assert "note" in out


def test_overseas_persistent_failure_returns_actionable_error(monkeypatch):
    """When every overseas attempt fails and CN fallback is off, error names remedies."""
    monkeypatch.setenv("VIBE_TRADING_SEARCH_BACKENDS", "duckduckgo")
    monkeypatch.setenv("VIBE_TRADING_SEARCH_BING_FALLBACK", "0")
    monkeypatch.setattr("src.tools.web_search_tool.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def text_impl(query, max_results, **kwargs):
        calls["n"] += 1
        raise RuntimeError("Ratelimit 429")

    with _patch_ddgs(monkeypatch, text_impl):
        out = json.loads(WebSearchTool().execute(query="tsla"))

    assert out["status"] == "error"
    assert calls["n"] == 3  # exhausted all attempts
    assert "VIBE_TRADING_SEARCH_BACKENDS" in out["error"]
    assert "read_url" in out["error"]


def test_overseas_network_failure_fast_fails_to_cn_chain(monkeypatch):
    """Overseas network errors break retry early (1 attempt) and fall to the CN chain."""
    monkeypatch.setenv("VIBE_TRADING_SEARCH_BACKENDS", "duckduckgo")
    monkeypatch.setattr("src.tools.web_search_tool.time.sleep", lambda *_: None)
    calls = {"ddgs": 0}

    def text_impl(query, max_results, **kwargs):
        calls["ddgs"] += 1
        raise RuntimeError("Connection timed out")

    monkeypatch.setattr(
        "src.tools.web_search_tool._qihu_search",
        lambda query, max_results=5: [{"title": "360 hit", "href": "https://www.so.com/link?m=x", "body": ""}],
    )
    monkeypatch.setattr("src.tools.web_search_tool._sogou_search", lambda *_a, **_k: [])
    monkeypatch.setattr("src.tools.web_search_tool._bing_cn_search", lambda *_a, **_k: [])

    with _patch_ddgs(monkeypatch, text_impl):
        out = json.loads(WebSearchTool().execute(query="maotai"))

    assert out["status"] == "ok"
    assert out["backends"] == "qihu_fallback"
    assert calls["ddgs"] == 1  # network error → break immediately, no extra retries


def test_overseas_max_results_capped_at_10(monkeypatch):
    """max_results is clamped to 10 before being forwarded to ddgs."""
    monkeypatch.setenv("VIBE_TRADING_SEARCH_BACKENDS", "duckduckgo")
    seen = {}

    def text_impl(query, max_results, **kwargs):
        seen["max_results"] = max_results
        return []  # ddgs returns empty → execute returns ok+empty without hitting CN chain

    with _patch_ddgs(monkeypatch, text_impl):
        WebSearchTool().execute(query="q", max_results=50)

    assert seen["max_results"] == 10


# --- Default CN-first path (no BACKENDS env) ---


def test_default_uses_cn_chain_without_overseas(monkeypatch):
    """Default (no BACKENDS env) goes straight to the CN chain; ddgs never called."""
    calls = {"ddgs": 0, "qihu": 0}

    def text_impl(query, max_results, **kwargs):
        calls["ddgs"] += 1
        return [{"title": "should not happen", "href": "http://x", "body": ""}]

    def fake_qihu(query, max_results=5):
        calls["qihu"] += 1
        return [{"title": "360 hit", "href": "https://www.so.com/link?m=abc", "body": ""}]

    monkeypatch.setattr("src.tools.web_search_tool._qihu_search", fake_qihu)
    monkeypatch.setattr("src.tools.web_search_tool._sogou_search", lambda *_a, **_k: [])
    monkeypatch.setattr("src.tools.web_search_tool._bing_cn_search", lambda *_a, **_k: [])

    with _patch_ddgs(monkeypatch, text_impl):
        out = json.loads(WebSearchTool().execute(query="双环传动"))

    assert out["status"] == "ok"
    assert out["backends"] == "qihu_fallback"
    assert calls["ddgs"] == 0  # overseas engines never contacted by default
    assert calls["qihu"] == 1


def test_default_cn_chain_falls_through_to_sogou(monkeypatch):
    """When 360 returns nothing, the chain degrades to sogou."""
    monkeypatch.setattr("src.tools.web_search_tool._qihu_search", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "src.tools.web_search_tool._sogou_search",
        lambda query, max_results=5: [{"title": "sogou hit", "href": "https://www.sogou.com/link?url=x", "body": ""}],
    )
    monkeypatch.setattr("src.tools.web_search_tool._bing_cn_search", lambda *_a, **_k: [])

    out = json.loads(WebSearchTool().execute(query="茅台"))

    assert out["status"] == "ok"
    assert out["backends"] == "sogou_fallback"


def test_default_all_cn_fail_returns_error(monkeypatch):
    """All CN engines failing yields a structured error envelope, not an exception."""
    monkeypatch.setattr("src.tools.web_search_tool._qihu_search", lambda *_a, **_k: [])
    monkeypatch.setattr("src.tools.web_search_tool._sogou_search", lambda *_a, **_k: [])
    monkeypatch.setattr("src.tools.web_search_tool._bing_cn_search", lambda *_a, **_k: [])

    out = json.loads(WebSearchTool().execute(query="xxx"))

    assert out["status"] == "error"
    assert "CN chain" in out["error"]


def test_default_mode_preserves_backend_exception_details(monkeypatch, caplog):
    secret_text = "backend leaked token=direct-secret; internal host=10.0.0.1"

    def fail_search(*args, **kwargs):
        raise RuntimeError(secret_text)

    monkeypatch.setattr("src.tools.web_search_tool._qihu_search", fail_search)
    monkeypatch.setattr("src.tools.web_search_tool._sogou_search", fail_search)
    monkeypatch.setattr("src.tools.web_search_tool._bing_cn_search", fail_search)
    with caplog.at_level(logging.WARNING):
        out = json.loads(WebSearchTool().execute(query="direct"))

    assert secret_text in out["error"]
    assert secret_text in caplog.text


def test_bing_fallback_disabled_skips_cn_chain(monkeypatch):
    """VIBE_TRADING_SEARCH_BING_FALLBACK=0 disables the whole CN chain."""
    monkeypatch.setenv("VIBE_TRADING_SEARCH_BING_FALLBACK", "0")
    monkeypatch.setattr(
        "src.tools.web_search_tool._qihu_search",
        lambda *_a, **_k: [{"title": "x", "href": "http://y", "body": ""}],
    )

    out = json.loads(WebSearchTool().execute(query="q"))

    assert out["status"] == "error"
    assert "disabled" in out["error"]


# --- Aliyun IQS fast-path ---


def test_iqs_fast_path_when_key_set(monkeypatch):
    """ALIYUN_IQS_API_KEY set → web_search calls IQS first, skips every other engine."""
    monkeypatch.setenv("ALIYUN_IQS_API_KEY", "test-key")
    calls = {"iqs": 0, "qihu": 0}

    def fake_iqs(query, max_results=5):
        calls["iqs"] += 1
        return [{"title": "iqs hit", "href": "https://cloud-iqs.example/x", "body": "snippet"}]

    monkeypatch.setattr("src.tools.web_search_tool._aliyun_iqs_search", fake_iqs)
    monkeypatch.setattr("src.tools.web_search_tool._qihu_search", lambda *_a, **_k: (calls.__setitem__("qihu", 1) or []))

    out = json.loads(WebSearchTool().execute(query="测试"))

    assert out["status"] == "ok"
    assert out["backends"] == "aliyun_iqs"
    assert out["results"][0]["url"] == "https://cloud-iqs.example/x"
    assert calls["iqs"] == 1
    assert calls["qihu"] == 0  # IQS hit → CN chain not contacted


# --- _qihu_search parser unit test (no network) ---


def test_qihu_search_parses_html(monkeypatch):
    """_qihu_search extracts title + so.com link href from a 360 results page."""
    sample_html = (
        '<a href="/help">搜索帮助</a>'
        '<a href="https://www.so.com/link?m=abc">双环传动[002472]股票行情_九方智投</a>'
        '<a href="https://www.so.com/link?m=def">[新闻] 双环传动8月4日下跌0.03%</a>'
        '<a href="//www.so.com/link?m=ghi">双环传动:关于公司担保的公告</a>'
        '<a href="javascript:void(0)">短</a>'
    )

    class _FakeResp:
        def read(self):
            return sample_html.encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=10: _FakeResp())

    from src.tools.web_search_tool import _qihu_search

    out = _qihu_search("双环传动", max_results=5)
    titles = [r["title"] for r in out]

    assert "双环传动[002472]股票行情_九方智投" in titles
    assert all(r["href"].startswith("https://") for r in out)
    assert all("搜索" not in r["title"] for r in out)  # navigation words filtered
    assert len(out) == 3  # 3 valid results (excluded: 搜索帮助 nav, 短 too short)
