"""自选股 HTTP 路由（供 Web UI 使用）。

由 ``agent/api_server.py`` 通过 ``register_watchlist_routes(app, ...)`` 挂载。
仿 ``alpha_routes.py`` 抽出独立模块，避免继续膨胀 ``api_server.py``。

路由（鉴权依赖由 host 模块提供，见 ``register_watchlist_routes`` 的 fallback）：

- ``GET    /watchlist``            —— 列出自选股（本地或鉴权可读）
- ``POST   /watchlist``            —— 加入一只股票（需鉴权），重复返回 409
- ``DELETE /watchlist/{symbol}``   —— 移除一只股票（需鉴权），不存在返回 404
- ``GET    /watchlist/search``     —— 按名称/代码搜索候选（包装 ``SymbolSearchTool``）

进程内共享一个模块级 ``WatchlistStore`` 单例（与 goal store 同库），首次调用懒初始化。
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Query

from src.watchlist import WatchlistItemExists, WatchlistStore

logger = logging.getLogger(__name__)

# 进程内共享的自选股存储单例。首次请求时懒初始化，绑定默认库路径
# （可由 VIBE_TRADING_WATCHLIST_DB_PATH 覆盖，见 WatchlistStore）。
_STORE: WatchlistStore | None = None


def _get_store() -> WatchlistStore:
    """返回进程级自选股存储，首次调用时构建。"""
    global _STORE
    if _STORE is None:
        _STORE = WatchlistStore()
    return _STORE


AuthDep = Callable[..., Awaitable[Any] | Any]


def register_watchlist_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
    require_local_or_auth: AuthDep | None = None,
) -> None:
    """把自选股路由挂载到 ``app`` 上。

    Args:
        app: 宿主 FastAPI 应用。
        require_auth: 写操作的 Bearer 鉴权依赖。
        require_local_or_auth: 读操作的“本地或鉴权”依赖。

    为向后兼容，未显式传入依赖时从 host ``api_server`` 模块解析
    （同 ``register_alpha_routes`` 的 fallback）。新调用点建议显式传入。
    """
    if require_auth is None or require_local_or_auth is None:
        import sys as _sys

        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        if host is None:  # pragma: no cover - 仅在异常 import 布局下触发
            raise RuntimeError(
                "register_watchlist_routes: api_server module not in sys.modules; "
                "pass require_auth/require_local_or_auth explicitly"
            )
        if require_auth is None:
            require_auth = host.require_auth
        if require_local_or_auth is None:
            require_local_or_auth = host.require_local_or_auth

    # -----------------------------------------------------------------------
    # GET /watchlist
    # -----------------------------------------------------------------------

    @app.get("/watchlist", dependencies=[Depends(require_local_or_auth)])
    async def list_watchlist(market: str = Query("a_share", max_length=32)) -> list[dict[str, Any]]:
        """列出某市场的自选股，按加入顺序返回。"""
        rows = _get_store().list(market=market)
        return [dataclasses.asdict(r) for r in rows]

    # -----------------------------------------------------------------------
    # POST /watchlist
    # -----------------------------------------------------------------------

    @app.post("/watchlist", status_code=201, dependencies=[Depends(require_auth)])
    async def add_watchlist(payload: dict[str, Any]) -> dict[str, Any]:
        """加入一只股票；(symbol, market) 重复时返回 409。"""
        symbol = str(payload.get("symbol") or "").strip()
        market = str(payload.get("market") or "a_share").strip() or "a_share"
        name = str(payload.get("name") or "").strip()
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol 不能为空")
        try:
            row = _get_store().add(symbol, market, name)
        except WatchlistItemExists:
            raise HTTPException(status_code=409, detail=f"{symbol} 已在自选股中")
        return dataclasses.asdict(row)

    # -----------------------------------------------------------------------
    # DELETE /watchlist/{symbol}
    # -----------------------------------------------------------------------

    @app.delete("/watchlist/{symbol}", dependencies=[Depends(require_auth)])
    async def remove_watchlist(
        symbol: str, market: str = Query("a_share", max_length=32)
    ) -> dict[str, str]:
        """移除一只股票；不存在时返回 404。"""
        if not _get_store().remove(symbol, market=market):
            raise HTTPException(status_code=404, detail=f"{symbol} 不在自选股中")
        return {"status": "removed"}

    # -----------------------------------------------------------------------
    # GET /watchlist/search
    # -----------------------------------------------------------------------

    @app.get("/watchlist/search", dependencies=[Depends(require_local_or_auth)])
    async def search_symbol(q: str = Query(..., min_length=1, max_length=64)) -> list[dict[str, Any]]:
        """按名称/代码搜索候选，仅透出 {symbol, name, market} 三字段。"""
        from src.tools.symbol_search_tool import SymbolSearchTool

        raw = SymbolSearchTool().execute(query=q, limit=10)
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("symbol search 返回非法 JSON: %r", raw[:200])
            return []
        if not payload.get("ok"):
            logger.warning("symbol search 失败: %s", payload.get("error"))
            return []
        candidates = (payload.get("data") or {}).get("candidates") or []
        return [
            {
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "market": c.get("market"),
            }
            for c in candidates
            if isinstance(c, dict) and c.get("symbol")
        ]
