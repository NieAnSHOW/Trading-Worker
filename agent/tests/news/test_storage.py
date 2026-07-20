import json
import stat
from datetime import datetime, timezone

import pytest

from src.news.models import CANONICAL_TRACK_IDS, FeedItem, FeedSource, NewsSnapshot, TrackAi, TrackSnapshot
from src.news.storage import AtomicSnapshotStore, SnapshotStorageError


NOW = datetime(2026, 7, 20, 8, 30, tzinfo=timezone.utc)


def make_snapshot() -> NewsSnapshot:
    tracks = []
    for track_id in CANONICAL_TRACK_IDS:
        entry = FeedItem(
            id=f"{track_id}-1",
            track_id=track_id,
            title="A valid title",
            summary="A short summary.",
            source=FeedSource(id="source-1", name="Source", url="https://example.com/feed"),
            published_at=NOW,
            url="https://example.com/article",
        )
        tracks.append(
            TrackSnapshot(
                track_id=track_id,
                state="fresh",
                generated_at=NOW,
                stale=False,
                items=[entry],
                ai=TrackAi(available=True, generated_at=NOW, highlights=["One", "Two", "Three"]),
            )
        )
    return NewsSnapshot(
        schema_version=1,
        generated_at=NOW,
        upstream_commit="d98aa603228f4839fb48859812c63a58ca10cead",
        source_success_count=1,
        source_failure_count=0,
        errors=[],
        tracks=tracks,
    )


def test_write_then_read_uses_private_file_and_directory_permissions(tmp_path) -> None:
    store = AtomicSnapshotStore(tmp_path / "news" / "latest.json")
    expected = make_snapshot()

    store.write(expected)

    assert store.read() == expected
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_default_path_is_under_user_data_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("src.news.storage.get_data_dir", lambda: tmp_path / "runtime")

    assert AtomicSnapshotStore().path == tmp_path / "runtime" / "news" / "latest.json"


def test_corrupt_or_secret_bearing_snapshot_is_rejected_without_details(tmp_path) -> None:
    store = AtomicSnapshotStore(tmp_path / "news" / "latest.json")
    store.path.parent.mkdir(mode=0o700)
    store.path.write_text('{"api_key":"secret"}', encoding="utf-8")

    with pytest.raises(SnapshotStorageError, match="unavailable") as exc:
        store.read()

    assert exc.value.code == "snapshot_unavailable"
    assert "api_key" not in str(exc.value)


def test_oversized_snapshot_is_rejected_safely(tmp_path) -> None:
    store = AtomicSnapshotStore(tmp_path / "news" / "latest.json")
    store.path.parent.mkdir(mode=0o700)
    store.path.write_bytes(b"{" + b" " * (store.max_bytes + 1) + b"}")

    with pytest.raises(SnapshotStorageError, match="unavailable"):
        store.read()


def test_replace_failure_preserves_old_snapshot_and_cleans_temp(monkeypatch, tmp_path) -> None:
    store = AtomicSnapshotStore(tmp_path / "news" / "latest.json")
    old = make_snapshot()
    store.write(old)
    old_contents = store.path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("src.news.storage.os.replace", fail_replace)
    with pytest.raises(SnapshotStorageError, match="could not be saved") as exc:
        store.write(make_snapshot())

    assert exc.value.code == "snapshot_write_failed"
    assert store.path.read_bytes() == old_contents
    assert list(store.path.parent.glob(".latest.json.*")) == []


def test_write_revalidates_serialized_snapshot_before_replace(monkeypatch, tmp_path) -> None:
    store = AtomicSnapshotStore(tmp_path / "news" / "latest.json")
    store.write(make_snapshot())
    old_contents = store.path.read_bytes()

    real_loads = json.loads
    monkeypatch.setattr("src.news.storage.json.loads", lambda value: {"api_key": "secret"})
    with pytest.raises(SnapshotStorageError, match="could not be saved"):
        store.write(make_snapshot())
    monkeypatch.setattr("src.news.storage.json.loads", real_loads)

    assert store.path.read_bytes() == old_contents
