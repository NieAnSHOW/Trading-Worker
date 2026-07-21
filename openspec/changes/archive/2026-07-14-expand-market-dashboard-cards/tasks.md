## 1. Market Snapshot Data

- [x] 1.1 Define market-snapshot DTOs in the `stockSdk` adapter and implement normalized full-market breadth, emotion, trend, limit-up ladder, and concept-heat data with a 60-second cache.
- [x] 1.2 Add mock-SDK tests for market-snapshot calculations, cache reuse, and stale fallback when a single upstream fails.

## 2. Dashboard State and UI

- [x] 2.1 Extend the market-dashboard Zustand store so the added snapshot area has independent loading, error, and last-successful-data state and participates in existing visibility polling.
- [x] 2.2 Implement responsive breadth, emotion radar, trend, limit-up ladder, and concept-heat cards on `/dashboard`, including local loading, error, and stale states.
- [x] 2.3 Add English and Chinese dashboard copy and store/page coverage for the new cards and partial degradation.

## 3. Verification and Delivery

- [x] 3.1 Run focused Vitest, the frontend production build, and strict OpenSpec validation; confirm existing AI summary, watchlist, and Agent handoff behavior does not regress.

## 4. Verification Remediation

- [x] 4.1 Pause dashboard polling while hidden and resume it when the page returns to the foreground.
- [x] 4.2 Retain each snapshot area's latest successful data, source, and timestamp through partial upstream failures, and render unavailable states instead of placeholder market values.
- [x] 4.3 Cache only fully successful snapshots and deduplicate concurrent full-market snapshot requests.
