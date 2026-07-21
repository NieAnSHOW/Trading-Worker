---
comet_change: market-watchlist
role: technical-design
canonical_spec: openspec
archived-with: 2026-07-12-market-watchlist
status: final
---

# 自选股盯盘 设计文档

> 本文档是 `market-watchlist` change 的深度技术设计（design 阶段产物，2026-07-11）。
> 上游事实源为 OpenSpec delta spec：`openspec/changes/market-watchlist/specs/*/spec.md`。
> open 阶段 `design.md` 给出高层决策框架；本文档对其做实现级细化，不重写需求。

## 1. 上下文

**现状缺口**：Vibe Trading 具备 LangGraph Agent 分析、回测引擎、Alpha Zoo，但无原生盯盘入口。用户必须切换到同花顺/东方财富等外部工具查看涨跌，再回到 WebUI 发起分析，上下文切换成本高。

**目标**：补齐一条最小可用链路——自选股清单管理（SQLite 持久化）+ 批量实时行情（provider 抽象，A 股落地）+ 常驻盯盘页面（前端轮询）+ Agent 联动（prefill query param）。

**已有可复用**：
- `agent/backtest/loaders/a_stock_data.py::tencent_quote(codes)` — 无 auth，支持批量 A 股实时行情（价格、涨跌幅、涨跌额、高低价等）
- `frontend/src/hooks/useSSE.ts` — 本次不用 SSE，但基础设施完善
- `~/.vibe-trading/` 运行时目录 — 已有 venv、agent 代码等，适合存放本地 DB
- `frontend/src/pages/Agent.tsx` 已有 `useSearchParams` — prefill 支持只需 3 行

**非目标**：港股/美股/期货/加密、SSE 推送、迷你 K 线图、价格预警、多设备云同步。

## 2. 后端架构

### 2.1 存储层：`agent/src/api/watchlist_routes.py`

选择在路由文件中内联存储逻辑（而非单独 `store.py`），原因是 CRUD 逻辑极简，独立模块收益不足以抵消额外层次。如后续扩展（reorder、多市场），可再拆出。

**DB 路径**：独立文件 `~/.vibe-trading/watchlist.db`（不复用 sessions.db，watchlist 是用户偏好数据，独立文件更清晰，迁移/删除互不影响）。

**路径获取**：
```python
from pathlib import Path
DB_PATH = Path.home() / ".vibe-trading" / "watchlist.db"
```

**表结构**（幂等建表）：
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

**DB 初始化时机**：FastAPI `lifespan` startup（而非首次请求），避免首次 API 调用有额外延迟。

**异步化**：stdlib `sqlite3`（同步）+ `asyncio.get_event_loop().run_in_executor(None, ...)` 包装写操作。读操作用线程池同样包装，保持风格一致。零额外依赖。

**路由设计**：

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `/watchlist/stocks` | `require_local_or_auth` | 返回完整列表，按 `added_at` 倒序 |
| POST | `/watchlist/stocks` | `require_local_or_auth` | body `{code, market?}`；重复时返回 HTTP 200 + `{exists: true}` |
| DELETE | `/watchlist/stocks/{code}` | `require_local_or_auth` | 不存在时返回 404 |
| GET | `/watchlist/quotes` | `require_local_or_auth` | `?codes=000001,600519`；批量行情 |

> `require_local_or_auth` 与现有 `/settings/llm` 等只读接口保持一致，本地运行无需 key。

### 2.2 行情层：QuoteProvider 抽象 + TencentQuoteProvider

**Protocol 定义**（Python `typing.Protocol`，比 ABC 更轻量）：
```python
from typing import Protocol

class QuoteProvider(Protocol):
    market: str
    def fetch(self, codes: list[str]) -> dict[str, dict]: ...
```

**TencentQuoteProvider**（A 股，默认）：
- 直接调用现有 `tencent_quote(codes)` 函数，**零改动**，只做包装
- 返回结构对齐 spec 要求：`{code, name, price, change_pct, change_amt, high, low, open, last_close}`

**市场路由**（当前只有 A 股）：
```python
# codes 中若包含未支持市场（如 00700.HK），按条目降级
def _route(code: str) -> str:
    if code.replace(".", "").isdigit() and len(code) == 6:
        return "a_stock"
    return "unsupported"
```

**内存 TTL 缓存**（模块级，自建，不引 cachetools）：
```python
_quote_cache: dict[str, tuple[float, dict]] = {}  # code -> (expires_at, data)
CACHE_TTL = 5.0  # seconds
```

- 每次查询先过滤缓存命中，只把缺失/过期的 codes 打到上游
- 超时 10s（`tencent_quote` 内部已有 10s timeout）
- 失败时返回缓存值（`stale: true`）或空值（`error` 字段），**HTTP 状态码始终 200**

**响应结构**：
```json
{
  "000001": {"code": "000001", "name": "平安银行", "price": 11.2, "change_pct": 2.3, "change_amt": 0.25, "stale": false},
  "000002": {"code": "000002", "name": null, "price": null, "error": "fetch_failed", "stale": false}
}
```

### 2.3 注册到 api_server.py

```python
from src.api.watchlist_routes import router as watchlist_router
app.include_router(watchlist_router)
```

在现有 `optional_deps_router` 注册后追加，不影响其他路由。

## 3. 前端架构

### 3.1 Zustand Store：`frontend/src/stores/watchlist.ts`（独立）

选择独立 store 而非扩展 agent store，原因：agent store 只管聊天/SSE 状态，watchlist 是完全独立的 domain；混合违反单一职责，且 agent store 已有 50+ 行 interface，测试隔离也更难。

**State interface**：
```typescript
interface WatchlistState {
  stocks: WatchlistStock[];         // 自选股列表
  quotes: Record<string, QuoteData>; // code -> 行情
  selected: Set<string>;            // 选中的 codes
  loading: boolean;
  quotesLoading: boolean;
  error: string | null;

  loadStocks: () => Promise<void>;
  addStock: (code: string) => Promise<void>;
  removeStock: (code: string) => Promise<void>;
  toggleSelect: (code: string) => void;
  clearSelection: () => void;
  fetchQuotes: () => Promise<void>;
}
```

### 3.2 轮询逻辑

在 `Watchlist.tsx` 的 `useEffect` 中管理（而非 store 内部），原因：轮询生命周期绑定组件挂载/卸载，放在组件更自然，也更易测试（mock timer 不需要 mock store）。

```typescript
useEffect(() => {
  fetchQuotes(); // 立即触发一次
  const id = setInterval(fetchQuotes, 3000);

  const onVisibilityChange = () => {
    if (!document.hidden) fetchQuotes(); // 切回立即刷新
  };
  document.addEventListener("visibilitychange", onVisibilityChange);

  return () => {
    clearInterval(id);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  };
}, [stocks]); // stocks 变化（添加/删除）时重新绑定，更新 codes 列表
```

注意：当页面 `hidden` 时 `setInterval` 在浏览器中会被节流（通常降到 1 次/秒），自然实现降频，无需手动切换间隔。

### 3.3 页面结构：`frontend/src/pages/Watchlist.tsx`

```
Watchlist.tsx
├── AddStockForm        // 输入框 + 6位校验 + 提交
├── (空状态 EmptyState) // stocks.length === 0 时展示
└── StockTable          // 行情表格
    ├── StockRow[]      // 每行：checkbox + code + name + price + change_pct + change_amt + 删除按钮
    └── BulkActions     // 底部：已选 N 支 + 「发给 Agent 分析」按钮
```

**涨跌颜色**（A 股惯例，上涨红 / 下跌绿 / 平盘灰）：
```typescript
const changeColor = (pct: number | null) => {
  if (pct === null) return "text-muted-foreground";
  if (pct > 0) return "text-red-500";
  if (pct < 0) return "text-green-500";
  return "text-muted-foreground";
};
```

**添加校验**：正则 `/^\d{6}$/`，前端 inline 验证，不发网络请求。

**删除确认**：用 `window.confirm` 或 Radix AlertDialog（复用现有 UI 库），防误删。

### 3.4 Agent 联动：prefill query param

```typescript
// 发给 Agent 按钮 onClick
const sendToAgent = () => {
  const selectedStocks = stocks.filter(s => selected.has(s.code));
  const text = selectedStocks.length === 1
    ? `帮我分析 ${selectedStocks[0].code} ${selectedStocks[0].name}`
    : `帮我分别分析以下股票：${selectedStocks.map(s => `${s.code} ${s.name}`).join("、")}`;
  navigate(`/agent?prefill=${encodeURIComponent(text)}`);
};
```

**Agent 页面修改**（最小改动，3 行）：
```typescript
// frontend/src/pages/Agent.tsx，在 useState 初始化后
const prefillParam = searchParams.get("prefill");
useEffect(() => {
  if (prefillParam) {
    setInput(decodeURIComponent(prefillParam));
    setSearchParams(); // 读后清除，防刷新重填
  }
}, []); // eslint-disable-line react-hooks/exhaustive-deps
```

### 3.5 路由与导航

**router.tsx**：
```typescript
const Watchlist = lazy(() => import("@/pages/Watchlist").then((m) => ({ default: m.Watchlist })));
// 在 routes 数组中 /agent 后加：
{ path: "/watchlist", element: wrap(Watchlist) },
```

**Layout.tsx 侧边栏**（/agent 行后新增一行）：
```tsx
<NavLink to="/watchlist" icon={Star} label={t("layout.watchlist")} ... />
```

图标用 `lucide-react` 的 `Star`（已在现有导航中使用 `Layers`、`BarChart3` 等，风格一致）。

**Vite proxy**（`PROXY_PATHS` 数组加一项）：
```typescript
"/watchlist",  // 新增
```

**i18n**：在 `frontend/src/i18n/` 中新增 `layout.watchlist` 键，中文值「自选股」。

## 4. 数据流

```
用户打开 /watchlist
  → store.loadStocks()   GET /watchlist/stocks   → SQLite → [stock list]
  → 渲染表格（行情加载中...）
  → fetchQuotes()        GET /watchlist/quotes?codes=...
      后端: 缓存命中(TTL 5s)? → 返回缓存
            否则 TencentQuoteProvider.fetch(codes)
              → tencent_quote(codes) → qt.gtimg.cn → GBK 解析
              → 写缓存 → 返回 {code: QuoteData}
  → 表格更新（红绿颜色、价格）
  → setInterval 3s 后再次 fetchQuotes()
  → 用户切标签页 → visibilitychange hidden → interval 被浏览器节流（降频）
  → 用户切回 → visibilitychange visible → 立即 fetchQuotes()

用户勾选股票 → 点「发给 Agent 分析」
  → navigate('/agent?prefill=帮我分析 000001 平安银行')
  → Agent 页面 useEffect 读 prefill → setInput(text) → setSearchParams({})
  → 用户编辑后发送
```

## 5. 关键取舍与风险

| 风险 | 缓解措施 |
|------|---------|
| 腾讯行情接口偶发超时/限流 | TTL 5s 缓存 + stale 降级，前端不崩溃，显示上次值 |
| macOS/Windows DB 路径差异 | `Path.home() / ".vibe-trading"` 统一，已有约定 |
| prefill 刷新重填 | `setSearchParams({})` 读后清除，URL 不残留 |
| DB 并发（极低）| stdlib sqlite3 单用户本地，无并发压力 |
| `tencent_quote` GBK 解析 | 现有函数已处理，零改动 |
| 空 stocks 时无谓轮询 | fetchQuotes 内部判断 codes 为空直接返回，不发请求 |

## 6. 测试策略

**后端**（`agent/tests/test_watchlist_*.py`）：
- CRUD 幂等：首次建库建表、`POST` 重复添加返回 200 + `exists:true`、`DELETE` 不存在返回 404
- 行情：mock `tencent_quote` 返回固定数据，验证响应结构和字段映射
- TTL 缓存：同 codes 两次调用，第二次不触上游（mock 计数器验证）
- 部分失败：一支 code mock 超时，其他正常返回，HTTP 200
- 空 codes：返回空对象，HTTP 200
- monkeypatch `DB_PATH` 到 `tmp_path`，避免污染 `~/.vibe-trading/`

**前端**（`frontend/src/**/__tests__/`）：
- watchlist store：add/remove/toggleSelect actions，loading/error 状态
- Watchlist 页面：空状态渲染、6位校验（有效/无效代码）、行情颜色（红/绿/灰）
- 轮询：vitest fake timer 验证 mount 启动 / unmount 清理 / `stocks` 变化重绑定
- Agent prefill：`useSearchParams` mock，验证 input 预填 + param 清除

**集成手验**：添加 `000001` → 3s 刷新价格 → 关闭重开浏览器确认持久化 → 勾选 → 发给 Agent → 输入框预填。

## 7. 文件清单（build 阶段产出）

**新建**：
- `agent/src/api/watchlist_routes.py` — CRUD + 行情路由 + QuoteProvider + TencentQuoteProvider + TTL 缓存
- `agent/tests/test_watchlist_routes.py` — 后端测试
- `frontend/src/stores/watchlist.ts` — Zustand store
- `frontend/src/pages/Watchlist.tsx` — 盯盘页面
- 前端测试文件

**修改**：
- `agent/api_server.py` — 注册 watchlist router + lifespan startup 初始化 DB
- `frontend/src/router.tsx` — 新增 `/watchlist` 懒加载路由
- `frontend/src/components/layout/Layout.tsx` — 侧边栏新增 `Star` 导航项
- `frontend/src/lib/api.ts` — 新增 watchlist CRUD + quotes API 函数
- `frontend/src/pages/Agent.tsx` — 新增 prefill useEffect（3 行）
- `frontend/vite.config.ts` — `PROXY_PATHS` 新增 `"/watchlist"`
- i18n 资源文件 — 新增 `layout.watchlist` 等键
