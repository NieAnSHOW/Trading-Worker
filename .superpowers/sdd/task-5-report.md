# Task 5 Report: Deterministic Track Merge Pipeline

Status: NEEDS_CONTEXT

## Commit

`da340f5 feat(news): add deterministic track merge pipeline`

## Changed Files

- `agent/src/news/pipeline.py`
- `agent/tests/news/test_pipeline.py`

## TDD Evidence

RED command (run before `pipeline.py` existed):

```sh
pytest agent/tests/news/test_pipeline.py -q
```

Result: exit 2 during collection, with the expected missing-feature error:

```text
ModuleNotFoundError: No module named 'src.news.pipeline'
```

GREEN command:

```sh
pytest agent/tests/news/test_pipeline.py -q
```

Result: `10 passed in 0.11s`.

Repeat-stability command:

```sh
pytest agent/tests/news/test_pipeline.py -q
```

Result: `10 passed in 0.09s`.

## Implemented Behavior

- Canonical 12-track output, assignment/catalog filtering, seven-day window, undated-item ordering, endpoint cap (6), track cap (100), URL fragment/default-port canonicalization, and deterministic endpoint/track deduplication.
- Stable sorting uses publication time, summary completeness, source ID, then stable item ID.
- Fresh current data merges with prior items by ID; missing current data reuses prior items as stale; missing data without a prior item is unavailable.
- Per-track source statistics set `partial` only for fresh tracks with current endpoint or assignment failures. First-run all-unavailable data raises `NoTrackUpdatedError("no_track_updated")`.

## Risk Signals

- Snapshot state merge: reviewed against `TrackSnapshot` validation. Fresh, stale, and unavailable construction follows its generated-time/stale/partial contract. Stale reuses prior items, generation time, AI data, and source stats while clearing `partial` as required by the persisted model.
- Persisted data contract: reviewed against `FeedItem`, `FeedSource`, and `SourceStats` validation. The pipeline constructs validated models only and bounds item counts to the persisted limit of 100.

## Concerns

The brief contains a direct tension: it requires `NoTrackUpdatedError("no_track_updated")` when `updated_track_ids` is empty, while its required stale-fallback example calls the pipeline with no current items and expects a stale result. The implementation preserves the explicit stale fallback and raises only when every track would be unavailable (the first-run, no-current case).

## Independent Read-Only Review

Review package: `.superpowers/sdd/review-102a160..da340f5.diff`

Verdicts:

- Spec compliance: failed
- Task quality: failed

Finding (Important): `agent/src/news/pipeline.py:83` raises `NoTrackUpdatedError` only if there are neither fresh updates nor stale tracks. Thus an all-stale refresh returns `updated_track_ids=frozenset()` instead of blocking LLM/storage as the literal Step 4 contract says. `agent/tests/news/test_pipeline.py:97` encodes the stale-return interpretation. The reviewer found no Critical findings and cannot resolve the contradiction between the brief's required stale-fallback example and its literal empty-update error condition. A requirement decision or interface adjustment is needed; no scope expansion was performed.

## Follow-up: Empty Update Boundary Repair

Status: COMPLETE

The prior plan conflict is resolved by the user's committed decision in `2f0df14`: this pipeline always returns all 12 track states and `updated_track_ids`, including an empty update set. The Task 7 coordinator owns the `no_track_updated` early exit before LLM invocation or persistence.

### TDD Evidence

RED command, after changing the no-current/no-previous test to expect 12 unavailable tracks and an empty update set:

```sh
pytest agent/tests/news/test_pipeline.py -q
```

Result: `1 failed, 9 passed`; the expected failure was `NoTrackUpdatedError: no_track_updated` from the old pipeline exit.

GREEN command:

```sh
pytest agent/tests/news/test_pipeline.py -q
```

Result: `10 passed in 0.10s`.

Repeat-stability command:

```sh
pytest agent/tests/news/test_pipeline.py -q
```

Result: `10 passed in 0.08s`.

### Change

- Removed the pipeline-only `NoTrackUpdatedError` API and its empty-update exception branch.
- Replaced the no-current/no-previous test with an assertion for canonical 12 unavailable tracks and `frozenset()` updates.

### Follow-up Commit

`fix(news): return empty track updates from pipeline` (current `HEAD`)

### Changed Files

- `agent/src/news/pipeline.py`
- `agent/tests/news/test_pipeline.py`
- `.superpowers/sdd/task-5-report.md`

---

## LLM Usage Integration Addendum

### Status

Implemented live and persisted LLM usage integration for the Agent stream, session history, and Run Detail overview.

### TDD Evidence

- RED attempt: added `dispatches llm usage events to the dedicated handler` to `frontend/src/hooks/__tests__/useSSE.test.ts` and ran the focused hook suite first.
- Result: the new test passed immediately (18 tests passed). This is not a valid RED observation because `useSSE` already subscribed to `llm_usage` before this task; the test records and protects the existing event channel.
- GREEN: wired the page-level live summary to `parseLLMUsageDelta` and `accumulateLLMUsage`, then reran the required regression suite successfully.

### Verification

- `cd frontend && npx vitest run src/hooks/__tests__/useSSE.test.ts` — 18 passed.
- `cd frontend && npx vitest run src/hooks/__tests__/useSSE.test.ts src/lib/__tests__/llmUsage.test.ts src/components/chat/__tests__/LLMUsagePanel.test.tsx src/components/chat/__tests__/MessageBubble.test.tsx src/i18n/__tests__/locales.test.ts` — 52 passed across 5 files.
- `cd frontend && npm run build` — passed (`tsc -b` and Vite production build).

### Risk Signals

- Cross-module: Agent consumes shared `llmUsage` parsing, API `RunData.llm_usage`, and chat panel behavior; no shared module was modified.
- SSE: LLM usage is parsed and reduced only into component-local summary state. Raw event data is not added to Zustand, chat content, or exports. Existing Last-Event-ID deduplication test remains in place.
- Public state: persisted chat messages contain only the typed `llmUsage` summary returned by `getRun`; history and completion fetch failure do not add a persisted-usage message.

### Limitations

- No Agent/Run Detail integration test exists within this task's permitted test-file ownership, so live rendering and API-failure retention are verified by code path review plus production type/build checks rather than a new page-level test.
- When completion's `getRun` request fails, the live summary remains visible until a subsequent `attempt.created` or an attempt failure clears it, so the UI does not imply persistence that was not confirmed.

### Independent Review Follow-up

- The first independent review identified two P2 lifecycle gaps: a missed terminal SSE recovery could retain live usage after persisted history loaded, and session changes could carry live usage into another session.
- The recovery path now clears live usage only when the completed attempt's run was successfully loaded with persisted usage; failed run reads retain it. Both session-switch and session-removal branches clear it unconditionally.
- The required regression suite and production build were rerun after this repair.
- A follow-up review found and the implementation closes an await-time session-switch race: `syncCompletedAttempt` rechecks the current session after history refresh before clearing component-local live usage.

---

## Attempt Completion Race Repair

### Status

Complete.

### TDD Evidence

RED command, before the attempt identity guard:

```sh
cd frontend && npx vitest run src/pages/__tests__/Agent.attempt-completion.test.tsx
```

Result: `1 failed`. The deferred `getRun` request for attempt A resumed after
attempt B had accumulated live usage, and the old callback removed the live
usage panel.

GREEN command:

```sh
cd frontend && npx vitest run src/pages/__tests__/Agent.attempt-completion.test.tsx
```

Result: `2 passed` (success and rejection completion-fetch paths).

### Change

- Track the active attempt id plus attempt and session generations.
- Revalidate session id, attempt id, and both generations after asynchronous
  completion/history reads before writing component or Zustand state.
- Invalidate the active attempt on session changes and replace it on a new
  `attempt.created` event.
- Preserve live usage when `getRun` fails; a successful persisted usage read
  clears the live summary only for the still-current attempt.

### Verification

- `cd frontend && npx vitest run src/pages/__tests__/Agent.attempt-completion.test.tsx src/hooks/__tests__/useSSE.test.ts src/lib/__tests__/llmUsage.test.ts src/components/chat/__tests__/LLMUsagePanel.test.tsx src/components/chat/__tests__/MessageBubble.test.tsx src/i18n/__tests__/locales.test.ts` - 54 passed.
- `cd frontend && npx vitest run --coverage src/pages/__tests__/Agent.attempt-completion.test.tsx` - 2 passed.
- `cd frontend && npm run build` - passed.
