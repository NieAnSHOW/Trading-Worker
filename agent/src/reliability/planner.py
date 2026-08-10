"""Bounded execution-plan models and DAG validation for the reliability runtime.

Provides:
  - :class:`PlanStep` and :class:`ExecutionPlan` (Pydantic 2 models).
  - :class:`PlanValidator` with cycle detection (topological traversal),
    missing-dependency checks, unknown-capability rejection, side-effect
    gating for readonly capabilities, and step-count/token/wall-clock budget
    enforcement.
  - :meth:`PlanValidator.ready_steps` returns steps whose dependencies are
    all in the ``satisfied`` set (steps whose outcome lets a dependent
    proceed), in stable declaration order.

No graph library: the DAG logic is standard Kahn-style traversal.
"""

from __future__ import annotations

from typing import Collection

from pydantic import BaseModel, Field

from src.reliability.contracts import CapabilityRoute

# Capabilities that are inherently readonly — a side_effecting=True step in
# any of these is rejected, since readonly categories must not mutate state.
# ponytail: a static set is the minimum structure that encodes the rule; if
# capabilities proliferate, move this into CapabilityRoute as an attribute.
_READONLY_CAPABILITIES: frozenset[str] = frozenset(
    {"market_data", "symbol", "fundamentals", "news", "general_research"}
)


class PlanStep(BaseModel):
    """One step in an execution plan.

    Attributes:
        id: Unique step identifier within the plan.
        capability: Capability this step exercises (must be in the route's
            ``capabilities`` tuple to pass validation).
        tool: Tool the step will invoke, if known.
        depends_on: IDs of steps that must complete before this one.
        expected_fields: Fields the step is expected to produce.
        retry_limit: Maximum retry attempts on recoverable failure.
        timeout_seconds: Per-step wall-clock budget.
        side_effecting: Whether this step mutates state. Readonly
            capabilities reject side-effecting steps.
    """

    id: str
    capability: str
    tool: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    expected_fields: list[str] = Field(default_factory=list)
    retry_limit: int = 0
    timeout_seconds: float = 30.0
    side_effecting: bool = False
    # Tool call arguments for this step. Populated by the service-supplied
    # plan_provider; empty when the step's tool accepts no required fields.
    # Task 7 handoff resolution (option a): the runtime reads step.arguments
    # via _step_arguments so enforce mode can drive real tools with real args.
    arguments: dict[str, object] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    """A bounded execution plan: steps plus declared budget envelopes."""

    steps: list[PlanStep]
    budgets: dict[str, int]


class PlanValidator:
    """Validates an :class:`ExecutionPlan` against a :class:`CapabilityRoute`.

    ``validate`` raises ``ValueError`` on:
      - steps referencing unknown capabilities (not in route.capabilities),
      - missing dependencies (depends_on IDs not present in the plan),
      - dependency cycles (topological traversal fails),
      - side-effecting steps in readonly capabilities,
      - step-count, token, or wall-clock budget overflow.
    """

    def validate(self, plan: ExecutionPlan, route: CapabilityRoute) -> None:
        steps = plan.steps
        step_ids = {s.id for s in steps}
        allowed_caps = set(route.capabilities)

        # 1. unknown capabilities
        for s in steps:
            if s.capability not in allowed_caps:
                raise ValueError(
                    f"step {s.id!r} declares capability {s.capability!r} "
                    f"which is not in route capabilities {sorted(allowed_caps)}"
                )

        # 2. side-effecting in readonly capability
        for s in steps:
            if s.side_effecting and s.capability in _READONLY_CAPABILITIES:
                raise ValueError(
                    f"step {s.id!r} is side_effecting but capability "
                    f"{s.capability!r} is readonly"
                )

        # 3. missing dependencies
        for s in steps:
            missing = [d for d in s.depends_on if d not in step_ids]
            if missing:
                raise ValueError(
                    f"step {s.id!r} depends on unknown step ids: {sorted(missing)}"
                )

        # 4. cycle detection (Kahn's algorithm — no graph lib)
        self._check_cycles(steps, step_ids)

        # 5. budget enforcement
        self._check_budgets(plan, route)

    @staticmethod
    def _check_cycles(steps: list[PlanStep], step_ids: set[str]) -> None:
        """Topological traversal: if not all nodes are emitted, a cycle exists."""
        # in-degree keyed by node id
        deps = {s.id: set(s.depends_on) for s in steps}
        # Build reverse adjacency: for each node, who depends on it?
        children: dict[str, list[str]] = {sid: [] for sid in step_ids}
        for s in steps:
            for d in s.depends_on:
                children[d].append(s.id)
        # Kahn: start from nodes with no dependencies
        ready = [sid for sid, dset in deps.items() if not dset]
        emitted = 0
        while ready:
            sid = ready.pop()
            emitted += 1
            for child in children[sid]:
                deps[child].discard(sid)
                if not deps[child]:
                    ready.append(child)
        if emitted != len(step_ids):
            # The unemitted nodes participate in at least one cycle.
            stuck = sorted(sid for sid in step_ids if deps[sid])
            raise ValueError(
                f"dependency cycle detected among steps: {stuck}"
            )

    @staticmethod
    def _check_budgets(plan: ExecutionPlan, route: CapabilityRoute) -> None:
        # step-count: actual count vs declared plan budget
        plan_steps = plan.budgets.get("steps")
        if plan_steps is not None and len(plan.steps) > plan_steps:
            raise ValueError(
                f"plan has {len(plan.steps)} steps but budgets allow {plan_steps}"
            )
        # step-count: declared plan budget vs route envelope
        route_steps = route.budgets.get("steps")
        if plan_steps is not None and route_steps is not None and plan_steps > route_steps:
            raise ValueError(
                f"plan step budget {plan_steps} exceeds route envelope {route_steps}"
            )

        # tokens: declared plan budget vs route envelope
        plan_tokens = plan.budgets.get("tokens")
        route_tokens = route.budgets.get("tokens")
        if (
            plan_tokens is not None
            and route_tokens is not None
            and plan_tokens > route_tokens
        ):
            raise ValueError(
                f"plan token budget {plan_tokens} exceeds route envelope {route_tokens}"
            )

        # wall-clock: sum of per-step timeouts vs declared plan budget
        plan_wall = plan.budgets.get("wall_clock_seconds")
        if plan_wall is not None:
            total = sum(s.timeout_seconds for s in plan.steps)
            if total > plan_wall:
                raise ValueError(
                    f"plan wall-clock {total}s exceeds budget {plan_wall}s"
                )

        # wall-clock: declared plan budget vs route envelope
        route_wall = route.budgets.get("wall_clock_seconds")
        if (
            plan_wall is not None
            and route_wall is not None
            and plan_wall > route_wall
        ):
            raise ValueError(
                f"plan wall-clock budget {plan_wall}s exceeds route envelope {route_wall}s"
            )

    def ready_steps(self, plan: ExecutionPlan, satisfied: set[str]) -> list[PlanStep]:
        """Return steps whose dependencies are all in ``satisfied``.

        ``satisfied`` is the set of step ids whose final outcome lets a
        dependent proceed (SUCCESS/PARTIAL at the runtime layer) — NOT every
        terminal state. Dependents of a failed step stay un-scheduled and end
        BLOCKED. Steps already in ``satisfied`` are excluded. Output is in
        stable declaration order (the order steps appear in ``plan.steps``).
        """
        done = set(satisfied)
        return [
            s
            for s in plan.steps
            if s.id not in done and all(d in done for d in s.depends_on)
        ]


__all__ = ["ExecutionPlan", "PlanStep", "PlanValidator"]
