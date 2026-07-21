---
change: market-watchlist
design-doc: docs/superpowers/specs/2026-07-11-market-watchlist-design.md
base-ref: 8e19a4ba66d4c4a37d4eefb5e105a1979d17a1f0
archived-with: 2026-07-12-market-watchlist
---

# 自选股盯盘 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐个实施本计划。步骤用 `- [ ]` 复选框语法跟踪。

**Goal:** 在 WebUI 新增自选股盯盘页面（`/watchlist`），A 股实时涨跌轮询 + SQLite 持久化 + 一键发给 Agent 分析。

**Architecture:** 后端新建 `agent/src/api/watchlist_routes.py`，内联 SQLite 存储（独立文件 `~/.vibe-trading/watchlist.db`）+ `QuoteProvider` 抽象包装现有 `tencent_quote()`，TTL 5s 进程内缓存；前端新建独立 Zustand store + `/watchlist` 页面，3s 轮询 + Page Visibility 暂停/恢复；Agent 联动通过 URL `prefill` query param。

**Tech Stack:** Python 3.12 / FastAPI / stdlib sqlite3 / `tencent_quote`；React 19 + TypeScript / Zustand / Tailwind / react-i18next / Vite。

## Global Constraints

> 以下约束来自 Design Doc，**每个任务的验收都隐含必须满足**：

- **轮询间隔**：3 秒；页面 `hidden` 时浏览器自动节流，切回立即触发一次（`visibilitychange`）。
- **TTL 缓存**：5 秒，进程内 `dict[str, tuple[float, dict]]`，`key = code`；**不引 cachetools**。
- **行情数据源**：复用 `agent/backtest/loaders/a_stock_data.py::tencent_quote(codes)`，**零改动**，只做包装。
- **DB 路径**：独立文件 `Path.home() / ".vibe-trading" / "watchlist.db"`，不复用 sessions.db。
- **DB 初始化**：FastAPI `@app.on_event("startup")` 中幂等建表，不在首次请求中建。
- **异步化**：stdlib `sqlite3`（同步）+ `asyncio.get_event_loop().run_in_executor(None, ...)` 包装，零额外依赖。
- **鉴权**：所有 `/watchlist` 端点使用 `require_local_or_auth`（与 `/settings/llm` 等只读接口一致）。
- **涨跌颜色**：A 股惯例，上涨 `text-red-500`，下跌 `text-green-500`，平盘 `text-muted-foreground`。
- **添加校验**：前端正则 `/^\d{6}$/`，不发网络请求，inline 提示。
- **i18n**：所有新键先写 `zh-CN.json`，再补 `en.json`、`ja.json`、`ko.json`（最少补 en）。
- **安全边界**：本 change **不触及** `agent/src/live/`。验收测试**不跑**实盘窄测（`test_sdk_order_gate.py` / `test_mandate_enforcement.py`）。
- **测试命令**：后端 `pytest <path> -q`；前端 `cd frontend && npx vitest run`；前端构建 `cd frontend && npm run build`。
- **Commit**：每个任务结束立即 commit，`tasks.md` 打勾后不积攒。无 AI 署名 trailer。
