# Market Pulse Page Design

## Goal

Move Market Pulse out of the Watchlist into a standalone Market Pulse page. The page must classify market events, provide local filters, and follow the information layout of the Indices page.

## Scope

- Add a lazy-loaded `/market-pulse` route and a sidebar navigation item directly below Market Dashboard.
- Remove Market Pulse rendering, selectors, polling, and test fixtures from `frontend/src/pages/Watchlist.tsx`.
- Remove the dormant Market Pulse component and Market Pulse refresh call from `frontend/src/pages/Dashboard.tsx` and dashboard polling.
- Add an independent Market Pulse page and feature module.
- Add Chinese and English labels for the route, page, categories, filters, states, and detail panel.

## Architecture

The route page, `frontend/src/pages/MarketPulse.tsx`, renders the feature module without owning Market Pulse behavior.

```text
frontend/src/pages/MarketPulse.tsx
frontend/src/components/market-pulse/
  MarketPulsePanel.tsx
  marketPulse.ts
  __tests__/
```

`MarketPulsePanel` selects Market Pulse state from `useMarketDashboardStore`, refreshes on mount and every 15 seconds while the document is visible, and cleans up its visibility listener and timer on unmount. It owns filter, selection, and pagination state. It keeps user-selected filters through automatic and manual refreshes, clamping only a now-invalid page or selection.

`marketPulse.ts` contains pure, unit-tested functions for categorizing, filtering, selecting, and paginating `MarketPulseItem` data. The functions do not fetch data or render UI.

The store retains `pulse` as the last successfully loaded list and adds `pulseAsOf` and `pulseStale` metadata. A successful result replaces the list and clears the error. A stale/error result preserves the existing list, records the error, and marks it stale; on a first-load failure the page shows an error with an empty list. Dashboard refreshes no longer call `refreshPulse`; the standalone page is the only screen that requests or renders Market Pulse data.

Watchlist remains responsible only for watchlist quotes and stock detail behavior. It does not select, refresh, or render Market Pulse state.

## Categorization And Filters

`changeType` is trimmed before matching. Each event is assigned to exactly one category from its `changeType`, in this order:

1. Limit down / broken board: `打开涨停`, `炸板`, `封跌停`, `跌停`
2. Limit up / sealed board: `封涨停`, `涨停`, `封板`
3. Upward movement: `火箭发射`, `快速反弹`, `大笔买入`, `有大买盘`, `竞价上涨`, `高开`, `向上缺口`, `新高`, `拉升`, `上涨`, `大涨`
4. Downward movement: `加速下跌`, `高台跳水`, `大笔卖出`, `有大卖盘`, `竞价下跌`, `低开`, `向下缺口`, `新低`, `跳水`, `下跌`, `大跌`
5. Turnover / capital activity: `放量`, `缩量`, `成交`, `换手`, `资金`, `净流入`, `净流出`
6. Other

The first category intentionally captures `打开涨停` before generic `涨停`, preventing a broken board from being counted as a sealed board. Keyword rules live in one place. Any unknown or future change type is categorized as Other so events are never hidden. Available local filters are category, name/code search, and a Hide stale switch. Filtering never triggers another data request. Changing a filter returns the list to its first page.

## Layout

The page adopts the Indices page layout:

- A title and short description.
- Four compact summary cards for all events, limit-up/sealed-board events, upward events, and downward/limit-down events.
- A responsive workspace with a list panel on the left and an event detail panel on the right. On small screens, panels stack vertically.
- The list panel contains search, category segmented control with counts, stale-data switch, the event list, a 200/500/1000 page-size control, and pagination.
- The detail panel displays the selected event's stock name, code, change type, event time, description, source, and stale status. It selects the first currently filtered event by default.

The header includes an icon-only manual refresh button with an accessible label and tooltip. Loading retains previously loaded events. An error retains previous events and offers a retry action; an empty filtered result clearly distinguishes no matching events from no available market data.

## Routing And Navigation

`frontend/src/router.tsx` lazy-loads the new route at `/market-pulse`. `frontend/src/components/layout/Layout.tsx` adds the Market Pulse navigation link immediately after `/dashboard` and before `/indices`. The link uses the `Activity` icon and participates in the existing active-state and collapsed-sidebar behavior.

## Testing

- Unit tests for category assignment, unknown labels, search, stale filtering, category counts, and pagination.
- Component/page tests for default selection, filter changes, detail selection, retained filters during refresh, loading, error, empty, and retry states.
- Router and layout tests for the standalone route and navigation position.
- Dashboard and Watchlist tests proving neither page has Market Pulse UI, refresh behavior, or Market Pulse store subscriptions.
- Run focused Vitest tests and `npm run build` from `frontend/`.

## Acceptance Criteria

- Market Pulse is available only through `/market-pulse`, with a sidebar entry directly below Market Dashboard.
- Dashboard and Watchlist have no Market Pulse data fetching, polling, rendering, or dependency.
- The standalone page is visually structured like Indices, with summary, list, and detail areas.
- All incoming events are visible in one category, including unexpected change types.
- Local filtering and pagination work together and remain stable through data refreshes.
- The page handles loading, errors, stale data, empty data, and empty filtered results without losing the last successful data.
