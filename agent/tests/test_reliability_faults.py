"""Fault-injection tests for the reliability runtime.

These tests exercise the runtime's recovery and terminal-state policy under
injected provider/tool faults. They share the replay runner's fault adapters
but drive specific adapters directly (not via the JSON fixtures) so the fault
semantics are observable in isolation.

Each test injects a specific fault shape (timeout, empty data, auth error,
malformed metrics, side-effect failure) through the gateway interface and
asserts the runtime's response: retry budget respected, no side-effect retry,
correct terminal status, no upgrade of failed evidence.

Deterministic, no network, no live providers.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from src.reliability import (
    CapabilityRoute,
    ErrorCode,
    EvidenceRef,
    ExecutionPlan,
    GatewayPolicy,
    PlanStep,
    StepResult,
    StepStatus,
    ToolError,
)
from src.reliability.evidence import Claim, ClaimKind, EvidenceVerifier
from src.reliability.gateway import ToolGateway
from src.reliability.runtime import ReliabilityRuntime
from src.telemetry import counters
from tests._reliability_replay_runner import (
    _FAULT_OUTCOMES,
    _FakeRegistry,
    _FakeRouter,
    _RecordingGateway,
    _malformed_metric_outcome,
    _partial_evidence_outcome,
    _success_no_evidence_outcome,
    _cancelled_with_event_factory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route(*, capabilities: tuple[str, ...] = ("market_data",), tools: tuple[str, ...] = ("get_market_data",), budget: int = 6) -> CapabilityRoute:
    return CapabilityRoute(
        intent="market_data",
        capabilities=capabilities,
        allowed_tools=tools,
        complexity="low",
        budgets={"steps": budget, "tokens": 3000, "wall_clock_seconds": 120},
    )


def _plan(tool: str = "get_market_data", capability: str = "market_data", *, retry_limit: int = 0, side_effecting: bool = False, budget: int = 6) -> ExecutionPlan:
    return ExecutionPlan(
        steps=[PlanStep(id="s1", capability=capability, tool=tool, retry_limit=retry_limit, side_effecting=side_effecting)],
        budgets={"steps": budget, "tokens": 3000, "wall_clock_seconds": 120},
    )


def _synth(content: str = "ok", claims: list[Claim] | None = None) -> Callable[..., dict[str, object]]:
    def _exec(**kwargs: Any) -> dict[str, object]:
        return {"content": content, "claims": list(claims or [])}
    return _exec


def _run_with_fault(
    *,
    fault_outcome: Callable[[str, int], StepResult],
    route: CapabilityRoute,
    plan: ExecutionPlan,
    tmp_path: Path,
    synth: Callable[..., dict[str, object]] | None = None,
    allow_side_effects: bool = False,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, object], _RecordingGateway]:
    counters.reset_for_test()
    gw = _RecordingGateway(fault_outcome)
    cancel = cancel_event or threading.Event()
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=EvidenceVerifier(),
        runs_dir=tmp_path,
        run_dir=tmp_path,
        plan_provider=lambda r, msg: plan,
        allow_side_effects=allow_side_effects,
        cancel_event=cancel,
    )
    result = rt.run(
        user_message="test",
        session_id="fault-test",
        registry=_FakeRegistry(list(route.allowed_tools)),
        executor=synth or _synth(),
    )
    return result, gw


# ---------------------------------------------------------------------------
# Fault adapter sanity: every advertised outcome is a StepResult
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code,factory", list(_FAULT_OUTCOMES.items()))
def test_fault_adapter_returns_stepresult(code: str, factory: Callable[[str, int], StepResult]) -> None:
    """Every fault adapter in the table must produce a valid StepResult."""
    result = factory("s1", 1)
    assert isinstance(result, StepResult)
    assert result.step_id == "s1"
    # RECOVERABLE_ERROR requires an error (invariant enforced by StepResult).
    if result.status is StepStatus.RECOVERABLE_ERROR:
        assert result.error is not None


def test_cancelled_adapter_threads_cancel_event() -> None:
    """The cancelled adapter must set the cancel_event so the runtime sees it."""
    ev = threading.Event()
    factory = _cancelled_with_event_factory(ev)
    result = factory("s1", 1)
    assert result.status is StepStatus.CANCELLED
    assert ev.is_set(), "cancelled adapter must flip the cancel_event"


# ---------------------------------------------------------------------------
# Fault 1: provider_timeout with retry_limit=0 -> single attempt, failed
# ---------------------------------------------------------------------------


def test_fault_provider_timeout_no_retry(tmp_path: Path) -> None:
    """A retryable timeout with retry_limit=0 fires exactly once, then fails."""
    result, gw = _run_with_fault(
        fault_outcome=_FAULT_OUTCOMES["provider_timeout"],
        route=_route(),
        plan=_plan(retry_limit=0),
        tmp_path=tmp_path,
    )
    assert gw.call_count("s1") == 1, "retry_limit=0 -> exactly one gateway execute"
    assert result["status"] == "failed"
    assert result["reliability"]["steps_verified"] == 0


# ---------------------------------------------------------------------------
# Fault 2: empty provider response (data_unavailable) -> retryable, no fallback
# ---------------------------------------------------------------------------


def test_fault_empty_provider_response_no_fallback(tmp_path: Path) -> None:
    """data_unavailable is retryable; without a fallback, the run fails."""
    result, gw = _run_with_fault(
        fault_outcome=_FAULT_OUTCOMES["data_unavailable"],
        route=_route(),
        plan=_plan(retry_limit=0),
        tmp_path=tmp_path,
    )
    assert gw.call_count("s1") == 1
    assert result["status"] == "failed"
    # The runtime records the error code in the redacted tool_progress event stream.
    assert result["reliability"]["steps_failed"] >= 1


# ---------------------------------------------------------------------------
# Fault 3: auth_required -> non-retryable, single attempt, failed
# ---------------------------------------------------------------------------


def test_fault_auth_required_is_not_retried(tmp_path: Path) -> None:
    """auth_required is non-retryable; runtime must terminate after one attempt."""
    result, gw = _run_with_fault(
        fault_outcome=_FAULT_OUTCOMES["auth_required"],
        route=_route(),
        plan=_plan(retry_limit=5),  # even with retry budget, auth won't retry
        tmp_path=tmp_path,
    )
    assert gw.call_count("s1") == 1, "auth_required is non-retryable"
    assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Fault 4: malformed backtest metrics -> SUCCESS downgraded to BLOCKED
# ---------------------------------------------------------------------------


def test_fault_malformed_metric_downgrades_success_to_blocked(tmp_path: Path) -> None:
    """A step returns SUCCESS pointing at a metric file missing the named field.

    The verifier downgrades SUCCESS -> BLOCKED, the run lands on failed.
    """
    metrics_file = tmp_path / "metrics.json"
    metrics_file.write_text('{"total_return": 0.1}', encoding="utf-8")  # no "sharpe"
    result, _ = _run_with_fault(
        fault_outcome=_malformed_metric_outcome,
        route=_route(capabilities=("backtest",), tools=("backtest",)),
        plan=_plan(tool="backtest", capability="backtest", side_effecting=True, budget=6),
        tmp_path=tmp_path,
        allow_side_effects=True,
    )
    assert result["status"] == "failed"
    assert result["reliability"]["steps_verified"] == 0


# ---------------------------------------------------------------------------
# Fault 5: partial evidence -> SUCCESS downgraded to PARTIAL, partial run
# ---------------------------------------------------------------------------


def test_fault_partial_evidence_downgrades_success(tmp_path: Path) -> None:
    """SUCCESS with no resolving evidence refs is downgraded to PARTIAL."""
    result, _ = _run_with_fault(
        fault_outcome=_success_no_evidence_outcome,
        route=_route(),
        plan=_plan(),
        tmp_path=tmp_path,
    )
    assert result["status"] == "partial"
    assert result["reliability"]["steps_verified"] == 0  # PARTIAL doesn't count as verified


# ---------------------------------------------------------------------------
# Fault 6: budget exhaustion caps retry attempts
# ---------------------------------------------------------------------------


def test_fault_budget_caps_retry_attempts(tmp_path: Path) -> None:
    """A retryable read with step_budget=2 fires at most 2 times then fails."""
    result, gw = _run_with_fault(
        fault_outcome=_FAULT_OUTCOMES["provider_timeout"],
        route=_route(budget=2),
        plan=_plan(retry_limit=5, budget=2),
        tmp_path=tmp_path,
    )
    assert gw.call_count("s1") <= 2, "step_budget caps total gateway execute calls"
    assert gw.call_count("s1") >= 1
    assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Fault 7: side-effecting step never retried
# ---------------------------------------------------------------------------


def test_fault_side_effecting_step_never_retried(tmp_path: Path) -> None:
    """A side-effecting step that fails retryably must be invoked exactly once."""
    result, gw = _run_with_fault(
        fault_outcome=_FAULT_OUTCOMES["provider_timeout"],
        route=_route(capabilities=("backtest",), tools=("backtest",)),
        plan=_plan(tool="backtest", capability="backtest", side_effecting=True, retry_limit=5),
        tmp_path=tmp_path,
        allow_side_effects=True,
    )
    assert gw.call_count("s1") == 1, "side-effecting step must not be retried"
    assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Fault 8: unsafe_side_effect -> failed with "unsafe path blocked" reason
# ---------------------------------------------------------------------------


def test_fault_unsafe_side_effect_blocks_run(tmp_path: Path) -> None:
    """UNSAFE_ERROR from the gateway must land the run on failed."""
    result, _ = _run_with_fault(
        fault_outcome=_FAULT_OUTCOMES["unsafe_side_effect"],
        route=_route(capabilities=("backtest",), tools=("backtest",)),
        plan=_plan(tool="trading_place_order", capability="backtest", side_effecting=True),
        tmp_path=tmp_path,
        allow_side_effects=False,
    )
    assert result["status"] == "failed"
    assert result.get("reason") == "unsafe path blocked"


# ---------------------------------------------------------------------------
# Fault 9: cancellation propagates to terminal cancelled status
# ---------------------------------------------------------------------------


def test_fault_cancellation_propagates(tmp_path: Path) -> None:
    """An injected cancel_event mid-step produces status=cancelled."""
    cancel_event = threading.Event()
    result, _ = _run_with_fault(
        fault_outcome=_cancelled_with_event_factory(cancel_event),
        route=_route(),
        plan=_plan(),
        tmp_path=tmp_path,
        cancel_event=cancel_event,
    )
    assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Fault 10: invalid_argument is non-retryable
# ---------------------------------------------------------------------------


def test_fault_invalid_argument_not_retried(tmp_path: Path) -> None:
    """invalid_argument is non-retryable; single attempt, failed."""
    result, gw = _run_with_fault(
        fault_outcome=_FAULT_OUTCOMES["invalid_argument"],
        route=_route(),
        plan=_plan(retry_limit=5),
        tmp_path=tmp_path,
    )
    assert gw.call_count("s1") == 1
    assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Fault 11: unsupported claim keeps coverage < 1.0 on a partial run
# ---------------------------------------------------------------------------


def test_fault_unsupported_claim_keeps_coverage_below_one(tmp_path: Path) -> None:
    """A synthesis claim with no backing evidence yields coverage=0 on partial."""
    claim = Claim(text="buy AAPL", kind=ClaimKind.INTERPRETATION, evidence=[])
    result, _ = _run_with_fault(
        fault_outcome=_success_no_evidence_outcome,
        route=_route(),
        plan=_plan(),
        tmp_path=tmp_path,
        synth=_synth(content="buy call", claims=[claim]),
    )
    assert result["status"] == "partial"
    assert result["reliability"]["claims_coverage"] < 1.0


# ---------------------------------------------------------------------------
# Fault 12: verifier never upgrades a failed step to success
# ---------------------------------------------------------------------------


def test_verifier_never_upgrades_recoverable_to_success(tmp_path: Path) -> None:
    """A RECOVERABLE_ERROR result must not be upgraded by the verifier."""
    v = EvidenceVerifier()
    failed = StepResult(
        step_id="s1",
        status=StepStatus.RECOVERABLE_ERROR,
        error=ToolError(code=ErrorCode.PROVIDER_TIMEOUT, message="timeout", retryable=True),
    )
    verified = v.verify_step_result(failed, run_dir=tmp_path)
    assert verified.status is StepStatus.RECOVERABLE_ERROR, "verifier must not upgrade failures"
