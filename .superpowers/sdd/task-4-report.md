# Task 4 Report — Tool Gateway validation, retry, and fallback policy

Status: **DONE**

## What was implemented

1. **`agent/src/reliability/gateway.py`** (new) — `GatewayPolicy` (Pydantic 2 `BaseModel`, fields verbatim from the brief) and `ToolGateway`. The gateway enforces, in order: unknown-tool reject → disallowed-tool reject → side-effect gate → conservative argument normalization → minimal JSON-Schema validation → typed exception classification → bounded **read-only** retry with exponential backoff → **read-only** fallback on `data_unavailable`/`provider_timeout`. Exception text is redacted via the existing `src.tools.redaction.redact_internal_paths` before any `ToolError` is emitted.
2. **`agent/src/agent/tools.py`** — added `side_effecting: bool = True` class attribute on `BaseTool` (conservative default) with documentation explaining the two-axis split from the existing `is_readonly` (which still governs the timeout/kill decision in `_invoke_tool`).
3. **`agent/src/tools/bash_tool.py`** — explicit `side_effecting = True` (redundant with the default and the namespace rule, but the brief names bash directly).
4. **`agent/src/agent/loop.py`** — added optional `gateway_invoke: Callable[[str, dict], StepResult]` parameter to `AgentLoop.__init__`; added a `_tool_execute` helper that the two execute sites inside `_invoke_tool` now call instead of `self.registry.execute` directly; added module-level `_step_result_to_tool_result` converter. When `gateway_invoke is None` the path is the legacy registry call.
5. **`agent/src/reliability/__init__.py`** — re-export `GatewayPolicy`, `ToolGateway`.
6. **`agent/tests/test_reliability_gateway.py`** (new) — 21 tests covering every Step-1 case in the brief plus the never-retry-writes property.

## Side-effecting classification — design, default, justification

`ToolGateway.execute()` determines whether `tool_name` is side-effecting via the module-level predicate `_is_side_effecting(tool_name, tool)`:

```
side_effecting = (
    tool_name in {"bash"}                         # name denylist
    or tool_name.startswith("trading_")           # namespace denylist
    or getattr(tool, "side_effecting", True)      # per-tool attr, default True
)
```

**Default when metadata is unavailable: side-effecting (True).** This is the conservative choice on the retry axis the brief requires: the gateway refuses to retry or fall back from anything it cannot prove is read-only.

**Why a namespace denylist on top of the attribute.** `trading_*` and `bash` are forced side-effecting *regardless* of the per-tool attribute. This is defense-in-depth: a mislabeled trading tool (someone sets `side_effecting = False` on `trading_place_order`) still cannot become retryable. The brief asked to "mark the live-trading tools and shell as side-effecting"; the namespace rule is that mark, expressed as a guarantee stronger than a per-file attribute edit (which would have touched ~8 trading tool files for no marginal safety gain). `bash` gets both the denylist *and* the explicit attribute.

**Relationship to the pre-existing `is_readonly`.** These are two independent axes:
- `is_readonly` (pre-existing, default `True`) governs the `_invoke_tool` timeout/kill decision (read-only tools may be cancelled on timeout; write tools are awaited). Its default is permissive on that axis because the write path is guarded by a watchdog that never kills.
- `side_effecting` (new, default `True`) governs gateway retry/fallback. Its default is conservative because retrying a write is catastrophic (double execution).

Reusing `is_readonly` for retry was rejected because its default (`True`) is the *unsafe* direction for retry, and because trading read-query tools correctly set `is_readonly = True` but must remain non-retryable.

## Files changed

- `agent/src/reliability/gateway.py` (new)
- `agent/tests/test_reliability_gateway.py` (new)
- `agent/src/agent/tools.py` (+9 lines: attribute + docstring)
- `agent/src/agent/loop.py` (+53 lines: param, converter, helper, two call-site swaps, one import)
- `agent/src/tools/bash_tool.py` (+1 line)
- `agent/src/reliability/__init__.py` (+3 lines: exports)

## TDD evidence

**RED** — initial run of `agent/tests/test_reliability_gateway.py` failed at collection:
```
ImportError: cannot import name 'gateway' from 'src.reliability'
```
(expected — module did not exist yet).

**GREEN** — after implementation, two legitimate failures surfaced and were fixed:
1. `TestFallback::test_fallback_invoked_on_data_unavailable_for_readonly` — `_inspect_envelope` originally honored `empty`/`data_unavailable` flags only on success envelopes; fix: honor them on any envelope so a `{"status":"error","empty":true}` primary triggers read-only fallback.
2. `TestRedaction::test_exception_text_redacted_in_error_message` — test used a synthetic path the redactor does not know; fix: embed the real `agent/` dir (a guaranteed internal root). Implementation was already correct.

Final: `21 passed in 1.72s`.

## Step 7 safety-regression output

```
pytest agent/tests/test_reliability_gateway.py \
       agent/tests/test_tool_timeout.py \
       agent/tests/test_tool_registry_security.py \
       agent/tests/test_mandate_enforcement.py \
       agent/tests/test_sdk_order_gate.py -q
→ 73 passed, 1 warning in 3.55s
```

All five referenced files exist; none was skipped. No live-write test executed a real broker call — `test_mandate_enforcement.py` and `test_sdk_order_gate.py` are unit tests by design, and the gateway never touches the broker path.

Broader sweep (reliability + loop + progress tests): `82 passed`.

## How the never-retry-writes property was verified

Three layers of evidence:

1. **Unit tests in `TestNeverRetryWrites`** (the most safety-sensitive class):
   - `test_write_tool_not_retried_even_when_error_is_retryable`: `bash` with `allow_side_effects=True` and `retry_limit=5` raises `TimeoutError` (normally retryable) → exactly ONE attempt; `tool.call_count == 1`.
   - `test_write_tool_not_fallen_back_even_if_configured`: `trading_place_order` (mislabeled `side_effecting=False`) returns `data_unavailable`, a fallback is configured, `allow_side_effects=True` → fallback is NOT invoked (`fallback.call_count == 0`), primary attempted once.
   - `test_unknown_exception_not_retried_for_readonly`: even for read-only tools, an unknown error (retryable=False) is not retried.
2. **Predicate unit check** (direct): `_is_side_effecting("trading_place_order", T(False)) is True`; `_is_side_effecting("bash", T(False)) is True`; `_is_side_effecting("get_price", T(False)) is False`; unknown tool object → `True`.
3. **Code-path reasoning**: in `_execute_with_recovery`, `read_only = not _is_side_effecting(tool_name, tool)`. Both the `while` retry loop and the fallback block are gated on `read_only`. A side-effecting tool makes `read_only=False`, short-circuiting both. The predicate's namespace rule cannot be overridden by the attribute, so a mislabeled trading tool is still classified side-effecting.

## AgentLoop wiring — behavior preservation

The gateway path is **opt-in** via `gateway_invoke=None` (default). When absent, `_tool_execute` returns `self.registry.execute(tool_name, args)` — the exact legacy call. The two execute sites in `_invoke_tool` (write path and readonly-worker path) both now call `self._tool_execute(tool_name, args)`. Verified end-to-end:
- Legacy direct path returns the registry result verbatim.
- Gateway success path forwards `StepResult.data` as the result string.
- Gateway error path produces `{"status":"error","error_code":...,"error":...,"repair_hint":...}` which `_is_tool_success` reports as `False`, so duplicate-success tracking, telemetry, and trace behave identically to a native tool failure.
- `test_tool_timeout.py` (which calls `_invoke_tool` directly with no gateway) passes unchanged → heartbeat/timeout/cancellation scaffolding is byte-for-byte preserved.

## Concerns

- **Retry/fallback is effectively dormant for real tools today.** Because `side_effecting` defaults to `True` and `retry_limit` defaults to `0`, no production tool is retried or fallen-back-from until (a) a tool author explicitly sets `side_effecting = False` AND (b) a caller passes a `GatewayPolicy` with `retry_limit > 0` / `fallback_tools`. This is the intended conservative posture (the brief makes retry opt-in), but it means the recovery paths are exercised only by the fake-tool unit tests so far. Enabling retry for a real read-only tool is a one-line attribute change in a follow-up.
- **No permanent `AgentLoop` integration test drives the gateway-callback path under heartbeat/timeout.** The wiring is verified by an inline script (legacy/gateway-success/gateway-error + predicate), and the legacy `_invoke_tool` timeout behavior is covered by `test_tool_timeout.py`, but there is no permanent test that drives `_invoke_tool` through a gateway callback under a tight timeout. The composition is straightforward (the callback replaces the inner `registry.execute` call; the outer heartbeat/timeout scaffolding is untouched), but a permanent test would be stronger evidence. Left out to keep the diff minimal per the brief's "smallest diff" instruction; can be added when a real policy is wired in a later task.
- **`allowed_tools: frozenset[str]`** is a required field on `GatewayPolicy` (no default) per the brief; callers must always supply it. Pydantic 2.12 coerces a `set`/`list`/`tuple` of strings to `frozenset` on construction. No deviation from the brief.

## Fix: fallback schema validation

**Finding (reviewer, Important).** `_try_fallback` called `_execute_once(fb_name, fb_tool, arguments, step_id)` without first validating `arguments` against the FALLBACK tool's schema. The primary path validates at `gateway.py:190`, but the fallback path skipped validation, so a fallback tool whose parameter shape differs from the primary's could receive invalid arguments — violating the global constraint "Invalid arguments NEVER reach the actual tool execute call."

**Change.** Added a `_validate_schema(fb_tool, arguments)` guard inside `_try_fallback` (after the side-effecting check, before `_execute_once`); non-None result returns `None` (no fallback applied), so the primary's recoverable error surfaces unchanged. Two lines added in `agent/src/reliability/gateway.py`; one test added in `agent/tests/test_reliability_gateway.py` (`TestFallback::test_fallback_not_executed_when_args_invalid_for_fallback_schema`) constructing a primary that requires `symbol` and returns `data_unavailable`, plus a fallback that requires a different field `ticker`.

**RED command + failing output:**
```
python -m pytest agent/tests/test_reliability_gateway.py::TestFallback::test_fallback_not_executed_when_args_invalid_for_fallback_schema -q
>       assert fallback.call_count == 0
E       assert 1 == 0
FAILED agent/tests/test_reliability_gateway.py::TestFallback::test_fallback_not_executed_when_args_invalid_for_fallback_schema - assert 1 == 0
1 failed in 1.46s
```

**GREEN command + passing output:**
```
python -m pytest agent/tests/test_reliability_gateway.py -q
......................                                                   [100%]
22 passed in 1.59s
```

**Full regression command + output:**
```
pytest agent/tests/test_reliability_gateway.py agent/tests/test_tool_timeout.py agent/tests/test_tool_registry_security.py agent/tests/test_mandate_enforcement.py agent/tests/test_sdk_order_gate.py -q
........................................................................ [ 97%]
..                                                                       [100%]
74 passed, 1 warning in 2.89s
```

**Commit:** `c017271f` — `fix: validate fallback tool schema before execute` (DCO `Signed-off-by:` trailer present; no AI-attribution trailers).
