# watchlist-ui Specification

## Purpose
TBD - created by archiving change market-watchlist. Update Purpose after archive.
## Requirements
### Requirement: 自选股页面入口与导航
系统 SHALL 在 `/watchlist` 路径提供独立页面，并在侧边栏导航中新增「自选股」入口（图标使用 `Star` 或 `TrendingUp`），位于「Agent」导航项之后。

#### Scenario: 从侧边栏导航进入盯盘页面
- **WHEN** 用户点击侧边栏「自选股」导航项
- **THEN** 路由跳转到 `/watchlist`，侧边栏高亮显示「自选股」

#### Scenario: 空列表显示引导文案
- **WHEN** 用户进入 `/watchlist` 且自选股列表为空
- **THEN** 页面显示空状态提示，引导用户添加股票

### Requirement: 添加股票交互
用户 SHALL 能在盯盘页面输入股票代码并添加到自选股列表。输入框支持 A 股 6 位数字代码，提交后自动刷新列表。

#### Scenario: 输入有效代码并添加
- **WHEN** 用户在输入框输入 `000001` 并点击「添加」或按 Enter
- **THEN** 调用 `POST /watchlist/stocks`，成功后股票出现在列表顶部

#### Scenario: 输入无效代码时给出提示
- **WHEN** 用户输入非 6 位数字的代码（如 `abc`）并提交
- **THEN** 显示 inline 错误提示「请输入有效的 A 股代码（6 位数字）」，不发起网络请求

#### Scenario: 重复添加时提示已存在
- **WHEN** 用户添加已在列表中的股票
- **THEN** 显示提示「该股票已在自选股列表中」

### Requirement: 实时行情刷新
盯盘页面 SHALL 每 3 秒自动刷新一次行情数据，显示价格、涨跌幅、涨跌额；页面不可见时暂停轮询，重新可见时立即刷新。

#### Scenario: 页面可见时自动轮询
- **WHEN** 用户在 `/watchlist` 页面停留超过 3s
- **THEN** 行情数据自动更新，涨跌颜色（上涨红、下跌绿，遵循 A 股惯例）实时变化

#### Scenario: 页面切换到后台时暂停轮询
- **WHEN** 用户切换到其他浏览器标签页（`document.visibilityState === 'hidden'`）
- **THEN** 行情轮询暂停，不发送网络请求

#### Scenario: 返回页面时立即刷新
- **WHEN** 用户从后台切回 `/watchlist` 页面
- **THEN** 立即触发一次行情请求，不等待下一个 3s 间隔

### Requirement: 涨跌数据展示
自选股列表 SHALL 展示以下字段：股票代码、股票名称、当前价格、涨跌额（带正负号）、涨跌幅（带 % 和正负号），涨跌颜色遵循 A 股惯例（上涨红色、下跌绿色、平盘灰色）。

#### Scenario: 上涨股票显示红色
- **WHEN** 某股票 `change_pct > 0`
- **THEN** 涨跌幅和涨跌额以红色（`text-red-500`）显示，前缀 `+`

#### Scenario: 下跌股票显示绿色
- **WHEN** 某股票 `change_pct < 0`
- **THEN** 涨跌幅和涨跌额以绿色（`text-green-500`）显示

#### Scenario: 平盘股票显示灰色
- **WHEN** 某股票 `change_pct === 0`
- **THEN** 涨跌幅以灰色显示，不显示正负号前缀

### Requirement: 删除自选股
用户 SHALL 能从列表中删除股票，需有二次确认机制（tooltip 或确认弹框）防止误删。

#### Scenario: 点击删除图标并确认
- **WHEN** 用户点击某股票行的删除图标并确认
- **THEN** 调用 `DELETE /watchlist/stocks/{code}`，成功后从列表中移除该行

#### Scenario: 取消删除不操作
- **WHEN** 用户点击删除图标后取消
- **THEN** 列表不变，不发起网络请求

