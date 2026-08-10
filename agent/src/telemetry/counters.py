# agent/src/telemetry/counters.py
"""进程内脱敏指标计数器（§4.3）。

FastAPI 单进程足够；线程安全用一把全局锁（ponytail: 全局锁，若多 worker/吞吐
敏感可升级 per-skill 锁）。计数自上次 snapshot 后增量；snapshot 返回并清零，
供 ``GET /telemetry/sidecar-metrics`` 周期性拉取。
"""
from __future__ import annotations

import threading
import time
from typing import Any


class _Counters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._skill_calls: dict[str, int] = {}
        self._bt_count = 0
        self._bt_total_ms = 0
        self._bt_by_engine: dict[str, int] = {}
        self._err_count = 0
        self._err_by_type: dict[str, int] = {}
        # Reliability baseline (§Agent Reliability Runtime): redacted phase
        # timings (last-write-per-phase) + named event counters. Only numeric
        # elapsed values and short allowlisted code strings may be recorded.
        self._rel_phase_ms: dict[str, int] = {}
        self._rel_events: dict[str, int] = {}
        self._since = time.time()

    def record_skill_call(self, skill_name: str) -> None:
        with self._lock:
            self._skill_calls[skill_name] = self._skill_calls.get(skill_name, 0) + 1

    def record_backtest(self, engine: str, elapsed_ms: int) -> None:
        with self._lock:
            self._bt_count += 1
            self._bt_total_ms += int(elapsed_ms)
            self._bt_by_engine[engine] = self._bt_by_engine.get(engine, 0) + 1

    def record_error(self, error_type: str) -> None:
        with self._lock:
            self._err_count += 1
            self._err_by_type[error_type] = self._err_by_type.get(error_type, 0) + 1

    def record_reliability_phase(self, phase: str, elapsed_ms: int) -> None:
        """Record a redacted phase timing (last-write-per-phase wins).

        Reliability-runtime phases re-record on re-entry, so last elapsed
        reflects the most recent observation rather than a running sum.
        """
        with self._lock:
            self._rel_phase_ms[phase] = int(elapsed_ms)

    def record_reliability_event(self, name: str, value: int = 1) -> None:
        """Increment a named reliability event counter (allowlisted codes only)."""
        with self._lock:
            self._rel_events[name] = self._rel_events.get(name, 0) + int(value)

    def snapshot(self, since: float | None = None) -> dict[str, Any]:
        """返回自上次 snapshot 以来的增量，并重置计数（since 参数仅作记录用）。"""
        with self._lock:
            snap: dict[str, Any] = {
                "since": since if since is not None else self._since,
                "skill_calls": dict(self._skill_calls),
                "backtests": {
                    "count": self._bt_count,
                    "total_ms": self._bt_total_ms,
                    "by_engine": dict(self._bt_by_engine),
                },
                "errors": {
                    "count": self._err_count,
                    "by_type": dict(self._err_by_type),
                },
                "reliability": {
                    "phase_ms": dict(self._rel_phase_ms),
                    "events": dict(self._rel_events),
                },
            }
            # reset 增量窗口
            self._skill_calls.clear()
            self._bt_count = 0
            self._bt_total_ms = 0
            self._bt_by_engine.clear()
            self._err_count = 0
            self._err_by_type.clear()
            self._rel_phase_ms.clear()
            self._rel_events.clear()
            self._since = time.time()
            return snap

    def reset_for_test(self) -> None:
        with self._lock:
            self._skill_calls.clear()
            self._bt_count = 0
            self._bt_total_ms = 0
            self._bt_by_engine.clear()
            self._err_count = 0
            self._err_by_type.clear()
            self._rel_phase_ms.clear()
            self._rel_events.clear()
            self._since = time.time()


_state = _Counters()

record_skill_call = _state.record_skill_call
record_backtest = _state.record_backtest
record_error = _state.record_error
record_reliability_phase = _state.record_reliability_phase
record_reliability_event = _state.record_reliability_event
snapshot = _state.snapshot
reset_for_test = _state.reset_for_test
