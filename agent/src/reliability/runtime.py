"""ReliabilityRuntime: orchestrate the reliable research pipeline (Task 6).

The runtime wires together the components built in Tasks 2-5:

  * :class:`TaskRouter` maps a user message to a :class:`CapabilityRoute`.
  * A plan is obtained (default: fast path with no steps; or from a
    ``plan_provider`` callback) and validated by :class:`PlanValidator`.
  * Ready read steps are scheduled on a bounded ``ThreadPoolExecutor``;
    independent steps run concurrently, dependent steps wait on their
    dependencies. Every step executes through :class:`ToolGateway` (never
    via the registry directly) so retry / fallback / schema / safety policy
    applies.
  * Results are re-graded by :class:`EvidenceVerifier`.
  * Final synthesis runs ONLY with verified / partial evidence, via the
    ``executor`` callback. The runtime itself never calls an LLM.

Terminal-state policy (binding):
  * ``success`` only when every step verifies AND every claim is covered.
  * ``partial`` when some results verify but steps / budget / coverage are
    incomplete.
  * ``failed`` for runtime faults or unsafe blocked paths.
  * An unverified, success-LOOKING run never produces ``success`` and never
    emits ``attempt.completed``.

Safety: no side-effecting step is ever retried. The runtime enforces this
both structurally (gateway ``allow_side_effects=False`` by default) and
defensively (the scheduler never re-queues a ``PlanStep.side_effecting`` step,
even when the gateway reports a retryable error).

Design decision (plan source): the runtime does NOT include an LLM planner.
The plan comes from an optional ``plan_provider`` callback
``(route, user_message) -> ExecutionPlan | None``. When no provider is set
(or it returns ``None``), the runtime takes the fast path and invokes final
synthesis directly. Task 7 wires a real planner; this task keeps the seam
minimal and the tests inject the plan directly.

Budgets: a plan-wide step-count budget (``plan.budgets["steps"]`` or
``route.budgets["steps"]``) caps the total number of gateway execute calls
across all steps and retries. A persistently-failing read step is retried up
to ``PlanStep.retry_limit`` times, but never past the step-count budget — a
repeated failure trips the budget and the run terminates as ``partial`` /
``failed``.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event
from typing import Any, Callable

from src.reliability.contracts import (
    CapabilityRoute,
    ErrorCode,
    StepResult,
    StepStatus,
    ToolError,
)
from src.reliability.evidence import Claim, EvidenceVerifier
from src.reliability.gateway import GatewayPolicy, ToolGateway
from src.reliability.planner import ExecutionPlan, PlanStep, PlanValidator
from src.reliability.router import TaskRouter
from src.telemetry import counters

# Default per-run worker bound. ponytail: a small fixed pool is the standard
# tool; do not build a custom scheduler. Bump only if measured throughput needs it.
_DEFAULT_MAX_WORKERS = 4
_DEFAULT_STEP_BUDGET = 8


class ReliabilityRuntime:
    """Orchestrate routing, planning, gateway execution, and evidence verification."""

    def __init__(
        self,
        *,
        router: TaskRouter | None = None,
        planner: PlanValidator | None = None,
        verifier: EvidenceVerifier | None = None,
        gateway: ToolGateway | None = None,
        plan_provider: Callable[[CapabilityRoute, str], ExecutionPlan | None] | None = None,
        fallback_tools: dict[str, str] | None = None,
        allow_side_effects: bool = False,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        runs_dir: Path | None = None,
        run_dir: Path | None = None,
        cancel_event: Event | None = None,
    ) -> None:
        """Initialize the runtime.

        Args:
            router: Capability router (default ``TaskRouter``).
            planner: Plan validator (default ``PlanValidator``).
            verifier: Evidence verifier (default ``EvidenceVerifier``).
            gateway: Tool gateway. If None, built per run as
                ``ToolGateway(registry)``.
            plan_provider: Optional callback that proposes an ExecutionPlan
                from the route + user message. Returning None takes the fast
                path. If this constructor argument is itself None, the runtime
                also takes the fast path (no LLM planner lives here).
            fallback_tools: Optional ``primary -> fallback`` tool mapping,
                forwarded to every gateway policy.
            allow_side_effects: Forwarded to the gateway policy. False by
                default: the reliability runtime does not auto-run writes.
            max_workers: Thread-pool bound for concurrent step execution.
            runs_dir: Root for new run directories.
            run_dir: Optional explicit run_dir override (skips dir creation).
                Used when the caller has already prepared the run directory.
            cancel_event: Optional cancellation flag.
        """
        self._router = router or TaskRouter()
        self._planner = planner or PlanValidator()
        self._verifier = verifier or EvidenceVerifier()
        self._gateway = gateway
        self._plan_provider = plan_provider
        self._fallback_tools = dict(fallback_tools or {})
        self._allow_side_effects = allow_side_effects
        self._max_workers = max(1, int(max_workers))
        self._runs_dir = runs_dir
        self._run_dir_override = run_dir
        self._cancel_event = cancel_event or Event()
        # Local redacted counters mirrored into the global telemetry counters.
        self._phases: dict[str, int] = {}
        self._events: dict[str, int] = {}

    # -- public API ---------------------------------------------------------

    def run(
        self,
        *,
        user_message: str,
        session_id: str,
        registry: Any,
        executor: Callable[..., dict[str, object]],
        event_callback: Callable[[str, dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        """Run the reliability pipeline and return a result dict.

        The returned dict preserves the legacy ``status`` / ``content`` /
        ``reason`` / ``run_dir`` / ``run_id`` / ``metrics`` keys callers
        expect, plus a redacted ``reliability`` summary. Usage fields the
        executor returns are merged through unchanged.
        """
        emit = event_callback or (lambda _et, _data: None)
        self._phases.clear()
        self._events.clear()

        self._emit(emit, "attempt.started", {"session_id": session_id, "reliability": True})
        self._record_event("run_started")

        # 1. Route.
        t0 = time.perf_counter()
        tool_names = self._tool_names(registry)
        route = self._router.route(user_message, tool_names)
        self._record_phase("router", int((time.perf_counter() - t0) * 1000))

        run_dir = self._resolve_run_dir()
        step_budget = int(route.budgets.get("steps", _DEFAULT_STEP_BUDGET))

        # 2. Plan source (default: fast path).
        plan: ExecutionPlan | None = None
        if self._plan_provider is not None:
            t0 = time.perf_counter()
            plan = self._plan_provider(route, user_message)
            self._record_phase("planner", int((time.perf_counter() - t0) * 1000))

        # 3. Fast path: no plan steps -> synthesize directly.
        if plan is None or not plan.steps:
            return self._fast_path(
                route=route,
                user_message=user_message,
                session_id=session_id,
                run_dir=run_dir,
                executor=executor,
                emit=emit,
            )

        # 4. Validate the plan against the route.
        self._planner.validate(plan, route)

        # 5. Execute steps through the gateway (bounded, dependency-aware).
        gateway = self._gateway or ToolGateway(registry)
        ordered = self._execute_plan(
            plan=plan,
            route=route,
            gateway=gateway,
            session_id=session_id,
            step_budget=min(step_budget, int(plan.budgets.get("steps", step_budget))),
            emit=emit,
        )

        # 6. Verify evidence for each step result.
        verified: list[StepResult] = []
        for step in plan.steps:
            result = ordered.get(step.id)
            if result is None:
                continue
            verified.append(self._verifier.verify_step_result(result, run_dir=run_dir))

        # 7. Synthesize + terminal-state policy.
        return self._finalize(
            plan=plan,
            route=route,
            verified=verified,
            user_message=user_message,
            session_id=session_id,
            run_dir=run_dir,
            executor=executor,
            emit=emit,
        )

    # -- orchestration internals --------------------------------------------

    def _execute_plan(
        self,
        *,
        plan: ExecutionPlan,
        route: CapabilityRoute,
        gateway: ToolGateway,
        session_id: str,
        step_budget: int,
        emit: Callable[[str, dict[str, object]], None],
    ) -> dict[str, StepResult]:
        """Schedule ready steps concurrently; retry retryable reads; cap by budget.

        Returns a ``step_id -> StepResult`` map for every step that reached a
        terminal state (success, non-retryable failure, or budget collapse).
        Steps that never ran (e.g. a dependency failed) are absent and the
        caller treats them as unresolved.

        Dependency scheduling resolves against ``satisfied`` — the set of steps
        whose final outcome lets a dependent proceed (SUCCESS/PARTIAL) — not
        against every terminal state, so dependents of a failed step never run.
        Steps that already reached a terminal state are never re-offered
        (``_should_schedule`` alone only caps attempts for never-terminal steps).
        """
        completed: dict[str, StepResult] = {}
        satisfied: set[str] = set()  # final outcomes that satisfy dependents
        attempts: dict[str, int] = {s.id: 0 for s in plan.steps}
        total_attempts = 0
        cancelled = self._cancel_event
        run_start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            while True:
                if cancelled.is_set():
                    break
                ready = [
                    s for s in self._planner.ready_steps(plan, satisfied)
                    if s.id not in completed and self._should_schedule(s, attempts)
                ]
                if not ready:
                    break

                in_flight: dict[Any, PlanStep] = {}
                for step in ready:
                    if total_attempts >= step_budget:
                        break
                    total_attempts += 1
                    future = pool.submit(
                        self._run_step, step, gateway, route, session_id
                    )
                    in_flight[future] = step

                if not in_flight:
                    # Budget exhausted before any submission this round.
                    break

                for future in as_completed(in_flight):
                    step = in_flight[future]
                    try:
                        result = future.result()
                    except Exception:  # noqa: BLE001 - classify, don't crash the run
                        result = StepResult(
                            step_id=step.id,
                            status=StepStatus.RECOVERABLE_ERROR,
                            error=ToolError(
                                code=ErrorCode.UNKNOWN,
                                message="scheduler error",
                                retryable=False,
                            ),
                        )
                        self._record_event(f"scheduler_error:{step.id}")
                    attempts[step.id] = attempts.get(step.id, 0) + 1
                    self._emit(
                        emit,
                        "tool_progress",
                        self._progress_payload(step, result, attempts[step.id]),
                    )

                    if result.status in (StepStatus.SUCCESS, StepStatus.PARTIAL):
                        # Both satisfy dependents: the step ran and produced
                        # output; evidence grading is per-step and must not
                        # block a dependent that can consume the data.
                        completed[step.id] = result
                        satisfied.add(step.id)
                        self._record_event(
                            "step_success" if result.status is StepStatus.SUCCESS else "step_partial"
                        )
                    elif self._is_retryable_read(step, result, attempts[step.id]):
                        # Leave un-completed: ready_steps() will re-offer it.
                        self._record_event("step_retry")
                    else:
                        completed[step.id] = result
                        self._record_event(f"step_terminal:{result.status.value}")

                # Wall-clock budget between rounds (step.timeout_seconds is advisory
                # at the gateway level; the runtime enforces the run-level envelope).
                wall_budget = route.budgets.get("wall_clock_seconds")
                if wall_budget is not None and (time.perf_counter() - run_start) >= wall_budget:
                    break

                # Loop: ready_steps() recomputes with the new satisfied set.

        # Anything still unresolved (cancellation, budget collapse, or a failed
        # dependency) is recorded as blocked so the verifier sees a terminal state.
        for step in plan.steps:
            if step.id not in completed:
                reason = "cancelled" if cancelled.is_set() else "budget_or_dependency_unresolved"
                completed[step.id] = StepResult(
                    step_id=step.id,
                    status=StepStatus.CANCELLED if cancelled.is_set() else StepStatus.BLOCKED,
                    error=ToolError(
                        code=ErrorCode.CANCELLED if cancelled.is_set() else ErrorCode.BUDGET_EXCEEDED,
                        message=reason,
                        retryable=False,
                    ),
                )
                self._record_event(f"step_blocked:{step.id}")

        self._record_event("total_step_attempts", total_attempts)
        return completed

    def _should_schedule(self, step: PlanStep, attempts: dict[str, int]) -> bool:
        """True if ``step`` may still be scheduled this round.

        A side-effecting step is scheduled at most once (never retried). A read
        step is re-scheduled until it exhausts ``retry_limit``.
        """
        done = attempts.get(step.id, 0)
        if done == 0:
            return True
        if step.side_effecting:
            return False
        return done <= step.retry_limit

    def _is_retryable_read(
        self, step: PlanStep, result: StepResult, attempts_so_far: int
    ) -> bool:
        """True when a failed step may be re-queued for another attempt."""
        if step.side_effecting:
            return False
        if result.status is not StepStatus.RECOVERABLE_ERROR or result.error is None:
            return False
        if not result.error.retryable:
            return False
        # attempts_so_far counts the just-completed attempt. Allow one more
        # while we are still within retry_limit (1 + retry_limit total tries).
        return attempts_so_far <= step.retry_limit

    def _run_step(
        self,
        step: PlanStep,
        gateway: ToolGateway,
        route: CapabilityRoute,
        session_id: str,
    ) -> StepResult:
        """Execute one step through the gateway (the only execution path)."""
        if self._cancel_event.is_set():
            return StepResult(
                step_id=step.id,
                status=StepStatus.CANCELLED,
                error=ToolError(code=ErrorCode.CANCELLED, message="cancelled", retryable=False),
            )
        policy = GatewayPolicy(
            allowed_tools=frozenset(route.allowed_tools),
            retry_limit=0,  # ponytail: runtime owns retry budget; gateway still does fallback.
            fallback_tools=dict(self._fallback_tools),
            timeout_seconds=step.timeout_seconds,
            allow_side_effects=self._allow_side_effects,
        )
        t0 = time.perf_counter()
        result = gateway.execute(
            step.tool or "",
            self._step_arguments(step),
            step_id=step.id,
            policy=policy,
            session_id=session_id,
        )
        # Re-time if the gateway didn't populate elapsed (fake gateways may not).
        if result.elapsed_ms == 0:
            result = StepResult(
                step_id=result.step_id,
                status=result.status,
                data=result.data,
                error=result.error,
                evidence=result.evidence,
                provider=result.provider,
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
            )
        self._record_phase("tool", result.elapsed_ms)
        return result

    @staticmethod
    def _step_arguments(step: PlanStep) -> dict[str, object]:
        """Arguments for the step's tool call.

        Task 7 handoff (option a): the planner populates ``step.arguments``;
        the runtime forwards them verbatim. Empty when the tool accepts no
        required fields; schema validation at the gateway gate-keeps the rest.
        """
        return dict(step.arguments)

    # -- fast path + finalize ------------------------------------------------

    def _fast_path(
        self,
        *,
        route: CapabilityRoute,
        user_message: str,
        session_id: str,
        run_dir: Path,
        executor: Callable[..., dict[str, object]],
        emit: Callable[[str, dict[str, object]], None],
    ) -> dict[str, object]:
        t0 = time.perf_counter()
        synthesis = self._call_executor(
            executor,
            user_message=user_message,
            session_id=session_id,
            route=route,
            evidence=[],
            run_dir=str(run_dir),
        )
        self._record_phase("synthesis", int((time.perf_counter() - t0) * 1000))

        claims = self._coerce_claims(synthesis)
        coverage = self._verifier.coverage(claims, [])
        content = str(synthesis.get("content", ""))
        synthesis_fault = bool(synthesis.get("synthesis_fault"))

        if synthesis_fault:
            status = "failed"
            reason = "synthesis error"
        elif not claims or coverage >= 1.0:
            status = "success"
            reason = None
        else:
            status = "partial"
            reason = "unverified claims in fast-path synthesis"

        return self._build_result(
            status=status,
            content=content,
            reason=reason,
            run_dir=run_dir,
            route=route,
            steps_total=0,
            verified=[],
            coverage=coverage,
            synthesis=synthesis,
            emit=emit,
        )

    def _finalize(
        self,
        *,
        plan: ExecutionPlan,
        route: CapabilityRoute,
        verified: list[StepResult],
        user_message: str,
        session_id: str,
        run_dir: Path,
        executor: Callable[..., dict[str, object]],
        emit: Callable[[str, dict[str, object]], None],
    ) -> dict[str, object]:
        # Invoke final synthesis ONLY with verified/partial evidence.
        t0 = time.perf_counter()
        synthesis = self._call_executor(
            executor,
            user_message=user_message,
            session_id=session_id,
            route=route,
            evidence=verified,
            run_dir=str(run_dir),
        )
        self._record_phase("synthesis", int((time.perf_counter() - t0) * 1000))

        claims = self._coerce_claims(synthesis)
        coverage = self._verifier.coverage(claims, verified)
        content = str(synthesis.get("content", ""))

        success_count = sum(1 for r in verified if r.status is StepStatus.SUCCESS)
        partial_count = sum(1 for r in verified if r.status is StepStatus.PARTIAL)
        has_unsafe = any(r.status is StepStatus.UNSAFE_ERROR for r in verified)
        total_steps = len(plan.steps)
        all_steps_verified = success_count == total_steps
        claims_ok = (coverage >= 1.0) if claims else True

        if self._cancel_event.is_set():
            status = "cancelled"
            reason = "cancelled by user"
        elif has_unsafe:
            status = "failed"
            reason = "unsafe path blocked"
        elif bool(synthesis.get("synthesis_fault")):
            status = "failed"
            reason = "synthesis error"
        elif success_count and all_steps_verified and claims_ok:
            status = "success"
            reason = None
        elif success_count or partial_count:
            status = "partial"
            reason = self._partial_reason(verified, coverage, total_steps)
        else:
            status = "failed"
            reason = self._failed_reason(verified)

        return self._build_result(
            status=status,
            content=content,
            reason=reason,
            run_dir=run_dir,
            route=route,
            steps_total=total_steps,
            verified=verified,
            coverage=coverage,
            synthesis=synthesis,
            emit=emit,
        )

    # -- result assembly -----------------------------------------------------

    def _build_result(
        self,
        *,
        status: str,
        content: str,
        reason: str | None,
        run_dir: Path,
        route: CapabilityRoute,
        steps_total: int,
        verified: list[StepResult],
        coverage: float,
        synthesis: dict[str, object],
        emit: Callable[[str, dict[str, object]], None],
    ) -> dict[str, object]:
        success_count = sum(1 for r in verified if r.status is StepStatus.SUCCESS)
        failed_count = sum(
            1 for r in verified
            if r.status in (StepStatus.BLOCKED, StepStatus.RECOVERABLE_ERROR, StepStatus.UNSAFE_ERROR)
        )

        result: dict[str, object] = {
            "status": status,
            "content": content,
            "run_dir": str(run_dir),
            "run_id": run_dir.name,
            "metrics": {},
            "iterations": steps_total,
            "max_iterations": int(route.budgets.get("steps", _DEFAULT_STEP_BUDGET)),
            "reliability": {
                "intent": route.intent,
                "steps_total": steps_total,
                "steps_verified": success_count,
                "steps_failed": failed_count,
                "claims_coverage": round(coverage, 3),
                "phase_ms": dict(self._phases),
                "events": dict(self._events),
            },
        }
        if reason is not None:
            result["reason"] = reason
        # Merge executor usage / extra fields through (preserve legacy usage keys).
        for key, value in synthesis.items():
            if key in ("content", "claims", "status", "synthesis_fault"):
                continue
            result.setdefault(key, value)

        # Terminal events: attempt.completed ONLY on verified success.
        if status == "success":
            self._emit(
                emit,
                "attempt.completed",
                {"status": status, "run_id": result["run_id"], "reliability": result["reliability"]},
            )
        else:
            self._emit(
                emit,
                "attempt.failed",
                {"status": status, "reason": reason, "run_id": result["run_id"], "reliability": result["reliability"]},
            )
        self._record_event(f"terminal:{status}")
        return result

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _tool_names(registry: Any) -> list[str]:
        names = getattr(registry, "tool_names", None)
        if callable(names):
            names = names()
        if names is None:
            get_defs = getattr(registry, "get_definitions", None)
            if callable(get_defs):
                try:
                    return [d.get("name") for d in get_defs() if isinstance(d, dict)]
                except Exception:  # noqa: BLE001
                    return []
            return []
        return list(names)

    def _resolve_run_dir(self) -> Path:
        if self._run_dir_override is not None:
            rd = Path(self._run_dir_override)
            rd.mkdir(parents=True, exist_ok=True)
            return rd
        root = Path(self._runs_dir) if self._runs_dir else Path(".")
        root.mkdir(parents=True, exist_ok=True)
        name = f"rel-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        rd = root / name
        rd.mkdir(parents=True, exist_ok=True)
        return rd

    @staticmethod
    def _call_executor(executor: Callable[..., dict[str, object]], **kwargs: Any) -> dict[str, object]:
        try:
            out = executor(**kwargs)
        except Exception:  # noqa: BLE001 - synthesis crash is a runtime fault, not success
            return {"content": "", "claims": [], "synthesis_fault": True}
        if not isinstance(out, dict):
            return {"content": str(out), "claims": []}
        return out

    @staticmethod
    def _coerce_claims(synthesis: dict[str, object]) -> list[Claim]:
        raw = synthesis.get("claims") or []
        claims: list[Claim] = []
        for item in raw:
            if isinstance(item, Claim):
                claims.append(item)
        return claims

    def _progress_payload(self, step: PlanStep, result: StepResult, attempt: int) -> dict[str, object]:
        """Map a step result to an SSE ``tool_progress`` payload (frontend-compatible)."""
        return {
            "tool": step.tool or step.capability,
            "stage": result.status.value,
            "step_id": step.id,
            "attempt": attempt,
            "elapsed_ms": result.elapsed_ms,
            "reliability": {
                "status": result.status.value,
                "error_code": result.error.code.value if result.error is not None else None,
            },
        }

    def _record_phase(self, name: str, elapsed_ms: int) -> None:
        # Local mirror for the redacted summary; global counters feed telemetry.
        self._phases[name] = int(elapsed_ms)
        try:
            counters.record_reliability_phase(name, int(elapsed_ms))
        except Exception:  # noqa: BLE001 - telemetry must never break the run
            pass

    def _record_event(self, name: str, value: int = 1) -> None:
        self._events[name] = self._events.get(name, 0) + int(value)
        try:
            counters.record_reliability_event(name, int(value))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _emit(
        emit: Callable[[str, dict[str, object]], None],
        event_type: str,
        data: dict[str, object],
    ) -> None:
        try:
            emit(event_type, data)
        except Exception:  # noqa: BLE001 - event sink errors must not break the run
            pass

    @staticmethod
    def _partial_reason(verified: list[StepResult], coverage: float, total: int) -> str:
        success = sum(1 for r in verified if r.status is StepStatus.SUCCESS)
        if coverage < 1.0:
            return f"incomplete claim coverage ({round(coverage, 2)})"
        return f"partial step verification ({success}/{total} verified)"

    @staticmethod
    def _failed_reason(verified: list[StepResult]) -> str:
        codes = sorted({
            r.error.code.value for r in verified
            if r.error is not None and r.status is StepStatus.UNSAFE_ERROR
        })
        if codes:
            return f"unsafe blocked: {','.join(codes)}"
        return "no verified evidence produced"


__all__ = ["ReliabilityRuntime"]
