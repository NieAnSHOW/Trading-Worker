# Dashboard Card Height Design

## Goal

Dashboard data cards should expand with their content and must not show internal scrollbars during normal use.

## Scope

Update `frontend/src/pages/Dashboard.tsx` for:

- Market pulse items
- Concept and industry heat lists
- Stock ranking lists

## Design

The card containers retain their existing `h-full` behavior so cards in the same grid row share the row height. Their list content removes fixed `max-h-*` limits and `overflow-auto`, allowing each card to determine the grid row height from its content.

The page's existing outer content area remains responsible for page-level scrolling. Market pulse pagination, list limits, responsive grid breakpoints, data states, and content hierarchy remain unchanged.

## Acceptance Criteria

- No dashboard data-card list uses `overflow-auto` or a fixed `max-h-*` cap.
- Cards expand to include their displayed list items.
- The existing shared-height grid behavior remains intact.
- Market pulse pagination continues to render and work as before.
- Dashboard tests and the frontend production build pass.

## Testing

Add a focused component regression test that checks the affected card and list classes no longer impose internal scrolling or maximum heights. Run it before the implementation to confirm the current markup fails the intended behavior, then run the dashboard test suite and production build after the change.
