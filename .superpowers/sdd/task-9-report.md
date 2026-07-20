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

## UUID Contract Follow-up

The initial parser restricted UUID version and variant bits even though the
Task 8 Pydantic model uses the general `UUID` type. A valid all-zero UUID is
therefore legal under the backend contract. A new regression test first failed
with `Invalid news API response`, then the parser switched to the general
hexadecimal UUID shape used by that model. The full focused GREEN command was
rerun and passed with `16 passed`; the production build passed again.

## Parser Security Follow-up

Two frontend parser gaps identified during the second review round were
covered with test-first regressions. A successful response declared as JSON
but containing malformed JSON previously leaked `SyntaxError`; the RED run
showed that failure, and `request()` now returns `ApiError("Invalid JSON
response from <path>", 200)` instead. The news article URL parser also now
matches the backend's public URL contract: it rejects credential parameter
names in query strings and fragments, plus encoded Bearer, `sk-`, and JWT-like
credential values. The URL regression tests first failed because all four
URLs were accepted; after the parser change, `npx vitest run
src/lib/__tests__/api.test.ts` passed with `13 passed`.

## Review Contract Follow-up

An independent review found that WHATWG `URL` normalizes
`https:example.com/article`, while the backend's `urlsplit` rejects it for
lacking a raw authority. A regression test first showed the frontend accepting
that URL. The parser now requires a non-empty raw `http(s)://authority` before
constructing `URL`, and the focused API suite passed with `14 passed`.

## Unicode And Userinfo Follow-up

A final independent review found that JavaScript's UTF-16 `string.length`
would reject backend-valid non-BMP text, and that empty URL userinfo was not
visible through the WHATWG URL properties. Regression tests first failed for a
151-code-point emoji title and both `https://@example.com/...` and
`https://:@example.com/...`. String bounds now use Unicode code-point counts,
and raw authorities containing `@` are rejected before URL normalization. The
same review also identified JavaScript's normalization of invalid calendar
dates; strict UTC date parsing now rejects values such as February 30. The
focused API suite passed with `18 passed`.

## Year-Zero Follow-up

The closing review found that JavaScript accepts year zero even though the
backend's Python `datetime` rejects it. A `0000-01-01T10:00:00Z` regression
first failed, then the UTC parser added the backend-compatible lower year bound
of 1. The focused API suite passed with `19 passed`.

## Permissive URL Follow-up

The final review also caught two places where browser URL APIs were stricter
than the backend's `urlsplit` and `unquote_plus`: malformed percent escapes in
query values and out-of-range ports. Regression tests first showed both valid
backend URL shapes being rejected. URL component decoding now follows Python's
permissive replacement behavior, and a URL rejected only for its port is
validated with that port removed while preserving the original URL. The 202
refresh response test also confirmed that malformed JSON already preserves the
actual HTTP status. The focused API suite passed with `22 passed`.

## Scope

No page, router, layout navigation, global store, or polling hook was added;
those belong to the later news UI tasks.
