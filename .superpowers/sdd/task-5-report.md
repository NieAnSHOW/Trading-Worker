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
