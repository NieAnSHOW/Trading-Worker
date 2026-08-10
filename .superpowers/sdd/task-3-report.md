# Task 3 Report: Route capabilities and validate execution plans

## Status: DONE

## What was implemented

Three deliverables for the Agent Reliability Runtime (Task 3 of 9):

1. **`agent/src/reliability/router.py`** — `TaskRouter.route(user_message, tool_names) -> CapabilityRoute`. A small, deterministic, priority-ordered keyword table mapping user messages to one of 7 capability routes: `shadow_account`, `backtest`, `fundamentals`, `news`, `market_data`, `symbol_resolution`, and a conservative `general_research` fallback. The returned `allowed_tools` is the sorted intersection of the matched spec's candidate tools and the caller's `tool_names`. Unknown/ambiguous/empty intent falls back to `general_research`.

2. **`agent/src/reliability/planner.py`** — `PlanStep` and `ExecutionPlan` (Pydantic 2 `BaseModel`), plus `PlanValidator` with:
   - `validate(plan, route)` — raises `ValueError` on: unknown capabilities (not in `route.capabilities`); side-effecting steps in readonly capabilities (`market_data`, `symbol`, `fundamentals`, `news`, `general_research`); missing dependencies; dependency cycles (Kahn topological traversal); step-count/token/wall-clock budget overflow (both plan-internal and plan-vs-route-envelope).
   - `ready_steps(plan, completed)` — returns steps whose dependencies are all in `completed`, excluding completed steps, in stable declaration order.

3. **`agent/src/agent/context.py`** — extended `ContextBuilder.__init__` with `allowed_tool_names: Collection[str] | None = None`. When set, `_format_tool_descriptions()` renders only the named tools and `build_system_prompt` reports the filtered count. When `None` (default), existing behavior is byte-for-byte unchanged. The registry itself is never mutated — the gateway (Task 4) still sees all tools.

Plus `agent/src/reliability/__init__.py` re-exports for the new symbols.

## Files changed

| File | Change |
|---|---|
| `agent/src/reliability/router.py` | new (203 lines) |
| `agent/src/reliability/planner.py` | new (188 lines) |
| `agent/src/agent/context.py` | modified (+51/-7): added `allowed_tool_names` param, filtered rendering |
| `agent/src/reliability/__init__.py` | modified (+6): re-export `TaskRouter`, `PlanStep`, `ExecutionPlan`, `PlanValidator` |
| `agent/tests/test_reliability_router.py` | new (195 lines, 24 tests) |
| `agent/tests/test_reliability_planner.py` | new (363 lines, 26 tests) |

## TDD evidence

- **RED**: `pytest tests/test_reliability_router.py tests/test_reliability_planner.py -q` -> 2 collection errors (`ModuleNotFoundError: No module named 'src.reliability.router'` / `planner`).
- **GREEN after fix**: `pytest tests/test_reliability_router.py tests/test_reliability_planner.py -q` -> `50 passed in 2.00s`.
- Initial GREEN had 3 failures (shadow-route priority, `_format_tool_descriptions` not consulting stored allowlist); both fixed before commit.

## Step 8 regression output

```
pytest tests/test_context_attribution_layers.py tests/test_reliability_router.py tests/test_reliability_planner.py tests/test_reliability_contracts.py tests/test_agent_goal_context.py -q
-> 80 passed, 1 warning in 3.57s
```

All referenced files exist. `test_context_attribution_layers.py` passes unchanged — confirms the `ContextBuilder` default-`None` path preserves existing behavior.

Pre-existing collection errors unrelated to this task (confirmed by re-running on base commit `b2f9296b` with `git stash`):
- `tests/test_serve_open_flag.py` — `ImportError: cannot import name '_should_open_browser' from 'api_server'`
- `tests/test_telemetry_counters.py` — `ModuleNotFoundError: No module named 'agent'`

## Tool-name classification

Drawn from `agent/src/tools/*_tool.py` (the `name = "..."` attribute of each `BaseTool` subclass):

- **Shell**: `bash`
- **Live-trading** (prefix `trading_`): `trading_place_order`, `trading_cancel_order`, `trading_orders`, `trading_account`, `trading_positions`, `trading_quote`, `trading_history`, `trading_check`, `trading_connections`, `trading_select_connection`
- **Symbol**: `search_symbol`
- **Market data**: `get_market_data`, `get_a_stock_data`, `get_fund_flow`, `get_northbound_flow`, `get_block_trades`, `get_margin_trading`, `get_lockup_expiry`, `get_shareholder_count`, `get_sector_info`, `screen_market`, `get_dragon_tiger`, `get_stock_profile`, `get_options_chain`, `get_macro_series`
- **Fundamentals**: `get_fundamentals`, `get_financial_statements`
- **News**: `get_stock_news`, `get_research_reports`, `get_sec_filings`
- **Backtest**: `backtest`, `factor_analysis`, `options_pricing`, `alpha_bench`, `alpha_compare`, `alpha_zoo`, `pattern`, `report_audit`, `generate_backtest_config`, `scaffold_signal_engine`, `run_research_autopilot`, `link_autopilot_backtest`
- **Shadow account**: `extract_shadow_strategy`, `run_shadow_backtest`, `render_shadow_report`, `scan_shadow_signals`, `analyze_trade_journal`, `propose_mandate_profiles`
- **General research (fallback)**: `web_search`, `read_url`, `read_document`, `iwencai_search`, `search_symbol`, `session_search`, `get_stock_news`, `get_research_reports`

### Safety property (structural)

The router **never** emits shell or live-trading tools in any route — not by a runtime check, but because no `_RouteSpec.tools` tuple contains `bash` or any `trading_*` name. The unknown-intent `general_research` fallback only carries readonly research tools. Live trading and shell are gated downstream by the gateway (Task 4), which reads the full registry directly.

## Commit

`aeef7ab9` — `feat: route capabilities and validate execution plans` (DCO signed, no AI-attribution trailers).

## Concerns

1. **Router is readonly-only.** No capability route admits `bash` or `trading_*`. Intentional for Task 3 — the gateway (Task 4) enforces the real allowlist. If a future task needs the router to admit live-trading tools, a new spec must be added deliberately; the safety property is structural.
2. **`_READONLY_CAPABILITIES` is a static set in `planner.py`.** If the capability set grows, this should move into `CapabilityRoute` (e.g. `readonly: bool`). Marked with a `ponytail:` comment.
3. **`ContextBuilder` not refactored.** The change threads an optional filter through the existing method shape. The class is large; a future cleanup could split prompt-building from tool-rendering, out of scope here.
4. **`tool_count` reflects the rendered set** when `allowed_tool_names` is set (coherent: "you have N tools" matches N descriptions). When `None`, unchanged.

## Fix: wall-clock cross-check

**Finding (Important):** `PlanValidator.validate` enforced a plan-vs-route budget
cross-check for the "steps" and "tokens" dimensions but NOT for "wall_clock_seconds".
A plan could declare `wall_clock_seconds=1000` against a route envelope of `120`
and still pass. This was inconsistent with the other two dimensions and left the
user-visible "how long will this take" bound unenforced at the route layer.

**Change:** Added a wall-clock plan-vs-route cross-check in `_check_budgets`,
mirroring the exact guard/error-message style of the tokens cross-check. Fires
only when BOTH plan and route declare `wall_clock_seconds` (if either omits the
key, the check is skipped — consistent with steps/tokens behavior). The existing
sum-of-timeouts check (per-step `timeout_seconds` vs plan wall-clock) is untouched.

**Files:**
- `agent/src/reliability/planner.py` — +8 lines, the cross-check block.
- `agent/tests/test_reliability_planner.py` — +11 lines, new test
  `test_wall_clock_budget_exceeds_route_envelope_rejected`.

### RED (before fix)

```
$ python -m pytest agent/tests/test_reliability_planner.py -q
.................F...........                                            [100%]
=================================== FAILURES ===================================
__ TestBudgetOverflow.test_wall_clock_budget_exceeds_route_envelope_rejected ___
...
>       with pytest.raises(ValueError, match="wall.clock"):
E       Failed: DID NOT RAISE <class 'ValueError'>
agent/tests/test_reliability_planner.py:214: Failed
1 failed, 28 passed in 1.49s
```

### GREEN (after fix)

```
$ python -m pytest agent/tests/test_reliability_planner.py -q
.............................                                            [100%]
29 passed in 1.79s
```

### Full regression (planner + router)

```
$ python -m pytest agent/tests/test_reliability_planner.py agent/tests/test_reliability_router.py -q
...................................................                      [100%]
51 passed in 1.65s
```

Planner: 29/29. Router: 22/22. No collateral.

### Commit

`ea3d12ddfd43471ba16211d26f8bbbe3d319edcf` — fix: enforce wall-clock plan-vs-route budget cross-check
