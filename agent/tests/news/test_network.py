from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable

import httpx
import pytest

from src.news.catalog import FeedEndpoint
from src.news import network
from src.news.network import MAX_RESPONSE_BYTES, PublicFeedClient


class _ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, delay: float = 0.0) -> None:
        self._chunks = chunks
        self._delay = delay

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk

    async def aclose(self) -> None:
        return None


@dataclass
class FakeResolver:
    addresses: dict[str, list[str]]
    calls: list[str] = field(default_factory=list)

    async def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.addresses[host]


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: dict[str, httpx.Response] | None = None, *, delay: float = 0.0) -> None:
        self.responses = responses or {}
        self.requests: list[httpx.Request] = []
        self.delay = delay
        self.active = 0
        self.peak_active = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            response = self.responses[str(request.url)]
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=response.stream,
                request=request,
            )
        finally:
            self.active -= 1


class TimeoutTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise httpx.ReadTimeout("private diagnostic", request=request)


class BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self, expected_requests: int) -> None:
        self.expected_requests = expected_requests
        self.requests: list[httpx.Request] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) == self.expected_requests:
            self.entered.set()
        await self.release.wait()
        return httpx.Response(200, stream=_ByteStream([b"ok"]), request=request)


class BrokenResolver:
    async def resolve(self, host: str) -> list[str]:
        raise RuntimeError("private resolver diagnostic")


class HangingResolver:
    async def resolve(self, host: str) -> list[str]:
        await asyncio.Event().wait()
        return []


def endpoint(url: str = "https://feed.example/rss") -> FeedEndpoint:
    return FeedEndpoint(id="endpoint", url=url, assignments=())


def _async_test(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(function(*args, **kwargs))

    return run


@_async_test
async def test_fetch_pins_request_to_resolved_ip_and_preserves_host_and_sni() -> None:
    resolver = FakeResolver({"feed.example": ["93.184.216.34"]})
    transport = FakeTransport(
        {"https://93.184.216.34/rss": httpx.Response(200, stream=_ByteStream([b"<rss />"]))}
    )

    result = await PublicFeedClient(resolver, transport=transport).fetch(endpoint())

    assert result.body == b"<rss />"
    assert result.error_code is None
    request = transport.requests[0]
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "feed.example"
    assert request.extensions["sni_hostname"] == "feed.example"


@pytest.mark.parametrize(
    ("url", "addresses", "expected_code"),
    [
        ("ftp://feed.example/rss", {"feed.example": ["93.184.216.34"]}, "unsupported_scheme"),
        ("https://user:pass@feed.example/rss", {"feed.example": ["93.184.216.34"]}, "invalid_url"),
        ("https://feed.example/rss", {"feed.example": ["127.0.0.1"]}, "unsafe_target"),
        ("https://feed.example/rss", {"feed.example": ["10.0.0.1"]}, "unsafe_target"),
        ("https://feed.example/rss", {"feed.example": ["169.254.0.1"]}, "unsafe_target"),
        ("https://feed.example/rss", {"feed.example": ["224.0.0.1"]}, "unsafe_target"),
        ("https://feed.example/rss", {"feed.example": ["203.0.113.1"]}, "unsafe_target"),
    ],
)
@_async_test
async def test_fetch_rejects_unsafe_or_invalid_targets(
    url: str, addresses: dict[str, list[str]], expected_code: str
) -> None:
    transport = FakeTransport()

    result = await PublicFeedClient(FakeResolver(addresses), transport=transport).fetch(endpoint(url))

    assert result.body is None
    assert result.error_code == expected_code
    assert transport.requests == []


@_async_test
async def test_fetch_revalidates_each_redirect_before_pinning_next_request() -> None:
    resolver = FakeResolver(
        {"feed.example": ["93.184.216.34"], "redirect.example": ["93.184.216.35"]}
    )
    transport = FakeTransport(
        {
            "https://93.184.216.34/rss": httpx.Response(302, headers={"location": "https://redirect.example/new"}),
            "https://93.184.216.35/new": httpx.Response(200, stream=_ByteStream([b"redirected"])),
        }
    )

    result = await PublicFeedClient(resolver, transport=transport).fetch(endpoint())

    assert result.body == b"redirected"
    assert result.error_code is None
    assert resolver.calls == ["feed.example", "redirect.example"]
    assert [request.headers["host"] for request in transport.requests] == ["feed.example", "redirect.example"]
    assert [request.extensions["sni_hostname"] for request in transport.requests] == ["feed.example", "redirect.example"]


@_async_test
async def test_fetch_rejects_an_unsafe_redirect_target() -> None:
    resolver = FakeResolver({"feed.example": ["93.184.216.34"], "internal.example": ["127.0.0.1"]})
    transport = FakeTransport(
        {"https://93.184.216.34/rss": httpx.Response(302, headers={"location": "http://internal.example/secret"})}
    )

    result = await PublicFeedClient(resolver, transport=transport).fetch(endpoint())

    assert result.error_code == "unsafe_target"
    assert len(transport.requests) == 1


@_async_test
async def test_fetch_stops_after_three_redirects() -> None:
    resolver = FakeResolver({"feed.example": ["93.184.216.34"]})
    transport = FakeTransport(
        {
            "https://93.184.216.34/a": httpx.Response(302, headers={"location": "/b"}),
            "https://93.184.216.34/b": httpx.Response(302, headers={"location": "/c"}),
            "https://93.184.216.34/c": httpx.Response(302, headers={"location": "/d"}),
            "https://93.184.216.34/d": httpx.Response(302, headers={"location": "/e"}),
        }
    )

    result = await PublicFeedClient(resolver, transport=transport).fetch(endpoint("https://feed.example/a"))

    assert result.error_code == "too_many_redirects"
    assert len(transport.requests) == 4


@_async_test
async def test_fetch_limits_streamed_body_to_two_mebibytes() -> None:
    resolver = FakeResolver({"feed.example": ["93.184.216.34"]})
    transport = FakeTransport(
        {
            "https://93.184.216.34/rss": httpx.Response(
                200, stream=_ByteStream([b"a" * 65536] * ((MAX_RESPONSE_BYTES // 65536) + 1))
            )
        }
    )

    result = await PublicFeedClient(resolver, transport=transport).fetch(endpoint())

    assert result.body is None
    assert result.error_code == "response_too_large"


@_async_test
async def test_fetch_maps_timeouts_without_exposing_diagnostic_text() -> None:
    resolver = FakeResolver({"feed.example": ["93.184.216.34"]})
    transport = TimeoutTransport()

    result = await PublicFeedClient(resolver, transport=transport).fetch(endpoint())

    assert result.body is None
    assert result.error_code == "timeout"
    assert transport.requests[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 15.0,
        "write": 15.0,
        "pool": 5.0,
    }


@_async_test
async def test_fetch_maps_unexpected_resolver_failures_to_a_stable_code() -> None:
    result = await PublicFeedClient(BrokenResolver(), transport=FakeTransport()).fetch(endpoint())

    assert result.body is None
    assert result.error_code == "dns_failed"


@_async_test
async def test_fetch_applies_a_total_deadline_to_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network, "TOTAL_FETCH_TIMEOUT_SECONDS", 0.001, raising=False)

    result = await asyncio.wait_for(
        PublicFeedClient(HangingResolver(), transport=FakeTransport()).fetch(endpoint()), timeout=0.1
    )

    assert result.body is None
    assert result.error_code == "timeout"


@_async_test
async def test_fetch_applies_a_total_deadline_to_a_slow_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network, "TOTAL_FETCH_TIMEOUT_SECONDS", 0.02)
    resolver = FakeResolver({"feed.example": ["93.184.216.34"]})
    transport = FakeTransport(
        {"https://93.184.216.34/rss": httpx.Response(200, stream=_ByteStream([b"a"] * 10, delay=0.04))}
    )

    result = await PublicFeedClient(resolver, transport=transport).fetch(endpoint())

    assert result.body is None
    assert result.error_code == "timeout"


@_async_test
async def test_fetch_applies_total_deadline_while_waiting_for_global_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = FakeResolver({"feed.example": ["93.184.216.34"]})
    transport = BlockingTransport(expected_requests=16)
    client = PublicFeedClient(resolver, transport=transport)
    holders = [asyncio.create_task(client.fetch(endpoint(f"https://feed.example/{index}"))) for index in range(16)]
    await asyncio.wait_for(transport.entered.wait(), timeout=0.1)
    monkeypatch.setattr(network, "TOTAL_FETCH_TIMEOUT_SECONDS", 0.01)

    try:
        result = await asyncio.wait_for(client.fetch(endpoint("https://feed.example/queued")), timeout=0.1)
    finally:
        transport.release.set()
        await asyncio.gather(*holders)

    assert result.body is None
    assert result.error_code == "timeout"
    assert len(transport.requests) == 16


@_async_test
async def test_fetches_share_a_global_concurrency_limit_of_sixteen() -> None:
    resolver = FakeResolver({"feed.example": ["93.184.216.34"]})
    responses = {
        f"https://93.184.216.34/{index}": httpx.Response(200, stream=_ByteStream([b"ok"])) for index in range(20)
    }
    transport = FakeTransport(responses, delay=0.01)
    client = PublicFeedClient(resolver, transport=transport)

    results = await asyncio.gather(*(client.fetch(endpoint(f"https://feed.example/{index}")) for index in range(20)))

    assert all(result.body == b"ok" for result in results)
    assert transport.peak_active == 16
