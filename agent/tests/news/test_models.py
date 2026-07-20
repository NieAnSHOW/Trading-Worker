from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.news.models import (
    CANONICAL_TRACK_IDS,
    FeedItem,
    FeedSource,
    NewsSnapshot,
    PublicError,
    RefreshAcceptedResponse,
    RefreshStatus,
    SourceStats,
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
        return TrackSnapshot(
            track_id=track_id,
            state=state,
            generated_at=None,
            stale=False,
            partial=False,
            items=[],
            ai=TrackAi(),
            source_stats=SourceStats.empty(),
        )
    return TrackSnapshot(
        track_id=track_id,
        state=state,
        generated_at=NOW,
        stale=state == "stale",
        partial=False,
        items=[item(track_id)],
        ai=TrackAi(available=True, generated_at=NOW, highlights=["One useful point", "Another useful point", "Third useful point"]),
        source_stats=SourceStats(endpoint_success_count=1, endpoint_failure_count=0, assignment_success_count=1, assignment_failure_count=0),
    )


def snapshot() -> NewsSnapshot:
    return NewsSnapshot(
        schema_version=1,
        generated_at=NOW,
        upstream_commit="d98aa603228f4839fb48859812c63a58ca10cead",
        source_stats=SourceStats(endpoint_success_count=1, endpoint_failure_count=0, assignment_success_count=1, assignment_failure_count=0),
        errors=[],
        tracks=[track(track_id) for track_id in CANONICAL_TRACK_IDS],
    )


def test_unavailable_track_has_no_generation_time_or_items() -> None:
    with pytest.raises(ValidationError):
        TrackSnapshot(
            track_id="ai",
            state="unavailable",
            generated_at=NOW,
            stale=False,
            partial=False,
            items=[],
            ai=TrackAi(),
            source_stats=SourceStats.empty(),
        )

    with pytest.raises(ValidationError):
        TrackSnapshot(
            track_id="ai",
            state="unavailable",
            generated_at=None,
            stale=False,
            partial=False,
            items=[item()],
            ai=TrackAi(),
            source_stats=SourceStats.empty(),
        )

    valid = TrackSnapshot(
        track_id="ai",
        state="unavailable",
        generated_at=None,
        stale=False,
        partial=False,
        items=[],
        ai=TrackAi(),
        source_stats=SourceStats.empty(),
    )
    assert valid.ai.available is False

    with pytest.raises(ValidationError):
        TrackSnapshot(
            track_id="ai",
            state="unavailable",
            generated_at=None,
            stale=False,
            partial=False,
            items=[],
            ai=TrackAi(available=True, generated_at=NOW, highlights=["One", "Two", "Three"]),
            source_stats=SourceStats.empty(),
        )


def test_track_state_must_match_generation_and_stale_flags() -> None:
    with pytest.raises(ValidationError):
        TrackSnapshot(
            track_id="ai",
            state="fresh",
            generated_at=NOW,
            stale=True,
            partial=False,
            items=[item()],
            ai=TrackAi(),
            source_stats=SourceStats.empty(),
        )

    with pytest.raises(ValidationError):
        TrackSnapshot(track_id="ai", state="stale", generated_at=None, stale=True, partial=False, items=[item()], ai=TrackAi(), source_stats=SourceStats.empty())


def test_partial_marks_only_fresh_tracks_with_failed_sources() -> None:
    fresh_data = track("ai").model_dump()
    fresh_data["partial"] = True
    fresh_data["source_stats"]["assignment_failure_count"] = 1
    fresh = TrackSnapshot.model_validate(fresh_data)
    assert fresh.partial is True

    stale = track("ai", state="stale").model_dump()
    stale["partial"] = True
    with pytest.raises(ValidationError):
        TrackSnapshot.model_validate(stale)

    unavailable = track("ai", state="unavailable").model_dump()
    unavailable["partial"] = True
    with pytest.raises(ValidationError):
        TrackSnapshot.model_validate(unavailable)


def test_source_stats_expose_endpoint_and_assignment_counts_with_bounds() -> None:
    stats = SourceStats(endpoint_success_count=1, endpoint_failure_count=1, assignment_success_count=2, assignment_failure_count=1)
    assert stats.assignment_failure_count == 1
    assert SourceStats.empty().model_dump() == {
        "endpoint_success_count": 0,
        "endpoint_failure_count": 0,
        "assignment_success_count": 0,
        "assignment_failure_count": 0,
    }

    with pytest.raises(ValidationError):
        SourceStats(endpoint_success_count=107, endpoint_failure_count=0, assignment_success_count=0, assignment_failure_count=0)

    with pytest.raises(ValidationError):
        SourceStats(endpoint_success_count=0, endpoint_failure_count=0, assignment_success_count=100, assignment_failure_count=9)


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


@pytest.mark.parametrize(
    "url",
    [
        "https://reader:password@example.com/feed",
        "https://example.com/feed?access_token=secret-value",
        "https://example.com/feed?client_secret=secret-value",
        "https://example.com/feed#authorization=Bearer%20secret-value",
        "https://example.com/feed?continue=sk-secret-value",
    ],
)
def test_feed_urls_reject_embedded_credentials_and_secret_parameters(url: str) -> None:
    with pytest.raises(ValidationError):
        FeedSource(id="source-1", name="Source", url=url)

    data = item().model_dump()
    data["url"] = url
    with pytest.raises(ValidationError):
        FeedItem.model_validate(data)


def test_feed_urls_allow_public_http_urls() -> None:
    assert FeedSource(id="source-1", name="Source", url="https://example.com/feed?topic=markets").url.startswith("https://")
    assert FeedSource(
        id="source-1", name="Source", url="https://example.com/feed?id=550e8400-e29b-41d4-a716-446655440000"
    ).url.startswith("https://")
    assert FeedSource(
        id="source-1", name="Source", url="https://example.com/feed?post_id=article-20260720-12345678901234567890"
    ).url.startswith("https://")
    data = item().model_dump()
    data["url"] = "http://example.com/article#section-2"
    assert FeedItem.model_validate(data).url.startswith("http://")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/feed?X-Amz-Signature=aws-signature",
        "https://example.com/feed?sig=opaque-signature",
        "https://example.com/feed?session=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature",
        "https://example.com/feed?session=ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "https://example.com/feed?session=github_pat_abcdefghijklmnopqrstuvwxyz1234567890",
        "https://example.com/feed?next=secret-token-value",
        "https://example.com/feed?next=secret_abcdefghijklmnop",
        "https://example.com/feed?next=AKIAIOSFODNN7EXAMPLE",
        "https://example.com/feed?next=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "https://example.com/feed?next=xoxb-123456789012-abcdefghijklmnopqrstuvwxyz",
        "https://example.com/feed?topic=markets;api_key=secret-value",
        "https://example.com/feed#section=latest;sig=opaque-signature",
    ],
)
def test_feed_urls_reject_signature_and_opaque_credential_values(url: str) -> None:
    with pytest.raises(ValidationError):
        FeedSource(id="source-1", name="Source", url=url)


@pytest.mark.parametrize(
    "message",
    [
        "Authorization: Bearer secret-value",
        "request failed for https://api.openai.com/v1/chat/completions",
        "provider=anthropic rejected credential",
        "upstream returned sk-secret-value",
        "Provider failed to respond",
        "API key was rejected",
        "OpenAIError: upstream failure",
        "OpenRouter authentication failure",
        "Xiaomi MIMO authentication failure",
        "Z.ai upstream error",
        "LiteLLM.AuthenticationError: failed",
        "AWS Bedrock error",
        "authentication failure",
        "upstream error",
        "X-Amz-Signature=aws-signature",
        "sig=opaque-signature",
    ],
)
def test_public_error_rejects_provider_and_credential_like_messages(message: str) -> None:
    with pytest.raises(ValidationError):
        PublicError(code="upstream_failed", message=message)


def test_public_error_requires_the_fixed_public_message_for_its_code() -> None:
    error = PublicError(code="upstream_failed", message="news refresh failed")

    assert error.message == "news refresh failed"

    with pytest.raises(ValidationError):
        PublicError(code="upstream_failed", message="a generic but unapproved error")


@pytest.mark.parametrize("highlight", ["", "x" * 301])
def test_track_ai_rejects_empty_or_oversized_highlights(highlight: str) -> None:
    with pytest.raises(ValidationError):
        TrackAi(available=True, generated_at=NOW, highlights=[highlight, "Second point", "Third point"])


def test_snapshot_response_and_refresh_acceptance_expose_only_public_state() -> None:
    status = RefreshStatus(state="idle")
    response = SnapshotResponse(available=True, stale=False, snapshot=snapshot(), refresh=status, error=None)
    accepted = RefreshAcceptedResponse(task_id=uuid4(), reused=False, status=status)

    assert response.available is True
    assert accepted.status.state == "idle"

    with pytest.raises(ValidationError):
        SnapshotResponse(available=False, stale=False, snapshot=snapshot(), refresh=status, error=None)
