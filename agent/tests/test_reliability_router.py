"""Tests for TaskRouter deterministic capability routing (Task 3).

Safety property: the router NEVER emits shell (``bash``) or live-trading
(``trading_*``) tools in any route, including the unknown-intent fallback.
"""

from __future__ import annotations

import pytest

from src.reliability.contracts import CapabilityRoute
from src.reliability.router import TaskRouter

# Tool names classified from agent/src/tools/*_tool.py
SHELL_TOOLS = {"bash"}
LIVE_TRADING_TOOLS = {
    "trading_place_order",
    "trading_cancel_order",
    "trading_orders",
    "trading_account",
    "trading_positions",
    "trading_quote",
    "trading_history",
    "trading_check",
    "trading_connections",
    "trading_select_connection",
}
UNSAFE_TOOLS = SHELL_TOOLS | LIVE_TRADING_TOOLS

# A realistic tool_names pool the caller passes into route()
TOOL_POOL = [
    "get_market_data",
    "get_a_stock_data",
    "search_symbol",
    "get_fundamentals",
    "get_financial_statements",
    "get_stock_news",
    "get_research_reports",
    "get_sec_filings",
    "web_search",
    "read_url",
    "read_document",
    "backtest",
    "factor_analysis",
    "options_pricing",
    "extract_shadow_strategy",
    "run_shadow_backtest",
    "render_shadow_report",
    "scan_shadow_signals",
    "analyze_trade_journal",
    "iwencai_search",
    "session_search",
    # unsafe tools — must never be routed by the readonly router
    "bash",
    "trading_place_order",
    "trading_cancel_order",
]


@pytest.fixture
def router() -> TaskRouter:
    return TaskRouter()


# --- market-data routing -----------------------------------------------------


class TestMarketDataRouting:
    def test_market_data_request_includes_market_data_capability(self, router):
        route = router.route("What is the current price of AAPL?", TOOL_POOL)
        assert "market_data" in route.capabilities

    def test_market_data_request_includes_symbol_capability(self, router):
        route = router.route("Show me the candlestick chart for 600519", TOOL_POOL)
        assert "symbol" in route.capabilities or "market_data" in route.capabilities

    def test_market_data_request_excludes_live_trading(self, router):
        route = router.route("Show me AAPL candlestick data", TOOL_POOL)
        assert set(route.allowed_tools).isdisjoint(LIVE_TRADING_TOOLS)

    def test_market_data_request_excludes_shell(self, router):
        route = router.route("Get OHLC for BTC-USDT", TOOL_POOL)
        assert "bash" not in route.allowed_tools

    def test_market_data_route_returns_capability_route(self, router):
        route = router.route("price of AAPL", TOOL_POOL)
        assert isinstance(route, CapabilityRoute)


# --- unknown / ambiguous intent safety ---------------------------------------


class TestUnknownIntentSafety:
    def test_unknown_intent_returns_general_research(self, router):
        route = router.route("tell me a joke about cats", TOOL_POOL)
        assert route.intent == "general_research"

    def test_unknown_intent_never_includes_shell(self, router):
        route = router.route("asdf qwer zxcv random gibberish", TOOL_POOL)
        assert set(route.allowed_tools).isdisjoint(SHELL_TOOLS)

    def test_unknown_intent_never_includes_live_trading(self, router):
        route = router.route("asdf qwer zxcv random gibberish", TOOL_POOL)
        assert set(route.allowed_tools).isdisjoint(LIVE_TRADING_TOOLS)

    def test_unknown_intent_conservative_not_full_registry(self, router):
        route = router.route("completely unknown gibberish query", TOOL_POOL)
        assert len(route.allowed_tools) < len(TOOL_POOL)

    def test_empty_message_returns_general_research(self, router):
        route = router.route("", TOOL_POOL)
        assert route.intent == "general_research"


# --- stable ordering ---------------------------------------------------------


class TestStableOrdering:
    def test_market_data_allowed_tools_sorted(self, router):
        route = router.route("price of AAPL", TOOL_POOL)
        assert list(route.allowed_tools) == sorted(route.allowed_tools)

    def test_unknown_intent_allowed_tools_sorted(self, router):
        route = router.route("gibberish xyz", TOOL_POOL)
        assert list(route.allowed_tools) == sorted(route.allowed_tools)

    def test_backtest_allowed_tools_sorted(self, router):
        route = router.route("run a backtest", TOOL_POOL)
        assert list(route.allowed_tools) == sorted(route.allowed_tools)


# --- allowlist filtering -----------------------------------------------------


class TestAllowlistFiltering:
    def test_only_includes_tools_in_tool_names(self, router):
        narrow = ["get_market_data", "search_symbol"]
        route = router.route("price of AAPL", narrow)
        for t in route.allowed_tools:
            assert t in narrow

    def test_empty_pool_yields_empty_allowlist(self, router):
        route = router.route("price of AAPL", [])
        assert route.allowed_tools == ()

    def test_no_unsafe_tools_in_any_route(self, router):
        # Sweep across many intents: the unsafe tools must never appear.
        messages = [
            "price of AAPL",
            "backtest momentum strategy",
            "get fundamentals for TSLA",
            "latest news on NVDA",
            "search symbol BTC",
            "tell me a joke",
            "shadow account report",
            "",
        ]
        for msg in messages:
            route = router.route(msg, TOOL_POOL)
            assert set(route.allowed_tools).isdisjoint(UNSAFE_TOOLS), (
                f"unsafe tool routed for intent {route.intent!r} on {msg!r}"
            )


# --- other intent routing ----------------------------------------------------


class TestOtherRouting:
    def test_backtest_request_routes_to_backtest(self, router):
        route = router.route("Run a backtest on this momentum strategy", TOOL_POOL)
        assert route.intent == "backtest"
        assert "backtest" in route.capabilities

    def test_backtest_route_excludes_live_trading(self, router):
        route = router.route("backtest mean reversion", TOOL_POOL)
        assert set(route.allowed_tools).isdisjoint(LIVE_TRADING_TOOLS)

    def test_fundamentals_request_routes_to_fundamentals(self, router):
        route = router.route("Show me the balance sheet for AAPL", TOOL_POOL)
        assert route.intent == "fundamentals"
        assert "fundamentals" in route.capabilities

    def test_news_request_routes_to_news(self, router):
        route = router.route("latest news on NVDA", TOOL_POOL)
        assert route.intent == "news"
        assert "news" in route.capabilities

    def test_symbol_search_request(self, router):
        route = router.route("search for ticker AAPL", TOOL_POOL)
        assert "symbol" in route.capabilities

    def test_shadow_account_request(self, router):
        route = router.route("extract my shadow strategy", TOOL_POOL)
        assert route.intent == "shadow_account"
        assert "shadow_account" in route.capabilities
