## 1. 后端：watchlist 存储

- [x] 1.1 新建 watchlist SQLite store（仿 `agent/src/goal/store.py`）：`CREATE TABLE IF NOT EXISTS`、CRUD 方法、首次访问自动建表
- [ ] 1.2 watchlist REST 路由（`GET/POST/DELETE /watchlist`）并挂载到 `agent/api_server.py`；保证 agent 可程序化调用同一存储
- [ ] 1.3 股票搜索端点（代码 / 名称 → 候选列表，含市场标记），供前端添加自选

## 2. 后端：market-quotes 能力

- [ ] 2.1 定义 `QuoteProvider` 接口与 `Quote` 数据模型；A 股实现 `EastmoneyQuoteProvider`（复用 `market_screener_tool` 的 `push2/clist` 路径 + `_http.py` 的 `HostThrottle`）
- [ ] 2.2 进程内 TTL 缓存层（key = 排序后的 symbols fingerprint）
- [ ] 2.3 A 股交易时段判定（工作日 9:30–11:30、13:00–15:00）+ `market_closed` 标志
- [ ] 2.4 批量 quotes 路由（`POST /quotes`）：按市场分发，未支持市场返回明确 `该市场暂未支持` 响应

## 3. 前端：store 与 API 层

- [ ] 3.1 新建 `market` Zustand store（watchlist + quotes 状态、轮询生命周期、页面不可见时降频）
- [ ] 3.2 API 层封装 watchlist CRUD 与 quotes 调用（`frontend/src/lib/api.ts`）

## 4. 前端：盯盘页面

- [ ] 4.1 新增 `/watchlist` 路由与左侧导航项（`frontend/src/router.tsx` + 导航组件）
- [ ] 4.2 搜索添加组件（搜索框 + 候选下拉 + 防重复添加）
- [ ] 4.3 盯盘表格（最新价 / 涨跌幅 / 涨跌额 / 成交量 + 红绿配色 + sparkline 复用 `MiniEquityChart`）
- [ ] 4.4 非交易时段 `收盘` 标注与降级展示

## 5. 验证

- [ ] 5.1 后端窄测试：watchlist CRUD、quotes 批量、TTL 缓存命中、未支持市场响应
- [ ] 5.2 端到端手验：搜索添加 → 轮询刷新 → 刷新页面持久化 → 非交易时段降级
