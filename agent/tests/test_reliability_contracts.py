"""Tests for reliability contracts (Task 2).

Covers: enum wire values, JSON-safe to_wire(), invariant rejection,
and preservation of empty optional fields.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from src.reliability import (
    CapabilityRoute,
    ErrorCode,
    EvidenceRef,
    StepResult,
    StepStatus,
    ToolError,
)


# --- enum wire values --------------------------------------------------------

def test_step_status_wire_values():
    assert StepStatus.SUCCESS.value == "success"
    assert StepStatus.PARTIAL.value == "partial"
    assert StepStatus.RECOVERABLE_ERROR.value == "recoverable_error"
    assert StepStatus.BLOCKED.value == "blocked"
    assert StepStatus.UNSAFE_ERROR.value == "unsafe_error"
    assert StepStatus.CANCELLED.value == "cancelled"


def test_error_code_wire_values():
    assert ErrorCode.INVALID_ARGUMENT.value == "invalid_argument"
    assert ErrorCode.AMBIGUOUS_SYMBOL.value == "ambiguous_symbol"
    assert ErrorCode.DATA_UNAVAILABLE.value == "data_unavailable"
    assert ErrorCode.PROVIDER_TIMEOUT.value == "provider_timeout"
    assert ErrorCode.AUTH_REQUIRED.value == "auth_required"
    assert ErrorCode.SCHEMA_MISMATCH.value == "schema_mismatch"
    assert ErrorCode.UNSAFE_SIDE_EFFECT.value == "unsafe_side_effect"
    assert ErrorCode.BUDGET_EXCEEDED.value == "budget_exceeded"
    assert ErrorCode.CANCELLED.value == "cancelled"
    assert ErrorCode.UNKNOWN.value == "unknown"


def test_enums_are_str_subclasses():
    # str-Enum: members ARE str instances (wire-safe by construction)
    assert isinstance(StepStatus.SUCCESS, str)
    assert isinstance(ErrorCode.UNKNOWN, str)


# --- to_wire() is JSON-serializable ------------------------------------------


def test_tool_error_to_wire_json_safe():
    err = ToolError(
        code=ErrorCode.PROVIDER_TIMEOUT,
        message="upstream timed out",
        retryable=True,
        fallback="cached_value",
        repair_hint="retry with backoff",
    )
    wire = err.to_wire()
    json.dumps(wire)  # must not raise
    assert wire["code"] == "provider_timeout"
    assert wire["message"] == "upstream timed out"
    assert wire["retryable"] is True
    assert wire["fallback"] == "cached_value"
    assert wire["repair_hint"] == "retry with backoff"


def test_evidence_ref_to_wire_preserves_empty_optionals():
    ev = EvidenceRef(source_type="price_feed", source_id="AAPL")
    wire = ev.to_wire()
    json.dumps(wire)
    # Empty optionals preserved (as None), not dropped
    assert wire["field"] is None
    assert wire["as_of"] is None
    assert wire["source_type"] == "price_feed"
    assert wire["source_id"] == "AAPL"


def test_step_result_to_wire_json_safe_with_data():
    sr = StepResult(
        step_id="s1",
        status=StepStatus.SUCCESS,
        data={"price": 123.45, "ticker": "AAPL"},
        evidence=(EvidenceRef(source_type="price_feed", source_id="AAPL", field="close"),),
        provider="yfinance",
        elapsed_ms=42,
    )
    wire = sr.to_wire()
    json.dumps(wire)
    assert wire["step_id"] == "s1"
    assert wire["status"] == "success"
    assert wire["data"] == {"price": 123.45, "ticker": "AAPL"}
    assert wire["error"] is None
    assert wire["evidence"][0]["source_id"] == "AAPL"
    assert wire["provider"] == "yfinance"
    assert wire["elapsed_ms"] == 42


def test_step_result_to_wire_coerces_non_serializable_data():
    class Foo:
        def __str__(self):
            return "<Foo>"

    sr = StepResult(step_id="s2", status=StepStatus.SUCCESS, data=Foo())
    wire = sr.to_wire()
    json.dumps(wire)  # must not raise
    assert wire["data"] == "<Foo>"


def test_step_result_to_wire_preserves_empty_optionals():
    sr = StepResult(step_id="s3", status=StepStatus.SUCCESS)
    wire = sr.to_wire()
    json.dumps(wire)
    assert wire["data"] is None
    assert wire["error"] is None
    assert wire["evidence"] == []
    assert wire["provider"] is None
    assert wire["elapsed_ms"] == 0


def test_capability_route_to_wire_json_safe():
    cr = CapabilityRoute(
        intent="get_price",
        capabilities=("market_data",),
        allowed_tools=("yfinance", "ccxt"),
        complexity="low",
        budgets={"tokens": 1000, "steps": 5},
    )
    wire = cr.to_wire()
    json.dumps(wire)
    assert wire["intent"] == "get_price"
    assert wire["capabilities"] == ["market_data"]
    assert wire["allowed_tools"] == ["yfinance", "ccxt"]
    assert wire["complexity"] == "low"
    assert wire["budgets"] == {"tokens": 1000, "steps": 5}


# --- invariants: rejection of malformed states -------------------------------


def test_recoverable_error_without_error_rejected():
    with pytest.raises(ValueError):
        StepResult(step_id="s", status=StepStatus.RECOVERABLE_ERROR, error=None)


def test_success_with_malformed_evidence_rejected():
    # malformed = an item that isn't an EvidenceRef
    with pytest.raises((TypeError, ValueError)):
        StepResult(
            step_id="s",
            status=StepStatus.SUCCESS,
            evidence=({"source_type": "x", "source_id": "y"},),  # dict, not EvidenceRef
        )


def test_blocked_result_without_error_is_allowed():
    # Only recoverable_error strictly requires an error; blocked may carry none.
    sr = StepResult(step_id="s", status=StepStatus.BLOCKED)
    assert sr.error is None


# --- immutability ------------------------------------------------------------


def test_step_result_is_frozen():
    sr = StepResult(step_id="s", status=StepStatus.SUCCESS)
    with pytest.raises(FrozenInstanceError):
        sr.step_id = "other"  # type: ignore[misc]
