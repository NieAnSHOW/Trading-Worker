"""Single-task, in-process orchestration for investment-news refreshes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
import inspect
import logging
from typing import Any, Protocol
from uuid import UUID, uuid4

from .catalog import NewsCatalog, SourceAssignment, group_endpoints, load_catalog
from .feeds import RawFeedItem, parse_feed
from .llm import enrich_tracks
from .models import (
    CANONICAL_TRACK_IDS,
    NewsSnapshot,
    PublicError,
    RefreshAcceptedResponse,
    RefreshStatus,
    SnapshotResponse,
    SourceStats,
    TrackSnapshot,
)
from .network import EndpointFetchResult, PublicFeedClient
from .pipeline import AssignmentItems, TrackBuildResult, build_track_candidates
from .storage import AtomicSnapshotStore, SnapshotStorageError


logger = logging.getLogger(__name__)

_PUBLIC_ERROR_MESSAGES = {
    "cancelled": "news refresh was cancelled",
    "no_track_updated": "no news tracks were updated",
    "snapshot_corrupt": "news snapshot is corrupt",
    "snapshot_unavailable": "news snapshot is unavailable",
    "snapshot_write_failed": "news snapshot could not be saved",
    "upstream_failed": "news refresh failed",
}
_SAFE_ENDPOINT_ERROR_CODES = frozenset(
    {
        "dns_failed",
        "http_status",
        "invalid_redirect",
        "invalid_url",
        "network_error",
        "response_too_large",
        "timeout",
        "too_many_redirects",
        "unsafe_target",
        "unsupported_scheme",
    }
)


class FeedClient(Protocol):
    async def fetch(self, endpoint: Any) -> EndpointFetchResult: ...


class SnapshotStore(Protocol):
    def read(self) -> NewsSnapshot | None: ...

    def write(self, snapshot: NewsSnapshot) -> None: ...


Parser = Callable[[bytes, SourceAssignment], tuple[RawFeedItem, ...]]
Pipeline = Callable[[NewsCatalog, Sequence[AssignmentItems], NewsSnapshot | None, datetime], TrackBuildResult]
Enricher = Callable[
    [Sequence[TrackSnapshot], frozenset[str], Callable[[], Any]], Awaitable[tuple[TrackSnapshot, ...]]
]


class NewsRefreshCoordinator:
    """Own one background refresh and the last committed in-memory snapshot."""

    def __init__(
        self,
        *,
        catalog: NewsCatalog | None = None,
        client_factory: Callable[[], FeedClient] | None = None,
        parser: Parser = parse_feed,
        pipeline: Pipeline = build_track_candidates,
        enricher: Enricher = enrich_tracks,
        llm_factory: Callable[[], Any] | None = None,
        store: SnapshotStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._catalog = catalog or load_catalog()
        self._endpoints = group_endpoints(self._catalog)
        self._client_factory = client_factory or _default_client_factory
        self._parser = parser
        self._pipeline = pipeline
        self._enricher = enricher
        self._llm_factory = llm_factory or _build_project_llm
        self._store = store or AtomicSnapshotStore()
        self._now = now or _utc_now

        self._lock = asyncio.Lock()
        self._closing = False
        self._task: asyncio.Task[None] | None = None
        self._client: FeedClient | None = None
        self._snapshot: NewsSnapshot | None = None
        self._status = RefreshStatus(state="idle", total_endpoints=len(self._endpoints))

    async def start(self) -> RefreshAcceptedResponse:
        """Start one background refresh, or reuse the currently running task."""
        async with self._lock:
            if self._closing:
                raise RuntimeError("news refresh coordinator is closed")
            if self._task is not None and not self._task.done():
                assert self._status.task_id is not None
                return RefreshAcceptedResponse(
                    task_id=self._status.task_id,
                    reused=True,
                    status=self._status.model_copy(deep=True),
                )

            task_id = uuid4()
            self._status = RefreshStatus(
                state="fetching",
                task_id=task_id,
                started_at=self._now(),
                total_endpoints=len(self._endpoints),
            )
            self._task = asyncio.create_task(self._run(task_id), name=f"news-refresh-{task_id}")
            return RefreshAcceptedResponse(
                task_id=task_id,
                reused=False,
                status=self._status.model_copy(deep=True),
            )

    async def status(self) -> RefreshStatus:
        """Return an immutable copy of the current in-process task status."""
        async with self._lock:
            return self._status.model_copy(deep=True)

    async def snapshot_response(self) -> SnapshotResponse:
        """Return the latest committed snapshot without triggering refresh work."""
        current_status = await self.status()
        snapshot = self._snapshot
        read_error: PublicError | None = None
        if snapshot is None:
            try:
                snapshot = await asyncio.to_thread(self._store.read)
            except SnapshotStorageError as exc:
                read_error = _public_error(exc.code)
            except Exception:
                read_error = _public_error("snapshot_corrupt")
            if snapshot is not None:
                async with self._lock:
                    if self._snapshot is None:
                        self._snapshot = snapshot

        task_error = current_status.error if current_status.state == "failed" else None
        error = task_error or read_error
        return SnapshotResponse(
            available=snapshot is not None,
            stale=snapshot is not None and current_status.state == "failed",
            snapshot=snapshot,
            refresh=current_status,
            error=error,
        )

    async def wait_current(self) -> None:
        """Wait for the currently observed refresh task to reach a terminal state."""
        async with self._lock:
            task = self._task
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise

    async def close(self) -> None:
        """Cancel active work, await it, and release the lazily-created client."""
        async with self._lock:
            self._closing = True
            task = self._task
            should_cancel = self._status.state != "committing"
        if task is not None and not task.done():
            if should_cancel:
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        client = self._client
        self._client = None
        if client is None:
            return
        async_close = getattr(client, "aclose", None)
        if callable(async_close):
            result = async_close()
            if inspect.isawaitable(result):
                await result
            return
        sync_close = getattr(client, "close", None)
        if callable(sync_close):
            await asyncio.to_thread(sync_close)

    async def _run(self, task_id: UUID) -> None:
        try:
            previous = await self._read_previous()
            client = self._client
            if client is None:
                client = self._client_factory()
                self._client = client

            fetch_results = await self._fetch_all(client)
            await self._replace_status(state="normalizing")
            assignments, assignment_failures = await self._normalize(fetch_results)
            built = await asyncio.to_thread(self._pipeline, self._catalog, assignments, previous, self._now())
            if not built.updated_track_ids:
                await self._fail(task_id, "no_track_updated")
                return

            await self._replace_status(state="enriching", processed_tracks=len(CANONICAL_TRACK_IDS))
            tracks = await self._enricher(built.tracks, built.updated_track_ids, self._llm_factory)
            source_stats = self._source_stats(fetch_results, assignment_failures)
            errors = [_public_error("upstream_failed")] if source_stats.endpoint_failure_count or assignment_failures else []
            snapshot = NewsSnapshot(
                schema_version=1,
                generated_at=self._now(),
                upstream_commit=self._catalog.commit,
                source_stats=source_stats,
                errors=errors,
                tracks=list(tracks),
            )

            await self._replace_status(state="committing")
            await asyncio.to_thread(self._store.write, snapshot)
            async with self._lock:
                self._snapshot = snapshot
                self._status = self._status.model_copy(
                    update={"state": "succeeded", "completed_at": self._now(), "error": None}
                )
        except asyncio.CancelledError:
            await self._cancel(task_id)
            raise
        except SnapshotStorageError as exc:
            await self._fail(task_id, exc.code)
        except Exception:
            logger.warning("news refresh failed with a redacted internal error")
            await self._fail(task_id, "upstream_failed")

    async def _read_previous(self) -> NewsSnapshot | None:
        if self._snapshot is not None:
            return self._snapshot
        try:
            previous = await asyncio.to_thread(self._store.read)
        except SnapshotStorageError:
            return None
        except Exception:
            logger.warning("news snapshot read failed with a redacted internal error")
            return None
        if previous is not None:
            async with self._lock:
                if self._snapshot is None:
                    self._snapshot = previous
        return previous

    async def _fetch_all(self, client: FeedClient) -> tuple[EndpointFetchResult, ...]:
        async def fetch_one(endpoint: Any) -> EndpointFetchResult:
            try:
                return await client.fetch(endpoint)
            except asyncio.CancelledError:
                raise
            except Exception:
                return EndpointFetchResult(endpoint=endpoint, body=None, error_code="network_error")

        tasks = [asyncio.create_task(fetch_one(endpoint)) for endpoint in self._endpoints]
        results: list[EndpointFetchResult] = []
        try:
            for completed in asyncio.as_completed(tasks):
                result = await completed
                results.append(result)
                successes = sum(item.ok for item in results)
                await self._replace_status(
                    processed_endpoints=len(results),
                    successful_endpoints=successes,
                    failed_endpoints=len(results) - successes,
                )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return tuple(sorted(results, key=lambda item: item.endpoint.id))

    async def _normalize(
        self, fetch_results: Sequence[EndpointFetchResult]
    ) -> tuple[tuple[AssignmentItems, ...], int]:
        normalized: list[AssignmentItems] = []
        assignment_failures = 0
        for result in fetch_results:
            if not result.ok:
                self._log_endpoint_failure(result)
                for source in result.endpoint.assignments:
                    normalized.append(
                        AssignmentItems(source=source, items=(), endpoint_failed=True, assignment_failed=True)
                    )
                    assignment_failures += 1
                continue

            assert result.body is not None
            for source in result.endpoint.assignments:
                try:
                    items = await asyncio.to_thread(self._parser, result.body, source)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("news assignment parse failed: assignment_id=%s track_id=%s", source.id, source.track_id)
                    items = ()
                    assignment_failures += 1
                    normalized.append(AssignmentItems(source=source, items=items, assignment_failed=True))
                else:
                    normalized.append(AssignmentItems(source=source, items=items))
        return tuple(normalized), assignment_failures

    def _log_endpoint_failure(self, result: EndpointFetchResult) -> None:
        assignments = result.endpoint.assignments
        assignment_ids = tuple(source.id for source in assignments[:5])
        track_ids = tuple(dict.fromkeys(source.track_id for source in assignments))[:5]
        error_code = result.error_code if result.error_code in _SAFE_ENDPOINT_ERROR_CODES else "upstream_failed"
        logger.warning(
            "news endpoint failed: endpoint_id=%s code=%s assignment_ids=%s track_ids=%s",
            result.endpoint.id,
            error_code,
            assignment_ids,
            track_ids,
        )

    @staticmethod
    def _source_stats(fetch_results: Sequence[EndpointFetchResult], assignment_failures: int) -> SourceStats:
        successful_endpoints = sum(result.ok for result in fetch_results)
        assignment_count = sum(len(result.endpoint.assignments) for result in fetch_results)
        return SourceStats(
            endpoint_success_count=successful_endpoints,
            endpoint_failure_count=len(fetch_results) - successful_endpoints,
            assignment_success_count=assignment_count - assignment_failures,
            assignment_failure_count=assignment_failures,
        )

    async def _replace_status(self, **updates: Any) -> None:
        async with self._lock:
            self._status = self._status.model_copy(update=updates)

    async def _fail(self, task_id: UUID, code: str) -> None:
        async with self._lock:
            if self._status.task_id != task_id:
                return
            self._status = self._status.model_copy(
                update={"state": "failed", "completed_at": self._now(), "error": _public_error(code)}
            )

    async def _cancel(self, task_id: UUID) -> None:
        async with self._lock:
            if self._status.task_id != task_id:
                return
            self._status = self._status.model_copy(
                update={"state": "cancelled", "completed_at": self._now(), "error": _public_error("cancelled")}
            )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _public_error(code: str) -> PublicError:
    if code not in _PUBLIC_ERROR_MESSAGES:
        code = "upstream_failed"
    return PublicError(code=code, message=_PUBLIC_ERROR_MESSAGES[code])


def _default_client_factory() -> PublicFeedClient:
    return PublicFeedClient()


def _build_project_llm() -> Any:
    from src.providers.llm import build_llm

    return build_llm()


_news_coordinator: NewsRefreshCoordinator | None = None


def get_news_coordinator() -> NewsRefreshCoordinator:
    """Return the process coordinator without starting I/O or constructing an LLM."""
    global _news_coordinator
    if _news_coordinator is None:
        _news_coordinator = NewsRefreshCoordinator()
    return _news_coordinator
