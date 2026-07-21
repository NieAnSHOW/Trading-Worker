# Watchlist Market Pulse Design

## Goal

Move the Market Pulse card from the Dashboard to the Watchlist page, where it appears as a full-width section beneath the watchlist workspace and remains available with an empty watchlist.

## Scope

- Remove Market Pulse rendering and its page-level selectors from `frontend/src/pages/Dashboard.tsx`.
- Render the existing Market Pulse pagination UI in `frontend/src/pages/Watchlist.tsx`.
- Add a narrow Market Pulse refresh action to `frontend/src/stores/marketDashboard.ts`.
- Update Dashboard, Watchlist, and market-dashboard store tests.

## Design

`MarketPulseSection` and its pagination behavior move from Dashboard to Watchlist without altering the displayed fields, page-size choices, or empty and error states. The Watchlist renders the card after its main workspace in a separate full-width section, outside the conditional workspace so it is visible whether or not the user has saved any stocks.

The market-dashboard store exposes `refreshPulse`, which requests only the Market Pulse endpoint and maintains the existing `pulse`, `pulseLoading`, and `pulseError` state. Watchlist calls this action on mount and every 15 seconds while the document is visible, then clears its timer and visibility listener on unmount. It does not call the Dashboard's full-market initialization or polling APIs, avoiding duplicate quote, snapshot, index, and AI-summary requests.

Dashboard continues to initialize and poll its own market snapshot data but no longer selects or renders pulse state.

## Acceptance Criteria

- Dashboard does not render a `market-pulse-card` or select Market Pulse state.
- Watchlist renders `market-pulse-card` below the main workspace at full width.
- Watchlist renders the Market Pulse card when its watchlist is empty.
- Watchlist refreshes only Market Pulse data on mount and at 15-second visible-page intervals.
- The Market Pulse list retains pagination and has no internal maximum-height or scrolling constraints.
- Existing watchlist quote polling remains unchanged.
- Dashboard, Watchlist, and market-dashboard store tests pass, along with the frontend production build.

## Testing

Use test-first coverage for the new narrow store action and the Watchlist placement. The Dashboard test must assert that Market Pulse is absent, while Watchlist tests must assert it is present in both empty and populated states. Timer tests must show that the Watchlist refreshes only pulse data and cleans up its polling lifecycle.
