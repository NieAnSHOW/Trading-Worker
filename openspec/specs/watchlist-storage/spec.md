# watchlist-storage Specification

## Purpose
TBD - created by archiving change market-watchlist. Update Purpose after archive.
## Requirements
### Requirement: 初始化本地 SQLite 数据库
系统 SHALL 在 `~/.vibe-trading/watchlist.db` 初始化 SQLite 数据库，表结构包含 `code`（股票代码）、`name`（股票名称）、`market`（市场标识，默认 `a_stock`）、`added_at`（添加时间）、`sort_order`（排序序号）字段，主键为 `(code, market)`。数据库文件不存在时自动创建。

#### Scenario: 首次启动自动建库建表
- **WHEN** 应用首次调用任何 `/watchlist` API 接口
- **THEN** 系统在 `~/.vibe-trading/watchlist.db` 创建数据库和 `watchlist` 表（若已存在则跳过）

#### Scenario: 多次调用幂等
- **WHEN** watchlist 数据库已存在时再次初始化
- **THEN** 系统不抛出错误，返回正常结果

### Requirement: 查询自选股列表
系统 SHALL 提供 `GET /watchlist/stocks` 接口，返回当前用户所有自选股，按 `added_at` 倒序排列。

#### Scenario: 查询成功返回列表
- **WHEN** 用户调用 `GET /watchlist/stocks`
- **THEN** 返回 JSON 数组，每项包含 `code`、`name`、`market`、`added_at` 字段

#### Scenario: 列表为空时返回空数组
- **WHEN** 自选股列表为空时调用 `GET /watchlist/stocks`
- **THEN** 返回 `{"stocks": []}` 而非错误

### Requirement: 添加自选股
系统 SHALL 提供 `POST /watchlist/stocks` 接口，接收 `code`（必填）和 `market`（可选，默认 `a_stock`），将股票添加到自选股列表。

#### Scenario: 添加成功
- **WHEN** 用户 POST `{"code": "000001", "market": "a_stock"}` 到 `/watchlist/stocks`
- **THEN** 返回 HTTP 201，数据库写入该记录，`name` 字段由行情接口自动回填

#### Scenario: 重复添加幂等
- **WHEN** 用户添加一支已在自选股列表中的股票（相同 `code` + `market`）
- **THEN** 返回 HTTP 200（或 409），不产生重复记录

#### Scenario: 代码为空时拒绝
- **WHEN** 用户 POST `{"code": ""}` 到 `/watchlist/stocks`
- **THEN** 返回 HTTP 422，说明 `code` 不能为空

### Requirement: 删除自选股
系统 SHALL 提供 `DELETE /watchlist/stocks/{code}` 接口，从列表中移除指定股票。

#### Scenario: 删除存在的股票
- **WHEN** 用户调用 `DELETE /watchlist/stocks/000001`
- **THEN** 返回 HTTP 200，`000001` 从数据库中移除

#### Scenario: 删除不存在的股票
- **WHEN** 用户调用 `DELETE /watchlist/stocks/999999`（不在列表中）
- **THEN** 返回 HTTP 404，提示股票不在自选股列表中

