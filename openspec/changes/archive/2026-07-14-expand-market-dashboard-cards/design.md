## Context

`/dashboard` already uses the browser `stock-sdk` for indexes, market events, watchlist quotes, and an AI summary. Its adapter does not yet query the SDK's full-market quotes, concept boards, or limit-up pools, so the page has no cards that summarize breadth, strength structure, or theme persistence.

The data is research-only. Public quotes are delayed, and `batch.cn()` retrieves roughly 5,000 A shares, so it cannot be repeated with every ordinary 15-second market refresh.

## Goals / Non-Goals

**Goals:**

- Provide price-change distribution and breadth, an emotion radar, trend strength, a limit-up ladder, and concept heat from real browser market data.
- Normalize SDK responses into dashboard DTOs and let each added area retain its last successful data, source, timestamp, and failure state independently.
- Limit full-market request frequency so current page polling does not burden public upstreams.

**Non-Goals:**

- Do not add Python APIs, database tables, background jobs, browser credentials, or dependencies.
- Do not send browser market data into Agent, backtesting, risk, live-trading, or order paths.
- Do not calculate market-wide moving-average coverage, which would require per-stock historical K-lines; the trend card uses verifiable full-quote strength and 52-week high/low positions instead.

## Decisions

### A single market-snapshot adapter

Add market-snapshot DTOs and a reader to `stockSdk.ts`. It will concurrently retrieve `batch.cn()`, `board.concept.list()`, and limit-up, limit-down, and broken-board pools. The adapter normalizes and calculates breadth/distribution, an emotion radar, trend strength, a consecutive-board ladder, and concept heat. This keeps the page and Zustand store dependent on stable DTOs. Calling each SDK endpoint from the page would scatter error handling, make tests difficult, and duplicate the full-market request, so it is not used.

### Cache snapshots without lowering page polling

The adapter caches the most recent successful market snapshot for 60 seconds. The dashboard still calls its refresh action every 15 seconds; while the cache is valid, that action returns the successful snapshot without another public-upstream request. After expiry, upstream calls use `Promise.allSettled`; a failed area keeps its last successful value and becomes stale while the other cards continue to update.

An uncached full-market request every 15 seconds would unnecessarily load the browser and public sources. A server-side aggregator would offer shared caching, but is outside the scope of this frontend dashboard extension.

### Compact, independently degradable operating cards

New cards use the existing borders, typography, A-share red-up/green-down convention, and responsive grids. The emotion radar is an inline data visualization; remaining cards use fixed-proportion bars, metric grids, and scrollable lists so loading or long names cannot shift the layout. Missing data in one source produces a local unavailable or stale state and never blocks the AI summary, watchlist, or stock detail.

## Risks / Trade-offs

- [Full-market browser requests are large or upstreams rate-limit] -> A 60-second TTL, existing SDK request governance, and stale last-successful snapshots; tests never call live upstreams.
- [Public-data delay or timestamps differ by source] -> Each area exposes source and stale state; the existing research-only, non-trading boundary remains explicit.
- [Limit pools are unavailable before/after market hours or upstream fails] -> The limit card degrades alone and never invents ladder values.
- [52-week position is not short-period moving-average trend] -> UI and translations use high/low-position and strength terminology, not MA coverage.

## Migration Plan

1. Add snapshot DTOs, calculators, and the cached SDK adapter function with mock-SDK coverage for calculations and fallback.
2. Extend dashboard-store loading, errors, and polling state; add page cards, translations, and rendering coverage.
3. Run focused Vitest, the frontend production build, and strict OpenSpec validation. If a public upstream is unavailable in a real environment, only added areas degrade; existing indexes, watchlist, and AI summary remain available.

## Open Questions

None.
