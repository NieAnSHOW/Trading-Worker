# Surface LLM Usage Final Review Fix Report

Date: 2026-07-23

## Scope And Conclusion

- Fixed the Agent SSE watchdog activity regression without parsing or storing the `llm_usage` payload, rendering a live/chat usage card, or requesting run detail.
- Changed the Usage provider/model breakdown bars to cycle through the existing stable `theme.maColors` palette by sorted bucket index. API token totals remain the direct bar values, and the existing dark-theme rebuild lifecycle is unchanged.
- Did not modify the backend protocol, Run Detail, MessageBubble, OpenSpec tasks, Comet progress, or the pre-existing changes in `task-4-report.md` and `task-6-report.md`.

## TDD RED

Command:

```sh
cd frontend && npx vitest run src/pages/__tests__/Agent.attempt-completion.test.tsx src/pages/__tests__/Usage.test.tsx
```

Result: exit 1 with the two expected failures (32 passed, 2 failed). The fake-timer Agent regression observed `idle` instead of `streaming` 15 seconds after an ignored `llm_usage` event. The Usage option assertion observed numeric bar data `[600, 400]` with one series-level warning color instead of per-bucket palette colors.

## TDD GREEN

After the minimal production changes, the same command exited 0: 2 files and 34 tests passed.

The watchdog regression also verifies that `llm_usage` creates no `llm_usage` message or live card and never calls `api.getRun`; after the refreshed timeout expires, the watchdog still transitions to idle.

## Verification

| Command | Result |
| --- | --- |
| `cd frontend && npx vitest run src/pages/__tests__/Agent.attempt-completion.test.tsx src/pages/__tests__/Usage.test.tsx` | PASS: 2 files, 34 tests. |
| `cd frontend && npx vitest run src/pages/__tests__/Usage.test.tsx src/pages/__tests__/Agent.attempt-completion.test.tsx src/components/layout/__tests__/Layout.test.tsx src/__tests__/router.test.tsx src/i18n/__tests__/locales.test.ts` | PASS: Task 8.1 frontend group, 5 files, 55 tests. |
| `cd frontend && npm run build` | PASS: `tsc -b && vite build`; Vite emitted only its existing non-failing large-chunk advisory. |
| `git diff --check` | PASS: no whitespace errors. |

## Changed Files

- `frontend/src/pages/Agent.tsx`
- `frontend/src/pages/__tests__/Agent.attempt-completion.test.tsx`
- `frontend/src/pages/Usage.tsx`
- `frontend/src/pages/__tests__/Usage.test.tsx`
- `.superpowers/sdd/global-final-review-fix-report.md`

## Concern

- The existing Vite production bundle chunk-size advisory remains; it is unrelated to these focused fixes and does not fail the build.
