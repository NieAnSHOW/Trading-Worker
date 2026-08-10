"""Tests for PlanStep, ExecutionPlan, PlanValidator (Task 3).

Covers: model defaults, missing deps, cycles, unknown capabilities,
side-effecting steps outside the route, step-count/token/wall-clock budget
overflow, and ready_steps() ordering for independent and dependent nodes.
Also covers the ContextBuilder.allowed_tool_names extension.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.reliability.contracts import CapabilityRoute
from src.reliability.planner import (
    ExecutionPlan,
    PlanStep,
    PlanValidator,
)


def _route(
    capabilities: tuple[str, ...] = ("market_data", "symbol"),
    budgets: dict[str, int] | None = None,
) -> CapabilityRoute:
    return CapabilityRoute(
        intent="market_data",
        capabilities=tuple(capabilities),
        allowed_tools=("get_market_data",),
        complexity="low",
        budgets=budgets or {"steps": 6, "tokens": 3000, "wall_clock_seconds": 120},
    )


# --- model defaults ----------------------------------------------------------


class TestPlanStepModel:
    def test_step_defaults(self):
        s = PlanStep(id="s1", capability="market_data")
        assert s.depends_on == []
        assert s.expected_fields == []
        assert s.retry_limit == 0
        assert s.timeout_seconds == 30.0
        assert s.side_effecting is False
        assert s.tool is None

    def test_step_requires_id(self):
        with pytest.raises(ValidationError):
            PlanStep(capability="market_data")  # type: ignore[call-arg]

    def test_step_requires_capability(self):
        with pytest.raises(ValidationError):
            PlanStep(id="s1")  # type: ignore[call-arg]


class TestExecutionPlanModel:
    def test_plan_requires_steps(self):
        with pytest.raises(ValidationError):
            ExecutionPlan(budgets={"steps": 3})  # type: ignore[call-arg]

    def test_plan_requires_budgets(self):
        with pytest.raises(ValidationError):
            ExecutionPlan(steps=[PlanStep(id="s1", capability="market_data")])  # type: ignore[call-arg]


# --- valid plans -------------------------------------------------------------


class TestPlanValidatorValid:
    def test_valid_simple_plan_passes(self):
        plan = ExecutionPlan(
            steps=[PlanStep(id="s1", capability="market_data")],
            budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60},
        )
        PlanValidator().validate(plan, _route())

    def test_valid_chain_passes(self):
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="s1", capability="market_data"),
                PlanStep(id="s2", capability="symbol", depends_on=["s1"]),
            ],
            budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60},
        )
        PlanValidator().validate(plan, _route(capabilities=("market_data", "symbol")))

    def test_side_effecting_in_side_effect_capable_capability_passes(self):
        # backtest is a side-effect-capable capability (writes files)
        plan = ExecutionPlan(
            steps=[PlanStep(id="s1", capability="backtest", side_effecting=True)],
            budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60},
        )
        PlanValidator().validate(plan, _route(capabilities=("backtest",)))


# --- missing dependencies ----------------------------------------------------


class TestMissingDeps:
    def test_missing_dependency_rejected(self):
        plan = ExecutionPlan(
            steps=[PlanStep(id="s2", capability="market_data", depends_on=["s1"])],
            budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60},
        )
        with pytest.raises(ValueError, match="depend"):
            PlanValidator().validate(plan, _route())


# --- cycle detection ---------------------------------------------------------


class TestCycleDetection:
    def test_two_node_cycle_rejected(self):
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="a", capability="market_data", depends_on=["b"]),
                PlanStep(id="b", capability="market_data", depends_on=["a"]),
            ],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        with pytest.raises(ValueError, match="cycle"):
            PlanValidator().validate(plan, _route())

    def test_self_cycle_rejected(self):
        plan = ExecutionPlan(
            steps=[PlanStep(id="a", capability="market_data", depends_on=["a"])],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        with pytest.raises(ValueError, match="cycle"):
            PlanValidator().validate(plan, _route())

    def test_three_node_cycle_rejected(self):
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="a", capability="market_data", depends_on=["c"]),
                PlanStep(id="b", capability="market_data", depends_on=["a"]),
                PlanStep(id="c", capability="market_data", depends_on=["b"]),
            ],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        with pytest.raises(ValueError, match="cycle"):
            PlanValidator().validate(plan, _route())


# --- unknown capabilities ----------------------------------------------------


class TestUnknownCapability:
    def test_unknown_capability_rejected(self):
        plan = ExecutionPlan(
            steps=[PlanStep(id="s1", capability="nonexistent")],
            budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60},
        )
        with pytest.raises(ValueError, match="capability"):
            PlanValidator().validate(plan, _route(capabilities=("market_data",)))


# --- side-effecting outside route --------------------------------------------


class TestSideEffectingOutsideRoute:
    def test_side_effecting_in_readonly_capability_rejected(self):
        # market_data is readonly; a side-effecting step there is unsafe.
        plan = ExecutionPlan(
            steps=[PlanStep(id="s1", capability="market_data", side_effecting=True)],
            budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60},
        )
        with pytest.raises(ValueError, match="side.effect|readonly"):
            PlanValidator().validate(plan, _route(capabilities=("market_data",)))

    def test_side_effecting_in_news_rejected(self):
        plan = ExecutionPlan(
            steps=[PlanStep(id="s1", capability="news", side_effecting=True)],
            budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 60},
        )
        with pytest.raises(ValueError):
            PlanValidator().validate(plan, _route(capabilities=("news",)))


# --- budget overflow ---------------------------------------------------------


class TestBudgetOverflow:
    def test_step_count_overflow_rejected(self):
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="s1", capability="market_data"),
                PlanStep(id="s2", capability="market_data"),
                PlanStep(id="s3", capability="market_data"),
            ],
            budgets={"steps": 2, "tokens": 1000, "wall_clock_seconds": 60},
        )
        with pytest.raises(ValueError, match="step"):
            PlanValidator().validate(plan, _route())

    def test_token_budget_exceeds_route_envelope_rejected(self):
        # plan declares more tokens than the route envelope allows
        plan = ExecutionPlan(
            steps=[PlanStep(id="s1", capability="market_data")],
            budgets={"steps": 3, "tokens": 10000, "wall_clock_seconds": 60},
        )
        route = _route(budgets={"steps": 6, "tokens": 3000, "wall_clock_seconds": 120})
        with pytest.raises(ValueError, match="token"):
            PlanValidator().validate(plan, route)

    def test_wall_clock_budget_exceeds_route_envelope_rejected(self):
        # plan declares more wall-clock than the route envelope allows
        plan = ExecutionPlan(
            steps=[PlanStep(id="s1", capability="market_data")],
            budgets={"steps": 3, "tokens": 1000, "wall_clock_seconds": 1000},
        )
        route = _route(budgets={"steps": 6, "tokens": 3000, "wall_clock_seconds": 120})
        with pytest.raises(ValueError, match="wall.clock"):
            PlanValidator().validate(plan, route)

    def test_wall_clock_overflow_rejected(self):
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="s1", capability="market_data", timeout_seconds=60.0),
                PlanStep(id="s2", capability="market_data", timeout_seconds=60.0),
            ],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        with pytest.raises(ValueError, match="wall.clock|time"):
            PlanValidator().validate(plan, _route())

    def test_step_count_exceeds_route_envelope_rejected(self):
        plan = ExecutionPlan(
            steps=[PlanStep(id="s1", capability="market_data")],
            budgets={"steps": 100, "tokens": 1000, "wall_clock_seconds": 60},
        )
        route = _route(budgets={"steps": 6, "tokens": 3000, "wall_clock_seconds": 120})
        with pytest.raises(ValueError, match="step"):
            PlanValidator().validate(plan, route)


# --- ready_steps -------------------------------------------------------------


class TestReadySteps:
    def test_independent_steps_all_ready(self):
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="s1", capability="market_data"),
                PlanStep(id="s2", capability="symbol"),
            ],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        ready = PlanValidator().ready_steps(plan, satisfied=set())
        assert {s.id for s in ready} == {"s1", "s2"}

    def test_ready_steps_stable_declaration_order(self):
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="b", capability="market_data"),
                PlanStep(id="a", capability="symbol"),
                PlanStep(id="c", capability="market_data"),
            ],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        ready = PlanValidator().ready_steps(plan, satisfied=set())
        assert [s.id for s in ready] == ["b", "a", "c"]

    def test_uncompleted_dep_excluded(self):
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="s1", capability="market_data"),
                PlanStep(id="s2", capability="symbol", depends_on=["s1"]),
            ],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        ready = PlanValidator().ready_steps(plan, satisfied=set())
        assert [s.id for s in ready] == ["s1"]

    def test_completed_dep_releases_dependent(self):
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="s1", capability="market_data"),
                PlanStep(id="s2", capability="symbol", depends_on=["s1"]),
            ],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        ready = PlanValidator().ready_steps(plan, satisfied={"s1"})
        assert [s.id for s in ready] == ["s2"]

    def test_completed_steps_excluded(self):
        plan = ExecutionPlan(
            steps=[PlanStep(id="s1", capability="market_data")],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        ready = PlanValidator().ready_steps(plan, satisfied={"s1"})
        assert ready == []

    def test_diamond_dependency_ordering(self):
        # s1 -> {s2, s3} -> s4
        plan = ExecutionPlan(
            steps=[
                PlanStep(id="s1", capability="market_data"),
                PlanStep(id="s2", capability="market_data", depends_on=["s1"]),
                PlanStep(id="s3", capability="symbol", depends_on=["s1"]),
                PlanStep(id="s4", capability="market_data", depends_on=["s2", "s3"]),
            ],
            budgets={"steps": 5, "tokens": 1000, "wall_clock_seconds": 60},
        )
        v = PlanValidator()
        assert [s.id for s in v.ready_steps(plan, set())] == ["s1"]
        assert [s.id for s in v.ready_steps(plan, {"s1"})] == ["s2", "s3"]
        assert [s.id for s in v.ready_steps(plan, {"s1", "s2", "s3"})] == ["s4"]


# --- ContextBuilder allowed_tool_names extension -----------------------------


class TestContextBuilderAllowlist:
    """Tests for ContextBuilder.allowed_tool_names rendering filter."""

    @staticmethod
    def _build_registry():
        from src.agent.memory import WorkspaceMemory  # noqa: F401  (prove import works)
        from src.agent.tools import BaseTool, ToolRegistry

        class _FakeTool(BaseTool):
            def __init__(self, name: str, desc: str = "d"):
                self.name = name
                self.description = desc
                self.parameters = {"type": "object", "properties": {}, "required": []}

            def execute(self, **kwargs):  # noqa: ANN003
                return "{}"

        reg = ToolRegistry()
        reg.register(_FakeTool("get_market_data"))
        reg.register(_FakeTool("bash"))
        reg.register(_FakeTool("search_symbol"))
        return reg

    def test_allowlist_renders_only_named_tools(self):
        from src.agent.context import ContextBuilder
        from src.agent.memory import WorkspaceMemory

        cb = ContextBuilder(
            self._build_registry(),
            WorkspaceMemory(),
            allowed_tool_names=["get_market_data", "search_symbol"],
        )
        desc = cb._format_tool_descriptions()
        assert "### get_market_data" in desc
        assert "### search_symbol" in desc
        assert "### bash" not in desc

    def test_none_allowlist_renders_all(self):
        from src.agent.context import ContextBuilder
        from src.agent.memory import WorkspaceMemory

        cb = ContextBuilder(self._build_registry(), WorkspaceMemory())
        desc = cb._format_tool_descriptions()
        assert "### get_market_data" in desc
        assert "### bash" in desc
        assert "### search_symbol" in desc

    def test_empty_allowlist_renders_nothing(self):
        from src.agent.context import ContextBuilder
        from src.agent.memory import WorkspaceMemory

        cb = ContextBuilder(
            self._build_registry(),
            WorkspaceMemory(),
            allowed_tool_names=[],
        )
        desc = cb._format_tool_descriptions()
        assert "### get_market_data" not in desc
        assert desc.strip() == ""
