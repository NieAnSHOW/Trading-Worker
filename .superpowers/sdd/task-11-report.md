# Task 11 Report

## Status

- COMPLETE
- Commit: repository HEAD, `feat(news): add investment news workspace`

## Changed Files

- `frontend/src/pages/News.tsx`
- `frontend/src/pages/__tests__/News.test.tsx`
- `frontend/src/pages/__tests__/News.layout.test.tsx`
- `frontend/src/router.tsx`
- `frontend/src/components/layout/Layout.tsx`
- `frontend/src/components/layout/__tests__/Layout.test.tsx`
- `frontend/e2e/news-responsive.spec.ts`
- `frontend/playwright.config.ts`
- `frontend/package.json`
- `frontend/package-lock.json`
- `docs/superpowers/reviews/2026-07-20-investment-news-hub-viewport-evidence.md`
- `docs/superpowers/reviews/assets/investment-news-mobile.png`
- `docs/superpowers/reviews/assets/investment-news-desktop.png`

No Agent store, OpenSpec artifact, plan, or Comet checkpoint was modified.

## RED

Command:

```bash
cd frontend && npx vitest run src/pages/__tests__/News.test.tsx src/pages/__tests__/News.layout.test.tsx src/components/layout/__tests__/Layout.test.tsx
```

Observed result: exit 1. Both News suites failed to resolve the missing `../News` module, and the Layout test failed because the `投资资讯` `/news` link did not exist. The failure matched the missing page/navigation behavior under test.

## GREEN

Fresh commands and results:

```bash
cd frontend && npx vitest run src/pages/__tests__/News.test.tsx src/pages/__tests__/News.layout.test.tsx src/components/layout/__tests__/Layout.test.tsx
```

- PASS: 3 files, 22 tests.

```bash
cd frontend && npm run build
```

- PASS: TypeScript and Vite production build exited 0.

```bash
cd frontend && npx playwright install chromium
cd frontend && npx playwright test e2e/news-responsive.spec.ts
```

- PASS: Chromium installed; mobile 390x844 and desktop 1440x900 scenarios passed (2 tests).
- All `/news-api/**` requests and the Layout `/sessions` request were intercepted; no live backend, feed, or LLM was contacted.

## Viewport Evidence

- `docs/superpowers/reviews/2026-07-20-investment-news-hub-viewport-evidence.md`
- `docs/superpowers/reviews/assets/investment-news-mobile.png` (390x844, 64,868 bytes)
- `docs/superpowers/reviews/assets/investment-news-desktop.png` (1440x900, 83,365 bytes)

Both screenshots were visually inspected after Playwright verified no document-level horizontal overflow and pairwise separation of the track control, refresh button, AI highlights, and first article.

## Risk Signals

- Vite reports the existing warning that some generated chunks exceed 500 kB; the lazy News chunk is 10.57 kB and does not add a large eager bundle.
- Playwright reports the environment-level `NO_COLOR`/`FORCE_COLOR` warning; both Chromium scenarios still exit 0.
- Article URLs are defense-in-depth checked with `new URL()` and rendered only for `http:` or `https:`. Rendering remains text-only.

## Review Repair

Status: COMPLETE

The Important localization finding and Minor tab-semantics finding were repaired with test-first evidence.

### RED

```bash
cd frontend
npx vitest run src/pages/__tests__/News.test.tsx src/pages/__tests__/News.layout.test.tsx src/i18n/__tests__/newsLocales.test.ts
```

- Exit 1: 8 expected failures.
- All five locale fixtures failed on the first missing workspace key.
- The page failed the English workspace, resolved-language date, roving tabindex, keyboard navigation, and tab/panel relationship assertions.

### Repair

- Removed the remaining hard-coded Chinese UI copy from `News.tsx` and added the corresponding keys to `zh-CN`, `en`, `ja`, `ko`, and `ar`.
- Bound snapshot and article date formatting to `i18n.resolvedLanguage`.
- Added an English page-level regression test covering visible copy, accessible names, placeholders, refresh progress, article links/list, empty/loading states, and date locale.
- Implemented a single-tab-stop tablist with automatic activation for Arrow Left/Right (including wraparound), Home, and End.
- Linked every tab and lazily rendered panel with `aria-controls`, `role="tabpanel"`, and `aria-labelledby`.

### GREEN

```bash
cd frontend
npx vitest run src/pages/__tests__/News.test.tsx src/pages/__tests__/News.layout.test.tsx src/components/layout/__tests__/Layout.test.tsx src/i18n/__tests__/newsLocales.test.ts
```

- PASS: 4 files, 30 tests.

```bash
cd frontend
npm run build
```

- PASS: TypeScript and Vite production build exited 0.
- The pre-existing chunk-size warning remains; the lazy News chunk is 11.35 kB.

```bash
cd frontend
npx playwright test e2e/news-responsive.spec.ts
```

- PASS: mobile 390x844 and desktop 1440x900 (2 tests).
- Requests remained fixture-intercepted; no live backend, feed, or LLM was contacted.

Repair commit: `fix(news): localize news workspace` (DCO signed-off).
