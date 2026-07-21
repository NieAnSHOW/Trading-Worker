## MODIFIED Requirements

### Requirement: 市场优先的信息层级
The dashboard SHALL prioritize a market overview with current values and percentage changes for the Shanghai, Shenzhen, and ChiNext indexes. It SHALL then show market breadth and price-change distribution, an emotion radar, trend strength, a limit-up ladder, concept heat, market pulse, AI market summary, watchlist quotes, and selected-stock detail. The added market cards SHALL use traceable browser market data and expose the last successful data time, source, and availability state. Upward and downward market colors SHALL follow the A-share red-up/green-down convention.

#### Scenario: 行情加载成功
- **WHEN** index, market-snapshot, board, limit-pool, or watchlist data loads successfully
- **THEN** its area shows normalized data with last-market time and source status, while upward and downward values follow the A-share red-up/green-down convention

#### Scenario: Market snapshot loads successfully
- **WHEN** full-market quotes, concept boards, and limit pools are available
- **THEN** the dashboard shows breadth and distribution, an emotion radar with overall score and dimension values, trend strength based on price strength and 52-week high/low positions, a limit-up ladder grouped by consecutive-board count, and concept heat ordered by percentage change

#### Scenario: 单一区域失败
- **WHEN** any one of indexes, market snapshot, market pulse, watchlist, or stock detail fails to load
- **THEN** the system shows an error or stale state only in that area and continues showing all other successful areas

### Requirement: 看板轮询与可见性控制
The dashboard SHALL refresh market data every 15 seconds while its page is visible, pause polling while hidden, and refresh immediately when it becomes visible again. The high-cost full-market snapshot SHALL be browser-cached for at most 60 seconds, and ordinary dashboard polling SHALL reuse that snapshot without a repeat public-upstream request while it is valid. A cached AI summary SHALL not trigger a new model call during market polling.

#### Scenario: 页面可见时轮询
- **WHEN** a user remains on a visible `/dashboard` page for more than 15 seconds
- **THEN** the system refreshes dashboard market data and reuses the last successful full-market snapshot while its cache is valid

#### Scenario: 页面隐藏后暂停
- **WHEN** `document.visibilityState` becomes `hidden`
- **THEN** the system stops subsequent market polling until the page becomes visible again

#### Scenario: 返回看板标签页
- **WHEN** the page returns from hidden to visible
- **THEN** the system refreshes market data immediately without waiting for the next polling interval
