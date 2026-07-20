# Task 9 Report: Frontend news API, proxy, and i18n contract

## Status

Implemented the frontend boundary for the authenticated investment-news API:

- `api.getNewsSnapshot(signal)`
- `api.startNewsRefresh(signal)`
- `api.getNewsRefreshStatus(signal)`
- `parseNewsSnapshotResponse(value)`
- `/news-api` Vite development proxy
- identical `layout.news` and `news.*` key structures in `zh-CN`, `en`, `ja`,
  `ko`, and `ar`

The API boundary receives response JSON as `unknown` and validates it before
returning a typed value. It rejects non-object values, extra or missing model
fields, non-canonical track order, invalid enums, invalid arrays and string
lengths, non-UTC timestamps, unsupported URLs, invalid UUIDs, and incompatible
snapshot or track state. A malformed successful response raises the existing
`ApiError` with status 200; no response content is reflected in the error.

## TDD Evidence

The tests and locale contract test were added before the implementation.

RED:

```text
cd frontend && npx vitest run src/lib/__tests__/api.test.ts src/i18n/__tests__/newsLocales.test.ts src/__tests__/viteProxy.test.ts
```

Result: `12 failed, 3 passed`. The failures were expected: news API methods
and the runtime parser did not exist, `/news-api` was absent from the proxy
list, and every locale lacked `layout.news`.

GREEN:

```text
cd frontend && npx vitest run src/lib/__tests__/api.test.ts src/i18n/__tests__/newsLocales.test.ts src/__tests__/viteProxy.test.ts
```

Result: `3 passed` test files, `15 passed` tests.

The test suite covers a complete Task 8-compatible snapshot, authenticated
refresh requests with `AbortSignal`, missing tracks, unknown refresh phases,
non-HTTP(S) article URLs, 301-character article titles, `/news-api` proxying,
and every required locale key in all five languages.

## Verification

```text
cd frontend && npm run build
```

Result: passed (`tsc -b && vite build`). Vite emitted its existing chunk-size
warning for bundles over 500 kB; no build error occurred.

`git diff --check` also passed.

## Scope

No page, router, layout navigation, global store, or polling hook was added;
those belong to the later news UI tasks.
