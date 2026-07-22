from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.api.models import LLMUsageTotals
from src.session.models import Attempt, Message, Session
from src.session.store import SessionStore
from src.usage.llm_aggregation import LLMUsageAggregationService
from src.usage.models import LLMUsageAggregateResponse


def usage(
    provider: str,
    model: str | None,
    total: int,
    cache_read: int | None = None,
    cache_write: int | None = None,
) -> dict:
    totals = {
        "input_tokens": total - 2,
        "output_tokens": 2,
        "total_tokens": total,
        "calls": 1,
    }
    iteration = {
        "iter": 1,
        "input_tokens": total - 2,
        "output_tokens": 2,
        "total_tokens": total,
    }
    if cache_read is not None:
        totals["cache_read_tokens"] = cache_read
        iteration["cache_read_tokens"] = cache_read
    if cache_write is not None:
        totals["cache_write_tokens"] = cache_write
        iteration["cache_write_tokens"] = cache_write
    return {
        "provider": provider,
        "model": model,
        "metering_eligible": provider == "vip_server",
        "totals": totals,
        "per_iteration": [iteration],
    }


def _write_usage(runs_dir: Path, run_id: str, payload: dict) -> None:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "llm_usage.json").write_text(json.dumps(payload), encoding="utf-8")


def _create_session(
    store: SessionStore,
    session_id: str,
    *,
    title: str | None = None,
    updated_at: str = "2026-07-01T00:00:00Z",
) -> Session:
    session = Session(
        session_id=session_id,
        title=title or session_id,
        created_at=updated_at,
        updated_at=updated_at,
    )
    store.create_session(session)
    return session


def _add_attempt(
    store: SessionStore,
    session_id: str,
    run_dir: Path | str,
    created_at: str,
    *,
    attempt_id: str | None = None,
) -> None:
    store.create_attempt(
        Attempt(
            attempt_id=attempt_id or f"attempt-{Path(run_dir).name}",
            session_id=session_id,
            run_dir=str(run_dir),
            created_at=created_at,
        )
    )


def _add_run_message(
    store: SessionStore,
    session_id: str,
    run_id: str,
    created_at: str,
    *,
    message_id: str | None = None,
) -> None:
    store.append_message(
        Message(
            message_id=message_id or f"message-{len(run_id)}-{run_id[-8:]}",
            session_id=session_id,
            role="assistant",
            created_at=created_at,
            metadata={"run_id": run_id},
        )
    )


def test_public_aggregate_response_serializes_only_global_usage_contract() -> None:
    response = LLMUsageAggregateResponse.model_validate(
        {
            "generated_at": datetime(2026, 7, 3, tzinfo=timezone.utc),
            "timezone": "UTC",
            "period": {"start_at": None, "end_at": None},
            "totals": {
                "input_tokens": 8,
                "output_tokens": 2,
                "total_tokens": 10,
                "calls": 1,
                "runs": 1,
                "sessions": 1,
                "missing_usage_runs": 0,
                "invalid_usage_runs": 0,
            },
            "trend": [],
            "breakdown": [],
            "sessions": {
                "items": [],
                "page": 1,
                "page_size": 25,
                "total_items": 0,
                "total_pages": 0,
            },
        }
    )

    body = response.model_dump(mode="json")
    assert set(body) == {
        "generated_at",
        "timezone",
        "period",
        "totals",
        "trend",
        "breakdown",
        "sessions",
    }
    assert set(body["period"]) == {"start_at", "end_at"}
    assert "models" not in body


@pytest.mark.parametrize(
    "totals_path",
    [
        ("trend", 0, "totals"),
        ("breakdown", 0, "totals"),
        ("sessions", "items", 0, "totals"),
        ("sessions", "items", 0, "runs", 0, "totals"),
    ],
)
def test_public_aggregate_response_rejects_extra_fields_in_nested_totals(
    totals_path: tuple[str | int, ...],
) -> None:
    usage_totals = {
        "input_tokens": 8,
        "output_tokens": 2,
        "total_tokens": 10,
        "calls": 1,
    }
    payload = {
        "generated_at": datetime(2026, 7, 3, tzinfo=timezone.utc),
        "timezone": "UTC",
        "period": {"start_at": None, "end_at": None},
        "totals": {
            **usage_totals,
            "runs": 1,
            "sessions": 1,
            "missing_usage_runs": 0,
            "invalid_usage_runs": 0,
        },
        "trend": [{"date": datetime(2026, 7, 1).date(), "totals": dict(usage_totals)}],
        "breakdown": [
            {"provider": "openai", "model": "gpt-test", "totals": dict(usage_totals)}
        ],
        "sessions": {
            "items": [
                {
                    "session_id": "session-1",
                    "title": "Session 1",
                    "last_run_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                    "totals": dict(usage_totals),
                    "runs": [
                        {
                            "run_id": "run-1",
                            "occurred_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                            "provider": "openai",
                            "model": "gpt-test",
                            "metering_eligible": True,
                            "totals": dict(usage_totals),
                        }
                    ],
                }
            ],
            "page": 1,
            "page_size": 25,
            "total_items": 1,
            "total_pages": 1,
        },
    }
    nested = payload
    for key in totals_path:
        nested = nested[key]
    nested["unexpected"] = "must-be-rejected"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        LLMUsageAggregateResponse.model_validate(payload)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


def test_store_unbounded_readers_preserve_full_history(store: SessionStore) -> None:
    for index in range(51):
        _create_session(store, f"session-{index:02d}")

    for index in range(101):
        store.append_message(
            Message(
                message_id=f"message-{index:03d}",
                session_id="session-00",
                created_at=f"2026-07-01T00:{index // 60:02d}:{index % 60:02d}Z",
            )
        )

    _add_attempt(
        store,
        "session-00",
        "/runs/later",
        "2026-07-01T02:00:00Z",
        attempt_id="later",
    )
    _add_attempt(
        store,
        "session-00",
        "/runs/earlier",
        "2026-07-01T01:00:00Z",
        attempt_id="earlier",
    )

    assert len(store.list_sessions()) == 50
    assert len(store.list_all_sessions()) == 51
    assert len(store.get_messages("session-00")) == 100
    assert len(store.get_all_messages("session-00")) == 101
    assert [item.attempt_id for item in store.list_attempts("session-00")] == [
        "earlier",
        "later",
    ]


def test_aggregate_links_attempt_and_message_once_and_excludes_orphan(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _create_session(store, "session-a", title="Alpha")
    _create_session(store, "session-b", title="Beta")

    _write_usage(runs_dir, "run-a", usage("vip_server", "alpha", 10, cache_read=3))
    _write_usage(runs_dir, "run-b", usage("openai", None, 20))
    _write_usage(runs_dir, "orphan", usage("orphan-provider", "orphan-model", 999))
    _add_attempt(store, "session-a", runs_dir / "run-a", "2026-07-01T10:00:00Z")
    _add_run_message(store, "session-a", "run-a", "2026-07-01T10:01:00Z")
    _add_run_message(store, "session-b", "run-b", "2026-07-01T11:00:00Z")
    _add_attempt(store, "session-a", runs_dir / "run-c", "2026-07-01T12:00:00Z")
    _add_attempt(store, "session-b", runs_dir / "run-bad", "2026-07-01T13:00:00Z")
    invalid_payload = usage("openai", "bad", 12)
    invalid_payload["totals"]["total_tokens"] = "12"
    _write_usage(runs_dir, "run-bad", invalid_payload)

    result = LLMUsageAggregationService(store, runs_dir).aggregate(
        start_at=None,
        end_at=None,
        timezone_name="Asia/Shanghai",
        query=None,
        page=1,
        page_size=50,
    )

    assert result.totals.total_tokens == 30
    assert result.totals.runs == 2
    assert result.totals.sessions == 2
    assert result.totals.missing_usage_runs == 1
    assert result.totals.invalid_usage_runs == 1
    assert result.generated_at.tzinfo == timezone.utc
    assert result.totals.cache_read_tokens == 3
    assert result.totals.cache_read_reported_runs == 1
    nested_totals = [
        result.trend[0].totals,
        result.breakdown[0].totals,
        result.sessions.items[0].totals,
        result.sessions.items[0].runs[0].totals,
    ]
    assert all(type(totals) is not LLMUsageTotals for totals in nested_totals)
    run_ids = {run.run_id for row in result.sessions.items for run in row.runs}
    assert run_ids == {"run-a", "run-b"}
    assert "orphan" not in run_ids


def test_timezone_changes_daily_buckets_without_changing_totals(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _create_session(store, "attempt-session")
    _create_session(store, "message-session")
    _write_usage(runs_dir, "run-attempt", usage("openai", "gpt", 10))
    _write_usage(runs_dir, "run-message", usage("openai", "gpt", 20))
    _add_attempt(
        store,
        "attempt-session",
        runs_dir / "run-attempt",
        "2026-07-01T16:30:00Z",
    )
    _add_run_message(
        store,
        "message-session",
        "run-message",
        "2026-07-02T00:30:00Z",
    )
    service = LLMUsageAggregationService(store, runs_dir)

    shanghai = service.aggregate(timezone_name="Asia/Shanghai")
    utc = service.aggregate(timezone_name="UTC")

    assert [(bucket.date.isoformat(), bucket.totals.total_tokens) for bucket in shanghai.trend] == [
        ("2026-07-02", 30)
    ]
    assert [(bucket.date.isoformat(), bucket.totals.total_tokens) for bucket in utc.trend] == [
        ("2026-07-01", 10),
        ("2026-07-02", 20),
    ]
    assert shanghai.totals == utc.totals


def test_time_range_is_start_inclusive_and_end_exclusive_before_coverage_counts(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _create_session(store, "session-range")
    _add_attempt(store, "session-range", runs_dir / "before", "2026-07-01T16:30:00Z")
    _add_run_message(store, "session-range", "included", "2026-07-02T00:30:00Z")
    _add_attempt(store, "session-range", runs_dir / "at-end", "2026-07-03T00:00:00Z")
    _write_usage(runs_dir, "included", usage("openai", "gpt", 20))
    _write_usage(runs_dir, "at-end", usage("openai", "gpt", 40))

    result = LLMUsageAggregationService(store, runs_dir).aggregate(
        start_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        end_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        timezone_name="UTC",
    )

    assert result.totals.total_tokens == 20
    assert result.totals.runs == 1
    assert result.totals.missing_usage_runs == 0
    assert {run.run_id for row in result.sessions.items for run in row.runs} == {"included"}


def test_model_breakdown_merges_pairs_and_sorts_by_tokens_then_names(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _create_session(store, "session-models")
    rows = [
        ("a-1", "zeta", "same", 10),
        ("a-2", "zeta", "same", 20),
        ("b", "beta", "model", 30),
        ("c", "alpha", None, 5),
    ]
    for index, (run_id, provider, model, total) in enumerate(rows):
        _write_usage(runs_dir, run_id, usage(provider, model, total))
        _add_attempt(
            store,
            "session-models",
            runs_dir / run_id,
            f"2026-07-01T0{index}:00:00Z",
        )

    result = LLMUsageAggregationService(store, runs_dir).aggregate(timezone_name="UTC")

    assert [
        (bucket.provider, bucket.model, bucket.totals.total_tokens)
        for bucket in result.models
    ] == [
        ("beta", "model", 30),
        ("zeta", "same", 30),
        ("alpha", None, 5),
    ]


def test_attempt_owner_wins_over_message_fallback_in_another_session(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _create_session(store, "message-owner", title="Message owner")
    _create_session(store, "attempt-owner", title="Attempt owner")
    _write_usage(runs_dir, "shared", usage("openai", "gpt", 15))
    _add_run_message(store, "message-owner", "shared", "2026-07-02T00:00:00Z")
    _add_attempt(store, "attempt-owner", runs_dir / "shared", "2026-07-01T00:00:00Z")

    result = LLMUsageAggregationService(store, runs_dir).aggregate(timezone_name="UTC")

    assert result.totals.runs == 1
    assert [(row.session_id, [run.run_id for run in row.runs]) for row in result.sessions.items] == [
        ("attempt-owner", ["shared"])
    ]


def test_unsafe_refs_and_persisted_attempt_paths_cannot_escape_runs_dir(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    sentinel_dir = tmp_path / "outside" / "sentinel"
    sentinel_dir.mkdir(parents=True)
    (sentinel_dir / "llm_usage.json").write_text(
        json.dumps(usage("sentinel-provider", "sentinel-model", 999)),
        encoding="utf-8",
    )
    _create_session(store, "session-safe")
    for index, run_id in enumerate(
        ["/absolute/path", "../escape", "x" * 129, "illegal.run"]
    ):
        _add_run_message(
            store,
            "session-safe",
            run_id,
            f"2026-07-01T0{index}:00:00Z",
            message_id=f"unsafe-{index}",
        )
    _add_attempt(
        store,
        "session-safe",
        sentinel_dir,
        "2026-07-01T05:00:00Z",
        attempt_id="outside-attempt",
    )

    result = LLMUsageAggregationService(store, runs_dir).aggregate(timezone_name="UTC")

    assert result.totals.total_tokens == 0
    assert result.totals.runs == 0
    assert result.totals.missing_usage_runs == 1
    assert result.models == []


def test_artifact_symlink_cannot_escape_runs_dir(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    outside_artifact = tmp_path / "outside-usage.json"
    outside_artifact.write_text(
        json.dumps(usage("outside-provider", "outside-model", 999)),
        encoding="utf-8",
    )
    run_dir = runs_dir / "symlinked-artifact"
    run_dir.mkdir(parents=True)
    (run_dir / "llm_usage.json").symlink_to(outside_artifact)
    _create_session(store, "session-symlink")
    _add_attempt(store, "session-symlink", run_dir, "2026-07-01T00:00:00Z")
    _write_usage(runs_dir, "valid-sibling", usage("valid-provider", "valid-model", 10))
    _add_attempt(
        store,
        "session-symlink",
        runs_dir / "valid-sibling",
        "2026-07-01T01:00:00Z",
    )

    result = LLMUsageAggregationService(store, runs_dir).aggregate(timezone_name="UTC")

    assert result.totals.total_tokens == 10
    assert result.totals.runs == 1
    assert result.totals.invalid_usage_runs == 1
    assert [
        (bucket.provider, bucket.model, bucket.totals.total_tokens)
        for bucket in result.models
    ] == [("valid-provider", "valid-model", 10)]


def test_query_only_filters_session_page_and_does_not_recompute_aggregates(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _create_session(store, "first-id", title="Visible title")
    _create_session(store, "second-match", title="Other")
    for index, (session_id, run_id, total) in enumerate(
        [("first-id", "first", 10), ("second-match", "second", 20)]
    ):
        _write_usage(runs_dir, run_id, usage("openai", "gpt", total))
        _add_attempt(
            store,
            session_id,
            runs_dir / run_id,
            f"2026-07-01T0{index}:00:00Z",
        )

    result = LLMUsageAggregationService(store, runs_dir).aggregate(
        timezone_name="UTC",
        query="VISIBLE",
        page=1,
        page_size=1,
    )

    assert result.totals.total_tokens == 30
    assert [row.session_id for row in result.sessions.items] == ["first-id"]
    assert result.sessions.total_items == 1
    assert result.sessions.total_pages == 1


def test_invalid_attempt_time_falls_back_to_message_time_then_run_mtime(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _create_session(store, "session-fallback")
    _write_usage(runs_dir, "message-time", usage("openai", "gpt", 10))
    _write_usage(runs_dir, "mtime", usage("openai", "gpt", 20))
    _add_attempt(store, "session-fallback", runs_dir / "message-time", "invalid")
    _add_run_message(store, "session-fallback", "message-time", "2026-07-02T00:30:00Z")
    _add_attempt(store, "session-fallback", runs_dir / "mtime", "invalid")
    fallback_timestamp = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc).timestamp()
    os.utime(runs_dir / "mtime", (fallback_timestamp, fallback_timestamp))

    result = LLMUsageAggregationService(store, runs_dir).aggregate(timezone_name="UTC")

    assert [(bucket.date.isoformat(), bucket.totals.total_tokens) for bucket in result.trend] == [
        ("2026-07-02", 10),
        ("2026-07-03", 20),
    ]


def test_non_utf8_usage_file_is_invalid_without_stopping_other_runs(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _create_session(store, "session-corrupt")
    corrupt_dir = runs_dir / "corrupt"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "llm_usage.json").write_bytes(b"\xff\xfe")
    _add_attempt(store, "session-corrupt", corrupt_dir, "2026-07-01T00:00:00Z")
    _write_usage(runs_dir, "valid", usage("openai", "gpt", 10))
    _add_attempt(store, "session-corrupt", runs_dir / "valid", "2026-07-01T01:00:00Z")

    result = LLMUsageAggregationService(store, runs_dir).aggregate(timezone_name="UTC")

    assert result.totals.invalid_usage_runs == 1
    assert result.totals.runs == 1
    assert result.totals.total_tokens == 10


def test_zero_page_size_is_rejected_before_pagination_math(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        LLMUsageAggregationService(store, tmp_path / "runs").aggregate(page_size=0)


def test_global_cache_tokens_omit_unreported_fields_but_preserve_reported_zero(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    _create_session(store, "session-cache")
    _write_usage(runs_dir, "without-cache", usage("openai", "gpt", 10))
    _write_usage(
        runs_dir,
        "write-zero",
        usage("openai", "gpt", 20, cache_write=0),
    )
    _add_attempt(store, "session-cache", runs_dir / "without-cache", "2026-07-01T00:00:00Z")
    _add_attempt(store, "session-cache", runs_dir / "write-zero", "2026-07-01T01:00:00Z")

    result = LLMUsageAggregationService(store, runs_dir).aggregate(timezone_name="UTC")
    serialized_totals = result.model_dump()["totals"]

    assert "cache_read_tokens" not in serialized_totals
    assert serialized_totals["cache_read_reported_runs"] == 0
    assert serialized_totals["cache_write_tokens"] == 0
    assert serialized_totals["cache_write_reported_runs"] == 1
