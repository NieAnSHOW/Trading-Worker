## Context

The current dashboard requests three major A-share index quotes through `fetchDashboardIndexes`, but presents them only as compact summary cards. `fetchDashboardDailyBars` and `CandlestickChart` already provide normalized daily history and an accessible chart surface for market symbols. The reference project has a broader index catalog backed by dedicated endpoints; this repository does not expose those endpoints.

The user explicitly approved continuing the lightweight tweak workflow after the new-page escalation check. The change remains frontend-only and does not alter market-data contracts.

## Goals / Non-Goals

**Goals:**

- Provide an `/indices` route that lets a user choose a supported major index and inspect its current quote and daily candlestick history.
- Preserve the app's sidebar, theme tokens, Chinese-market rise/fall colors, responsive behavior, and i18n conventions.
- Cover navigation and page-state behavior with frontend tests before production code is written.

**Non-Goals:**

- Adding an index catalog, index search, background synchronization, intraday data, entitlements, or new backend endpoints.
- Changing dashboard behavior, market-data schemas, trading workflows, or desktop packaging.

## Decisions

### Reuse the existing market-data adapter

The page will request its supported index quote list with `fetchDashboardIndexes` and request daily history for the selected code through `fetchDashboardDailyBars`. This avoids a parallel data cache or a backend API change.

The alternative, copying the reference project's index-list and K-line APIs, was rejected because those endpoints, stored index universe, and synchronization jobs do not exist in this project.

### Use a focused master-detail layout

The page will present supported indexes as selectable, keyboard-accessible controls alongside a detail panel with quote context and a daily candlestick chart. The selection will be reflected in the `symbol` search parameter so direct links retain context. On narrow screens the layout will stack before the chart, avoiding horizontal clipping.

The alternative, expanding the market dashboard in place, was rejected because it would mix a research workflow into a summary surface and obscure the new route's purpose.

### Build on established chart and localization components

The implementation will use `CandlestickChart`, existing semantic color classes, and the existing locale files. It will not import the reference project's ECharts components or style tokens, which are from a different application.

## Risks / Trade-offs

- [The upstream provider does not return daily history for a selected index] -> Show the existing empty or unavailable-data state rather than retaining misleading prior data.
- [Quotes or history are temporarily unavailable] -> Retain explicit loading, error, and stale-data indicators so the page never implies data is current when it is not.
- [Only three indexes are available through the current adapter] -> Make the limitation visible through the concise supported-index list; a fuller catalog requires a separately scoped backend capability.

## Migration Plan

No persisted data or API migration is required. Deploying adds a lazy route and sidebar link; rollback consists of removing those frontend files and route entries.

## Open Questions

None for the current frontend-only scope.
