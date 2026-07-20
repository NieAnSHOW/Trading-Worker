from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from src.news.catalog import NewsCatalog, SourceAssignment
from src.news.coordinator import NewsRefreshCoordinator
from src.news.models import CANONICAL_TRACK_IDS
from src.news.network import PublicFeedClient
from src.news.storage import AtomicSnapshotStore


FIRST_RUN = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
SECOND_RUN = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
THIRD_RUN = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


class FakeResolver:
    async def resolve(self, host: str) -> tuple[str, ...]:
        assert host == "feeds.example"
        return ("93.184.216.34",)


class FakeLLM:
    async def ainvoke(self, messages: list[dict[str, object]]) -> object:
        raise RuntimeError("fixture LLM unavailable")


def _catalog() -> NewsCatalog:
    assignments = tuple(
        SourceAssignment(
            id=f"source-{track_id}",
            track_id=track_id,
            name=f"Source {track_id}",
            url=f"https://feeds.example/{track_id}.xml",
            filters=(),
        )
        for track_id in CANONICAL_TRACK_IDS
    )
    return NewsCatalog(
        schema_version=1,
        repository="https://github.com/simonlin1212/investment-news",
        commit="d98aa603228f4839fb48859812c63a58ca10cead",
        tracks=tuple({"id": track_id, "name": track_id} for track_id in CANONICAL_TRACK_IDS),
        assignments=assignments,
        filters=(),
    )


def _rss(track_id: str, run: int) -> bytes:
    title = f"{track_id} fixture headline {run}"
    link = f"https://news.example/{track_id}/{run}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<rss version=\"2.0\"><channel><title>Fixture feed</title>"
        f"<item><title>{title}</title><link>{link}</link>"
        "<description>Fixture summary only.</description>"
        "<pubDate>Mon, 20 Jul 2026 08:00:00 GMT</pubDate>"
        "</item></channel></rss>"
    ).encode("utf-8")


def test_refresh_degrades_without_network_or_llm_and_preserves_last_good_snapshot(tmp_path: Path) -> None:
    async def run_case() -> None:
        phase = {"run": 1, "now": FIRST_RUN}

        def handler(request: httpx.Request) -> httpx.Response:
            track_id = Path(urlsplit(str(request.url)).path).stem
            unavailable = {
                1: {"semi", "science"},
                2: {"ai"},
                3: set(CANONICAL_TRACK_IDS),
            }[phase["run"]]
            if track_id in unavailable:
                return httpx.Response(503, request=request)
            return httpx.Response(200, content=_rss(track_id, phase["run"]), request=request)

        transport = httpx.MockTransport(handler)
        store = AtomicSnapshotStore(tmp_path / "news" / "latest.json")
        coordinator = NewsRefreshCoordinator(
            catalog=_catalog(),
            client_factory=lambda: PublicFeedClient(FakeResolver(), transport=transport),
            llm_factory=FakeLLM,
            store=store,
            now=lambda: phase["now"],
        )

        await coordinator.start()
        await coordinator.wait_current()
        first = await coordinator.snapshot_response()
        assert first.refresh.state == "succeeded"
        assert first.snapshot is not None
        first_tracks = {track.track_id: track for track in first.snapshot.tracks}
        assert first_tracks["semi"].state == "unavailable"
        assert first_tracks["science"].state == "unavailable"
        assert first_tracks["ai"].state == "fresh"
        assert all(
            track.ai.error is not None and track.ai.error.code == "ai_unavailable"
            for track in first.snapshot.tracks
            if track.state == "fresh"
        )

        phase.update(run=2, now=SECOND_RUN)
        await coordinator.start()
        await coordinator.wait_current()
        second = await coordinator.snapshot_response()
        assert second.refresh.state == "succeeded"
        assert second.snapshot is not None
        second_tracks = {track.track_id: track for track in second.snapshot.tracks}
        assert second_tracks["ai"].state == "stale"
        assert second_tracks["ai"].items == first_tracks["ai"].items
        assert second_tracks["semi"].state == "fresh"
        assert second_tracks["semi"].ai.error is not None
        assert second_tracks["semi"].ai.error.code == "ai_unavailable"

        snapshot_path = store.path
        before = snapshot_path.read_bytes()
        before_stat = snapshot_path.stat()
        phase.update(run=3, now=THIRD_RUN)
        await coordinator.start()
        await coordinator.wait_current()
        failed = await coordinator.snapshot_response()
        assert failed.refresh.state == "failed"
        assert failed.refresh.error is not None
        assert failed.refresh.error.code == "no_track_updated"
        assert failed.stale is True
        assert snapshot_path.read_bytes() == before
        assert snapshot_path.stat().st_mtime_ns == before_stat.st_mtime_ns
        await coordinator.close()

        rebuilt = NewsRefreshCoordinator(catalog=_catalog(), store=AtomicSnapshotStore(snapshot_path))
        rebuilt_response = await rebuilt.snapshot_response()
        assert rebuilt_response.refresh.state == "idle"
        assert rebuilt_response.snapshot == second.snapshot
        await rebuilt.close()

        persisted = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert len(persisted["tracks"]) == len(CANONICAL_TRACK_IDS)

    asyncio.run(run_case())
