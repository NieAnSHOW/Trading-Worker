# Task 7 Report — Integrate the reliability runtime behind a feature flag

## Status: DONE

## What was implemented

A validated operator env-var flag `VIBE_RELIABILITY_RUNTIME=off|shadow|enforce`
(default `off`) that gates the Agent Reliability Runtime onto the live session
path, with a binding guarantee that `off` is byte-for-byte the current behavior.

### Config flag (`agent/src/config/schema.py`)
- Added `get_reliability_runtime_mode()` — reads `VIBE_RELIABILITY_RUNTIME`
  once per call, validates against `{off, shadow, enforce}`, and **falls back
  to `off` with a warning on unknown/empty values (never raises)**. Mode
  constants exported as `RELIABILITY_RUNTIME_MODES`.
- No `config/loader.py` change was needed: the flag is an operator env var,
  not disk-config plumbing. This is the simplest path the existing config
  system supports (mirrors `ALLOW_SESSION_MCP_SERVERS`).

### Service branches (`agent/src/session/service.py`)
- `_run_with_agent` reads the mode at the top and **early-returns to
  `_run_with_reliability` only for `shadow`/`enforce`**. The entire `off` code
  block below the branch (registry build → AgentLoop construct → run → metrics)
  is **unchanged** — zero reliability code executes on the hot path when off.
- New `_run_with_reliability(mode, ...)`:
  - Builds the SAME registry + `AgentLoop` + `PersistentMemory` + event
    callback stack as the `off` path, then layers the runtime.
  - **shadow**: runs the AgentLoop (it owns the attempt), then runs the
    runtime as a silent observer (`_observe_reliability`) using a stub
    executor that returns the AgentLoop's own synthesis — **no duplicate
    provider/tool call** — and attaches the redacted `reliability` summary to
    the unchanged AgentLoop result.
  - **enforce**: the runtime owns the attempt. Fast-path rollout wires
    `plan_provider=None` (no LLM planner yet) and `allow_side_effects=False`
    so the gateway blocks writes. The AgentLoop remains the synthesis engine
    (called once; its output is reused by the runtime executor to avoid a
    duplicate LLM call). The runtime's terminal verdict is what the caller
    sees. A runtime **fault** (unexpected exception, not a legitimate
    verdict) falls back to the AgentLoop result — safe because
    `allow_side_effects=False` guarantees no side effect began.
- Two event callbacks: the AgentLoop keeps the full callback (mandate/live
  relays unchanged); the runtime gets `runtime_event_callback` that **drops
  `attempt.*` events** (the service's `_run_attempt` owns those) and forwards
  tool-lifecycle events — both stamp the same `attempt_id`.

### Cancel handle
- Added `self._active_cancels: Dict[str, threading.Event]` populated only on
  the enforce path. `cancel_current` now signals **both** the AgentLoop (if
  active) and the runtime cancel event (if active). Off/shadow still cancel
  via `_active_loops` exactly as today.

### `partial`-status handling (Task 6 handoff — resolved)
Confirmed in `service.py`: `_run_attempt` checks
`if result.get("status") == "success"` → `mark_completed`, else
`mark_failed`. The runtime's new `partial` status is therefore
**caller-compatible and maps to a non-success terminal** (`attempt.failed`,
never `attempt.completed`). No change to `_run_attempt`'s branch logic was
needed — the existing non-success branch already does the right thing. Pinned
by `test_partial_runtime_result_does_not_complete`.

### Tool-args resolution (Task 6 handoff — resolved, option a)
Picked **option (a)**: added an `arguments: dict[str, object]` field to
`PlanStep` (`agent/src/reliability/planner.py`) and changed
`ReliabilityRuntime._step_arguments` to return `step.arguments`
(`agent/src/reliability/runtime.py`). The service-supplied `plan_provider`
populates it. This is the minimal change that lets `enforce` mode drive real
tools with real arguments when a planner is wired.

For the Task 7 rollout the service wires `plan_provider=None` (fast path) —
no LLM planner exists yet, so the runtime does not itself execute tools via
the gateway; the AgentLoop remains the tool executor and the runtime grades
its output. A later task swaps in a real planner. The
`arguments`→`gateway`→`tool` path is exercised end-to-end by
`test_plan_step_arguments_flow_to_gateway`, which injects a `plan_provider`
returning a `PlanStep(arguments={"symbol": "AAPL"})` and asserts the tool
receives `{"symbol": "AAPL"}` through the real runtime + gateway.

## Files changed (commit `48b96170`)

| File | Change |
|---|---|
| `agent/src/config/schema.py` | + `get_reliability_runtime_mode()`, mode constants |
| `agent/src/reliability/planner.py` | + `PlanStep.arguments` field |
| `agent/src/reliability/runtime.py` | `_step_arguments` returns `step.arguments` |
| `agent/src/session/service.py` | mode branch, `_run_with_reliability`, cancel handle, observers |
| `agent/tests/test_session_service_reliability.py` | new (9 tests) |
| `agent/tests/test_session_service_mcp.py` | + off-default regression test |

`config/loader.py` was not modified (flag needs no validated-config plumbing).

## TDD evidence

**RED** (before implementation) —
`pytest tests/test_session_service_reliability.py -q`:
```
8 failed, 1 passed
```
The single pass was `test_off_mode_preserves_agentloop_path` (the off path was
already correct). All flag/shadow/enforce/tool-args assertions failed because
the branch did not exist.

**GREEN** (after implementation) —
`pytest tests/test_session_service_reliability.py -q`:
```
9 passed
```

## Step 5 regression output

Per the brief, `test_api_live_runtime.py` was **excluded** (its 13 failures
are pre-existing and controller-verified — confirmed independently: the file
still shows exactly `13 failed, 9 passed` on the committed state, unrelated to
session-service code). All other referenced files exist and pass:

```
pytest tests/test_session_service_reliability.py \
       tests/test_session_service_mcp.py \
       tests/test_session_events.py \
       tests/test_mandate_enforcement.py \
       tests/test_sdk_order_gate.py -q
→ 64 passed
```

Reliability-layer regression (Tasks 1–6) also re-run to confirm the
`PlanStep.arguments` + `_step_arguments` changes did not regress them:
```
pytest tests/test_reliability_*.py -q → 131 passed
```

## How each safety property was verified

| Property | Verification |
|---|---|
| **off unchanged** | `git diff` shows the off code block (from `llm = ChatLLM()` → `return result`) is byte-for-byte unchanged; only a mode read + early-branch added at the top. `test_off_mode_preserves_agentloop_path` + `test_off_default_runs_agentloop_without_reliability_summary` assert no `reliability` key and AgentLoop invoked once with the real args. |
| **no fallback after writes** | Enforce wires `allow_side_effects=False`; `test_enforce_blocks_side_effecting_tools` registers a `trading_*` side-effecting tool and asserts its `execute()` is never called by the reliability path. The fallback guard only fires on a runtime *exception*, and only because `allow_side_effects=False` makes a started write impossible. |
| **unverified never completes** | `test_partial_runtime_result_does_not_complete`: an AgentLoop result with unresolvable claims → runtime returns `partial` → `_run_attempt` emits `attempt.failed` (asserts NO `attempt.completed` in the terminal events). |
| **attempt_id preserved** | `test_enforce_events_carry_same_attempt_id`: drives `_run_attempt` in enforce mode and asserts every buffered event's `attempt_id` equals the single attempt id; lifecycle (`attempt.started` + terminal) both present. |
| **SSE contract** | Existing `tests/test_session_events.py` (54 tests incl. session-service + mandate + order-gate suite) all pass unchanged. Mandate/live relays flow through the AgentLoop's full event callback (unchanged). |
| **mandate / order-gate / kill-switch authoritative** | Untouched — the reliability runtime never intercepts those paths; live writes still flow AgentLoop → registry → trading tools → order gate. |

## Concerns

1. **Enforce fast-path does not gate the AgentLoop's own tool execution.** In
   the rollout posture the AgentLoop runs first and the runtime *grades* its
   output. Live writes remain gated by the unchanged mandate / order-gate /
   kill-switch surfaces, so safety is not weakened, but enforce mode's
   "ownership" is currently verification-grading ownership, not tool-execution
   ownership. A future task wires a real `plan_provider` so the runtime drives
   tools through the gateway (the `PlanStep.arguments` seam is already in
   place and tested).

2. **Shadow creates a throwaway run directory** per attempt under `runs_dir/`
   for evidence grading. Cost is one `mkdir` per shadow attempt; the redacted
   summary does not leak the path. Acceptable for an observer.

3. **Pre-existing working-tree changes** to `.superpowers/sdd/task-{3,4,5}-report.md`
   were present at task start and were deliberately **excluded** from this
   commit (not Task 7 scope).
