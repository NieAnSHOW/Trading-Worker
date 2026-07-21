## Why

The market dashboard currently shows indexes, market events, watchlist quotes, and an AI summary, but it does not let users quickly assess market breadth, short-term sentiment, trend structure, or hot themes. Extending the existing dashboard lets research workflows establish a complete market view before moving to individual-stock analysis.

## What Changes

- Add cards for price-change distribution and breadth, an emotion radar, trend strength, limit-up ladder, and concept heat to the existing A-share market dashboard.
- Use the already integrated browser `stock-sdk` to retrieve full-market quotes, concept boards, and limit-up pools, normalizing them through the single dashboard SDK adapter.
- Include the new cards in the dashboard's existing polling, partial-failure, and stale-data behavior without adding server APIs, persistence, or trading functions.
- Add focused frontend tests for data aggregation, dashboard state, and card rendering.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `market-dashboard-ui`: Extend the market-first information hierarchy and data-availability requirements with fuller A-share breadth, sentiment, trend, limit-up, and concept-heat information.

## Impact

- Frontend: `frontend/src/lib/stockSdk.ts`, `frontend/src/stores/marketDashboard.ts`, `frontend/src/pages/Dashboard.tsx`, their tests, and English/Chinese translations.
- Dependencies and APIs: reuse the existing browser `stock-sdk` v2 calls and polling behavior; do not change Python APIs, database schema, Agent, backtesting, or live-trading paths.
