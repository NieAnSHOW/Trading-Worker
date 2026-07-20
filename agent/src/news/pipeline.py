"""Deterministic filtering and track-level merging for investment news."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import re
from typing import Sequence
from unicodedata import normalize
from urllib.parse import urlsplit, urlunsplit

from .catalog import NewsCatalog, SourceAssignment
from .feeds import RawFeedItem
from .models import CANONICAL_TRACK_IDS, FeedItem, FeedSource, NewsSnapshot, SourceStats, TrackAi, TrackSnapshot


MAX_ENDPOINT_ITEMS = 6
MAX_TRACK_ITEMS = 100
WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class AssignmentItems:
    """The normalized result for one catalog assignment."""

    source: SourceAssignment
    items: tuple[RawFeedItem, ...]
    endpoint_failed: bool = False
    assignment_failed: bool = False


@dataclass(frozen=True)
class TrackBuildResult:
    tracks: tuple[TrackSnapshot, ...]
    updated_track_ids: frozenset[str]


@dataclass(frozen=True)
class _Candidate:
    item: FeedItem
    endpoint_url: str


def build_track_candidates(
    catalog: NewsCatalog,
    current: Sequence[AssignmentItems],
    previous: NewsSnapshot | None,
    now: datetime,
) -> TrackBuildResult:
    """Build canonical track candidates without performing I/O or AI generation."""
    by_track: dict[str, list[_Candidate]] = {track_id: [] for track_id in CANONICAL_TRACK_IDS}
    stats_by_track = _source_stats_by_track(current)
    for assignment_items in current:
        source = assignment_items.source
        if source.track_id not in by_track:
            continue
        endpoint_url = _canonicalize_url(source.url)
        for raw_item in assignment_items.items:
            candidate = _normalize_item(raw_item, source, catalog.filters, now, endpoint_url)
            if candidate is not None:
                by_track[source.track_id].append(candidate)

    prior_tracks = {track.track_id: track for track in previous.tracks} if previous is not None else {}
    tracks: list[TrackSnapshot] = []
    updated_track_ids: set[str] = set()
    for track_id in CANONICAL_TRACK_IDS:
        current_items = _limit_track_items(by_track[track_id])
        prior = prior_tracks.get(track_id)
        if current_items:
            updated_track_ids.add(track_id)
            tracks.append(_fresh_track(track_id, current_items, prior, now, stats_by_track[track_id]))
        elif prior is not None and prior.items:
            tracks.append(_stale_track(prior))
        else:
            tracks.append(_unavailable_track(track_id, stats_by_track[track_id]))

    return TrackBuildResult(tracks=tuple(tracks), updated_track_ids=frozenset(updated_track_ids))


def _normalize_item(
    raw_item: RawFeedItem,
    source: SourceAssignment,
    catalog_filters: tuple[str, ...],
    now: datetime,
    endpoint_url: str,
) -> _Candidate | None:
    title = _normalize_text(raw_item.title)
    summary = _normalize_text(raw_item.summary) or None
    if not title or _is_filtered(title, summary, (*catalog_filters, *source.filters)):
        return None
    published_at = raw_item.published_at
    if published_at is not None and (published_at.tzinfo is None or published_at < now - WINDOW or published_at > now):
        return None
    try:
        article_url = _canonicalize_url(raw_item.url)
        item = FeedItem(
            id=_stable_id(source.track_id, article_url),
            track_id=source.track_id,
            title=title,
            summary=summary,
            source=FeedSource(id=source.id, name=_normalize_text(source.name), url=_canonicalize_url(source.url)),
            published_at=published_at,
            url=article_url,
        )
    except ValueError:
        return None
    return _Candidate(item=item, endpoint_url=endpoint_url)


def _source_stats_by_track(current: Sequence[AssignmentItems]) -> dict[str, SourceStats]:
    endpoints: dict[str, set[str]] = {track_id: set() for track_id in CANONICAL_TRACK_IDS}
    failed_endpoints: dict[str, set[str]] = {track_id: set() for track_id in CANONICAL_TRACK_IDS}
    assignment_successes: dict[str, int] = {track_id: 0 for track_id in CANONICAL_TRACK_IDS}
    assignment_failures: dict[str, int] = {track_id: 0 for track_id in CANONICAL_TRACK_IDS}
    for assignment_items in current:
        source = assignment_items.source
        track_id = source.track_id
        if track_id not in endpoints:
            continue
        endpoint_url = _canonicalize_url(source.url)
        endpoints[track_id].add(endpoint_url)
        if assignment_items.endpoint_failed:
            failed_endpoints[track_id].add(endpoint_url)
        if assignment_items.assignment_failed:
            assignment_failures[track_id] += 1
        else:
            assignment_successes[track_id] += 1
    return {
        track_id: SourceStats(
            endpoint_success_count=len(endpoints[track_id] - failed_endpoints[track_id]),
            endpoint_failure_count=len(failed_endpoints[track_id]),
            assignment_success_count=assignment_successes[track_id],
            assignment_failure_count=assignment_failures[track_id],
        )
        for track_id in CANONICAL_TRACK_IDS
    }


def _limit_track_items(candidates: Sequence[_Candidate]) -> list[FeedItem]:
    by_endpoint: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        by_endpoint.setdefault(candidate.endpoint_url, []).append(candidate)
    endpoint_limited = [
        candidate
        for endpoint_url in sorted(by_endpoint)
        for candidate in _deduplicate(by_endpoint[endpoint_url])[:MAX_ENDPOINT_ITEMS]
    ]
    return [candidate.item for candidate in _deduplicate(endpoint_limited)[:MAX_TRACK_ITEMS]]


def _deduplicate(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    result: list[_Candidate] = []
    seen_ids: set[str] = set()
    for candidate in sorted(candidates, key=_sort_key):
        if candidate.item.id not in seen_ids:
            seen_ids.add(candidate.item.id)
            result.append(candidate)
    return result


def _fresh_track(
    track_id: str,
    current_items: Sequence[FeedItem],
    prior: TrackSnapshot | None,
    now: datetime,
    source_stats: SourceStats,
) -> TrackSnapshot:
    by_id = {item.id: item for item in current_items}
    if prior is not None:
        for item in prior.items:
            by_id.setdefault(item.id, item)
    merged = sorted((_Candidate(item, "") for item in by_id.values()), key=_sort_key)[:MAX_TRACK_ITEMS]
    has_failures = source_stats.endpoint_failure_count > 0 or source_stats.assignment_failure_count > 0
    return TrackSnapshot(
        track_id=track_id,
        state="fresh",
        generated_at=now,
        stale=False,
        partial=has_failures,
        items=[candidate.item for candidate in merged],
        ai=TrackAi(),
        source_stats=source_stats,
    )


def _stale_track(prior: TrackSnapshot) -> TrackSnapshot:
    return TrackSnapshot(
        track_id=prior.track_id,
        state="stale",
        generated_at=prior.generated_at,
        stale=True,
        partial=False,
        items=prior.items,
        ai=prior.ai,
        source_stats=prior.source_stats,
    )


def _unavailable_track(track_id: str, source_stats: SourceStats) -> TrackSnapshot:
    return TrackSnapshot(
        track_id=track_id,
        state="unavailable",
        generated_at=None,
        stale=False,
        partial=False,
        items=[],
        ai=TrackAi(),
        source_stats=source_stats,
    )


def _sort_key(candidate: _Candidate) -> tuple[int, float, int, str, str]:
    item = candidate.item
    published_at = item.published_at
    return (
        0 if published_at is not None else 1,
        -published_at.timestamp() if published_at is not None else 0.0,
        0 if item.summary else 1,
        item.source.id,
        item.id,
    )


def _is_filtered(title: str, summary: str | None, filters: Sequence[str]) -> bool:
    haystack = f"{title}\n{summary or ''}".casefold()
    return any(term.casefold() in haystack for term in filters if term)


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", normalize("NFKC", value or "")).strip()


def _canonicalize_url(value: str) -> str:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if scheme not in {"http", "https"} or hostname is None or parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid URL")
    hostname = hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parsed.port
    netloc = hostname if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443) else f"{hostname}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def _stable_id(track_id: str, canonical_url: str) -> str:
    return hashlib.sha256(f"{track_id}\0{canonical_url}".encode("utf-8")).hexdigest()
