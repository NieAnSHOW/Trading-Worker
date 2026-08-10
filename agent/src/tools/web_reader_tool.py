"""Web reader tool: fetch a URL as Markdown text via the Jina Reader API."""

from __future__ import annotations

import ipaddress
import json
import logging
from urllib.parse import unquote, urlsplit

import requests
from urllib3.util import Timeout as _HTTPTimeout

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.security.scanner import with_security_warnings
from src.tools.web_search_tool import WebSearchTool

logger = logging.getLogger(__name__)

_JINA_PREFIX = "https://r.jina.ai/"
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 25
_TOTAL_TIMEOUT = 30
_MAX_LENGTH = 8000
_CACHED_MARKER = "Warning: This is a cached snapshot"
_ACCESS_RESTRICTED_STATUS_CODES = frozenset({401, 403, 429, 451})
_ACCESS_RESTRICTED_MARKERS = (
    "abusealleviationerror",
    "access denied",
    "access is denied",
    "automated access",
    "bot detection",
    "bot challenge",
    "captcha challenge",
    "captcha required",
    "captcha verification",
    "complete the captcha",
    "enter the captcha",
    "solve the captcha",
    "verify you are human",
)
_FALLBACK_MAX_RESULTS = 5
# DNS hostnames are at most 253 characters, so leave room for the complete
# ``site:<hostname>`` clause while still bounding the path-derived terms.
_FALLBACK_QUERY_MAX_LENGTH = 512
_SEARCH_OPERATOR_TOKENS = frozenset({
    "and",
    "cache",
    "define",
    "filetype",
    "inanchor",
    "inbody",
    "intext",
    "intitle",
    "inurl",
    "link",
    "not",
    "or",
    "related",
    "site",
})


def _url_allowed(url: str) -> tuple[bool, str]:
    """Return whether a URL is safe to forward to the remote reader service."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False, "target URL is not allowed"

    if parsed.scheme.lower() not in {"http", "https"}:
        return False, "target URL is not allowed"
    if not parsed.hostname:
        return False, "target URL is not allowed"
    if parsed.username or parsed.password:
        return False, "target URL is not allowed"

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False, "target URL is not allowed"

    ip_host = host.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(ip_host)
    except ValueError:
        return True, ""

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False, "target URL is not allowed"
    return True, ""


def _is_source_access_restricted(status_code: int, body: str) -> bool:
    """Return whether Jina reports a source access restriction."""
    if status_code in _ACCESS_RESTRICTED_STATUS_CODES:
        return True
    lowered = body.lower()
    return any(marker in lowered for marker in _ACCESS_RESTRICTED_MARKERS)


def _fallback_query(target_url: str) -> str:
    """Build a bounded site-scoped query without private URL components."""
    parsed = urlsplit(target_url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    site_clause = f"site:{hostname}".strip()
    path_terms = []
    for part in parsed.path.split("/"):
        if not part:
            continue
        decoded = part
        for _ in range(2):
            next_decoded = unquote(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
        words = []
        current = []
        for character in decoded:
            if character.isalnum():
                current.append(character)
            elif current:
                words.append("".join(current))
                current = []
        if current:
            words.append("".join(current))
        path_terms.extend(word for word in words if word.lower() not in _SEARCH_OPERATOR_TOKENS)
    terms = " ".join(path_terms).strip()
    terms_max_length = max(0, _FALLBACK_QUERY_MAX_LENGTH - len(site_clause) - 1)
    if terms_max_length <= 0:
        return site_clause
    bounded_terms = terms[:terms_max_length].rstrip()
    return f"{bounded_terms} {site_clause}".strip()


def _summary_source_reference(source_url: str) -> str:
    """Render only the approved origin/path portion of a source URL."""
    parsed = urlsplit(source_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}{parsed.path or '/'}"


def _summary_content(source_url: str, results: list[dict]) -> str:
    """Render search snippets as clearly labelled non-article evidence."""
    lines = [
        "Source access was denied. The following are search-result summaries, not article text.",
        f"Original URL: {_summary_source_reference(source_url)}",
        "",
    ]
    for index, result in enumerate(results, start=1):
        title = "" if result.get("title") is None else str(result.get("title")).strip()
        result_url = "" if result.get("url") is None else str(result.get("url")).strip()
        snippet = "" if result.get("snippet") is None else str(result.get("snippet")).strip()
        lines.append(f"{index}. {title}".rstrip())
        if result_url:
            lines.append(f"   {result_url}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines).strip()


def _search_fallback(target_url: str, status_code: int) -> tuple[dict[str, object] | None, str]:
    """Return a summary-only envelope or a safe, specific failure reason."""
    query = _fallback_query(target_url)
    try:
        raw_payload = WebSearchTool().execute(
            query=query,
            max_results=_FALLBACK_MAX_RESULTS,
            _safe_failure_logging=True,
        )
        payload = json.loads(raw_payload)
    except Exception:  # noqa: BLE001 - preserve source-access classification
        logger.warning("read_url summary fallback failed: search backend exception")
        return None, "summary search failed"

    if not isinstance(payload, dict):
        return None, "summary search failed"
    if payload.get("status") != "ok":
        return None, "summary search failed: search unavailable"
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None, "summary search returned no results"

    safe_results = [
        {
            "title": "" if result.get("title") is None else str(result.get("title")),
            "url": "" if result.get("url") is None else str(result.get("url")),
            "snippet": "" if result.get("snippet") is None else str(result.get("snippet")),
        }
        for result in results[:_FALLBACK_MAX_RESULTS]
        if isinstance(result, dict)
    ]
    if not safe_results:
        return None, "summary search returned no results"

    result = {
        "status": "ok",
        "access_level": "summary_only",
        "source_url": target_url,
        "url": target_url,
        "title": "",
        "content": _summary_content(target_url, safe_results),
        "fallback": {
            "reason": "source_access_denied",
            "upstream_status": status_code,
            "query": query,
            "backends": str(payload.get("backends", "")),
            "results": safe_results,
        },
    }
    return with_security_warnings(
        result,
        fields=("content", "fallback.results.*.title", "fallback.results.*.snippet"),
    ), ""


def read_url(url: str, no_cache: bool = False) -> str:
    """Fetch web page content via the Jina Reader API.

    The full URL (including query string) is sent to the third-party Jina
    Reader service (r.jina.ai); never pass credentials/tokens or private
    addresses. Results may be a cached snapshot.

    Args:
        url: Target URL.
        no_cache: When true, ask the reader for a fresh (uncached) fetch.

    Returns:
        JSON result with title, content, url; ``cached: true`` is added
        when the reader served a stale snapshot.
    """
    target_url = url.strip()
    allowed, error = _url_allowed(target_url)
    if not allowed:
        return json.dumps({"status": "error", "error": error}, ensure_ascii=False)

    try:
        headers = {"Accept": "text/markdown"}
        if no_cache:
            headers["x-no-cache"] = "true"
        emit_progress(
            "fetching",
            message=f"GET {target_url[:60]}{'…' if len(target_url) > 60 else ''}",
        )
        resp = requests.get(
            f"{_JINA_PREFIX}{target_url}",
            headers=headers,
            # A single scalar timeout applies independently to connect and
            # read phases, so a stalled connection can consume ~60 seconds.
            # Use urllib3's total deadline as well, so a slow upstream cannot
            # reset the read timeout indefinitely by streaming tiny chunks.
            timeout=_HTTPTimeout(
                connect=_CONNECT_TIMEOUT,
                read=_READ_TIMEOUT,
                total=_TOTAL_TIMEOUT,
            ),
        )
        emit_progress("parsing", message="extracting markdown")
        if resp.status_code != 200:
            restricted = _is_source_access_restricted(resp.status_code, resp.text)
            logger.warning(
                "read_url upstream HTTP %s (source_access_restricted=%s)",
                resp.status_code,
                restricted,
            )
            if restricted:
                fallback, fallback_reason = _search_fallback(target_url, resp.status_code)
                if fallback is not None:
                    return json.dumps(fallback, ensure_ascii=False)
                return json.dumps(
                    {
                        "status": "error",
                        "error_code": "source_access_denied",
                        "error": f"source access denied and {fallback_reason}",
                        "source_url": target_url,
                        "upstream_status": resp.status_code,
                        "suggested_query": _fallback_query(target_url),
                    },
                    ensure_ascii=False,
                )
            return json.dumps({
                "status": "error",
                "error": f"remote reader returned HTTP {resp.status_code}: {resp.text[:500]}",
            }, ensure_ascii=False)

        text = resp.text
        title = ""
        for line in text.split("\n"):
            if line.startswith("Title:"):
                title = line[6:].strip()
                break

        if len(text) > _MAX_LENGTH:
            text = text[:_MAX_LENGTH] + f"\n\n... (truncated, total {len(resp.text)} chars)"

        result = {
            "status": "ok",
            "title": title,
            "url": target_url,
            "content": text,
            "length": len(resp.text),
        }
        if _CACHED_MARKER in resp.text:
            result["cached"] = True
        result = with_security_warnings(result, fields=("content",))
        return json.dumps(result, ensure_ascii=False)

    except requests.Timeout:
        return json.dumps(
            {
                "status": "error",
                "error": (
                    "Request timed out "
                    f"(connect={_CONNECT_TIMEOUT}s, read={_READ_TIMEOUT}s, total={_TOTAL_TIMEOUT}s)"
                ),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.warning("read_url request failed: %s", exc)
        return json.dumps(
            {"status": "error", "error": f"remote reader request failed: {exc}"},
            ensure_ascii=False,
        )


class WebReaderTool(BaseTool):
    """Web reader tool."""

    name = "read_url"
    description = "Fetch web page content: provide a URL and receive the page as Markdown text. Useful for reading docs, articles, API references, etc."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL of the web page to read"},
            "no_cache": {"type": "boolean", "description": "Request a fresh (uncached) fetch", "default": False},
        },
        "required": ["url"],
    }
    repeatable = True

    def execute(self, **kwargs) -> str:
        """Fetch web page."""
        return read_url(kwargs["url"], no_cache=bool(kwargs.get("no_cache", False)))
