"""Replay runner + fault-injection adapters for reliability-runtime tests.

Test infrastructure only — does NOT touch production code. Loads deterministic
JSON fixtures and wires fake router / gateway / executor components so each
case can be driven through ``ReliabilityRuntime.run()`` without network.

The seams exercised are the same as ``test_reliability_runtime.py``:
``plan_provider`` (which plan), ``gateway`` (per-step outcomes), ``executor``
(synthesis + claims). No live market or broker calls.

Fault adapters translate a case's ``faults`` (provider/error_code pairs) and
optional ``setup.outcome`` (a named outcome) into a per-attempt ``StepResult``
factory used by the recording gateway. Each adapter is deterministic.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

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
    ToolGateway,
)
from src.reliability.evidence import Claim, ClaimKind, EvidenceVerifier
from src.reliability.gateway import ToolGateway
from src.reliability.runtime import ReliabilityRuntime
from src.reliability.router import TaskRouter
from src.telemetry import counters

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliability_cases.json"


# ---------------------------------------------------------------------------
# Fault adapters: (error_code, attempt) -> StepResult
# ---------------------------------------------------------------------------

# ponytail: a single dict mapping error_code -> outcome factory. Adding a new
# fault shape is one line. The factory takes the step_id and the attempt
# number so retry behaviour is observable; most adapters ignore attempt.
_FAULT_OUTCOMES: dict[str, Callable[[str, int], StepResult]] = {
    "provider_timeout": lambda sid, attempt: StepResult(
        step_id=sid,
        status=StepStatus.RECOVERABLE_ERROR,
        error=ToolError(code=ErrorCode.PROVIDER_TIMEOUT, message="timeout", retryable=True),
    ),
    "data_unavailable": lambda sid, attempt: StepResult(
        step_id=sid,
        status=StepStatus.RECOVERABLE_ERROR,
        error=ToolError(code=ErrorCode.DATA_UNAVAILABLE, message="empty", retryable=True),
    ),
    "invalid_argument": lambda sid, attempt: StepResult(
        step_id=sid,
        status=StepStatus.RECOVERABLE_ERROR,
        error=ToolError(code=ErrorCode.INVALID_ARGUMENT, message="bad arg", retryable=False),
    ),
    "auth_required": lambda sid, attempt: StepResult(
        step_id=sid,
        status=StepStatus.RECOVERABLE_ERROR,
        error=ToolError(code=ErrorCode.AUTH_REQUIRED, message="auth", retryable=False),
    ),
    "schema_mismatch": lambda sid, attempt: StepResult(
        step_id=sid,
        status=StepStatus.RECOVERABLE_ERROR,
        error=ToolError(code=ErrorCode.SCHEMA_MISMATCH, message="bad schema", retryable=False),
    ),
    "unsafe_side_effect": lambda sid, attempt: StepResult(
        step_id=sid,
        status=StepStatus.UNSAFE_ERROR,
        error=ToolError(code=ErrorCode.UNSAFE_SIDE_EFFECT, message="blocked", retryable=False),
    ),
    "cancelled": lambda sid, attempt: StepResult(
        step_id=sid,
        status=StepStatus.CANCELLED,
        error=ToolError(code=ErrorCode.CANCELLED, message="user cancelled", retryable=False),
    ),
    "budget_exceeded": lambda sid, attempt: StepResult(
        step_id=sid,
        status=StepStatus.BLOCKED,
        error=ToolError(code=ErrorCode.BUDGET_EXCEEDED, message="budget", retryable=False),
    ),
}


def _partial_evidence_outcome(sid: str, attempt: int) -> StepResult:
    """SUCCESS with an evidence ref that cannot resolve (no artifact on disk).

    The verifier downgrades SUCCESS -> PARTIAL, yielding a partial run.
    """
    return StepResult(
        step_id=sid,
        status=StepStatus.SUCCESS,
        data={"partial": True},
        evidence=(EvidenceRef(source_type="artifact", source_id="missing.json"),),
    )


def _success_no_evidence_outcome(sid: str, attempt: int) -> StepResult:
    """SUCCESS with no evidence refs — verifier downgrades to PARTIAL."""
    return StepResult(step_id=sid, status=StepStatus.SUCCESS, data={"ok": True})


def _malformed_metric_outcome(sid: str, attempt: int) -> StepResult:
    """SUCCESS with a metric ref whose field is absent.

    The verifier cannot load the named field and downgrades SUCCESS -> BLOCKED.
    The fixture file (metrics.json) is written by the runner under run_dir.
    """
    return StepResult(
        step_id=sid,
        status=StepStatus.SUCCESS,
        data={"ok": True},
        evidence=(EvidenceRef(source_type="metric", source_id="metrics.json", field="sharpe"),),
    )


def _cancelled_with_event_factory(
    cancel_event: threading.Event,
) -> Callable[[str, int], StepResult]:
    """Adapter that flips the cancel_event mid-step and returns CANCELLED."""

    def _impl(sid: str, attempt: int) -> StepResult:
        cancel_event.set()
        return StepResult(
            step_id=sid,
            status=StepStatus.CANCELLED,
            error=ToolError(code=ErrorCode.CANCELLED, message="cancelled mid-step", retryable=False),
        )

    return _impl


# Named outcomes referenced from the JSON ``setup.outcome`` field. Each maps
# to a factory producing a per-attempt StepResult. The runner may patch the
# "cancelled" entry at runtime to thread the cancel_event through.
_NAMED_OUTCOMES: dict[str, Callable[[str, int], StepResult]] = {
    "partial_evidence": _partial_evidence_outcome,
    "success_no_evidence": _success_no_evidence_outcome,
    "malformed_metric": _malformed_metric_outcome,
}


# ---------------------------------------------------------------------------
# Fake gateway (same shape as test_reliability_runtime._RecordingGateway)
# ---------------------------------------------------------------------------


class _RecordingGateway(ToolGateway):
    """Gateway fake that scripts a per-step outcome and records every call."""

    def __init__(self, outcome: Callable[[str, int], StepResult]) -> None:
        self._outcome = outcome
        self._attempt_counts: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        step_id: str,
        policy: GatewayPolicy,
        session_id: str = "",
    ) -> StepResult:
        with self._lock:
            self._attempt_counts[step_id] += 1
            attempt = self._attempt_counts[step_id]
        return self._outcome(step_id, attempt)

    def call_count(self, step_id: str) -> int:
        return self._attempt_counts.get(step_id, 0)


class _FakeRouter:
    """Returns a canned CapabilityRoute for any message."""

    def __init__(self, route: CapabilityRoute) -> None:
        self._route = route

    def route(self, user_message: str, tool_names: Any) -> CapabilityRoute:
        return self._route


class _FakeRegistry:
    """Minimal registry exposing tool_names (no real tools)."""

    def __init__(self, names: list[str]) -> None:
        self._names = list(names)

    @property
    def tool_names(self) -> list[str]:
        return list(self._names)


# ponytail: the runner builds its own CapabilityRoute (not the real TaskRouter)
# so each fixture case fully controls the route's capabilities and tools.


# ---------------------------------------------------------------------------
# Case -> runtime wiring
# ---------------------------------------------------------------------------


def _route_for(setup: dict[str, Any], intent: str) -> CapabilityRoute:
    step_budget = int(setup.get("step_budget", 6))
    return CapabilityRoute(
        intent=intent,
        capabilities=tuple(setup.get("route_capabilities", ("market_data",))),
        allowed_tools=tuple(setup.get("route_allowed_tools", ())),
        complexity="low",
        budgets={"steps": step_budget, "tokens": 3000, "wall_clock_seconds": 120},
    )


def _plan_for(setup: dict[str, Any]) -> ExecutionPlan:
    step_budget = int(setup.get("step_budget", 6))
    step = PlanStep(
        id="s1",
        capability=str(setup.get("step_capability", "market_data")),
        tool=str(setup.get("step_tool", "get_market_data")),
        side_effecting=bool(setup.get("step_side_effecting", False)),
        retry_limit=int(setup.get("retry_limit", 0)),
    )
    return ExecutionPlan(steps=[step], budgets={"steps": step_budget, "tokens": 3000, "wall_clock_seconds": 120})


def _outcome_for(
    case: dict[str, Any],
    cancel_event: threading.Event,
) -> Callable[[str, int], StepResult]:
    """Resolve the per-case outcome callable.

    Priority:
      1. ``setup.outcome == "cancelled"`` -> adapter that flips cancel_event.
      2. ``setup.outcome`` (other named) -> ``_NAMED_OUTCOMES`` entry.
      3. ``faults[0].error_code`` -> ``_FAULT_OUTCOMES`` entry.
      4. Default: success.
    """
    setup = case.get("setup", {})
    named = setup.get("outcome")
    if named == "cancelled":
        return _cancelled_with_event_factory(cancel_event)
    if named in _NAMED_OUTCOMES:
        return _NAMED_OUTCOMES[named]
    faults = case.get("faults") or []
    if faults:
        code = faults[0].get("error_code", "")
        if code in _FAULT_OUTCOMES:
            return _FAULT_OUTCOMES[code]
    return lambda sid, attempt: StepResult(step_id=sid, status=StepStatus.SUCCESS, data={"ok": True})


def _synthesis_for(case: dict[str, Any]) -> Callable[..., dict[str, object]]:
    synth_spec = case.get("synthesis") or {}
    content = str(synth_spec.get("content", ""))
    raw_claims = synth_spec.get("claims") or []
    claims: list[Claim] = []
    for raw in raw_claims:
        if not isinstance(raw, dict):
            continue
        kind_str = str(raw.get("kind", "fact")).lower()
        try:
            kind = ClaimKind(kind_str)
        except ValueError:
            kind = ClaimKind.FACT
        ev_refs = []
        for ref in raw.get("evidence") or []:
            if isinstance(ref, dict):
                ev_refs.append(
                    EvidenceRef(
                        source_type=str(ref.get("source_type", "artifact")),
                        source_id=str(ref.get("source_id", "")),
                        field=ref.get("field"),
                        as_of=ref.get("as_of"),
                    )
                )
        claims.append(Claim(text=str(raw.get("text", "")), kind=kind, evidence=ev_refs))

    def _exec(**kwargs: Any) -> dict[str, object]:
        return {"content": content, "claims": claims}

    return _exec


def _run_dir_setup(case: dict[str, Any], run_dir: Path) -> None:
    """Drop any artifacts the case needs under run_dir.

    Today only the malformed-metric case needs an on-disk file (a JSON metric
    file that LACKS the field the evidence ref names, so the verifier
    downgrades the step to BLOCKED).
    """
    setup = case.get("setup", {})
    if setup.get("outcome") == "malformed_metric":
        # metrics.json present but missing the "sharpe" field the ref expects.
        (run_dir / "metrics.json").write_text(
            json.dumps({"total_return": 0.1}),  # no "sharpe"
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def load_cases() -> list[dict[str, Any]]:
    """Read the fixture JSON and return the case list."""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return list(raw.get("cases", []))


def run_case(case: dict[str, Any], *, tmp_path: Path) -> dict[str, object]:
    """Drive one fixture case through ReliabilityRuntime and return its result.

    Builds fake router/gateway/executor from the case spec, calls
    ``ReliabilityRuntime.run()``, and returns the result dict. The caller
    asserts on deterministic fields (status, reliability counts, etc).
    """
    counters.reset_for_test()
    setup = case.get("setup", {})
    intent = str(case.get("expected_intent", "general_research"))
    route = _route_for(setup, intent)
    plan = _plan_for(setup)

    run_dir = tmp_path / "rel-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    _run_dir_setup(case, run_dir)

    cancel_event = threading.Event()
    outcome = _outcome_for(case, cancel_event)
    gateway = _RecordingGateway(outcome)

    allow_side_effects = bool(setup.get("allow_side_effects", False))
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gateway,
        verifier=EvidenceVerifier(),
        runs_dir=tmp_path,
        run_dir=run_dir,
        plan_provider=lambda r, msg: plan,
        allow_side_effects=allow_side_effects,
        cancel_event=cancel_event,
    )

    registry = _FakeRegistry(list(route.allowed_tools))
    executor = _synthesis_for(case)

    return rt.run(
        user_message=str(case.get("prompt", "")),
        session_id="replay",
        registry=registry,
        executor=executor,
    )


__all__ = ["FIXTURE_PATH", "load_cases", "run_case"]
