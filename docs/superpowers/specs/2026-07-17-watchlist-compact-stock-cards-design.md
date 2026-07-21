# Watchlist Compact Stock Cards Design

## Goal

Replace the table in the Watchlist page's left panel with compact, independent stock data cards. The list should remain quick to scan while giving each stock a clear click target for opening its chart.

## Scope

Modify only `frontend/src/pages/Watchlist.tsx` and its focused page test. Preserve the existing watchlist store, three-second quote polling, add/remove flow, bulk selection, external K-line link, selected-stock chart source, and Agent navigation behavior.

## Considered Approaches

1. Reduce the spacing in the existing table. This retains maximum density but does not make each stock feel like an independent item.
2. Render one compact data card per stock in the existing scrollable left panel. This preserves the available quote data and interactions while improving scanning and touch targets. Selected for implementation.
3. Add sparklines to each card. The page does not load per-stock intraday series, so this would require additional requests and make quote polling more expensive. Excluded from this change.

## Card Layout

The left panel keeps its heading, add-code form, and bulk Agent action. Its table is replaced by a vertically stacked list of compact cards with a consistent 8px gap.

Each card uses a two-row layout with an action cluster:

- The top row contains the selection checkbox, stock name, code, and the external K-line and delete icon buttons.
- The data row places the latest price on the left and the change amount plus percentage chip on the right.
- Positive quotes retain the existing A-share red color, negative quotes use green, and zero or unavailable values use the existing muted color.
- The percentage chip keeps a fixed minimum width and tabular numbers so updates do not shift the card layout.

Cards are compact without becoming table-like: they use a full border, a small radius consistent with the existing panel, and restrained hover feedback. The active chart selection gets a primary-tinted background and inset ring. Bulk-selected cards use a quieter primary tint so the two states stay distinguishable. Stale quotes retain their reduced-opacity indicator and title.

The list panel remains scrollable on desktop. On narrow screens, it remains above the chart panel in document order. Card content has `min-w-0`, truncates only overly long stock names, and maintains visible focus feedback for every keyboard-accessible control.

## Interaction And State

Clicking a card selects that stock for the detail chart. Clicking the checkbox, K-line link, delete button, or delete-confirmation controls does not trigger chart selection. The two-step delete confirmation remains inline within the affected card, replacing its normal action controls until the user confirms or cancels.

The current select-all control moves from the table header to the list heading. Its label and behavior remain unchanged: it selects every unselected stock, or clears the selection when all stocks are selected.

The initial list-loading state changes from table rows to four compact card skeletons with stable heights. The empty state, page-level error, overview values, and right-side detail panel remain unchanged.

## Accessibility

The card list uses an accessible list structure. Card selection remains mouse and keyboard operable through a native button or equivalent keyboard interaction, while nested form controls retain independent labels and stop event propagation. The selected-detail state is exposed with `aria-pressed`, and the active stock is communicated through the button label. The icon controls retain their existing translated accessible names.

## Testing

Update the focused Watchlist page test to assert that each stock is rendered as an independent compact card and that the legacy table is absent for populated lists. Retain coverage for quote colors, selection and Agent prefill, K-line links, and two-step deletion. Add focused checks that a card click selects the detail stock and that interacting with its checkbox or destructive controls does not select it.

Run `npx vitest run src/pages/__tests__/Watchlist.test.tsx` from `frontend/`, followed by `npm run build`.
