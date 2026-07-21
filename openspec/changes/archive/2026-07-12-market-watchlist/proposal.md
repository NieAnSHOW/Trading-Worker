## Why

用户在使用 Vibe Trading 进行量化研究和 Agent 分析时，缺乏一个可以实时盯盘的入口——目前必须离开 WebUI 切换到同花顺、东方财富等外部工具才能查看自选股涨跌。提供原生的自选股盯盘页面，可以将「看盘→分析」的工作流闭合在 Vibe Trading 内部，降低上下文切换成本。

## What Changes

- **新增独立盯盘页面** `/watchlist`：展示用户自选股的实时价格、涨跌幅、涨跌额
- **新增侧边栏导航项**：在现有导航中插入「自选股」入口
- **新增自选股管理**：支持添加（按股票代码）、删除自选股
- **新增本地持久化**：自选股列表存储到 `~/.vibe-trading/watchlist.db`（SQLite）
- **新增行情查询 API**：后端包装现有 `tencent_quote` 实现 `GET /watchlist/quotes?codes=...`，预留多市场 provider 抽象
- **新增 Agent 联动**：用户可选中一支或多支股票，一键发送到 `/agent` 会话，预填「帮我分析 XXXXX 股票名称」

## Capabilities

### New Capabilities

- `watchlist-storage`：本地 SQLite 自选股 CRUD（增删查），存储路径 `~/.vibe-trading/watchlist.db`
- `watchlist-quotes`：A 股实时行情查询接口，包装 `tencent_quote`，预留 provider 扩展点
- `watchlist-ui`：前端盯盘页面，含自选股列表、实时刷新（3s 轮询）、添加/删除交互
- `watchlist-agent-link`：选中股票→联动 Agent 会话，自动预填分析请求

### Modified Capabilities

（无现有 spec 需修改）

## Impact

**后端**
- `agent/src/api/watchlist_routes.py`（新建）：CRUD + 行情路由
- `agent/api_server.py`：注册新 router
- `agent/backtest/loaders/a_stock_data.py`：`tencent_quote` 被行情接口复用（只读，无改动）
- 依赖：`aiosqlite` 或 `sqlite3`（stdlib，无需新增依赖）

**前端**
- `frontend/src/pages/Watchlist.tsx`（新建）
- `frontend/src/router.tsx`：新增 `/watchlist` 路由
- `frontend/src/components/layout/Layout.tsx`：侧边栏新增导航项
- `frontend/src/lib/api.ts`：新增 watchlist CRUD + quotes API 函数
- `frontend/src/stores/`：新增 watchlist Zustand store（或扩展现有 store）
- Vite 代理：新增 `/watchlist` 路径代理到后端 `:8899`

**无破坏性变更**，现有功能不受影响。
