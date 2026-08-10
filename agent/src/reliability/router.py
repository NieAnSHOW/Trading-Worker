"""Deterministic keyword-based capability routing for the reliability runtime.

The router maps a user message to a :class:`CapabilityRoute` — a static rule
declaring which capabilities and tool names may serve the intent. The mapping
is a small keyword table, not an LLM classifier: it must be deterministic and
auditable.

Safety property: the router NEVER emits shell (``bash``) or live-trading
(``trading_*``) tools in any route, including the unknown-intent fallback.
Shell access and live trading are gated by the downstream gateway (Task 4),
not by the router. The router's job is to narrow the rendered tool set for
readonly research intents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.reliability.contracts import CapabilityRoute

# ponytail: the router only emits readonly-research tools. Shell (bash) and
# live-trading (trading_*) tools are intentionally absent from every spec's
# candidate list, so the safety property is structural — no runtime check can
# accidentally let them through. The gateway (Task 4) gates these categories.


@dataclass(frozen=True)
class _RouteSpec:
    """Static routing rule with candidate tool names (pre-filtering)."""

    intent: str
    capabilities: tuple[str, ...]
    keywords: tuple[str, ...]
    tools: tuple[str, ...]
    complexity: str
    budgets: dict[str, int]


_GENERAL_TOOLS: tuple[str, ...] = (
    "web_search",
    "read_url",
    "read_document",
    "iwencai_search",
    "search_symbol",
    "session_search",
    "get_stock_news",
    "get_research_reports",
)

# Priority order: first keyword match wins. Shadow must precede backtest so
# "shadow strategy" matches shadow (not backtest's generic "strategy" kw).
_SPECS: tuple[_RouteSpec, ...] = (
    _RouteSpec(
        intent="shadow_account",
        capabilities=("shadow_account",),
        keywords=(
            "shadow", "trade journal", "shadow account", "shadow strategy",
            "shadow backtest", "shadow report", "交割单",
        ),
        tools=(
            "extract_shadow_strategy", "run_shadow_backtest",
            "render_shadow_report", "scan_shadow_signals",
            "analyze_trade_journal", "propose_mandate_profiles",
        ),
        complexity="high",
        budgets={"steps": 10, "tokens": 6000, "wall_clock_seconds": 240},
    ),
    _RouteSpec(
        intent="backtest",
        capabilities=("backtest",),
        keywords=(
            "backtest", "backtesting", "strategy", "factor analysis",
            "sharpe", "signal engine", "mean reversion", "momentum strategy",
            " monte carlo", "maxdd", "max drawdown",
        ),
        tools=(
            "backtest", "factor_analysis", "options_pricing",
            "alpha_bench", "alpha_compare", "alpha_zoo", "pattern",
            "report_audit", "generate_backtest_config", "scaffold_signal_engine",
            "run_research_autopilot", "link_autopilot_backtest",
        ),
        complexity="high",
        budgets={"steps": 12, "tokens": 8000, "wall_clock_seconds": 300},
    ),
    _RouteSpec(
        intent="fundamentals",
        capabilities=("fundamentals",),
        keywords=(
            "fundamental", "financial statement", "balance sheet",
            "income statement", "cash flow statement", "earnings", "财报",
            "profitability", "pe ratio", "pb ratio",
        ),
        tools=("get_fundamentals", "get_financial_statements"),
        complexity="medium",
        budgets={"steps": 5, "tokens": 3000, "wall_clock_seconds": 120},
    ),
    _RouteSpec(
        intent="news",
        capabilities=("news",),
        keywords=(
            "news", "filing", "10-k", "10-q", "research report",
            "announcement", "公告", "新闻", "press release", "sec filing",
        ),
        tools=("get_stock_news", "get_research_reports", "get_sec_filings"),
        complexity="low",
        budgets={"steps": 5, "tokens": 3000, "wall_clock_seconds": 120},
    ),
    _RouteSpec(
        intent="market_data",
        capabilities=("market_data", "symbol"),
        keywords=(
            "price", "quote", "market data", "candlestick", "candle",
            "k线", "kline", "ohlc", "fund flow", "northbound",
            "sector", "options chain", "macro", "screener", "a-stock",
            "a stock", "stock data", "dragon tiger", "margin trading",
            "lockup", "shareholder", "block trade", "stock profile",
        ),
        tools=(
            "get_market_data", "get_a_stock_data", "get_fund_flow",
            "get_northbound_flow", "get_block_trades", "get_margin_trading",
            "get_lockup_expiry", "get_shareholder_count", "get_sector_info",
            "screen_market", "get_dragon_tiger", "get_stock_profile",
            "get_options_chain", "get_macro_series",
        ),
        complexity="low",
        budgets={"steps": 6, "tokens": 3000, "wall_clock_seconds": 120},
    ),
    _RouteSpec(
        intent="symbol_resolution",
        capabilities=("symbol",),
        keywords=("symbol", "ticker", "股票代码", "search symbol", "resolve symbol"),
        tools=("search_symbol",),
        complexity="low",
        budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60},
    ),
)

# Conservative fallback for unknown / ambiguous intent. NEVER includes shell
# or live-trading tools.
_FALLBACK = _RouteSpec(
    intent="general_research",
    capabilities=("general_research", "symbol"),
    keywords=(),
    tools=_GENERAL_TOOLS,
    complexity="medium",
    budgets={"steps": 8, "tokens": 4000, "wall_clock_seconds": 180},
)


class TaskRouter:
    """Deterministic keyword-based router.

    Maps a user message to a :class:`CapabilityRoute` by matching against a
    small priority-ordered keyword table. The returned route's
    ``allowed_tools`` is the intersection of the matched spec's candidate
    tools and the ``tool_names`` the caller passes (so only tools that the
    caller actually has available are advertised). The allowlist is sorted
    for stable output.

    Unknown or ambiguous intent falls back to a conservative general-research
    route that never includes shell or live-trading tools.
    """

    def route(self, user_message: str, tool_names: Sequence[str]) -> CapabilityRoute:
        """Route ``user_message`` to a capability rule.

        Args:
            user_message: The user's natural-language request.
            tool_names: Tool names the caller has available. Only names in
                this sequence may appear in the returned route's
                ``allowed_tools``.

        Returns:
            A ``CapabilityRoute`` whose ``allowed_tools`` is the sorted
            intersection of the matched spec's candidate tools and
            ``tool_names``.
        """
        available = set(tool_names)
        msg = (user_message or "").lower()
        spec = _match(msg) if msg else _FALLBACK
        allowed = tuple(t for t in spec.tools if t in available)
        # ponytail: sorted() gives stable output without a stable-sort lib.
        allowed = tuple(sorted(allowed))
        return CapabilityRoute(
            intent=spec.intent,
            capabilities=spec.capabilities,
            allowed_tools=allowed,
            complexity=spec.complexity,
            budgets=dict(spec.budgets),
        )


def _match(lowered_message: str) -> _RouteSpec:
    """Return the first spec whose keywords appear in the lowercased message."""
    for spec in _SPECS:
        for kw in spec.keywords:
            if kw in lowered_message:
                return spec
    return _FALLBACK


__all__ = ["TaskRouter"]
