# Task 5 Report — Evidence and Artifact Verification

## What was implemented

Created `agent/src/reliability/evidence.py` with the three interfaces from the brief, verbatim:

- `ClaimKind(str, Enum)` — `FACT = "fact"`, `DERIVED = "derived"`, `INTERPRETATION = "interpretation"`.
- `Claim(BaseModel)` — `text: str`, `kind: ClaimKind`, `evidence: list[EvidenceRef]` (Pydantic 2, matching `planner.py`/`gateway.py` conventions).
- `EvidenceVerifier` with:
  - `verify_step_result(result, run_dir=None) -> StepResult`
  - `verify_claim(claim, evidence) -> bool`
  - `coverage(claims, evidence) -> float`

`agent/src/agent/trace.py` was **not** modified. The verifier returns plain values and booleans; any trace hook for evidence is deferred to Task 6 (runtime) which actually consumes this verifier — adding one now would be speculative.

## Files changed

- `agent/src/reliability/evidence.py` (new, 287 lines incl. docstrings/comments)
- `agent/tests/test_reliability_evidence.py` (new, 342 lines, 28 tests)

Commit: `20efc248 feat: verify research evidence and claims` (DCO `Signed-off-by` trailer, no AI-attribution trailers).

## Run-card / metrics reuse

**Did not reuse** the existing metrics parser. `agent/backtest/run_card.py` is a *writer* only — there is no parser to call. The closest reader, `_load_metrics` in `agent/src/shadow_account/backtester.py`, is a private (`_`-prefixed) function tightly coupled to a dict-of-artifacts interface and shadow-account semantics; importing a private symbol across module boundaries would couple the reliability layer to shadow-account concerns and require a wider refactor.

Instead, `evidence.py` has a tiny local `_load_metric_field(path, field)` (~15 LOC) that does only what the verifier needs: confirm a top-level `field` exists in JSON or as a CSV column header. This is documented in a `ponytail:` comment on the function. **Concern flagged:** if more sophisticated metric parsing (typed coercion, multi-row CSV, nested validation) is needed later, the right move is to promote `_load_metrics` out of `shadow_account/backtester.py` into a shared helper module rather than grow the local parser.

## TDD evidence

**RED** (before implementation):
```
ModuleNotFoundError: No module named 'src.reliability.evidence'
1 error in 1.79s
```

**GREEN** (after implementation):
```
28 passed in 1.77s
```

## Step 5 regression

All four referenced files exist. Command and result:
```
pytest tests/test_reliability_evidence.py \
       tests/test_run_card_strict_json.py \
       tests/test_run_card_content_filter.py \
       tests/test_agent_loop_trace.py -q
.....................................                                    [100%]
37 passed in 3.24s
```
(28 new evidence tests + 9 existing run-card/trace regressions, all green.)

Lint: `ruff check` clean on both new files.

## Security and correctness verification

**Path containment** — `_resolve_under(run_dir, source_id)`:
1. Joins `run_dir / source_id` and calls `.resolve()` to collapse any traversal.
2. Resolves `run_dir` itself.
3. Uses `Path.relative_to` (via `_contains`) to verify the candidate sits inside `run_dir`.
4. Returns `None` otherwise — refs that escape are silently rejected (no exception, no upgrade).

Covered by `test_path_traversal_ref_rejected` (`source_id="../secret.txt"`) and `test_absolute_path_outside_run_dir_rejected` (`source_id="/etc/passwd"`). Both downgrade to non-success.

**Never-success-on-missing-evidence** — enforced structurally in `verify_step_result`:
- Non-success terminal states (BLOCKED, RECOVERABLE_ERROR, UNSAFE_ERROR, CANCELLED) are returned unchanged — evidence cannot repair them.
- SUCCESS with empty `evidence` tuple → downgraded to PARTIAL (unverifiable claim).
- SUCCESS/PARTIAL with refs but `run_dir=None` → downgraded (can't contain paths without a root).
- Any ref stale (`as_of` older than 30 days, or unparseable) → that ref fails.
- All refs fail → BLOCKED; some refs fail → PARTIAL; never SUCCESS.

Covered by: `test_missing_artifact_ref_downgrades`, `test_missing_metric_field_downgrades`, `test_malformed_metric_json_downgrades`, `test_stale_as_of_downgrades`, `test_partial_not_upgraded_to_success`, `test_blocked_not_upgraded`, `test_success_without_evidence_downgrades`, `test_run_dir_none_artifact_ref_downgrades`.

**Redaction** — the module returns only `StepResult` copies, booleans, and floats. No file contents, no user data, no raw artifacts leak through the public API.

## Claim verification semantics

`verify_claim`:
- Empty `claim.evidence` → False (covers "unsupported numerical claim": a FACT asserted with zero backing refs).
- FACT / INTERPRETATION → True iff ≥1 ref in `claim.evidence` is also carried by a SUCCESS `StepResult` in `evidence`. INTERPRETATION with absent premises → False (unsupported).
- DERIVED → same as FACT, plus the backing ref must declare a non-None `field` (derived values must trace to a concrete source field).

`coverage` → verified/total, 0.0 when `claims` is empty.

## Concerns

1. **Metrics parsing duplication** — flagged above. The local parser is intentionally minimal; a future task should extract a shared metrics helper if the runtime (Task 6) needs richer parsing.
2. **`as_of` recency window (30 days)** is a constant. If the runtime needs per-intent recency policy (e.g. intraday vs. quarterly fundamentals), this should become a parameter on `EvidenceVerifier` or read from `CapabilityRoute`. Left as a constant now — YAGNI until a concrete need appears.
3. **Source-type vocabulary** — the verifier recognizes `artifact`/`file`/`document` and `metric`/`metrics`. Other source types fail-closed (return False). If Task 6 produces additional source types (e.g. `api_call`, `provider_snapshot`), they'll need to be added to the frozen sets — by design, unknown types don't silently pass.
