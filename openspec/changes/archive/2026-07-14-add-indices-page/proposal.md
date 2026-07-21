## Why

The market dashboard exposes only a compact strip of major index quotes, so investors cannot focus on an index or inspect its daily trend without leaving the product. A dedicated index view makes those existing market-data capabilities discoverable in a focused research workflow.

## What Changes

- Add an `/indices` page for viewing the supported major A-share indexes, selecting an index, and inspecting its daily candlestick trend.
- Add a sidebar entry and lazy route so the view is available from the primary workspace navigation.
- Reuse the frontend market-data adapter for quotes and daily bars, including loading, unavailable-data, and stale-data states.
- Use the reference project's index-page information hierarchy as inspiration, without adding its backend-only synchronization, index catalog, entitlement, or intraday-data features.

## Capabilities

### New Capabilities

- `market-indices-ui`: A dedicated, navigable index research view with index selection, current quote context, and daily price history.

### Modified Capabilities

- None.

## Impact

- Affected frontend code: the route registry, sidebar navigation, localized labels, a new page component and its tests, and the existing market-data adapter if a small index-history helper is needed.
- No backend API, schema, desktop-shell, dependency, or trading-flow changes.
