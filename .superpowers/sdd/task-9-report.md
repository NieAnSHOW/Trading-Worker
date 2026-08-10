# Task 9 Report — Reliability Replay Benchmarks, Fault Injection, and Rollout Docs

## What was implemented

Task 9 is the capstone of the Agent Reliability Runtime plan (Tasks 1-9).
This task delivered test infrastructure + operator documentation; it touched
NO production code.

Deliverables:

1. **`agent/tests/fixtures/reliability_cases.json`** — 11 deterministic replay
   cases (the brief's Step 1 list) in the brief's fixture format.
2. **`agent/tests/_reliability_replay_runner.py`** — minimal test helper that
   loads the JSON fixtures, wires fake router/gateway/executor/verifier
   components per case, and drives each case through
   `ReliabilityRuntime.run()`. Contains the fault-adapter table mapping
   `error_code -> StepResult` factory.
3. **`agent/tests/test_reliability_replay.py`** — parametrized replay tests
   asserting terminal status, redacted summaries, attempt-count budget,
   side-effect no-retry, evidence coverage, and absence of unsupported
   claims on unsafe paths.
4. **`agent/tests/test_reliability_faults.py`** — fault-injection tests that
   exercise each fault adapter in isolation (provider_timeout,
   data_unavailable, auth_required, invalid_argument, malformed_metric,
   partial_evidence, unsafe_side_effect, cancelled, side-effect-no-retry,
   unsupported_claim, verifier-never-upgrades).
5. **`docs/desktop/agent-reliability.md`** — operator documentation for
   `VIBE_RELIABILITY_RUNTIME` modes, error statuses, evidence semantics,
   cache behavior, replay fields, telemetry, troubleshooting, plus a plain
   statement of the `enforce` known-limitation (verification-grading slice).

`agent/src/telemetry/counters.py` was NOT modified — no new redacted fields
were needed.

## Replay cases (11 total, per the brief's Step 1)

| # | Case | Fault injected | Expected status |
|---|------|----------------|-----------------|
| 1 | `wrong_tool_prevention` | `unsafe_side_effect` | failed |
| 2 | `invalid_symbol` | `invalid_argument` | failed |
| 3 | `bad_date` | `invalid_argument` | failed |
| 4 | `provider_timeout_no_fallback` | `provider_timeout` | failed |
| 5 | `empty_provider_response` | `data_unavailable` | failed |
| 6 | `partial_financial_statement` | success-no-evidence (downgrades to PARTIAL) | partial |
| 7 | `budget_exhaustion` | `provider_timeout` (step_budget=2) | failed |
| 8 | `malformed_backtest_metrics` | `schema_mismatch` (missing metric field) | failed |
| 9 | `unsupported_claim` | success-no-evidence + claim with no backing | partial |
| 10 | `cancellation` | `cancelled` (flips cancel_event) | cancelled |
| 11 | `side_effect_no_retry` | `provider_timeout` on a side-effecting step | failed |

## Files changed

- `agent/tests/fixtures/reliability_cases.json` (new, 11 cases)
- `agent/tests/_reliability_replay_runner.py` (new, ~280 lines, test helper)
- `agent/tests/test_reliability_replay.py` (new, ~140 lines)
- `agent/tests/test_reliability_faults.py` (new, ~250 lines)
- `docs/desktop/agent-reliability.md` (new, ~230 lines)

No production code modified. No new dependencies.

## RED → GREEN evidence

The replay/fault tests passed on first run because the runner was implemented
alongside the tests (the brief permitted driving components directly rather
than through RED-first when seams are clean). The runner's fault-adapter
table was iterated once to fix two issues discovered during development:

1. The `_FakeRouter` was added so the fixture controls the route's
   capabilities (the real `TaskRouter` derives them from the prompt
   keywords, which conflicted with side-effecting step validation).
2. The readonly-capability check in `PlanValidator` rejects
   `side_effecting=True` steps in `general_research`/`market_data`/
   `fundamentals`/`news`/`symbol`; the fixture was adjusted to use
   `backtest` capability for side-effecting cases.

Final new-tests result:

```
$ pytest agent/tests/test_reliability_replay.py agent/tests/test_reliability_faults.py -q
........................................................................ [ 90%]
........                                                                 [100%]
80 passed in 1.86s
```

## Step 7 validation — full output

### 1. New replay/fault tests

```
$ pytest agent/tests/test_reliability_replay.py agent/tests/test_reliability_faults.py -q
........................................................................ [ 90%]
........                                                                 [100%]
80 passed in 1.72s
```

### 2. Safety-critical narrow tests

```
$ pytest agent/tests/test_sdk_order_gate.py agent/tests/test_mandate_enforcement.py -q
................................................                         [100%]
=============================== warnings summary ===============================
../../../../.pyenv/versions/3.11.0/lib/python3.11/site-packages/starlette/formparsers.py:12
  PendingDeprecationWarning: Please use `import python_multipart` instead.
    import multipart
-- Docs: https://docs.pytest.org/en/how-to/capture-warnings.html
48 passed, 1 warning in 1.91s
```

### 3. Broad suite (excluding e2e)

```
$ pytest --ignore=agent/tests/e2e_backtest \
         --ignore=agent/tests/test_e2e_harness_v2.py \
         --ignore=agent/tests/test_serve_open_flag.py \
         --ignore=agent/tests/test_telemetry_counters.py \
         --tb=short -q
76 failed, 5528 passed, 11 skipped, 61 warnings, 26 errors in 163.72s (0:02:43)
```

**Pre-existing failures analysis.** I stashed my changes and reran on clean
HEAD `48707bde`:

```
[on clean HEAD 48707bde, same exclusions]
76 failed, 5528 passed, 11 skipped, 61 warnings, 26 errors in 159.54s (0:02:39)
```

Identical. My changes introduce ZERO new failures and ZERO new errors.

The 13 `test_api_live_runtime.py` failures predicted by the brief are all
present:

```
$ pytest agent/tests/test_api_live_runtime.py --tb=no -q
13 failed, 9 passed, 7 warnings in 2.31s
```

The remaining 63 failures + 26 errors span `test_channels_api.py`,
`test_security_auth_api.py`, `test_settings_api.py`, `test_registry.py`,
`test_qveris_routes.py`, `test_upload_security.py`, and others — all
pre-existing on clean HEAD, none related to Task 9 (which adds test
infrastructure and docs only; no production code touched).

The two collection errors explicitly ignored
(`test_serve_open_flag.py`, `test_telemetry_counters.py`) are also
pre-existing on clean HEAD — verified by stashing.

**No regression from Task 9.**

### 4. Compile checks

```
$ python -m compileall -q agent/cli
OK_cli
$ python -m py_compile agent/api_server.py agent/mcp_server.py
OK_compile
```

## Documentation table of contents

`docs/desktop/agent-reliability.md`:

1. Modes and Rollout (`off` / `shadow` / `enforce`; rollback path; off-path safety)
2. Reliability Statuses and Error Codes (terminal statuses; step statuses; error code table)
3. Evidence Verification Semantics (what makes a claim verified; partial vs blocked; path traversal; recency)
4. Cache Behavior (bounded LRU; TTL; never caches side-effects; key normalization)
5. Benchmark and Replay Fields (fixture shape; 11-case corpus; redacted summary)
6. Telemetry (phase timings; event counts)
7. Troubleshooting (partial / failed / cancelled / cache)

The doc's header carries the prominent KNOWN LIMITATION that `enforce` is a
verification-grading slice, not a full tool-driving ownership, so operators
do not over-trust it.

## Concerns

1. **Broad-suite pre-existing failure volume.** The brief predicted 13
   pre-existing failures in `test_api_live_runtime.py`; the clean HEAD
   actually carries 76 pre-existing failures + 26 pre-existing collection
   errors across ~20 test files. None are caused by Task 9 (verified by
   stash-and-rerun), but the broad suite is in worse shape than the brief
   anticipated. The narrow safety suite and the new reliability tests are
   clean.

2. **Runner is fixture-data driven; the JSON carries a `setup` block.** The
   brief's minimal fixture format has 5 fields. I extended each case with a
   `setup` block (route capabilities, plan step, outcome name, retry budget)
   so the runner stays generic. Adding a new case is a JSON row plus, if it
   needs a new outcome shape, one entry in `_FAULT_OUTCOMES` or
   `_NAMED_OUTCOMES`.

## Final-review fix wave

Whole-branch review (commits 54b9394c..5581b2b4) fixes, one commit per logical group, on `feat/agent-loop`. RED→GREEN for Fixes A and C; B pinned with a new test.

### Fix A — enforce mode forwards the AgentLoop run_dir (merge blocker)
- **Change:** `agent/src/session/service.py` enforce branch now passes `run_dir=Path(agentloop_result["run_dir"])` into `ReliabilityRuntime(...)` (constructor param is `run_dir`, the explicit override consulted first by `_resolve_run_dir`); `runs_dir` kept as fallback. The runtime reuses the AgentLoop's directory instead of minting a fresh empty `rel-<ts>-<uuid>`, so `attempt.run_dir` / returned `run_id` point at the real artifacts (metrics loading etc. resolve).
- **RED:** `test_enforce_forwards_agentloop_run_dir` failed with `result["run_dir"]` = `.../runs/rel-1786342759035-5f900bc3` vs expected AgentLoop dir. **GREEN:** after fix, 10/10 in `test_session_service_reliability.py` (existing no-run_dir fallback test unaffected).
- Commit: `88f14f15`

### Fix B — enforce fault fallback marked un-graded
- **Change:** the `except Exception → return agentloop_result` fallback now sets `reliability.faulted = True` (creates the dict if absent, in-place like the shadow path does). Terminal `status`/`content` untouched. No dict-shape conflict found: the AgentLoop result never carries a non-dict `reliability`; the `isinstance` guard makes overwrite safe.
- **Test:** new `test_enforce_fault_fallback_marks_ungraded` (monkeypatches `ReliabilityRuntime.run` to raise; asserts status/content preserved and `faulted is True`). RED → GREEN.
- Commit: `fca3dcd7`

### Fix C — failed deps no longer schedule dependents
- **Change:** `_execute_plan` tracks a separate `satisfied` set (SUCCESS **and** PARTIAL — the step ran and produced data; per-step evidence grading must not block a dependent that can consume the output — decided explicitly and documented in `_execute_plan` + `ready_steps` docstrings). `ready_steps(plan, satisfied)` now receives the success-only set; `planner.py` param renamed `completed` → `satisfied`. Dependents of failed/blocked steps are never scheduled and end BLOCKED, matching the documented `blocked` = "budget or failed dependency" semantics.
- **Regression caught by existing tests:** `ready_steps` previously also excluded terminal steps (they were in `completed`); with the split, a non-retryable failure under `retry_limit=5` was re-offered 5 extra times (`test_fault_auth_required_is_not_retried`, `test_fault_invalid_argument_not_retried` failed with call_count 6). Fixed by excluding terminal steps at the call site (`s.id not in completed and self._should_schedule(...)`), preserving the old no-re-offer guarantee. Amended into the Fix C commit (no external references to the interim SHA).
- **RED:** `test_failed_dependency_never_schedules_dependent` failed with `call_count('s2') == 1`; **GREEN:** s2 never invoked, run `failed`, steps_failed 2 (s1 failed + s2 blocked). 42/42 runtime+planner tests; faults/replay suites green.
- Commit: `e67c3281`

### Fix D — docs off-path import claim corrected
- Docs now say no reliability CODE runs on the off path, but a few lightweight modules ARE imported for type contracts (`loop.py` imports `src.reliability.contracts` at module level, executing the package `__init__`), with a sentence requiring future reliability imports to stay dependency-light.
- Commit: `709a32ef`

### Fix E — reliability error-code docstring tightened
- `_extract_tool_error_code` docstring now says it extracts the envelope's `error_code` field — a bounded short string (≤60 chars) written by the tool layer, never user data — instead of the false "allowlisted codes only" claim. Docstring reword chosen over code tightening (a fixed allowlist could drop new codes). No behavior change.
- Commit: `f9d357d6`

### Fix F — hygiene (loop.py, contracts.py, context.py)
- `loop.py` `_extract_tool_error_code`: quick-reject `if '"status": "error"' not in result[:512]: return None` before `json.loads` on the success path (error envelopes always open with the status key; parse kept for the error path).
- `loop.py` lazy `from src.telemetry import counters` hoisted to module level — **cycle check done:** the telemetry package is counters-only, importing only stdlib (`threading`, `time`, `typing`); no cycle.
- `contracts.py` `_coerce_data`: `set`/`frozenset` → `sorted(..., key=str)` (deterministic, never raises on mixed types — the docstring's "never raises" contract is preserved) before the `str()` fallback.
- `context.py`: extracted one `_visible_tools()` helper for the two identical allowlist-filtering comprehensions (system-prompt builder + `_format_tool_descriptions`); clean extraction, same shape at both sites.
- Commit: `8071bbcd`

### Test outcomes
- Full required suite (reliability × 11 + session × 2 + loop/tool regressions + safety mandate/order-gate): **303 passed** (after the final state; Fix C interim caught by faults suite and fixed).
- Extra context/telemetry/loop-helper net: 82 + 9 passed.
- `py_compile` on service.py / runtime.py / loop.py / contracts.py / context.py (+ planner.py): OK.
- `test_api_live_runtime.py` not run (pre-existing failures, controller-verified).

### Skipped / noted
- Important #2 (per-tool `side_effecting` audit) and #3 (hard runtime timeout): deliberately NOT implemented (future planner-task preconditions). No `side_effecting` flags flipped.
- TRIAGE-listed Minor findings: untouched (adjudicated DEFER).
- Fix B dict shape: no interaction problem found, applied as specified.
- `docs/` is parent-ignored in `.gitignore` (tracked files still stage with `git add -f`); the docs commit required `-f` on add — harmless for an already-tracked file.
