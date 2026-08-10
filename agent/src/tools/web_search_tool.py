"""Web search tool: CN-first free engines, overseas engines opt-in.

Default path uses free, key-less, CN-direct engines (360 Search → Sogou →
cn.bing) so users in mainland China get results without a proxy. The overseas
ddgs engines (DuckDuckGo/Google/Bing/Brave/Mojeek/Yahoo) are opt-in via
``VIBE_TRADING_SEARCH_BACKENDS`` — they are unreachable from typical CN egress
(sidecar logs 2026-08-07: ``search.brave.com`` timed out, Yahoo 403), so
contacting them by default only burned ~20-30s before falling back. A key-gated
Aliyun IQS fast-path stays first when configured.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from src.agent.tools import BaseTool
from src.security.scanner import with_security_warnings

logger = logging.getLogger(__name__)

# Overseas engines aggregated by ddgs. Opt-in only via VIBE_TRADING_SEARCH_BACKENDS
# (default empty = skip ddgs entirely; CN users get the CN chain directly).
_DEFAULT_BACKENDS = "duckduckgo, google, bing, brave, mojeek, yahoo"
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.8
_HTTP_TIMEOUT_SECONDS = 10


class _FallbackDeadlineExceeded(Exception):
    """Raised when the reader's private summary-search budget is exhausted."""


def _bounded_http_timeout(deadline: float | None) -> float:
    """Return the existing HTTP timeout capped by an internal fallback deadline."""
    if deadline is None:
        return _HTTP_TIMEOUT_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _FallbackDeadlineExceeded
    return min(_HTTP_TIMEOUT_SECONDS, remaining)


def _deadline_error() -> str:
    """Return the internal error envelope used by the bounded reader fallback."""
    return json.dumps(
        {"status": "error", "error": "Web search fallback deadline exhausted."},
        ensure_ascii=False,
    )


def _failure_classification(exc: Exception) -> str:
    """Return a stable category suitable for internal redacted diagnostics."""
    lowered = str(exc).lower()
    if any(marker in lowered for marker in ("timeout", "timed out", "unreachable", "connection", "max retries")):
        return "network_error"
    return "backend_error"


def _aliyun_iqs_search(
    query: str,
    max_results: int = 5,
    *,
    deadline: float | None = None,
) -> list[dict] | None:
    """Search via Alibaba Cloud IQS (cloud-iqs.aliyuncs.com) — official API,
    structured (title/link/snippet/hostname), CN-direct (~1s), supports a finance
    category and rerank. Requires ``ALIYUN_IQS_API_KEY``; returns None when unset.
    Pure stdlib.
    """
    key = os.getenv("ALIYUN_IQS_API_KEY", "").strip()
    if not key:
        return None
    import json as _json
    import urllib.request

    body = _json.dumps({
        "query": query,
        "engineType": "Generic",
        "contents": {"mainText": False, "summary": False, "rerankScore": True},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://cloud-iqs.aliyuncs.com/search/unified",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=_bounded_http_timeout(deadline))
    data = _json.loads(resp.read().decode("utf-8"))
    out: list[dict] = []
    for item in data.get("pageItems", [])[:max_results]:
        out.append({
            "title": item.get("title", ""),
            "href": item.get("link", ""),
            "body": item.get("snippet", ""),
        })
    return out


def _bing_cn_search(
    query: str,
    max_results: int = 5,
    *,
    deadline: float | None = None,
) -> list[dict]:
    """Scrape cn.bing.com organic results — no API key, works where ddgs engines are blocked.

    Used as a fallback when every ddgs backend times out (common behind restricted
    egress, e.g. CN hosts without VPN where duckduckgo/google/brave are unreachable
    but cn.bing.com is). Returns ddgs-shaped dicts (title/href/body) so the caller
    can treat the two paths uniformly. Pure stdlib — no new dependency.
    """
    import re as _re
    import urllib.parse
    import urllib.request

    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    html = urllib.request.urlopen(req, timeout=_bounded_http_timeout(deadline)).read().decode("utf-8", "ignore")
    blocks = _re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, _re.S)
    out: list[dict] = []
    for b in blocks[:max_results]:
        h2 = _re.search(r"<h2[^>]*>(.*?)</h2>", b, _re.S)
        if not h2:
            continue
        a = _re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', h2.group(1), _re.S)
        if not a:
            continue
        title = _re.sub(r"<[^>]+>", "", a.group(2)).strip()
        href = a.group(1)
        snip_m = _re.search(r"<p[^>]*>(.*?)</p>", b, _re.S)
        snippet = _re.sub(r"<[^>]+>", "", snip_m.group(1)).strip() if snip_m else ""
        if title and href.startswith("http"):
            out.append({"title": title, "href": href, "body": snippet[:180]})
    return out


def _sogou_search(
    query: str,
    max_results: int = 5,
    *,
    deadline: float | None = None,
) -> list[dict]:
    """Scrape sogou.com results — better CN financial query quality than cn.bing.

    Sogou's organic titles hit financial queries precisely (e.g. '茅台 2024 营收
    1708.99 亿') where cn.bing drifts to regional/tourism results for natural-language
    queries without a ticker. URLs are sogou jump links (/link?url=...) absolute-ized
    so a downstream read_url can follow them. Snippet is left empty because the title
    already carries the key figure. Pure stdlib.
    """
    import re as _re
    import urllib.parse
    import urllib.request

    url = "https://www.sogou.com/web?" + urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    html = urllib.request.urlopen(req, timeout=_bounded_http_timeout(deadline)).read().decode("utf-8", "ignore")
    blocks = _re.split(r'<div class="vrwrap"', html)[1:]
    out: list[dict] = []
    for b in blocks[:max_results]:
        tm = _re.search(r'<a[^>]*target="_blank"[^>]*>(.*?)</a>', b, _re.S)
        hm = _re.search(r'<a[^>]*href="([^"]*)"', b)
        title = _re.sub(r"<[^>]+>", "", tm.group(1)).strip() if tm else ""
        href = hm.group(1) if hm else ""
        if href.startswith("/"):
            href = "https://www.sogou.com" + href
        if title and href:
            out.append({"title": title, "href": href, "body": ""})
    return out


def _qihu_search(
    query: str,
    max_results: int = 5,
    *,
    deadline: float | None = None,
) -> list[dict]:
    """Scrape so.com (360 Search) organic results — no API key, CN-direct, stable.

    Among free CN engines 360 returns the most precise financial-entity hits
    (company sites, stock forum, announcements, news) and stays stable across
    repeated calls, unlike shenma/quark which punish scripted access. Result
    hrefs are ``so.com/link?m=...`` redirect wrappers (the real URL is not in
    the HTML), kept as-is so a downstream ``read_url`` can follow them — same
    shape as :func:`_sogou_search`'s sogou jump links. Pure stdlib.
    """
    import re as _re
    import urllib.parse
    import urllib.request

    url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    html = urllib.request.urlopen(req, timeout=_bounded_http_timeout(deadline)).read().decode("utf-8", "ignore")
    pairs = _re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, _re.S)
    out: list[dict] = []
    for href, inner in pairs:
        title = _re.sub(r"<[^>]+>", "", inner).strip()
        if not (8 <= len(title) <= 80):
            continue
        if any(x in title for x in ("搜索", "首页", "登录", "注册", "360", "安全", "导航", "下载")):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif not href.startswith("http"):
            continue
        out.append({"title": title, "href": href, "body": ""})
        if len(out) >= max_results:
            break
    return out


class WebSearchTool(BaseTool):
    """Search the web via free CN engines by default; overseas engines opt-in."""

    name = "web_search"

    @classmethod
    def check_available(cls) -> bool:
        """Available only if ddgs or duckduckgo_search is installed."""
        try:
            try:
                import ddgs  # noqa: F401
            except ImportError:
                import duckduckgo_search  # noqa: F401
            return True
        except ImportError:
            return False
    description = (
        "Search the web via free CN engines (360, Sogou, cn.bing) by default; "
        "overseas engines (DuckDuckGo/Google/Bing/Brave/...) are opt-in via "
        "VIBE_TRADING_SEARCH_BACKENDS. Returns top results with title, URL, "
        "and snippet. Use this to find information, news, or URLs before "
        "reading them with read_url."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5, max 10)",
                "default": 5,
            },
        },
        "required": ["query"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Run a web search, CN-first by default, overseas engines opt-in.

        Engine selection: the overseas ddgs engines are contacted only when
        ``VIBE_TRADING_SEARCH_BACKENDS`` is set (proxy / overseas egress). The
        default CN chain (360 → sogou → cn.bing) is free, key-less, CN-direct —
        CN users without a proxy get results immediately instead of burning
        ~20-30s on brave/yahoo/duckduckgo timeouts.

        Args:
            **kwargs: Must include query; optionally max_results.

        Returns:
            JSON envelope with status, query, the backend list used, and results
            (or an actionable error message on persistent failure).
        """
        query = kwargs["query"]
        max_results = min(int(kwargs.get("max_results", 5)), 10)
        safe_failure_logging = bool(kwargs.pop("_safe_failure_logging", False))
        deadline = kwargs.pop("_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            return _deadline_error()
        backends_env = os.getenv("VIBE_TRADING_SEARCH_BACKENDS", "").strip()
        overseas_enabled = bool(backends_env)
        backends = backends_env or _DEFAULT_BACKENDS

        # Fast path: Alibaba Cloud IQS if configured (official API, CN-direct,
        # ~1s, structured + snippet, best quality). Skips every other engine.
        if os.getenv("ALIYUN_IQS_API_KEY", "").strip():
            try:
                if deadline is None:
                    raw = _aliyun_iqs_search(query, max_results=max_results)
                else:
                    raw = _aliyun_iqs_search(query, max_results=max_results, deadline=deadline)
                if raw:
                    payload = {
                        "status": "ok",
                        "query": query,
                        "backends": "aliyun_iqs",
                        "results": [
                            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
                            for r in raw
                        ],
                    }
                    payload = with_security_warnings(
                        payload, fields=("results.*.title", "results.*.snippet")
                    )
                    return json.dumps(payload, ensure_ascii=False)
                logger.warning("aliyun_iqs returned no results, falling through")
            except _FallbackDeadlineExceeded:
                return _deadline_error()
            except Exception as exc:  # noqa: BLE001
                if safe_failure_logging:
                    logger.warning("aliyun_iqs failed: %s, falling through", _failure_classification(exc))
                else:
                    logger.warning("aliyun_iqs failed: %s, falling through", exc)

        # Overseas ddgs engines are opt-in. CN users without a proxy would otherwise
        # burn ~20-30s on timeouts (brave/yahoo/duckduckgo unreachable from typical
        # CN egress) before reaching the CN chain, so we only contact them when the
        # user explicitly sets VIBE_TRADING_SEARCH_BACKENDS (proxy / overseas egress).
        # DDGS offers no per-request timeout. The private reader fallback only
        # uses HTTP helpers that can honor its deadline; direct web_search keeps
        # the existing DDGS behavior.
        if overseas_enabled and deadline is None:
            try:
                from ddgs import DDGS

                supports_backend = True
            except ImportError:
                try:
                    from duckduckgo_search import DDGS  # legacy package, no engine selection
                except ImportError:
                    return json.dumps(
                        {
                            "status": "error",
                            "error": "Web search package not installed. Run: pip install ddgs",
                        },
                        ensure_ascii=False,
                    )
                supports_backend = False

            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    with DDGS() as client:
                        if supports_backend:
                            raw = list(client.text(query, max_results=max_results, backend=backends))
                        else:
                            raw = list(client.text(query, max_results=max_results))
                except TypeError:
                    # Older ddgs/duckduckgo_search without the backend kwarg.
                    supports_backend = False
                    continue
                except Exception as exc:  # noqa: BLE001 — surface a clean error to the agent
                    # "No results found" is a definitive empty answer, not a transient
                    # failure — retrying or switching engines won't change it.
                    if "no results" in str(exc).lower():
                        return json.dumps(
                            {
                                "status": "ok",
                                "query": query,
                                "backends": backends if supports_backend else "duckduckgo",
                                "results": [],
                                "note": "No results found for this query across the search engines.",
                            },
                            ensure_ascii=False,
                        )
                    if safe_failure_logging:
                        logger.warning(
                            "web_search attempt %d/%d failed: %s",
                            attempt,
                            _MAX_ATTEMPTS,
                            _failure_classification(exc),
                        )
                    else:
                        logger.warning("web_search attempt %d/%d failed: %s", attempt, _MAX_ATTEMPTS, exc)
                    # Network/egress errors (timeout, connection refused, unreachable)
                    # won't recover on retry — stop retrying ddgs and fall through to
                    # the CN chain instead of wasting ~20-30s on more timeouts.
                    err_msg = str(exc).lower()
                    if any(s in err_msg for s in ("timeout", "timed out", "unreachable", "connection", "max retries")):
                        break
                    if attempt < _MAX_ATTEMPTS:
                        time.sleep(_BACKOFF_BASE_SECONDS * attempt)
                    continue

                results = [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "snippet": r.get("body", ""),
                    }
                    for r in raw
                ]
                payload = {
                    "status": "ok",
                    "query": query,
                    "backends": backends if supports_backend else "duckduckgo",
                    "results": results,
                }
                payload = with_security_warnings(
                    payload,
                    fields=("results.*.title", "results.*.snippet"),
                )
                return json.dumps(payload, ensure_ascii=False)

        # CN chain: 360 → sogou → cn.bing. All free, key-less, CN-direct. Reached
        # by default (no overseas opt-in) or after overseas engines fail. Toggle
        # the whole chain via VIBE_TRADING_SEARCH_BING_FALLBACK (default on).
        fb_err = "disabled"
        if os.getenv("VIBE_TRADING_SEARCH_BING_FALLBACK", "1").strip().lower() in ("1", "true", "yes"):
            for fb_name, fb_fn in (("qihu", _qihu_search), ("sogou", _sogou_search), ("bing_cn", _bing_cn_search)):
                try:
                    if deadline is not None and time.monotonic() >= deadline:
                        return _deadline_error()
                    if deadline is None:
                        raw = fb_fn(query, max_results=max_results)
                    else:
                        raw = fb_fn(query, max_results=max_results, deadline=deadline)
                    if raw:
                        payload = {
                            "status": "ok",
                            "query": query,
                            "backends": f"{fb_name}_fallback",
                            "results": [
                                {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
                                for r in raw
                            ],
                        }
                        payload = with_security_warnings(
                            payload, fields=("results.*.title", "results.*.snippet")
                        )
                        return json.dumps(payload, ensure_ascii=False)
                    fb_err = f"{fb_name} returned no results"
                except _FallbackDeadlineExceeded:
                    return _deadline_error()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "%s fallback failed: %s",
                        fb_name,
                        _failure_classification(exc) if safe_failure_logging else exc,
                    )
                    fb_err = (
                        f"{fb_name}: {_failure_classification(exc)}"
                        if safe_failure_logging
                        else f"{fb_name}: {exc}"
                    )
                    continue
        return json.dumps(
            {
                "status": "error",
                "error": (
                    "Web search failed: CN chain (360/sogou/cn.bing) exhausted "
                    f"({fb_err}). Overseas engines are opt-in — set "
                    "VIBE_TRADING_SEARCH_BACKENDS (e.g. 'duckduckgo,google,bing,brave,"
                    "mojeek,yahoo') to enable them, set VIBE_TRADING_SEARCH_BING_FALLBACK=0 "
                    "to disable CN fallback, or read a known URL directly with read_url."
                ),
            },
            ensure_ascii=False,
        )
