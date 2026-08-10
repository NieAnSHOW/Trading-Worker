"""Reliability telemetry baseline tests (Task 1).

Covers the new ``reliability`` snapshot section and the redacted phase/event
recorders that later reliability-runtime tasks will write into.
"""

from __future__ import annotations

from src.telemetry import counters


def test_reliability_snapshot_contains_phase_timings():
    counters.reset_for_test()
    counters.record_reliability_phase("router", 12)
    counters.record_reliability_event("recovery_success")
    snapshot = counters.snapshot()
    assert snapshot["reliability"]["phase_ms"] == {"router": 12}
    assert snapshot["reliability"]["events"] == {"recovery_success": 1}


def test_reliability_phase_last_write_wins():
    """Phases re-record on re-entry; last elapsed wins (no accumulation)."""
    counters.reset_for_test()
    counters.record_reliability_phase("tool", 5)
    counters.record_reliability_phase("tool", 9)
    snapshot = counters.snapshot()
    assert snapshot["reliability"]["phase_ms"] == {"tool": 9}


def test_reliability_event_accumulates():
    counters.reset_for_test()
    counters.record_reliability_event("tool_error:tool_timeout")
    counters.record_reliability_event("tool_error:tool_timeout", value=2)
    snapshot = counters.snapshot()
    assert snapshot["reliability"]["events"] == {"tool_error:tool_timeout": 3}


def test_reliability_section_resets_on_snapshot():
    counters.reset_for_test()
    counters.record_reliability_phase("router", 12)
    counters.record_reliability_event("recovery_success")
    first = counters.snapshot()
    assert first["reliability"]["phase_ms"] == {"router": 12}
    second = counters.snapshot()
    assert second["reliability"]["phase_ms"] == {}
    assert second["reliability"]["events"] == {}


def test_reliability_section_is_redacted_safe():
    """Snapshot must carry only numeric timings + short event names, no payloads."""
    counters.reset_for_test()
    counters.record_reliability_phase("tool", 42)
    counters.record_reliability_event("tool_error:tool_timeout")
    snapshot = counters.snapshot()
    rel = snapshot["reliability"]
    flat = str(rel)
    # Privacy boundary: no prompt/text/symbol/credential leakage.
    for taboo in ("prompt", "query", "symbol", "amount", "credential", "token", "secret"):
        assert taboo not in flat.lower()
