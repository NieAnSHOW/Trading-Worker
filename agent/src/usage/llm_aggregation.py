"""Aggregate strict per-run LLM usage artifacts across persisted sessions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from pydantic import ConfigDict, PositiveInt, TypeAdapter, ValidationError

from src.api.models import LLMUsageSummary, LLMUsageTotals
from src.session.models import Attempt, Message, Session
from src.session.store import SessionStore
from src.usage.models import (
    DailyBucket,
    LLMUsageAggregateTotals,
    LLMUsageAggregateResponse,
    LLMUsagePublicTotals,
    ModelBucket,
    Period,
    RunRow,
    SessionPage,
    SessionRow,
)


_POSITIVE_INT = TypeAdapter(PositiveInt, config=ConfigDict(strict=True))


def _public_usage_totals(totals: LLMUsageTotals) -> LLMUsagePublicTotals:
    return LLMUsagePublicTotals(
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        total_tokens=totals.total_tokens,
        calls=totals.calls,
        cache_read_tokens=totals.cache_read_tokens,
        cache_write_tokens=totals.cache_write_tokens,
    )


def _safe_run_id(value: str) -> str | None:
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None


def _attempt_run_id(attempt: Attempt) -> str | None:
    return _safe_run_id(Path(attempt.run_dir).name) if attempt.run_dir else None


def _message_run_refs(messages: Sequence[Message]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for message in messages:
        raw = (
            message.metadata.get("run_id")
            if message.role == "assistant" and isinstance(message.metadata, dict)
            else None
        )
        safe = _safe_run_id(raw) if isinstance(raw, str) else None
        if safe is not None:
            refs.setdefault(safe, message.created_at)
    return refs


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normalize_boundary(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("usage aggregation boundaries must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class _RunRef:
    session: Session
    run_id: str
    attempt_created_at: str | None
    message_created_at: str | None


@dataclass
class _TotalsAccumulator:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_reported_runs: int = 0
    cache_write_reported_runs: int = 0

    def add(self, totals: LLMUsageTotals) -> None:
        self.input_tokens += totals.input_tokens
        self.output_tokens += totals.output_tokens
        self.total_tokens += totals.total_tokens
        self.calls += totals.calls
        if totals.cache_read_tokens is not None:
            self.cache_read_tokens += totals.cache_read_tokens
            self.cache_read_reported_runs += 1
        if totals.cache_write_tokens is not None:
            self.cache_write_tokens += totals.cache_write_tokens
            self.cache_write_reported_runs += 1

    def public_totals(self) -> LLMUsagePublicTotals:
        return LLMUsagePublicTotals(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            calls=self.calls,
            cache_read_tokens=(
                self.cache_read_tokens if self.cache_read_reported_runs else None
            ),
            cache_write_tokens=(
                self.cache_write_tokens if self.cache_write_reported_runs else None
            ),
        )


class LLMUsageAggregationService:
    """Build an on-demand global usage view from session-linked run artifacts."""

    def __init__(self, store: SessionStore, runs_dir: Path) -> None:
        self.store = store
        self.runs_dir = Path(runs_dir)

    def aggregate(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        timezone_name: str = "UTC",
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> LLMUsageAggregateResponse:
        page = _POSITIVE_INT.validate_python(page)
        page_size = _POSITIVE_INT.validate_python(page_size)
        local_timezone = ZoneInfo(timezone_name)
        normalized_start = _normalize_boundary(start_at)
        normalized_end = _normalize_boundary(end_at)
        sessions = self.store.list_all_sessions()
        refs = self._collect_run_refs(sessions)

        aggregate = _TotalsAccumulator()
        missing_usage_runs = 0
        invalid_usage_runs = 0
        session_totals: dict[str, _TotalsAccumulator] = {}
        session_runs: dict[str, list[RunRow]] = {}
        daily_totals: dict[date, _TotalsAccumulator] = {}
        model_totals: dict[tuple[str, str | None], _TotalsAccumulator] = {}

        for ref in refs:
            run_dir = self._run_dir(ref.run_id)
            if run_dir is None:
                continue
            occurred_at = self._occurred_at(ref, run_dir)
            if occurred_at is None:
                continue
            if normalized_start is not None and occurred_at < normalized_start:
                continue
            if normalized_end is not None and occurred_at >= normalized_end:
                continue

            artifact = self._usage_artifact(run_dir)
            if artifact is None:
                invalid_usage_runs += 1
                continue
            try:
                summary = LLMUsageSummary.model_validate_json(
                    artifact.read_text(encoding="utf-8")
                )
            except FileNotFoundError:
                missing_usage_runs += 1
                continue
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
                invalid_usage_runs += 1
                continue

            aggregate.add(summary.totals)
            session_accumulator = session_totals.setdefault(
                ref.session.session_id, _TotalsAccumulator()
            )
            session_accumulator.add(summary.totals)
            session_runs.setdefault(ref.session.session_id, []).append(
                RunRow(
                    run_id=ref.run_id,
                    occurred_at=occurred_at,
                    provider=summary.provider,
                    model=summary.model,
                    metering_eligible=summary.metering_eligible,
                    totals=_public_usage_totals(summary.totals),
                )
            )
            local_date = occurred_at.astimezone(local_timezone).date()
            daily_totals.setdefault(local_date, _TotalsAccumulator()).add(summary.totals)
            model_totals.setdefault(
                (summary.provider, summary.model), _TotalsAccumulator()
            ).add(summary.totals)

        rows = self._session_rows(sessions, session_totals, session_runs)
        filtered_rows = self._filter_rows(rows, query)
        total_items = len(filtered_rows)
        total_pages = (total_items + page_size - 1) // page_size
        offset = (page - 1) * page_size

        trend = [
            DailyBucket(date=day, totals=totals.public_totals())
            for day, totals in sorted(daily_totals.items())
        ]
        models = [
            ModelBucket(provider=provider, model=model, totals=totals.public_totals())
            for (provider, model), totals in model_totals.items()
        ]
        models.sort(key=lambda item: (item.provider, item.model or ""))
        models.sort(key=lambda item: item.totals.total_tokens, reverse=True)

        return LLMUsageAggregateResponse(
            generated_at=datetime.now(timezone.utc),
            timezone=timezone_name,
            period=Period(
                start_at=normalized_start,
                end_at=normalized_end,
            ),
            totals=LLMUsageAggregateTotals(
                input_tokens=aggregate.input_tokens,
                output_tokens=aggregate.output_tokens,
                total_tokens=aggregate.total_tokens,
                calls=aggregate.calls,
                runs=sum(len(items) for items in session_runs.values()),
                sessions=len(session_runs),
                missing_usage_runs=missing_usage_runs,
                invalid_usage_runs=invalid_usage_runs,
                cache_read_tokens=(
                    aggregate.cache_read_tokens
                    if aggregate.cache_read_reported_runs
                    else None
                ),
                cache_write_tokens=(
                    aggregate.cache_write_tokens
                    if aggregate.cache_write_reported_runs
                    else None
                ),
                cache_read_reported_runs=aggregate.cache_read_reported_runs,
                cache_write_reported_runs=aggregate.cache_write_reported_runs,
            ),
            trend=trend,
            breakdown=models,
            sessions=SessionPage(
                items=filtered_rows[offset : offset + page_size],
                page=page,
                page_size=page_size,
                total_items=total_items,
                total_pages=total_pages,
            ),
        )

    def _collect_run_refs(self, sessions: Sequence[Session]) -> list[_RunRef]:
        histories = [
            (
                session,
                self.store.list_attempts(session.session_id),
                _message_run_refs(self.store.get_all_messages(session.session_id)),
            )
            for session in sessions
        ]
        refs: list[_RunRef] = []
        claimed_run_ids: set[str] = set()

        for session, attempts, message_refs in histories:
            for attempt in attempts:
                run_id = _attempt_run_id(attempt)
                if run_id is None or run_id in claimed_run_ids:
                    continue
                claimed_run_ids.add(run_id)
                refs.append(
                    _RunRef(
                        session=session,
                        run_id=run_id,
                        attempt_created_at=attempt.created_at,
                        message_created_at=message_refs.get(run_id),
                    )
                )

        for session, _, message_refs in histories:
            for run_id, created_at in message_refs.items():
                if run_id in claimed_run_ids:
                    continue
                claimed_run_ids.add(run_id)
                refs.append(
                    _RunRef(
                        session=session,
                        run_id=run_id,
                        attempt_created_at=None,
                        message_created_at=created_at,
                    )
                )
        return refs

    def _run_dir(self, run_id: str) -> Path | None:
        candidate = self.runs_dir / run_id
        try:
            candidate.resolve(strict=False).relative_to(self.runs_dir.resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return None
        return candidate

    def _usage_artifact(self, run_dir: Path) -> Path | None:
        artifact = run_dir / "llm_usage.json"
        try:
            if artifact.is_symlink():
                return None
            resolved_artifact = artifact.resolve(strict=False)
            resolved_artifact.relative_to(self.runs_dir.resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved_artifact

    @staticmethod
    def _occurred_at(ref: _RunRef, run_dir: Path) -> datetime | None:
        occurred_at = _parse_utc(ref.attempt_created_at) or _parse_utc(
            ref.message_created_at
        )
        if occurred_at is not None:
            return occurred_at
        try:
            return datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    @staticmethod
    def _session_rows(
        sessions: Sequence[Session],
        totals: dict[str, _TotalsAccumulator],
        runs: dict[str, list[RunRow]],
    ) -> list[SessionRow]:
        rows: list[SessionRow] = []
        for session in sessions:
            session_run_rows = runs.get(session.session_id)
            if not session_run_rows:
                continue
            session_run_rows.sort(key=lambda item: item.run_id)
            session_run_rows.sort(key=lambda item: item.occurred_at, reverse=True)
            rows.append(
                SessionRow(
                    session_id=session.session_id,
                    title=session.title,
                    last_run_at=session_run_rows[0].occurred_at,
                    totals=totals[session.session_id].public_totals(),
                    runs=session_run_rows,
                )
            )
        rows.sort(key=lambda item: item.session_id)
        rows.sort(key=lambda item: item.last_run_at, reverse=True)
        return rows

    @staticmethod
    def _filter_rows(rows: Sequence[SessionRow], query: str | None) -> list[SessionRow]:
        if not query:
            return list(rows)
        normalized = query.casefold()
        return [
            row
            for row in rows
            if normalized in row.title.casefold() or normalized in row.session_id.casefold()
        ]
