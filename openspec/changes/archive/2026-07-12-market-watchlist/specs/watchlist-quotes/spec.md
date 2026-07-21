## ADDED Requirements

### Requirement: 批量查询实时行情
系统 SHALL 提供 `GET /watchlist/quotes?codes=000001,600519` 接口，接收逗号分隔的股票代码列表，返回每支股票的实时行情数据。内部通过 `QuoteProvider` 抽象层调用数据源，默认使用 `TencentQuoteProvider`（A 股）。

#### Scenario: 批量查询成功
- **WHEN** 用户调用 `GET /watchlist/quotes?codes=000001,600519`
- **THEN** 返回 JSON 对象，键为股票代码，值包含 `code`、`name`、`price`、`change_pct`、`change_amt`、`high`、`low`、`open`、`last_close` 字段

#### Scenario: 部分代码查询失败时降级返回
- **WHEN** 查询多支股票时，其中一支代码无效或数据源超时
- **THEN** 失败的股票条目 `price` 为 null，包含 `error` 字段，其他股票正常返回

#### Scenario: 空 codes 参数时返回错误
- **WHEN** 用户调用 `GET /watchlist/quotes` 不携带 `codes` 参数
- **THEN** 返回 HTTP 422，说明 `codes` 参数必填

### Requirement: QuoteProvider 抽象扩展点
系统 SHALL 定义 `QuoteProvider` Protocol 接口，包含 `fetch(codes: list[str]) -> dict[str, dict]` 方法。路由层通过股票代码前缀或 `market` 参数选择 Provider，当前仅实现 `TencentQuoteProvider`（A 股，代码无前缀或以 `sh`/`sz` 前缀开头）。

#### Scenario: A 股代码路由到 TencentQuoteProvider
- **WHEN** 请求包含纯数字 A 股代码（如 `000001`、`600519`）
- **THEN** 系统使用 `TencentQuoteProvider` 获取数据

#### Scenario: 未知市场代码返回明确错误
- **WHEN** 请求包含当前不支持的市场代码前缀（如港股 `00700.HK`）
- **THEN** 返回该条目 `error: "market not supported"`，其他支持的条目正常返回

### Requirement: 行情接口超时与错误处理
行情接口 SHALL 设置 10 秒超时，数据源不可用时返回最后一次缓存值（若存在）并在响应中标注 `stale: true`；无缓存时返回空值并标注 `error`，不抛出 HTTP 500。

#### Scenario: 数据源超时时返回 stale 数据
- **WHEN** 腾讯行情接口超过 10s 未响应
- **THEN** 接口在 10s 内返回，包含上次缓存的行情数据并附 `stale: true` 标记

#### Scenario: 首次请求且数据源不可用
- **WHEN** 首次请求某股票行情且数据源不可用（无缓存）
- **THEN** 该条目 `price` 为 null，包含 `error` 字段，HTTP 状态码为 200
