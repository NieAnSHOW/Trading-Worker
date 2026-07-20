from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from src.news.catalog import NewsCatalog, SourceAssignment
from src.news.feeds import RawFeedItem
from src.news.models import CANONICAL_TRACK_IDS, FeedItem, FeedSource, NewsSnapshot, SourceStats, TrackAi, TrackSnapshot
from src.news.pipeline import AssignmentItems, build_track_candidates


NOW = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
OLD_TIME = NOW - timedelta(days=2)


def catalog() -> NewsCatalog:
    assignments = (
        assignment("ai-a", "ai", "https://feeds.example/ai-a"),
        assignment("ai-b", "ai", "https://feeds.example/ai-b"),
        assignment("semi-a", "semi", "https://feeds.example/semi-a"),
    )
    return NewsCatalog(
        schema_version=1,
        repository="https://example.invalid/news",
        commit="a" * 40,
        tracks=tuple(MappingProxyType({"id": track_id, "name": track_id}) for track_id in CANONICAL_TRACK_IDS),
        assignments=assignments,
        filters=("crypto", "gambling"),
    )


def assignment(identifier: str, track_id: str, url: str) -> SourceAssignment:
    return SourceAssignment(id=identifier, track_id=track_id, name=f"Source {identifier}", url=url, filters=())


def raw(
    source: SourceAssignment,
    title: str = "article",
    *,
    published_at: datetime | None = NOW,
    url: str = "https://news.example/article",
    summary: str | None = "summary",
) -> RawFeedItem:
    return RawFeedItem(title=title, summary=summary, published_at=published_at, url=url, source=source)


def current_items_for(track_id: str) -> list[AssignmentItems]:
    source = next(item for item in catalog().assignments if item.track_id == track_id)
    return [AssignmentItems(source, (raw(source),))]


def make_track(track_id: str, *, state: str, generated_at: datetime | None, title: str = "old") -> TrackSnapshot:
    if state == "unavailable":
        return TrackSnapshot(
            track_id=track_id,
            state="unavailable",
            generated_at=None,
            stale=False,
            partial=False,
            items=[],
            ai=TrackAi(),
            source_stats=SourceStats.empty(),
        )
    item = FeedItem(
        id=f"old-{track_id}",
        track_id=track_id,
        title=title,
        source=FeedSource(id="old-source", name="Old Source", url="https://old.example/feed"),
        published_at=OLD_TIME,
        url=f"https://old.example/{track_id}",
    )
    return TrackSnapshot(
        track_id=track_id,
        state=state,
        generated_at=generated_at,
        stale=state == "stale",
        partial=False,
        items=[item],
        ai=TrackAi(),
        source_stats=SourceStats.empty(),
    )


def make_snapshot_with_tracks(overrides: list[TrackSnapshot]) -> NewsSnapshot:
    by_id = {track.track_id: track for track in overrides}
    return NewsSnapshot(
        schema_version=1,
        generated_at=OLD_TIME,
        upstream_commit="a" * 40,
        source_stats=SourceStats.empty(),
        tracks=[by_id.get(track_id, make_track(track_id, state="unavailable", generated_at=None)) for track_id in CANONICAL_TRACK_IDS],
    )


def test_track_without_current_items_reuses_old_track_as_stale() -> None:
    old = make_track("semi", state="fresh", generated_at=OLD_TIME, title="old")

    result = build_track_candidates(catalog(), [], make_snapshot_with_tracks([old]), NOW)

    semi = next(item for item in result.tracks if item.track_id == "semi")
    assert semi.state == "stale"
    assert semi.generated_at == OLD_TIME
    assert semi.items[0].title == "old"


def test_first_run_missing_track_is_unavailable() -> None:
    result = build_track_candidates(catalog(), current_items_for("ai"), None, NOW)

    science = next(item for item in result.tracks if item.track_id == "science")
    assert science.state == "unavailable"
    assert science.items == []


def test_pipeline_filters_old_items_and_keeps_undated_items_after_dated_items() -> None:
    source = catalog().assignments[0]
    result = build_track_candidates(
        catalog(),
        [
            AssignmentItems(
                source,
                (
                    raw(source, "old", published_at=NOW - timedelta(days=8), url="https://news.example/old"),
                    raw(source, "undated", published_at=None, url="https://news.example/undated"),
                    raw(source, "new", url="https://news.example/new"),
                ),
            )
        ],
        None,
        NOW,
    )

    ai = result.tracks[0]
    assert [item.title for item in ai.items] == ["new", "undated"]


def test_pipeline_caps_each_endpoint_at_six_items() -> None:
    source = catalog().assignments[0]
    items = tuple(raw(source, f"item-{index}", url=f"https://news.example/{index}", published_at=NOW - timedelta(minutes=index)) for index in range(7))

    result = build_track_candidates(catalog(), [AssignmentItems(source, items)], None, NOW)

    assert [item.title for item in result.tracks[0].items] == [f"item-{index}" for index in range(6)]


def test_pipeline_caps_track_at_one_hundred_items() -> None:
    assignments = tuple(assignment(f"ai-{index:03d}", "ai", f"https://feeds.example/{index}") for index in range(101))
    custom_catalog = NewsCatalog(
        schema_version=1,
        repository="https://example.invalid/news",
        commit="a" * 40,
        tracks=catalog().tracks,
        assignments=assignments,
        filters=(),
    )
    current = [AssignmentItems(source, (raw(source, f"item-{index}", url=f"https://news.example/{index}"),)) for index, source in enumerate(assignments)]

    result = build_track_candidates(custom_catalog, current, None, NOW)

    assert len(result.tracks[0].items) == 100
    assert result.tracks[0].items[-1].title == "item-99"


def test_pipeline_filters_catalog_and_assignment_terms() -> None:
    source = SourceAssignment(id="ai-filtered", track_id="ai", name="Filtered", url="https://feeds.example/filtered", filters=("private",))
    custom_catalog = NewsCatalog(
        schema_version=1,
        repository="https://example.invalid/news",
        commit="a" * 40,
        tracks=catalog().tracks,
        assignments=(source,),
        filters=("crypto",),
    )

    result = build_track_candidates(
        custom_catalog,
        [AssignmentItems(source, (raw(source, "crypto market"), raw(source, "private market"), raw(source, "permitted")))],
        None,
        NOW,
    )

    assert [item.title for item in result.tracks[0].items] == ["permitted"]


def test_pipeline_canonicalizes_url_and_deduplicates_across_assignments() -> None:
    first, second = catalog().assignments[:2]
    result = build_track_candidates(
        catalog(),
        [
            AssignmentItems(first, (raw(first, "from-b", url="https://NEWS.example:443/article#read"),)),
            AssignmentItems(second, (raw(second, "from-a", url="https://news.example/article"),)),
        ],
        None,
        NOW,
    )

    ai = result.tracks[0]
    assert [(item.title, item.url, item.source.id) for item in ai.items] == [("from-b", "https://news.example/article", "ai-a")]


def test_pipeline_merges_current_and_old_items_then_marks_partial() -> None:
    source = catalog().assignments[0]
    old = make_track("ai", state="fresh", generated_at=OLD_TIME)
    result = build_track_candidates(
        catalog(),
        [AssignmentItems(source, (raw(source, "current", url="https://news.example/current"),), assignment_failed=True)],
        make_snapshot_with_tracks([old]),
        NOW,
    )

    ai = result.tracks[0]
    assert ai.state == "fresh"
    assert ai.partial is True
    assert [item.title for item in ai.items] == ["current", "old"]
    assert "ai" in result.updated_track_ids


def test_pipeline_returns_all_unavailable_tracks_when_no_current_or_previous_items() -> None:
    result = build_track_candidates(catalog(), [], make_snapshot_with_tracks([]), NOW)

    assert len(result.tracks) == len(CANONICAL_TRACK_IDS) == 12
    assert [track.track_id for track in result.tracks] == list(CANONICAL_TRACK_IDS)
    assert all(track.state == "unavailable" and track.items == [] for track in result.tracks)
    assert result.updated_track_ids == frozenset()


def test_pipeline_output_is_stable_with_tie_breakers() -> None:
    first, second = catalog().assignments[:2]
    current = [
        AssignmentItems(second, (raw(second, "second", url="https://news.example/second", summary=None),)),
        AssignmentItems(first, (raw(first, "first", url="https://news.example/first", summary=None),)),
    ]

    first_result = build_track_candidates(catalog(), current, None, NOW)
    second_result = build_track_candidates(catalog(), list(reversed(current)), None, NOW)

    assert first_result == second_result
    assert [item.source.id for item in first_result.tracks[0].items] == ["ai-a", "ai-b"]
