from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.news.models import (
    CANONICAL_TRACK_IDS,
    FeedItem,
    FeedSource,
    NewsSnapshot,
    RefreshAcceptedResponse,
    RefreshStatus,
    SnapshotResponse,
    TrackAi,
    TrackSnapshot,
)


NOW = datetime(2026, 7, 20, 8, 30, tzinfo=timezone.utc)


def item(track_id: str = "ai") -> FeedItem:
    return FeedItem(
        id="item-1",
        track_id=track_id,
        title="A valid title",
        summary="A short, normalized summary.",
        source=FeedSource(id="source-1", name="Source", url="https://example.com/feed"),
        published_at=NOW,
        url="https://example.com/article",
    )


def track(track_id: str, state: str = "fresh") -> TrackSnapshot:
    if state == "unavailable":
        return TrackSnapshot(track_id=track_id, state=state, generated_at=None, stale=False, items=[], ai=TrackAi())
    return TrackSnapshot(
        track_id=track_id,
        state=state,
        generated_at=NOW,
        stale=state == "stale",
        items=[item(track_id)],
        ai=TrackAi(available=True, generated_at=NOW, highlights=["One useful point", "Another useful point", "Third useful point"]),
    )


def snapshot() -> NewsSnapshot:
    return NewsSnapshot(
        schema_version=1,
        generated_at=NOW,
        upstream_commit="d98aa603228f4839fb48859812c63a58ca10cead",
        source_success_count=1,
        source_failure_count=0,
        errors=[],
        tracks=[track(track_id) for track_id in CANONICAL_TRACK_IDS],
    )


def test_unavailable_track_has_no_generation_time_or_items() -> None:
    with pytest.raises(ValidationError):
        TrackSnapshot(track_id="ai", state="unavailable", generated_at=NOW, stale=False, items=[], ai=TrackAi())

    with pytest.raises(ValidationError):
        TrackSnapshot(track_id="ai", state="unavailable", generated_at=None, stale=False, items=[item()], ai=TrackAi())

    valid = TrackSnapshot(track_id="ai", state="unavailable", generated_at=None, stale=False, items=[], ai=TrackAi())
    assert valid.ai.available is False


def test_track_state_must_match_generation_and_stale_flags() -> None:
    with pytest.raises(ValidationError):
        TrackSnapshot(track_id="ai", state="fresh", generated_at=NOW, stale=True, items=[item()], ai=TrackAi())

    with pytest.raises(ValidationError):
        TrackSnapshot(track_id="ai", state="stale", generated_at=None, stale=True, items=[item()], ai=TrackAi())


def test_snapshot_requires_canonical_track_ids_in_canonical_order() -> None:
    valid = snapshot().model_dump(mode="json")
    valid["tracks"][0], valid["tracks"][1] = valid["tracks"][1], valid["tracks"][0]

    with pytest.raises(ValidationError):
        NewsSnapshot.model_validate(valid)


def test_datetime_values_must_be_utc_aware() -> None:
    naive = item().model_dump()
    naive["published_at"] = datetime(2026, 7, 20, 8, 30)

    with pytest.raises(ValidationError):
        FeedItem.model_validate(naive)

    offset = item().model_dump()
    offset["published_at"] = datetime.fromisoformat("2026-07-20T16:30:00+08:00")
    with pytest.raises(ValidationError):
        FeedItem.model_validate(offset)


def test_item_lengths_and_unknown_fields_are_rejected() -> None:
    too_long = item().model_dump()
    too_long["title"] = "x" * 301
    with pytest.raises(ValidationError):
        FeedItem.model_validate(too_long)

    secret = item().model_dump()
    secret["api_key"] = "must-not-persist"
    with pytest.raises(ValidationError):
        FeedItem.model_validate(secret)


def test_snapshot_response_and_refresh_acceptance_expose_only_public_state() -> None:
    status = RefreshStatus(state="idle")
    response = SnapshotResponse(available=True, stale=False, snapshot=snapshot(), refresh=status, error=None)
    accepted = RefreshAcceptedResponse(task_id=uuid4(), reused=False, status=status)

    assert response.available is True
    assert accepted.status.state == "idle"

    with pytest.raises(ValidationError):
        SnapshotResponse(available=False, stale=False, snapshot=snapshot(), refresh=status, error=None)
