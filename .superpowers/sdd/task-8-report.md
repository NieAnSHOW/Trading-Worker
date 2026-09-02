# Task 8 Report — Python Gateway Bridge

Commit: `8bbe2bab` `feat(pi-sidecar): gateway bridge with idempotency + parallel reads/serial writes` (DCO sign-off, no AI trailers)

## What was implemented

`agent/src/pi_sidecar/gateway_bridge.py` (204 lines, new) + `agent/tests/pi_sidecar/test_gateway_bridge.py` (216 lines, new), per the task-8 brief:

- `build_gateway_policy(allowed_names)` — cross-task contract: `GatewayPolicy(allowed_tools=..., retry_limit=2, fallback_tools={}, timeout_seconds=1800.0, allow_side_effects=True)`.
- `GatewayBridge(registry, *, gateway=None, max_parallel_reads=4, now=time.monotonic)` — the host-side `tool_invoke` handler:
  - **Idempotency**: key = `idempotencyKey` (`session_id:assistantEntryId:toolCallId`); completed duplicates replay the recorded `{content, isError, outcome}` without re-executing. Error and `outcome_unknown` results are recorded too, so a re-sent write call returns the unknown envelope (never re-executes, prompting state verification per Global Constraint).
  - **Concurrency**: read-only calls bounded by `asyncio.Semaphore(max_parallel_reads)` + `run_in_executor` (parallel); side-effecting calls under `asyncio.Lock` (strictly serial). Side-effecting calls are never auto-retried — retry/fallback only flow through the gateway's existing read-only recovery (`retry_limit=2` in the policy).
  - **Routing**: the only execution path is `gw.execute(tool_name, args, step_id=..., policy=build_policy(), session_id=...)` — every tool execution passes through `ToolGateway` (mandate/order-gate/kill-switch/safety classification untouched; no bypass). `ToolGateway(registry)` is built lazily when no gateway is injected.
  - **Failure semantics**: write-class (`side_effecting=True`) connection/process failure, timeout, or cancellation → `outcome:"outcome_unknown"`, `isError:true` (never fabricated); read-only → `outcome:"error"`. Timeout enforced via `asyncio.wait_for` with the manifest's `timeout_seconds`.
  - **Output hygiene**: `StepResult`/dict → `to_wire()` → `redact_payload()` (src.tools.redaction) → JSON, truncated to `TOOL_RESULT_LIMIT` = 10000 chars with `[truncated]` marker, before returning to Pi.
  - `handle_cancel(call_id)` — no-op on unknown call ids (interface contract; `_pending` wiring per plan).
  - Manifest fields (`is_readonly`/`side_effecting`/`repeatable`/`timeout_seconds`) consumed from `tool_invoke` params, with registry-derived fallback via `_tool_meta`.

## TDD evidence

**RED** (`pytest agent/tests/pi_sidecar/test_gateway_bridge.py --tb=short -q`):

```
agent/tests/pi_sidecar/test_gateway_bridge.py:6: in <module>
    from src.pi_sidecar.gateway_bridge import GatewayBridge
E   ModuleNotFoundError: No module named 'src.pi_sidecar.gateway_bridge'
=========================== short test summary info ============================
ERROR agent/tests/pi_sidecar/test_gateway_bridge.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

(Matches the brief's expected RED exactly.)

**GREEN** (`pytest agent/tests/pi_sidecar -q`): `33 passed` (10 new + 23 existing). Scoped run: `10 passed in 0.48s`. Stability: 3 consecutive reruns all `10 passed`. Mutation check: removing the write lock flips the concurrency test to `assert 2 == 1` (fails); restored → green — the tests verify real serialization, not mocks.

**Lint**: `ruff check agent/src/pi_sidecar agent/tests/pi_sidecar` → `All checks passed!`. Full-tree `ruff check agent/src agent/tests` shows 297 pre-existing errors — identical count with my changes stashed (baseline), zero findings from my two files.

**Safety narrow tests** (must stay green): `pytest agent/tests/test_sdk_order_gate.py agent/tests/test_mandate_enforcement.py -q` → `48 passed`. Order gate / mandate / kill switch untouched.

## Files changed

- `agent/src/pi_sidecar/gateway_bridge.py` (create)
- `agent/tests/pi_sidecar/test_gateway_bridge.py` (create)

Nothing else touched (pre-existing modified `task-1..6-report.md` files belong to other tasks; left alone).

## Deviations from plan/brief (with reasons)

1. **Unknown-tool pre-check guarded by `self._registry is not None`** (impl). The brief's draft code returned `unknown tool` whenever `registry.get()` → None — which fires for *every* call when the registry is None, and 9 of the 10 brief tests (plus the plan's own Task 15 integration tests at plan lines 6067/6087/6397, e.g. `GatewayBridge(None, gateway=CountingGateway())`) construct the bridge with `registry=None` and require gateway delegation. Verbatim code could pass at most 2/10 tests. Resolution: when the bridge has a registry it fast-fails unknown tools (test 8); when it has no registry it defers to the gateway — which is the mandated single choke point and itself rejects unregistered tools (`ToolGateway._lookup` → UNKNOWN StepResult). This satisfies the Global Constraint "所有工具调用必须穿过 ToolGateway.execute" and keeps the plan's downstream tests coherent.
2. **`test_unknown_tool_is_error` fixture uses the plan's own unused `make_registry({})` helper** (test) instead of `registry=None`. The test's intent — unknown tool rejected with `"unknown tool"` in content, gateway never invoked (`NoopGateway` asserts not-called) — is preserved; with `registry=None` the bridge legitimately cannot know the tool is unknown, so the fixture now supplies an empty registry. This is the only way to keep the test meaningful alongside tests 1–7/9.
3. **Concurrency test metric isolated per class** (test). The brief's `active["n"]` counter is shared by reads and writes, so `max_write_overlap` is polluted by the 3 concurrent reads (observed `4` = 3 reads + 1 write with a *correctly working* lock). The design explicitly allows reads to overlap a write (semaphore + independent write lock), so `max_write_overlap == 1` is only expressible with a dedicated write counter. Now `max_write_overlap` counts only write-write overlap (1 with lock, 2 without — mutation-verified).
4. **Removed unused `import json` and `import pytest` from the test file** (F401) and **unused `started = self._now()` from the impl** (F841) — the brief's step 5 requires ruff-clean; these lines are dead in the draft.
5. **Test count is 10, not 11** (brief step 4 says "11 passed"). The specified test file contains exactly 10 test methods (8 + 2). Plan miscount, recorded; all 10 pass.

## Self-review findings

- Safety re-checked: the only execution path is `gw.execute(...)`; idempotency replay returns a *recorded* gateway result (no re-execution); unknown-tool rejection happens before execution; `side_effecting` from params only selects scheduling/outcome semantics, never relaxes gateway classification (gateway's `_is_side_effecting` + `allow_side_effects` untouched).
- `_tool_meta` defaults `side_effecting=True` for tools missing the attribute (conservative → serialized as write); production params always carry manifest values, so the fallback is a safety net.
- No scope creep: `_pending`/`handle_cancel` kept verbatim as the cross-task interface (no-op today); no extra features added.
- Test quality: collaborator fakes only — idempotency, concurrency, exception mapping, truncation, redaction, and unknown-tool rejection all exercise real bridge logic (mutation-verified for the lock). The bridge↔real-ToolGateway contract (policy/step_id/session_id passthrough) is covered by the plan's Task 15 integration test, out of scope here.

## Concerns

- `asyncio.CancelledError` is converted to a result envelope rather than re-raised (brief verbatim; design §Idempotency: user-cancel → outcome_unknown for writes). Callers must not rely on cancellation propagation through `handle_invoke`.
- `run_in_executor` work cannot be interrupted by `wait_for` timeout; a timed-out write may still complete in the background thread — inherent to the design, and the reason `outcome_unknown` requires state verification rather than fabricating a result.
