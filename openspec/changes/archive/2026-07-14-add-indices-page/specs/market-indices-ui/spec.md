## ADDED Requirements

### Requirement: Dedicated index research route

The frontend SHALL expose an `/indices` route and a primary sidebar navigation entry that open a dedicated index research view. The view MUST list every index supported by the existing frontend index adapter and show the selected index's name, code, current quote context, and daily candlestick history.

#### Scenario: User opens the index route

- **WHEN** a user selects the Indices sidebar entry or navigates to `/indices`
- **THEN** the application displays the index research view and a supported index is selected when index data is available

#### Scenario: User selects a different index

- **WHEN** a user activates a different supported index in the list
- **THEN** the application updates the selected quote and daily chart for that index and records its code in the `symbol` search parameter

### Requirement: Honest market-data states

The index research view SHALL distinguish loading, unavailable, error, and stale market-data states. It MUST NOT show historical bars as the current selection's data after a selected-index request fails or returns no rows.

#### Scenario: Daily history is unavailable

- **WHEN** the selected index has no daily history or its history request fails
- **THEN** the application shows a clear unavailable or error state instead of a candlestick chart with unrelated data

#### Scenario: Quote data is stale

- **WHEN** the frontend index adapter marks the quote response as stale
- **THEN** the application labels the displayed quote context as stale while preserving the last available values
