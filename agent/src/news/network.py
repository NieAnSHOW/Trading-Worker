"""Bounded, SSRF-hardened transport for configured public news feeds."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
import ipaddress
import socket
from typing import Protocol
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
from weakref import WeakKeyDictionary

import httpx

from .catalog import FeedEndpoint


CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0
TOTAL_FETCH_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 3
MAX_CONCURRENT_REQUESTS = 16
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


class HostResolver(Protocol):
    """Resolve a hostname to numeric IP addresses."""

    def resolve(self, host: str) -> Awaitable[Sequence[str]]:
        """Return all addresses currently resolved for *host*."""


class _SystemResolver:
    async def resolve(self, host: str) -> Sequence[str]:
        records = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
        return tuple(record[4][0] for record in records)


@dataclass(frozen=True)
class EndpointFetchResult:
    """A feed payload or a non-sensitive, stable failure code."""

    endpoint: FeedEndpoint
    body: bytes | None
    error_code: str | None
    final_url: str | None = None

    @property
    def ok(self) -> bool:
        return self.body is not None and self.error_code is None


class PublicFeedClient:
    """Fetch fixed feed endpoints without allowing DNS or redirect SSRF bypasses."""

    _semaphores: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()

    def __init__(self, resolver: HostResolver | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._resolver = resolver or _SystemResolver()
        self._transport = transport

    async def fetch(self, endpoint: FeedEndpoint) -> EndpointFetchResult:
        """Fetch one configured endpoint, returning failures as stable codes."""
        try:
            async with asyncio.timeout(TOTAL_FETCH_TIMEOUT_SECONDS):
                async with self._semaphore():
                    return await self._fetch(endpoint)
        except TimeoutError:
            return self._failure(endpoint, "timeout")

    async def _fetch(self, endpoint: FeedEndpoint) -> EndpointFetchResult:
        current_url = endpoint.url
        redirects = 0
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
            write=READ_TIMEOUT_SECONDS,
            pool=CONNECT_TIMEOUT_SECONDS,
        )
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
                timeout=timeout,
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as client:
                while True:
                    target, error_code = await self._validated_target(current_url)
                    if error_code is not None:
                        return self._failure(endpoint, error_code)
                    assert target is not None

                    try:
                        request = httpx.Request(
                            "GET",
                            target.request_url,
                            headers={
                                "Host": target.host_header,
                                "Accept": "application/atom+xml, application/rss+xml, application/xml",
                                "Connection": "close",
                            },
                            extensions={"sni_hostname": target.hostname},
                        )
                    except (UnicodeError, ValueError, httpx.InvalidURL):
                        return self._failure(endpoint, "invalid_url")
                    response = await client.send(request, stream=True)
                    try:
                        if response.is_redirect:
                            if redirects >= MAX_REDIRECTS:
                                return self._failure(endpoint, "too_many_redirects")
                            location = response.headers.get("location")
                            if not location:
                                return self._failure(endpoint, "invalid_redirect")
                            current_url = urljoin(current_url, location)
                            redirects += 1
                            continue
                        if response.status_code < 200 or response.status_code >= 300:
                            return self._failure(endpoint, "http_status")
                        if self._content_length_too_large(response):
                            return self._failure(endpoint, "response_too_large")
                        body, error_code = await self._read_body(response)
                        if error_code is not None:
                            return self._failure(endpoint, error_code)
                        return EndpointFetchResult(endpoint=endpoint, body=body, error_code=None, final_url=current_url)
                    finally:
                        await response.aclose()
        except httpx.TimeoutException:
            return self._failure(endpoint, "timeout")
        except httpx.RequestError:
            return self._failure(endpoint, "network_error")

    @classmethod
    def _semaphore(cls) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        semaphore = cls._semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
            cls._semaphores[loop] = semaphore
        return semaphore

    async def _validated_target(self, value: str) -> tuple["_RequestTarget | None", str | None]:
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return None, "invalid_url"
        if parsed.scheme not in {"http", "https"}:
            return None, "unsupported_scheme"
        if not hostname or parsed.username is not None or parsed.password is not None:
            return None, "invalid_url"
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            return None, "invalid_url"

        try:
            addresses = await self._resolver.resolve(hostname)
        except Exception:  # Resolver implementations are untrusted network boundaries.
            return None, "dns_failed"
        if not addresses:
            return None, "dns_failed"
        try:
            parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
        except ValueError:
            return None, "dns_failed"
        if any(not address.is_global or address.is_multicast for address in parsed_addresses):
            return None, "unsafe_target"

        selected = parsed_addresses[0]
        host_header = _host_with_port(hostname, port)
        request_url = _request_url(parsed, selected, port)
        return _RequestTarget(request_url=request_url, hostname=hostname, host_header=host_header), None

    @staticmethod
    def _content_length_too_large(response: httpx.Response) -> bool:
        try:
            return int(response.headers.get("content-length", "0")) > MAX_RESPONSE_BYTES
        except ValueError:
            return False

    @staticmethod
    async def _read_body(response: httpx.Response) -> tuple[bytes | None, str | None]:
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=READ_CHUNK_BYTES):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                return None, "response_too_large"
        return bytes(body), None

    @staticmethod
    def _failure(endpoint: FeedEndpoint, code: str) -> EndpointFetchResult:
        return EndpointFetchResult(endpoint=endpoint, body=None, error_code=code)


@dataclass(frozen=True)
class _RequestTarget:
    request_url: str
    hostname: str
    host_header: str


def _host_with_port(hostname: str, port: int | None) -> str:
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{rendered_host}:{port}" if port is not None else rendered_host


def _request_url(parsed: SplitResult, address: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int | None) -> str:
    rendered_address = f"[{address.compressed}]" if address.version == 6 else address.compressed
    netloc = f"{rendered_address}:{port}" if port is not None else rendered_address
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
