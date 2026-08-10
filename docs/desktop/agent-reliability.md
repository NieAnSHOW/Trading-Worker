# Agent Reliability Runtime — Operator Guide

The Agent Reliability Runtime adds a deterministic verification + recovery
layer over the existing AgentLoop. It grades tool outputs against on-disk
evidence, retries or falls back on transient provider faults, and refuses to
trust synthesis that lacks backing evidence.

This runtime is **off by default**. The legacy AgentLoop path is unchanged
when it is off. Rolling forward is a single environment variable; rolling
back is the same variable set to `off`.

> **Known limitation — `enforce` is a verification-grading slice.** In the
> current release, `enforce` mode does NOT take ownership of tool driving.
> The AgentLoop still owns tool calls; the runtime grades the AgentLoop's
> synthesis output and may veto an unverified result. Full tool-driving
> ownership via a real LLM planner is a future follow-up. Do not over-trust
> `enforce` to prevent every wrong-tool path — the gateway gates (allowed
> tools, side-effect refusal) still do that work today.

---

## Table of Contents

1. [Modes and Rollout](#modes-and-rollout)
2. [Reliability Statuses and Error Codes](#reliability-statuses-and-error-codes)
3. [Evidence Verification Semantics](#evidence-verification-semantics)
4. [Cache Behavior](#cache-behavior)
5. [Benchmark and Replay Fields](#benchmark-and-replay-fields)
6. [Telemetry](#telemetry)
7. [Troubleshooting](#troubleshooting)

---

## Modes and Rollout

The runtime is controlled by one environment variable:

```
VIBE_RELIABILITY_RUNTIME=off|shadow|enforce
```

Default when unset / empty / unknown: `off`. Unknown values log a warning and
fall back to `off` — the runtime never raises on a bad mode.

| Mode | What it does | What changes for the user |
|------|--------------|---------------------------|
| `off` (default) | Legacy AgentLoop path runs untouched. No reliability code on the hot path. | Identical to pre-runtime behavior. |
| `shadow` | The AgentLoop owns the attempt. The runtime runs as an observer, recording route + verification decisions WITHOUT duplicating provider calls and WITHOUT replacing the result. | Output carries an extra `reliability` summary. Terminal status is the AgentLoop's. |
| `enforce` | The runtime owns the attempt. It grades the AgentLoop's synthesis output and its terminal verdict is what the caller sees. A runtime fault (unexpected exception, not a legitimate verdict) falls back to the AgentLoop result — safe because `allow_side_effects=False` means no side effect can have begun. | Terminal status may be `partial` / `failed` where the AgentLoop alone would have returned `success`. |

### Rollback

To roll back to legacy behavior:

```bash
unset VIBE_RELIABILITY_RUNTIME
# or
export VIBE_RELIABILITY_RUNTIME=off
```

Restart the API / desktop sidecar afterwards. No data migration, no schema
change. The runtime writes no persistent state of its own.

### Off-path safety

When `mode == "off"`, no reliability CODE runs on the attempt path — the
service builds the AgentLoop directly and returns. This is enforced by a
single early-return branch in `SessionService._run_attempt`. No feature flag,
no hidden behavior. (A few lightweight reliability modules ARE imported for
type contracts — `agent/src/agent/loop.py` imports `src.reliability.contracts`
at module level, which executes the reliability package `__init__.py` — but
they are never invoked and their imports must stay dependency-light so the off
path cannot be regressed by a future heavy import.)

---

## Reliability Statuses and Error Codes

### Terminal statuses (top-level `status` field)

| Status | Meaning |
|--------|---------|
| `success` | Every plan step verified AND every synthesis claim is backed by successful evidence. Emits `attempt.completed`. |
| `partial` | Some evidence verified, but coverage is incomplete (unverified claims, partial step verification, missing evidence). Does NOT emit `attempt.completed`. |
| `failed` | No verified evidence produced, OR an unsafe path was blocked, OR synthesis crashed. Does NOT emit `attempt.completed`. |
| `cancelled` | The user (or a scheduler) set the cancel_event. |

### Step statuses (`reliability.steps_*` counters, per-step `tool_progress.stage`)

| StepStatus | Meaning |
|------------|---------|
| `success` | The gateway executed the tool and the verifier resolved its evidence. |
| `partial` | The step produced output but its evidence could not fully resolve (e.g. some refs missing). |
| `recoverable_error` | A retryable or non-retryable tool error. The error code is in `error.code`. |
| `blocked` | The step's evidence could not resolve at all, OR the step never ran (budget or failed dependency). |
| `unsafe_error` | The gateway refused a side-effecting tool. The run will land on `failed` with `reason="unsafe path blocked"`. |
| `cancelled` | The step was cancelled mid-execution. |

### Error codes (`error.code` on a failed `StepResult`)

| ErrorCode | Retryable | Cause |
|-----------|-----------|-------|
| `invalid_argument` | No | Schema validation failed, missing required field, bad type. |
| `ambiguous_symbol` | No | A symbol lookup matched multiple tickers. |
| `data_unavailable` | Yes | Provider returned an empty envelope or `data_unavailable: true`. Triggers fallback to a configured read-only tool. |
| `provider_timeout` | Yes | TimeoutError or `tool_timeout` envelope. Triggers fallback. |
| `auth_required` | No | PermissionError or `auth` / `unauthorized` / `api key` heuristic. |
| `schema_mismatch` | No | ValueError / TypeError from the tool (validation failed). |
| `unsafe_side_effect` | No | A side-effecting tool was invoked with `allow_side_effects=False`. |
| `budget_exceeded` | No | The step-count budget tripped before the step reached a terminal state. |
| `cancelled` | No | Cancel event was set. |
| `unknown` | No | Anything else. |

---

## Evidence Verification Semantics

The `EvidenceVerifier` re-grades each `StepResult` after execution. It can
only **downgrade** — never upgrade a non-success result to `success`.

### What makes a claim verified

A `Claim` (FACT, DERIVED, or INTERPRETATION) is verified when at least one of
its `Claim.evidence` refs also appears in the `evidence` tuple of a SUCCESS
`StepResult`. DERIVED claims additionally require the backing ref to declare a
named source `field` — derived values must trace to a concrete column, not a
whole artifact.

### What yields partial vs blocked

For a step that returned SUCCESS:

- **No evidence refs at all** → downgraded to PARTIAL. The step's claim is
  unverifiable.
- **Some refs resolve, some don't** → SUCCESS is downgraded to PARTIAL.
- **No refs resolve** → downgraded to BLOCKED. None of the step's evidence
  could be confirmed.

For a step that returned PARTIAL: stays PARTIAL regardless of ref resolution.

For a step that returned RECOVERABLE_ERROR / BLOCKED / UNSAFE_ERROR /
CANCELLED: returned unchanged. Evidence cannot repair an already-failed step.

### Path-traversal protection

All artifact and metric refs are resolved strictly under the approved
`run_dir`. Absolute paths outside `run_dir` and `../` traversal are rejected.
A claim cannot pull arbitrary filesystem objects.

### Recency

A ref carrying an explicit `as_of` date older than 30 days is treated as
stale and does not resolve. A missing `as_of` is NOT stale — many artifact
refs simply don't carry a date. A future `as_of` is accepted (clock-skew
tolerant).

---

## Cache Behavior

The reliability runtime MAY cache read-only tool results in a bounded in-memory
LRU cache (`ResultCache`). The cache is opt-in: when no cache is wired at the
gateway, no caching occurs and existing behavior is untouched.

| Property | Value |
|----------|-------|
| Bounded size | Default 128 entries (LRU eviction). |
| TTL | Default 300 seconds per entry. Expired entries are removed lazily on read. |
| Side-effects | NEVER cached. The cache rejects keys that reference side-effecting tool names and rejects results carrying `UNSAFE_ERROR` / `UNSAFE_SIDE_EFFECT`. |
| Key normalization | JSON keys are re-serialized with sorted keys so argument ordering does not matter: `{"a":1,"b":2}` and `{"b":2,"a":1}` map to the same entry. |
| Secrets | No raw prompt or credential values are added to keys by the cache module. The caller is responsible for not passing secrets as keys. |

The cache does NOT replace the loader caches in `agent/src/providers/`. Those
are untouched when the reliability runtime is off.

---

## Benchmark and Replay Fields

The replay suite (`agent/tests/fixtures/reliability_cases.json`) drives the
runtime with fake provider/LLM outcomes and asserts deterministic fields.
The same fields are stable across runs, suitable for before/after benchmark
comparison.

### Fixture case shape

```json
{
  "name": "provider_timeout_no_fallback",
  "prompt": "Get AAPL daily data for the last 20 sessions",
  "expected_intent": "market_data",
  "expected_status": "failed",
  "faults": [{"provider": "primary", "error_code": "provider_timeout"}],
  "required_evidence": [],
  "setup": { /* runner-private wiring */ },
  "synthesis": {"content": "...", "claims": [...]}
}
```

| Field | Type | Purpose |
|-------|------|---------|
| `name` | string | Unique case ID (benchmark key). |
| `prompt` | string | Natural-language user prompt. |
| `expected_intent` | string | The route intent the runtime should resolve (informational; the replay runner uses an injected route). |
| `expected_status` | string | The deterministic terminal status (`success` / `partial` / `failed` / `cancelled`). |
| `faults` | list | Provider/error_code pairs the case exercises. The first pair's `error_code` drives the gateway outcome. |
| `required_evidence` | list | Capability names that the case considers load-bearing for verification (informational; assertions are via `expected_status`). |
| `setup` | object | Runner-private wiring (route capabilities, plan step, outcome name, retry budget). |
| `synthesis` | object | The fake executor's synthesis output (content + claims). |

### Replay corpus (11 cases)

| Case | Fault injected | Expected status |
|------|----------------|-----------------|
| `wrong_tool_prevention` | `unsafe_side_effect` | failed |
| `invalid_symbol` | `invalid_argument` | failed |
| `bad_date` | `invalid_argument` | failed |
| `provider_timeout_no_fallback` | `provider_timeout` | failed |
| `empty_provider_response` | `data_unavailable` | failed |
| `partial_financial_statement` | (success, no evidence) | partial |
| `budget_exhaustion` | `provider_timeout` (budget=2) | failed |
| `malformed_backtest_metrics` | `schema_mismatch` (missing metric field) | failed |
| `unsupported_claim` | (success, no evidence) | partial |
| `cancellation` | `cancelled` | cancelled |
| `side_effect_no_retry` | `provider_timeout` (side-effecting) | failed |

### Redacted reliability summary (in every run result)

```json
{
  "reliability": {
    "intent": "market_data",
    "steps_total": 1,
    "steps_verified": 1,
    "steps_failed": 0,
    "claims_coverage": 1.0,
    "phase_ms": {"router": 0, "planner": 0, "tool": 12, "synthesis": 45},
    "events": {"run_started": 1, "step_success": 1, "total_step_attempts": 1, "terminal:success": 1}
  }
}
```

The summary is JSON-serializable and never contains prompts, arguments,
symbols, credentials, or any user data — only short codes, counts, and
millisecond timings. This property is asserted by
`test_replay_case_redacted_reliability_summary`.

---

## Telemetry

The runtime mirrors its phase timings and event counts into
`agent/src/telemetry/counters.py`. Telemetry is best-effort: any failure in
the counters module is swallowed and does not affect the attempt.

| Counter kind | Example names |
|--------------|---------------|
| Phase timings (`record_reliability_phase`) | `router`, `planner`, `tool`, `synthesis`, `registry_build`, `agent_loop` |
| Event counts (`record_reliability_event`) | `run_started`, `step_success`, `step_retry`, `step_terminal:<status>`, `step_blocked:<id>`, `terminal:<status>`, `cache_hit`, `cache_miss`, `tool_error:<code>` |

---

## Troubleshooting

### A `partial` result you didn't expect

`partial` means the run produced some evidence but not every claim was
backed. Common causes:

- The synthesis made a claim whose `Claim.evidence` refs don't appear in any
  SUCCESS step's evidence tuple.
- A step returned SUCCESS with no evidence refs (the verifier downgrades it
  to PARTIAL).
- A step returned SUCCESS with evidence refs that point at files missing
  from the `run_dir`, or at metric files missing the named field.

Inspect `result["reliability"]["events"]` for `step_terminal:partial` /
`step_terminal:blocked` entries, and `result["reliability"]["claims_coverage"]`
for the fraction of verified claims.

### A `failed` result with `reason="unsafe path blocked"`

A tool was classified as side-effecting and the gateway policy had
`allow_side_effects=False`. The tool name either starts with `trading_`, is
`bash`, or did not declare `side_effecting = False` explicitly. Confirm the
intended tool is in the route's `allowed_tools` and that the action is
authorized.

### A `failed` result with `reason="synthesis error"`

The synthesis executor (the LLM callback) raised an exception. The runtime
treats this as a runtime fault (not success) and lands on `failed`. Check
the AgentLoop logs for the underlying exception.

### `cancelled` after a user cancel

The cancel_event was set. All unresolved steps are recorded as CANCELLED and
the run terminates. This is the expected path; no action needed.

### Cache not invalidating

The cache key is the JSON of `{tool, args}` with sorted keys. Changing the
arg dict in any way (including adding a previously-absent key) produces a new
cache entry. TTL defaults to 300s; wait or restart the sidecar.
