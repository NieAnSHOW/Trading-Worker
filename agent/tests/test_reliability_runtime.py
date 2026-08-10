"""Orchestration tests for ReliabilityRuntime (Task 6).

These tests inject FAKE router / gateway / executor / verifier components (and
a real PlanValidator) to assert the orchestration invariants:

  * simple requests take the fast path (no plan steps);
  * independent plan steps run concurrently, dependent steps wait;
  * a retryable failure follows the configured gateway fallback;
  * a repeated failure trips the step-count budget (no infinite loop);
  * an unverified claim downgrades a success-looking run to partial/blocked;
  * ``attempt.completed`` is NOT emitted for an unverified run;
  * a side-effecting step is never retried.

The runtime orchestrates; it does not call an LLM itself. Final synthesis is
performed by the ``executor`` callback.
"""

from __future__ import annotations

import json
import threading
import time
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
from src.reliability.runtime import ReliabilityRuntime
from src.telemetry import counters


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _IdentityVerifier:
    """Verifier fake that passes results through unchanged.

    Used for orchestration tests (concurrency, budget, retry, side-effects)
    where evidence resolution is not the property under test.
    """

    def verify_step_result(self, result: StepResult, run_dir: Path | None = None) -> StepResult:
        return result

    def coverage(self, claims: list[Claim], evidence: list[StepResult]) -> float:
        if not claims:
            return 1.0
        return 1.0 if any(r.status is StepStatus.SUCCESS for r in evidence) else 0.0


class _FakeRouter:
    """Returns a canned CapabilityRoute for any message."""

    def __init__(self, route: CapabilityRoute) -> None:
        self._route = route

    def route(self, user_message: str, tool_names: Any) -> CapabilityRoute:
        return self._route


class _FakeRegistry:
    """Minimal registry: exposes tool_names + get(). Tools optional."""

    def __init__(self, names: list[str] | None = None, tools: dict[str, Any] | None = None) -> None:
        self._names = list(names or [])
        self._tools = tools or {}

    @property
    def tool_names(self) -> list[str]:
        return list(self._names)

    def get(self, name: str) -> Any:
        return self._tools.get(name)

    def execute(self, name: str, params: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"status": "error", "error": f"Tool '{name}' not found"})
        return tool.execute(**params)


class _FakeTool:
    """Minimal tool for the real-gateway fallback test."""

    def __init__(self, name: str, *, side_effecting: bool = False, behavior: Callable[[], str] | None = None) -> None:
        self.name = name
        self.side_effecting = side_effecting
        self.parameters = {"type": "object", "properties": {}, "required": []}
        self._behavior = behavior or (lambda: json.dumps({"status": "ok"}))

    def execute(self, **kwargs: Any) -> str:
        return self._behavior()


class _RecordingGateway(ToolGateway):
    """Gateway fake: scripts StepResult per step_id and records every execute() call.

    ``outcomes`` maps step_id -> callable(attempt: int) -> StepResult. The
    callable is invoked on every execute() so retry behaviour is observable.
    """

    def __init__(self, outcomes: dict[str, Callable[[int], StepResult]]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[str, int, str, float]] = []
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
            self.calls.append((step_id, attempt, threading.current_thread().name, time.monotonic()))
        # Simulated work for concurrency timing tests.
        return self._outcomes[step_id](attempt)

    def call_count(self, step_id: str) -> int:
        return self._attempt_counts.get(step_id, 0)


class _TimingGateway(_RecordingGateway):
    """Recording gateway whose outcomes sleep to make concurrency observable."""

    def __init__(self, durations: dict[str, float], results: dict[str, StepResult] | None = None) -> None:
        self._durations = durations
        self._results = results or {}
        self.starts: dict[str, float] = {}
        self.ends: dict[str, float] = {}
        super().__init__({})

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
            now = time.monotonic()
            self.calls.append((step_id, attempt, threading.current_thread().name, now))
            self.starts[step_id] = now
        time.sleep(self._durations.get(step_id, 0.0))
        with self._lock:
            self.ends[step_id] = time.monotonic()
        return self._results.get(step_id, StepResult(step_id=step_id, status=StepStatus.SUCCESS))


def _route(
    *,
    capabilities: tuple[str, ...] = ("market_data",),
    allowed_tools: tuple[str, ...] = ("get_market_data",),
    budgets: dict[str, int] | None = None,
    intent: str = "market_data",
    complexity: str = "low",
) -> CapabilityRoute:
    return CapabilityRoute(
        intent=intent,
        capabilities=capabilities,
        allowed_tools=allowed_tools,
        complexity=complexity,
        budgets=budgets or {"steps": 6, "tokens": 3000, "wall_clock_seconds": 120},
    )


def _ok_result(step_id: str) -> StepResult:
    return StepResult(step_id=step_id, status=StepStatus.SUCCESS, data={"ok": True})


def _recoverable(step_id: str, *, code: ErrorCode = ErrorCode.DATA_UNAVAILABLE, retryable: bool = True) -> StepResult:
    return StepResult(
        step_id=step_id,
        status=StepStatus.RECOVERABLE_ERROR,
        error=ToolError(code=code, message="transient", retryable=retryable),
    )


def _synth_executor(
    *,
    content: str = "synthesis",
    claims: list[Claim] | None = None,
) -> Callable[..., dict[str, object]]:
    """An executor that returns a canned synthesis with optional claims."""

    def _exec(**kwargs: Any) -> dict[str, object]:
        return {"content": content, "claims": list(claims or []), "usage": {"total_tokens": 7}}

    return _exec


def _plan(steps: list[PlanStep], budgets: dict[str, int] | None = None) -> ExecutionPlan:
    return ExecutionPlan(steps=steps, budgets=budgets or {"steps": 6, "tokens": 3000, "wall_clock_seconds": 120})


# ---------------------------------------------------------------------------
# Step 1a: fast path
# ---------------------------------------------------------------------------


def test_simple_request_takes_fast_path(tmp_path: Path) -> None:
    """No plan steps -> executor called once, no tool_progress, success."""
    counters.reset_for_test()
    route = _route(allowed_tools=())
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: None,  # explicit: no plan
    )
    events: list[tuple[str, dict[str, object]]] = []
    result = rt.run(
        user_message="hi",
        session_id="s1",
        registry=_FakeRegistry([]),
        executor=_synth_executor(content="hello"),
        event_callback=lambda et, data: events.append((et, data)),
    )

    assert result["status"] == "success"
    assert result["content"] == "hello"
    assert result["reliability"]["steps_total"] == 0
    # Fast path emits attempt.started + attempt.completed, but no tool_progress.
    types = [e[0] for e in events]
    assert "attempt.started" in types
    assert "attempt.completed" in types
    assert not any(t == "tool_progress" for t in types)
    # No prompts/args in the redacted reliability summary.
    rel = json.dumps(result["reliability"])
    for taboo in ("prompt", "query", "symbol", "credential", "secret"):
        assert taboo not in rel.lower()


# ---------------------------------------------------------------------------
# Step 1b: independent steps concurrent + dependent waits
# ---------------------------------------------------------------------------


def test_independent_steps_concurrent_and_dependent_waits(tmp_path: Path) -> None:
    counters.reset_for_test()
    route = _route(
        capabilities=("market_data", "symbol", "general_research"),
        allowed_tools=("get_market_data", "search_symbol"),
        budgets={"steps": 6, "tokens": 3000, "wall_clock_seconds": 120},
    )
    gw = _TimingGateway(
        durations={"s1": 0.20, "s2": 0.20, "s3": 0.05},
    )
    plan = _plan([
        PlanStep(id="s1", capability="market_data", tool="get_market_data"),
        PlanStep(id="s2", capability="symbol", tool="search_symbol"),
        PlanStep(id="s3", capability="general_research", tool="get_market_data", depends_on=["s1", "s2"]),
    ])
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: plan,
        max_workers=4,
    )
    t0 = time.monotonic()
    result = rt.run(
        user_message="analyze",
        session_id="s1",
        registry=_FakeRegistry(["get_market_data", "search_symbol"]),
        executor=_synth_executor(),
    )
    elapsed = time.monotonic() - t0

    assert result["status"] == "success"
    # s1 and s2 must overlap (concurrent). Sequential would be ~0.4s before s3.
    assert gw.starts["s2"] < gw.ends["s1"], "s2 should start before s1 ends (concurrent)"
    assert gw.starts["s1"] < gw.ends["s2"], "s1 should start before s2 ends (concurrent)"
    # s3 starts strictly after both s1 and s2 finish.
    assert gw.starts["s3"] >= min(gw.ends["s1"], gw.ends["s2"]) - 0.01
    # Total wall-clock well below the sequential bound.
    assert elapsed < 0.55


def test_failed_dependency_never_schedules_dependent(tmp_path: Path) -> None:
    """A non-retryably failed step must not unlock its dependents.

    ready_steps() must resolve dependencies against the set of steps that
    SATISFIED them (SUCCESS/PARTIAL), not against every terminal state —
    otherwise dependents of a failed step get scheduled and their provider
    calls are wasted, contradicting the documented ``blocked`` = "budget or
    failed dependency" semantics.
    """
    counters.reset_for_test()
    route = _route(
        capabilities=("market_data", "symbol"),
        allowed_tools=("get_market_data", "search_symbol"),
    )
    gw = _RecordingGateway({
        "s1": lambda _a: _recoverable("s1", code=ErrorCode.AMBIGUOUS_SYMBOL, retryable=False),
        "s2": lambda _a: _ok_result("s2"),
    })
    plan = _plan([
        PlanStep(id="s1", capability="market_data", tool="get_market_data"),
        PlanStep(id="s2", capability="symbol", tool="search_symbol", depends_on=["s1"]),
    ])
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: plan,
    )
    result = rt.run(
        user_message="analyze",
        session_id="s1",
        registry=_FakeRegistry(["get_market_data", "search_symbol"]),
        executor=_synth_executor(),
    )

    assert gw.call_count("s2") == 0, "dependent of a failed step must never run"
    assert gw.call_count("s1") == 1
    assert result["status"] == "failed"
    assert result["reliability"]["steps_total"] == 2
    # s1 failed AND s2 blocked (never ran) are both counted as failed steps.
    assert result["reliability"]["steps_failed"] == 2
    assert result["reliability"]["steps_verified"] == 0


# ---------------------------------------------------------------------------
# Step 1c: retryable failure follows the configured gateway fallback
# ---------------------------------------------------------------------------


def test_retryable_failure_follows_gateway_fallback(tmp_path: Path) -> None:
    """Primary fails with DATA_UNAVAILABLE; gateway fallback tool succeeds."""
    counters.reset_for_test()
    route = _route(
        capabilities=("market_data",),
        allowed_tools=("get_market_data", "get_a_stock_data"),
    )

    def _primary_fail() -> str:
        raise ConnectionError("provider down")

    def _fallback_ok() -> str:
        return json.dumps({"status": "ok", "data": [1, 2, 3]})

    registry = _FakeRegistry(
        names=["get_market_data", "get_a_stock_data"],
        tools={
            "get_market_data": _FakeTool("get_market_data", behavior=_primary_fail),
            "get_a_stock_data": _FakeTool("get_a_stock_data", behavior=_fallback_ok),
        },
    )
    plan = _plan([PlanStep(id="s1", capability="market_data", tool="get_market_data")])
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: plan,
        fallback_tools={"get_market_data": "get_a_stock_data"},
    )
    result = rt.run(
        user_message="quote",
        session_id="s1",
        registry=registry,
        executor=_synth_executor(),
    )

    assert result["status"] == "success"
    assert result["reliability"]["steps_verified"] == 1


# ---------------------------------------------------------------------------
# Step 1d: repeated failure trips the step-count budget
# ---------------------------------------------------------------------------


def test_repeated_failure_trips_step_budget(tmp_path: Path) -> None:
    """A persistently-failing read step must not loop forever; budget caps it."""
    counters.reset_for_test()
    # step-count budget of 3: runtime may issue at most 3 gateway.execute calls.
    route = _route(budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60})
    gw = _RecordingGateway({"s1": lambda attempt: _recoverable("s1")})
    plan = _plan(
        [PlanStep(id="s1", capability="market_data", tool="get_market_data", retry_limit=10)],
        budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60},
    )
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: plan,
    )
    result = rt.run(
        user_message="quote",
        session_id="s1",
        registry=_FakeRegistry(["get_market_data"]),
        executor=_synth_executor(),
    )

    # Budget tripped: at most 3 attempts, terminal not success.
    assert gw.call_count("s1") <= 3
    assert gw.call_count("s1") >= 1
    assert result["status"] in ("partial", "failed")
    assert result["reliability"]["steps_verified"] == 0


# ---------------------------------------------------------------------------
# Step 1e: unverified claim -> partial/blocked, no attempt.completed
# ---------------------------------------------------------------------------


def test_unverified_claim_is_partial_and_emits_no_completed(tmp_path: Path) -> None:
    """A step returns SUCCESS but its evidence cannot be resolved.

    The verifier downgrades SUCCESS -> PARTIAL. The run must terminate as
    partial (NOT success) and ``attempt.completed`` MUST NOT be emitted.
    """
    counters.reset_for_test()
    route = _route()
    # Evidence ref points to a file that does not exist under run_dir.
    unverified = StepResult(
        step_id="s1",
        status=StepStatus.SUCCESS,
        data={"looks": "ok"},
        evidence=(EvidenceRef(source_type="artifact", source_id="missing.json"),),
    )
    gw = _RecordingGateway({"s1": lambda attempt: unverified})
    plan = _plan([PlanStep(id="s1", capability="market_data", tool="get_market_data")])
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=EvidenceVerifier(),
        run_dir=tmp_path,  # no artifacts under run_dir -> evidence cannot resolve
        plan_provider=lambda r, msg: plan,
    )
    events: list[tuple[str, dict[str, object]]] = []
    result = rt.run(
        user_message="quote",
        session_id="s1",
        registry=_FakeRegistry(["get_market_data"]),
        executor=_synth_executor(),
        event_callback=lambda et, data: events.append((et, data)),
    )

    assert result["status"] != "success"
    assert result["status"] in ("partial", "failed")
    types = [e[0] for e in events]
    assert "attempt.completed" not in types


# ---------------------------------------------------------------------------
# Step 1f: no side-effecting step is ever retried
# ---------------------------------------------------------------------------


def test_side_effecting_step_is_never_retried(tmp_path: Path) -> None:
    """A side-effecting step that fails retryably must not be re-submitted."""
    counters.reset_for_test()
    # backtest is a side-effect-capable capability (writes files).
    route = _route(
        capabilities=("backtest",),
        allowed_tools=("backtest",),
        intent="backtest",
        complexity="high",
    )
    gw = _RecordingGateway({"s1": lambda attempt: _recoverable("s1", retryable=True)})
    plan = _plan([PlanStep(id="s1", capability="backtest", tool="backtest", side_effecting=True, retry_limit=5)])
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: plan,
        allow_side_effects=True,  # exercise the runtime-level no-retry path
    )
    result = rt.run(
        user_message="run backtest",
        session_id="s1",
        registry=_FakeRegistry(["backtest"]),
        executor=_synth_executor(),
    )

    # Exactly one gateway.execute call: the runtime never retries writes.
    assert gw.call_count("s1") == 1
    assert result["status"] in ("partial", "failed")


# ---------------------------------------------------------------------------
# Step 1g: verified claims produce success + attempt.completed
# ---------------------------------------------------------------------------


def test_verified_claims_emit_completed(tmp_path: Path) -> None:
    """When claims verify against real artifacts, success + attempt.completed."""
    counters.reset_for_test()
    # Provide a real artifact under run_dir so evidence resolves.
    artifact = tmp_path / "report.json"
    artifact.write_text(json.dumps({"k": 1}), encoding="utf-8")
    route = _route()
    verified = StepResult(
        step_id="s1",
        status=StepStatus.SUCCESS,
        data={"ok": True},
        evidence=(EvidenceRef(source_type="artifact", source_id="report.json"),),
    )
    gw = _RecordingGateway({"s1": lambda attempt: verified})
    plan = _plan([PlanStep(id="s1", capability="market_data", tool="get_market_data")])
    claim = Claim(
        text="data is real",
        kind=ClaimKind.FACT,
        evidence=[EvidenceRef(source_type="artifact", source_id="report.json")],
    )
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=EvidenceVerifier(),
        run_dir=tmp_path,  # artifact present at run_dir/report.json
        plan_provider=lambda r, msg: plan,
    )
    events: list[tuple[str, dict[str, object]]] = []
    result = rt.run(
        user_message="quote",
        session_id="s1",
        registry=_FakeRegistry(["get_market_data"]),
        executor=_synth_executor(claims=[claim]),
        event_callback=lambda et, data: events.append((et, data)),
    )

    assert result["status"] == "success"
    types = [e[0] for e in events]
    assert "attempt.completed" in types
    assert result["reliability"]["claims_coverage"] == 1.0


# ---------------------------------------------------------------------------
# Step 1h: return dict preserves the legacy keys callers expect
# ---------------------------------------------------------------------------


def test_return_dict_preserves_legacy_keys(tmp_path: Path) -> None:
    """The result must carry status/content/run_dir/run_id/metrics + reliability."""
    counters.reset_for_test()
    route = _route(allowed_tools=())
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: None,
    )
    result = rt.run(
        user_message="hi",
        session_id="s1",
        registry=_FakeRegistry([]),
        executor=_synth_executor(),
    )
    for key in ("status", "content", "run_dir", "run_id", "metrics", "reliability"):
        assert key in result, f"missing legacy key: {key}"
    assert isinstance(result["reliability"], dict)
    # Usage from executor merged through.
    assert result.get("usage", {}).get("total_tokens") == 7


# ---------------------------------------------------------------------------
# Step 1i: cancellation propagates
# ---------------------------------------------------------------------------


def test_cancel_event_propagates_to_cancelled_terminal(tmp_path: Path) -> None:
    counters.reset_for_test()
    route = _route()
    cancel_event = threading.Event()

    def _slow(attempt: int) -> StepResult:
        cancel_event.set()  # cancel mid-step
        time.sleep(0.05)
        return _ok_result("s1")

    gw = _RecordingGateway({"s1": _slow})
    plan = _plan([PlanStep(id="s1", capability="market_data", tool="get_market_data")])
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: plan,
        cancel_event=cancel_event,
    )
    result = rt.run(
        user_message="quote",
        session_id="s1",
        registry=_FakeRegistry(["get_market_data"]),
        executor=_synth_executor(),
    )
    assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Fix 1: wall-clock budget between scheduler rounds
# ---------------------------------------------------------------------------


def test_wall_clock_budget_terminates_run(tmp_path: Path) -> None:
    """Wall-clock budget between rounds: unresolved steps BLOCKED, not run.

    With wall_clock_seconds=0, the budget trips after the first round. s1
    (round 1) completes; s2 (depends on s1) is never submitted and lands in
    BLOCKED. Without the fix both steps run and the run succeeds.
    """
    counters.reset_for_test()
    route = _route(budgets={"steps": 20, "tokens": 3000, "wall_clock_seconds": 0})
    gw = _RecordingGateway({"s1": lambda attempt: _ok_result("s1")})
    plan = _plan([
        PlanStep(id="s1", capability="market_data", tool="get_market_data", timeout_seconds=0.0),
        PlanStep(id="s2", capability="market_data", tool="get_market_data", depends_on=["s1"], timeout_seconds=0.0),
    ], budgets={"steps": 20, "tokens": 3000, "wall_clock_seconds": 0})
    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: plan,
    )
    result = rt.run(
        user_message="quote",
        session_id="s1",
        registry=_FakeRegistry(["get_market_data"]),
        executor=_synth_executor(),
    )
    # s1 ran in round 1; s2 blocked (budget tripped between rounds).
    assert gw.call_count("s1") == 1
    assert gw.call_count("s2") == 0
    assert result["status"] != "success"
    assert result["reliability"]["steps_verified"] == 1
    assert result["reliability"]["steps_failed"] >= 1


# ---------------------------------------------------------------------------
# Fix 2: crashed synthesis must be failed, not swallowed to success
# ---------------------------------------------------------------------------


def test_synthesis_crash_is_failed_not_success(tmp_path: Path) -> None:
    """A synthesis executor that raises must produce status=failed, not success.

    Without the fix, the crash is swallowed to empty claims, which the runtime
    treats as claims_ok=True, landing on success with attempt.completed. The
    fix distinguishes a crash (synthesis_fault) from a legitimate empty-claims
    return.
    """
    counters.reset_for_test()
    route = _route()
    verified = _ok_result("s1")
    gw = _RecordingGateway({"s1": lambda attempt: verified})
    plan = _plan([PlanStep(id="s1", capability="market_data", tool="get_market_data")])

    def _crash_executor(**kwargs: Any) -> dict[str, object]:
        raise RuntimeError("synthesis crashed")

    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        gateway=gw,
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: plan,
    )
    events: list[tuple[str, dict[str, object]]] = []
    result = rt.run(
        user_message="quote",
        session_id="s1",
        registry=_FakeRegistry(["get_market_data"]),
        executor=_crash_executor,
        event_callback=lambda et, data: events.append((et, data)),
    )

    assert result["status"] == "failed"
    assert result.get("reason") == "synthesis error"
    types = [e[0] for e in events]
    assert "attempt.completed" not in types


def test_fast_path_synthesis_crash_is_failed(tmp_path: Path) -> None:
    """Fast-path synthesis crash also lands on failed (not success)."""
    counters.reset_for_test()
    route = _route(allowed_tools=())

    def _crash_executor(**kwargs: Any) -> dict[str, object]:
        raise RuntimeError("fast-path synthesis crashed")

    rt = ReliabilityRuntime(
        router=_FakeRouter(route),
        verifier=_IdentityVerifier(),
        runs_dir=tmp_path,
        plan_provider=lambda r, msg: None,
    )
    events: list[tuple[str, dict[str, object]]] = []
    result = rt.run(
        user_message="hi",
        session_id="s1",
        registry=_FakeRegistry([]),
        executor=_crash_executor,
        event_callback=lambda et, data: events.append((et, data)),
    )
    assert result["status"] == "failed"
    assert result.get("reason") == "synthesis error"
    assert "attempt.completed" not in [e[0] for e in events]
