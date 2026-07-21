## 1. Test-first coverage

- [x] 1.1 Add failing frontend tests for the index page's default selection, URL-backed index selection, and unavailable daily-history state.
- [x] 1.2 Extend sidebar coverage to require the new index navigation entry.

## 2. Index research interface

- [x] 2.1 Implement the dedicated index page using the existing quote and daily-history adapter functions and the shared candlestick chart.
- [x] 2.2 Register the lazy `/indices` route and sidebar navigation entry, preserving responsive and accessible selection behavior.
- [x] 2.3 Add localized navigation and index-page labels for all supported application languages.

## 3. Verification

- [x] 3.1 Run the focused frontend tests and frontend production build, then resolve any regressions.

## 4. Remediation

- [x] 4.1 Preserve market-qualified index symbols for daily-history requests while retaining normalized URL selection.
