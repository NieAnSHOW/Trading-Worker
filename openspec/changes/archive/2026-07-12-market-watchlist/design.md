## Context

Vibe Trading Desktop 已具备 LangGraph Agent 分析能力、回测引擎和 Alpha 策略库，但缺少实时盯盘入口。用户需要在外部工具和 WebUI 之间频繁切换。

现有可复用能力：
- `agent/backtest/loaders/a_stock_data.py` 中的 `tencent_quote(codes)` 函数：无 auth、支持批量查询 A 股实时行情（价格、涨跌幅、涨跌额、高低价等）
- `frontend/src/hooks/useSSE.ts`：SSE hook（本次不用 SSE，但基础设施完善）
- `~/.vibe-trading/` 运行时目录：已有 venv、agent 代码等，适合存放本地 DB

## Goals / Non-Goals

**Goals:**
- 用户可在 `/watchlist` 页面管理自选股（增/删/查）
- 每 ~3 秒自动刷新行情，展示价格、涨跌幅、涨跌额
- 自选股列表持久化到 `~/.vibe-trading/watchlist.db`（SQLite）
- 侧边栏新增「自选股」导航项
- 支持选中股票一键发送到 Agent 会话做分析

**Non-Goals:**
- 港股、美股、期货、加密货币
- 后端 SSE 推流（用前端轮询替代）
- 迷你 K 线图
- 价格预警/自动触发 Agent
- 多设备云同步

## Decisions

### D1：行情数据源 — 继续使用 `tencent_quote`，包装为 HTTP 接口

**决定**：后端新增 `GET /watchlist/quotes?codes=000001,600519`，内部调用现有 `tencent_quote` 函数。

**为什么不直接在前端调用腾讯接口**：腾讯行情接口不支持 CORS，浏览器直接调用会被拦截。

**多市场扩展点**：引入 `QuoteProvider` 抽象（Protocol），`tencent_quote` 实现为 `TencentQuoteProvider`。将来接入港股（东方财富）或美股（yfinance）时只需增加新 Provider，路由层通过股票代码前缀或 `market` 参数派发。

```
GET /watchlist/quotes?codes=000001,600519
                      ↓
              QuoteProvider.fetch(codes)
                      ↓
         TencentQuoteProvider (A股，默认)
         HKQuoteProvider      (港股，未来)
         USQuoteProvider      (美股，未来)
```

### D2：持久化 — SQLite via stdlib `sqlite3`，路径 `~/.vibe-trading/watchlist.db`

**决定**：使用 Python 标准库 `sqlite3`（同步），FastAPI 端点用 `run_in_executor` 包装异步化。

**为什么不用 aiosqlite**：无需新增依赖，`~/.vibe-trading/` 是单用户本地目录，并发极低，性能不是瓶颈。

**为什么不复用现有 DB**：watchlist 是用户偏好数据，独立文件更清晰，迁移/删除互不影响。

表结构：
```sql
CREATE TABLE IF NOT EXISTS watchlist (
    code     TEXT NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    market   TEXT NOT NULL DEFAULT 'a_stock',
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, market)
);
```

### D3：前端刷新 — `setInterval` 轮询，间隔 3000ms

**决定**：`useEffect` 挂载时启动 interval，每 3s 请求一次 `/watchlist/quotes`；页面 unmount 时清理。

**为什么不用 SSE**：`tencent_quote` 本身是 pull 模型，后端 SSE 只是再包一层 push，增加复杂度而无收益。轮询对盯盘延迟（3-5s）完全够用。

### D4：Agent 联动 — `navigate('/agent')` + URL query param

**决定**：选中股票后调用 `navigate('/agent?prefill=帮我分析 000001 平安银行')`，Agent 页面读取 `prefill` query param 并预填消息输入框（不自动提交，用户可先编辑）。

**为什么不直接注入 Zustand store**：URL query param 更显式，支持直接链接跳转，不依赖 store 初始化顺序。

**多选支持**：多只股票时合并为一条消息，如「帮我分别分析以下股票：000001 平安银行、600519 贵州茅台」。

### D5：Vite 代理 — 新增 `/watchlist` 路径

**决定**：在 `frontend/vite.config.ts` 的 proxy 配置中新增 `/watchlist`，与现有 `/sessions`、`/settings` 等路径保持一致。

## Risks / Trade-offs

| 风险 | 缓解措施 |
|------|---------|
| 腾讯行情接口不稳定/限流 | 接口失败时前端显示上次缓存值 + 错误提示，不崩溃页面 |
| `~/.vibe-trading/` 在桌面版中路径解析 | 复用现有 `runtime_dir` 模块的路径逻辑，保证 macOS/Windows 一致 |
| 轮询在标签页后台仍运行 | 用 `document.visibilityState` 暂停后台轮询，减少无谓请求 |
| Agent 页面不支持 `prefill` param | Agent 页面改动最小：仅读取并预填输入框，不影响现有功能 |

## Open Questions

- 添加股票时是否需要股票名称补全（输入代码自动显示股票名）？本次先用行情接口返回的 `name` 字段回填，后续可加搜索补全。
- `sort_order` 是否支持用户拖拽排序？本次按 `added_at` 倒序，不做拖拽。
