# Task 6 Report: Orchestrate the reliability runtime

## Status: DONE

## What was implemented

`agent/src/reliability/runtime.py` — `ReliabilityRuntime.run(...)` wires together the five component modules built in Tasks 2–5:

1. **Route** the request via `TaskRouter` → `CapabilityRoute`.
2. **Build the plan** from an optional `plan_provider` callback (see design decision below). When no provider is registered (or it returns `None`), the runtime takes the fast path and invokes synthesis directly.
3. **Validate** the plan via `PlanValidator.validate(plan, route)`.
4. **Schedule ready steps** on a bounded `ThreadPoolExecutor(max_workers=4)`; independent steps run concurrently, dependent steps wait for their dependencies via `PlanValidator.ready_steps`. Every step executes through `ToolGateway.execute` — the runtime never calls `registry.execute` for a step.
5. **Verify evidence** via `EvidenceVerifier.verify_step_result(result, run_dir=run_dir)`.
6. **Synthesize** via the `executor` callback, invoked only with verified/partial evidence.
7. **Apply terminal-state policy** and emit SSE events.

`agent/src/reliability/__init__.py` — now exports `ReliabilityRuntime` (plus `Claim`, `ClaimKind`, `EvidenceVerifier`, which Task 5 had not re-exported).

`agent/tests/test_reliability_runtime.py` — 9 orchestration tests covering every brief assertion.

### Run signature (verbatim from the brief)

```python
def run(self, *, user_message: str, session_id: str, registry,
        executor: Callable[..., dict[str, object]],
        event_callback: Callable[[str, dict[str, object]], None] | None = None) -> dict[str, object]: ...
```

### Constructor seam (not constrained by the brief)

```python
ReliabilityRuntime(*, router=None, planner=None, verifier=None, gateway=None,
                   plan_provider=None, fallback_tools=None, allow_side_effects=False,
                   max_workers=4, runs_dir=None, run_dir=None, cancel_event=None)
```

`router` / `planner` / `verifier` / `gateway` default to the real components; tests inject fakes. `gateway=None` means the runtime builds `ToolGateway(registry)` per run.

## Plan-source design decision

**The runtime does NOT include an LLM planner.** The plan comes from an optional `plan_provider: (route, user_message) -> ExecutionPlan | None` callback on the constructor. When no provider is set or it returns `None`, the runtime takes the fast path and calls `executor` directly with empty evidence.

Justification:
- The brief's hard constraint is "the runtime orchestrates; it does not itself call an LLM beyond the executor hook." An LLM-based planner would violate this.
- The brief's tests inject FAKE router/planner/gateway/executor/verifier and must control plan structure (dependencies, side-effects, retry limits). A route-derived default plan cannot express dependencies or side-effects, so it cannot satisfy the tests. The `plan_provider` seam is the minimal surface that lets tests drive the plan while keeping the runtime planner-free.
- Task 7 (integration) wires a real planner by supplying a `plan_provider`; the runtime's core does not change.

This is the minimal coherent option. The default (`plan_provider=None`) = fast path, so a runtime constructed without a planner never orchestrates blindly.

## Real return-dict keys preserved

The result dict matches the shape `AgentLoop.run` / `SessionService` produce (verified against `agent/src/agent/loop.py:1156` and `agent/src/session/service.py:165`):

- `status` — `"success" | "partial" | "failed" | "cancelled"` (`"partial"` is new; SessionService treats non-success as `mark_failed`, which is compatible).
- `content` — synthesis text from the executor.
- `reason` — present on non-success terminals (matches issue #114).
- `run_dir`, `run_id` — the run directory path and name.
- `metrics` — `{}` (SessionService loads metrics.csv separately; the runtime leaves this empty for the caller to fill).
- `iterations`, `max_iterations` — steps executed vs. the route step budget.
- `reliability` — the new redacted summary (see below).
- Usage fields the executor returns are merged through via `result.setdefault(key, value)` (so `usage`, `llm_usage`, etc. pass through unchanged).

### Reliability summary (redacted)

```python
"reliability": {
    "intent": route.intent,            # short code
    "steps_total": int,
    "steps_verified": int,
    "steps_failed": int,
    "claims_coverage": round(coverage, 3),
    "phase_ms": {phase: elapsed_ms},   # numeric only
    "events": {event_name: count},     # short-code keys, numeric values
}
```

No prompts, arguments, credentials, or response text appear. Phase timings and event counters are recorded both locally (for this summary) and via Task 1's `counters.record_reliability_phase` / `record_reliability_event` (for the `/telemetry` endpoint). The local mirror is used in the summary so the runtime does not consume the global snapshot delta.

## Files changed

- `agent/src/reliability/runtime.py` (new, 671 lines incl. docstrings/comments)
- `agent/src/reliability/__init__.py` (added `ReliabilityRuntime`, `EvidenceVerifier`, `Claim`, `ClaimKind` exports)
- `agent/tests/test_reliability_runtime.py` (new, 9 tests)

## TDD evidence

**RED** — `test_reliability_runtime.py` collection failed with `ModuleNotFoundError: No module named 'src.reliability.runtime'` before implementation.

**GREEN** — after implementation + fixing the side-effecting-step test to use a side-effect-capable capability (`backtest`, since `market_data` is readonly and correctly rejected by `PlanValidator`):

```
9 passed in 2.15s
```

## Step 5 regression output

```
pytest tests/test_reliability_runtime.py tests/test_agent_loop_terminal_state.py \
       tests/test_agent_loop_stream_retry.py tests/test_api_live_runtime.py -q
```

Result: **39 passed, 13 failed**. All 13 failures are in `test_api_live_runtime.py` and are **PRE-EXISTING** — I verified by `git stash` + re-run on clean HEAD `20efc248`: the same 13 `test_api_live_runtime.py` tests fail identically without my changes (`13 failed, 9 passed`). My task did not touch live-runtime code; those failures are unrelated to Task 6.

The three files that matter for this task all pass:
- `test_reliability_runtime.py` — 9 passed
- `test_agent_loop_terminal_state.py` — 17 passed
- `test_agent_loop_stream_retry.py` — 13 passed

Full reliability suite (sibling modules) also unaffected: `128 passed` across contracts / router / planner / gateway / evidence / telemetry / runtime.

## How each binding property was verified

### Terminal-state policy

- **`success` only on verified claims/artifacts**: `_finalize` sets `success` only when `success_count == total_steps` AND `coverage >= 1.0` (when claims exist) AND no unsafe step. `test_verified_claims_emit_completed` proves a run with a resolving artifact + a verifiable claim emits `attempt.completed`; `test_unverified_claim_is_partial_and_emits_no_completed` proves the opposite.
- **`partial` when incomplete**: `partial` when some steps verify (`success_count or partial_count`) but coverage/steps are incomplete. `test_repeated_failure_trips_step_budget` and `test_side_effecting_step_is_never_retried` both land here or at `failed`.
- **`failed` for runtime faults / unsafe blocked paths**: `_finalize` sets `failed` when `has_unsafe` or when no step verified.
- **Unverified success-LOOKING run never produces `success` and never emits `attempt.completed`**: the verifier downgrades SUCCESS-with-unresolvable-evidence to BLOCKED/PARTIAL before synthesis. `_build_result` emits `attempt.completed` **only** when `status == "success"`. Asserted directly by `test_unverified_claim_is_partial_and_emits_no_completed`.

### Never-retry-writes

Enforced at two layers:
1. **Gateway policy**: the runtime constructs `GatewayPolicy(allow_side_effects=self._allow_side_effects)` with default `False`, so the gateway rejects side-effecting tools with `UNSAFE_ERROR` before execution.
2. **Scheduler**: `_should_schedule` returns `False` for any `side_effecting` step after its first attempt, and `_is_retryable_read` returns `False` for side-effecting steps. So even when `allow_side_effects=True` (the path `test_side_effecting_step_is_never_retried` exercises) and the gateway reports a retryable error, the runtime calls `gateway.execute` exactly once for that step.

### Budget enforcement

The plan-wide step-count budget (`min(route.budgets["steps"], plan.budgets["steps"])`) caps the total number of `gateway.execute` calls across all steps and retries. `total_attempts` is incremented before each submission; when it reaches the budget, the scheduler stops submitting and marks unresolved steps `BLOCKED` (`BUDGET_EXCEEDED`). `test_repeated_failure_trips_step_budget` proves a step with `retry_limit=10` and budget `3` is attempted at most 3 times.

### Cancellation propagation

The scheduler checks `cancel_event.is_set()` at the top of each round; `_run_step` checks it before invoking the gateway. On cancellation, unresolved steps become `CANCELLED` and the terminal status is `cancelled`. `test_cancel_event_propagates_to_cancelled_terminal` proves this.

### Gateway-only execution

`_run_step` is the only place a step is executed, and it calls `gateway.execute(...)`. The runtime never calls `registry.execute` for a step.

## Commit

```
298719c0 feat: orchestrate reliable research execution
```

DCO trailer present; no AI-attribution trailers. Only the three task files were committed; pre-existing uncommitted edits to `.superpowers/sdd/task-{3,4,5}-report.md` (left over from earlier SDD sessions) were intentionally left unstaged.

## Concerns

1. **Step arguments**: the runtime passes empty arguments to `gateway.execute` (`_step_arguments` returns `{}`). This is correct for the Task 6 tests (all tools have no required fields) but means that, in production, Task 7 must either extend `PlanStep` to carry arguments or have `plan_provider` return steps whose tools accept no required fields. This is the intended seam; documented in `_step_arguments`.
2. **`partial` is a new status value** not produced by `AgentLoop`. Callers that switch on `status == "success"` (e.g. SessionService's `mark_completed` check) treat `partial` as non-success, which is correct, but any caller that asserts `status in ("success", "failed", "cancelled")` would need updating. Task 7 should confirm no such caller exists.
3. **Pre-existing `test_api_live_runtime.py` failures (13)** are unrelated to this task but will surface in any broad regression run; they predate this branch's HEAD.

## Fix: wall-clock budget + synthesis fault

Two Important findings from the task review (binding invariants all held; these close gaps).

### Fix 1: enforce wall-clock budget between scheduler rounds

**Problem:** `PlanValidator` validates a `wall_clock_seconds` budget, but `_execute_plan` never checked elapsed time during execution. A long-running tool would pass, then the scheduler would keep scheduling more rounds.

**Change:** 4 lines in `_execute_plan` — `run_start = time.perf_counter()` before the loop, and a budget check at the bottom of each round (after `as_completed`, before the loop iterates). When `elapsed >= route.budgets["wall_clock_seconds"]`, the loop breaks; the existing unresolved-step path tags remaining steps `BLOCKED` (`BUDGET_EXCEEDED`). Skipped when the route has no `wall_clock_seconds` key, matching how steps/tokens are skipped when omitted.

**RED:** `test_wall_clock_budget_terminates_run` — `wall_clock_seconds=0`, two dependent steps. Without the fix, both run (s2 `call_count == 1`); with the fix, s2 is never submitted (`call_count == 0`) and the status is not `success`.

**GREEN:** after the fix, `gw.call_count("s2") == 0`, `steps_verified == 1`, `steps_failed >= 1`.

### Fix 2: crashed synthesis is `failed`, not swallowed to `success`

**Problem:** `_call_executor` caught all exceptions and returned `{"content": "", "claims": []}` — indistinguishable from a legitimate empty-claims synthesis. `_finalize`/`_fast_path` treated empty claims as `claims_ok = True`, landing on `status="success"` and emitting `attempt.completed` for a run whose synthesis crashed.

**Change:** `_call_executor` now returns `{"content": "", "claims": [], "synthesis_fault": True}` on crash. `_finalize` and `_fast_path` check the flag and set `status="failed"`, `reason="synthesis error"` (before the success-eligibility checks). `_build_result` skips the internal flag in the merge-through so it doesn't leak into the result dict. The defensive catch remains — `run` never raises.

**RED:** `test_synthesis_crash_is_failed_not_success` (plan path) and `test_fast_path_synthesis_crash_is_failed` (fast path) — executor raises `RuntimeError`. Without the fix, both assert `status == "failed"` but get `"success"`.

**GREEN:** after the fix, both assert `status == "failed"`, `reason == "synthesis error"`, and `attempt.completed` is NOT emitted. The legitimate empty-claims fast path (`test_simple_request_takes_fast_path`) and plan path (`test_independent_steps_concurrent_and_dependent_waits`, `test_retryable_failure_follows_gateway_fallback`) still return `success`.

### Regression

```
pytest agent/src/reliability agent/tests/test_reliability_runtime.py \
       agent/tests/test_agent_loop_terminal_state.py \
       agent/tests/test_agent_loop_stream_retry.py -q
→ 42 passed, 1 warning in 5.68s
```

`test_api_live_runtime.py` excluded (13 pre-existing failures, unrelated).

