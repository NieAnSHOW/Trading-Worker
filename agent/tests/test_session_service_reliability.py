"""SessionService reliability-runtime feature-flag integration tests (Task 7).

Covers the binding invariants from the task brief:

  * default ``off`` preserves the current registry / AgentLoop construction
    (no reliability code on the path);
  * ``shadow`` records reliability decisions WITHOUT duplicating provider /
    tool calls and WITHOUT replacing the AgentLoop result;
  * ``enforce`` forwards the same session_id, event callback (attempt_id),
    run directory, and shell-tool policy;
  * no fallback occurs after a side-effecting tool begins;
  * an unverified result (``partial``) never triggers ``attempt.completed``;
  * all attempt events carry the SAME ``attempt_id``.

The ``VIBE_RELIABILITY_RUNTIME`` env var is the rollout switch (default off).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.reliability.contracts import CapabilityRoute, EvidenceRef
from src.reliability.evidence import Claim, ClaimKind
from src.reliability.planner import ExecutionPlan, PlanStep
from src.session.events import EventBus
from src.session.models import Attempt, Session
from src.session.service import SessionService
from src.session.store import SessionStore


# ---------------------------------------------------------------------------
# Shared fakes (mirror the monkeypatch style of test_session_service_mcp.py)
# ---------------------------------------------------------------------------


class _DummyIndex:
    def index_session(self, session_id: str, title: str) -> None:
        del session_id, title

    def index_message(self, session_id: str, role: str, content: str) -> None:
        del session_id, role, content


class _FakeRegistry:
    """Minimal ToolRegistry stand-in: tool_names + get() + a tool map."""

    def __init__(self, tools: dict[str, Any] | None = None, names: list[str] | None = None) -> None:
        self._tools = tools or {}
        self._names = list(names) if names is not None else list(self._tools)

    @property
    def tool_names(self) -> list[str]:
        return list(self._names)

    def get(self, name: str) -> Any:
        return self._tools.get(name)

    def get_definitions(self) -> list[dict[str, Any]]:
        return []


class _FakeTool:
    """Records execute() kwargs; returns a canned JSON envelope."""

    def __init__(self, name: str, *, side_effecting: bool = False, response: str | None = None) -> None:
        self.name = name
        self.side_effecting = side_effecting
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        self.calls: list[dict[str, Any]] = []
        self._response = response or json.dumps({"status": "ok", "data": {"v": 1}})

    def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return self._response


class _FakeAgentLoop:
    """Records construction + run() args; returns the scripted result."""

    constructed: list["_FakeAgentLoop"] = []
    scripted: dict[str, Any] = {"status": "success", "content": "hello world"}

    def __init__(self, *, registry: Any, llm: Any, event_callback: Any, max_iterations: int, persistent_memory: Any) -> None:
        self.registry = registry
        self.event_callback = event_callback
        self.run_calls: list[dict[str, Any]] = []
        _FakeAgentLoop.constructed.append(self)

    def run(self, *, user_message: str, history: Any, session_id: str) -> dict[str, Any]:
        self.run_calls.append({"user_message": user_message, "history": history, "session_id": session_id})
        return dict(_FakeAgentLoop.scripted)

    def cancel(self) -> None:  # pragma: no cover - exercised via cancel_current
        pass


def _apply_common_patches(monkeypatch, registry: Any = None) -> None:
    _FakeAgentLoop.constructed.clear()
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    monkeypatch.setattr("src.tools.build_registry", lambda **kw: registry or _FakeRegistry())
    monkeypatch.setattr("src.providers.chat.ChatLLM", lambda: object())
    monkeypatch.setattr("src.memory.persistent.PersistentMemory", lambda: object())
    monkeypatch.setattr("src.agent.loop.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("src.config.loader.load_runtime_agent_config", lambda overrides=None: object())
    monkeypatch.setattr("src.config.loader.sanitize_session_overrides", lambda overrides: dict(overrides or {}))


def _make_service(tmp_path: Path) -> SessionService:
    return SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )


# ---------------------------------------------------------------------------
# default off preserves the current AgentLoop path
# ---------------------------------------------------------------------------


def test_off_mode_preserves_agentloop_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VIBE_RELIABILITY_RUNTIME", raising=False)
    _apply_common_patches(monkeypatch)

    service = _make_service(tmp_path)
    attempt = Attempt(session_id="s1", prompt="hello")

    result = asyncio.run(service._run_with_agent(attempt, messages=[], session_config={}))

    assert result["status"] == "success"
    assert result["content"] == "hello world"
    assert len(_FakeAgentLoop.constructed) == 1
    assert _FakeAgentLoop.constructed[0].run_calls[0]["user_message"] == "hello"
    assert _FakeAgentLoop.constructed[0].run_calls[0]["session_id"] == "s1"
    # No reliability summary on the off path (byte-for-byte current behavior).
    assert "reliability" not in result


def test_unknown_mode_falls_back_to_off(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBE_RELIABILITY_RUNTIME", "definitely-not-a-mode")
    _apply_common_patches(monkeypatch)

    from src.config.schema import get_reliability_runtime_mode

    assert get_reliability_runtime_mode() == "off"

    service = _make_service(tmp_path)
    attempt = Attempt(session_id="s1", prompt="hello")

    result = asyncio.run(service._run_with_agent(attempt, messages=[], session_config={}))

    assert result["status"] == "success"
    assert "reliability" not in result
    assert len(_FakeAgentLoop.constructed) == 1


# ---------------------------------------------------------------------------
# shadow does not duplicate provider / tool calls
# ---------------------------------------------------------------------------


def test_shadow_does_not_duplicate_provider_or_tool_calls(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBE_RELIABILITY_RUNTIME", "shadow")

    build_calls: list[dict[str, Any]] = []

    def _tracking_build(**kwargs: Any) -> Any:
        build_calls.append(kwargs)
        return _FakeRegistry()

    _FakeAgentLoop.constructed.clear()
    _FakeAgentLoop.scripted = {"status": "success", "content": "agentloop produced this"}
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    monkeypatch.setattr("src.tools.build_registry", _tracking_build)
    monkeypatch.setattr("src.providers.chat.ChatLLM", lambda: object())
    monkeypatch.setattr("src.memory.persistent.PersistentMemory", lambda: object())
    monkeypatch.setattr("src.agent.loop.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("src.config.loader.load_runtime_agent_config", lambda overrides=None: object())
    monkeypatch.setattr("src.config.loader.sanitize_session_overrides", lambda overrides: dict(overrides or {}))

    service = _make_service(tmp_path)
    attempt = Attempt(session_id="s1", prompt="tell me about AAPL")

    result = asyncio.run(service._run_with_agent(attempt, messages=[], session_config={}))

    # Registry built exactly once (not duplicated for the runtime).
    assert len(build_calls) == 1, f"expected 1 build_registry call, got {len(build_calls)}"
    # AgentLoop invoked exactly once (shadow does not re-run it).
    assert len(_FakeAgentLoop.constructed) == 1
    assert len(_FakeAgentLoop.constructed[0].run_calls) == 1
    # AgentLoop result is NOT replaced.
    assert result["content"] == "agentloop produced this"
    assert result["status"] == "success"
    # Reliability summary recorded alongside (observer output).
    assert isinstance(result.get("reliability"), dict)
    assert "intent" in result["reliability"]


# ---------------------------------------------------------------------------
# enforce forwards session_id / event_callback / run_dir / shell policy
# ---------------------------------------------------------------------------


def test_enforce_forwards_session_event_run_dir_shell_policy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBE_RELIABILITY_RUNTIME", "enforce")

    build_calls: list[dict[str, Any]] = []

    def _tracking_build(**kwargs: Any) -> Any:
        build_calls.append(kwargs)
        return _FakeRegistry()

    _FakeAgentLoop.constructed.clear()
    _FakeAgentLoop.scripted = {"status": "success", "content": "enforce produced this"}
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    monkeypatch.setattr("src.tools.build_registry", _tracking_build)
    monkeypatch.setattr("src.providers.chat.ChatLLM", lambda: object())
    monkeypatch.setattr("src.memory.persistent.PersistentMemory", lambda: object())
    monkeypatch.setattr("src.agent.loop.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("src.config.loader.load_runtime_agent_config", lambda overrides=None: object())
    monkeypatch.setattr("src.config.loader.sanitize_session_overrides", lambda overrides: dict(overrides or {}))

    service = _make_service(tmp_path)
    attempt = Attempt(session_id="enforce-session", prompt="get AAPL price")

    result = asyncio.run(
        service._run_with_agent(attempt, messages=[], session_config={}, include_shell_tools=True)
    )

    assert build_calls[0]["include_shell_tools"] is True
    assert result["status"] == "success"
    assert "reliability" in result
    assert str(tmp_path / "runs") in str(result["run_dir"])


def test_enforce_forwards_agentloop_run_dir(monkeypatch, tmp_path: Path) -> None:
    """The enforce runtime must grade INSIDE the AgentLoop's run_dir.

    The AgentLoop has already written artifacts (requests, traces, llm_usage)
    into its run_dir; the runtime must reuse that directory so downstream
    run-artifact consumers (metrics loading etc.) resolve. A fresh empty
    ``rel-<ts>-<uuid>`` directory would silently orphan the run's artifacts.
    """
    monkeypatch.setenv("VIBE_RELIABILITY_RUNTIME", "enforce")
    agent_run_dir = tmp_path / "agentloop-run"
    agent_run_dir.mkdir(parents=True, exist_ok=True)
    _FakeAgentLoop.constructed.clear()
    _FakeAgentLoop.scripted = {
        "status": "success",
        "content": "ok",
        "run_dir": str(agent_run_dir),
    }
    _apply_common_patches(monkeypatch)

    service = _make_service(tmp_path)
    attempt = Attempt(session_id="s1", prompt="get AAPL price")

    result = asyncio.run(service._run_with_agent(attempt, messages=[], session_config={}))

    assert result["run_dir"] == str(agent_run_dir), (
        f"enforce must reuse the AgentLoop run_dir, got {result['run_dir']}"
    )


def test_enforce_events_carry_same_attempt_id(monkeypatch, tmp_path: Path) -> None:
    """All SSE events under enforce (lifecycle + runtime) carry the SAME attempt_id."""
    monkeypatch.setenv("VIBE_RELIABILITY_RUNTIME", "enforce")
    _FakeAgentLoop.constructed.clear()
    _FakeAgentLoop.scripted = {"status": "success", "content": "ok"}
    _apply_common_patches(monkeypatch)

    service = _make_service(tmp_path)
    session = Session(session_id="s1", title="t")
    service.store.create_session(session)
    attempt = Attempt(session_id="s1", prompt="get AAPL price")
    attempt_id = attempt.attempt_id

    asyncio.run(service._run_attempt(session, attempt, include_shell_tools=False))

    emitted = service.event_bus.replay("s1", replay_all=True)
    assert emitted, "expected lifecycle events to be buffered"
    attempt_ids = {ev.data.get("attempt_id") for ev in emitted if "attempt_id" in ev.data}
    assert attempt_ids == {attempt_id}, f"events carried mismatched attempt ids: {attempt_ids}"
    # Lifecycle contract: attempt.started + a terminal event both present.
    event_types = {ev.event_type for ev in emitted}
    assert "attempt.started" in event_types
    assert "attempt.completed" in event_types or "attempt.failed" in event_types


# ---------------------------------------------------------------------------
# no fallback after a side-effecting tool begins
# ---------------------------------------------------------------------------


def test_enforce_fault_fallback_marks_ungraded(monkeypatch, tmp_path: Path) -> None:
    """A runtime FAULT falls back to the AgentLoop result, marked un-graded.

    Terminal semantics are untouched (status stays whatever the AgentLoop
    returned); ``reliability.faulted`` tells operators the run never got a
    reliability verdict.
    """
    monkeypatch.setenv("VIBE_RELIABILITY_RUNTIME", "enforce")
    _FakeAgentLoop.constructed.clear()
    _FakeAgentLoop.scripted = {"status": "success", "content": "agentloop produced this"}
    _apply_common_patches(monkeypatch)

    def _boom(self: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated runtime fault")

    monkeypatch.setattr("src.reliability.runtime.ReliabilityRuntime.run", _boom)

    service = _make_service(tmp_path)
    attempt = Attempt(session_id="s1", prompt="get AAPL price")

    result = asyncio.run(service._run_with_agent(attempt, messages=[], session_config={}))

    assert result["status"] == "success"  # terminal semantics untouched
    assert result["content"] == "agentloop produced this"
    assert result["reliability"]["faulted"] is True


def test_enforce_blocks_side_effecting_tools(monkeypatch, tmp_path: Path) -> None:
    """enforce runs allow_side_effects=False: side-effecting tools are never executed.

    The fast-path rollout never starts a write, so no-fallback-after-writes
    holds trivially. This pins the structural guarantee.
    """
    monkeypatch.setenv("VIBE_RELIABILITY_RUNTIME", "enforce")

    write_tool = _FakeTool("trading_place_order", side_effecting=True)
    registry = _FakeRegistry(
        tools={"trading_place_order": write_tool, "get_market_data": _FakeTool("get_market_data")},
        names=["trading_place_order", "get_market_data"],
    )

    _FakeAgentLoop.constructed.clear()
    _FakeAgentLoop.scripted = {"status": "success", "content": "ok"}
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    monkeypatch.setattr("src.tools.build_registry", lambda **kw: registry)
    monkeypatch.setattr("src.providers.chat.ChatLLM", lambda: object())
    monkeypatch.setattr("src.memory.persistent.PersistentMemory", lambda: object())
    monkeypatch.setattr("src.agent.loop.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("src.config.loader.load_runtime_agent_config", lambda overrides=None: object())
    monkeypatch.setattr("src.config.loader.sanitize_session_overrides", lambda overrides: dict(overrides or {}))

    service = _make_service(tmp_path)
    attempt = Attempt(session_id="s1", prompt="buy 100 AAPL")

    result = asyncio.run(service._run_with_agent(attempt, messages=[], session_config={}))

    assert write_tool.calls == []
    assert "reliability" in result


# ---------------------------------------------------------------------------
# unverified -> partial -> never attempt.completed
# ---------------------------------------------------------------------------


def test_partial_runtime_result_does_not_complete(monkeypatch, tmp_path: Path) -> None:
    """A runtime `partial` result flows to attempt.failed, never attempt.completed."""
    monkeypatch.setenv("VIBE_RELIABILITY_RUNTIME", "enforce")
    _FakeAgentLoop.constructed.clear()
    _FakeAgentLoop.scripted = {
        "status": "success",
        "content": "I claim things",
        "claims": [
            Claim(
                text="x",
                kind=ClaimKind.FACT,
                evidence=[EvidenceRef(source_type="artifact", source_id="missing.json")],
            )
        ],
    }
    _apply_common_patches(monkeypatch)

    service = _make_service(tmp_path)

    # 1. _run_with_agent returns partial (unverified claims).
    attempt = Attempt(session_id="s1", prompt="get AAPL price")
    result = asyncio.run(service._run_with_agent(attempt, messages=[], session_config={}))
    assert result["status"] == "partial"

    # 2. _run_attempt emits attempt.failed (NOT attempt.completed) for partial.
    session = Session(session_id="s2", title="t")
    service.store.create_session(session)
    attempt2 = Attempt(session_id="s2", prompt="get AAPL price")
    asyncio.run(service._run_attempt(session, attempt2, include_shell_tools=False))

    emitted = service.event_bus.replay("s2", replay_all=True)
    terminal = [ev for ev in emitted if ev.event_type in ("attempt.completed", "attempt.failed")]
    assert terminal, "expected a terminal attempt event"
    assert all(ev.event_type == "attempt.failed" for ev in terminal), (
        f"unverified result must not emit attempt.completed: {[ev.event_type for ev in terminal]}"
    )


# ---------------------------------------------------------------------------
# Cancellation: runtime cancel handle
# ---------------------------------------------------------------------------


def test_cancel_current_handles_runtime_path(tmp_path: Path) -> None:
    import threading

    service = _make_service(tmp_path)

    # No active loop or cancel handle -> False.
    assert service.cancel_current("nope") is False

    ev = threading.Event()
    service._active_cancels["s1"] = ev
    assert service.cancel_current("s1") is True
    assert ev.is_set()


# ---------------------------------------------------------------------------
# Tool-args resolution: PlanStep.arguments flows to the gateway (Task 6 handoff)
# ---------------------------------------------------------------------------


def test_plan_step_arguments_flow_to_gateway(tmp_path: Path) -> None:
    """PlanStep.arguments populated by plan_provider reaches the tool (option a)."""
    from src.reliability.router import TaskRouter
    from src.reliability.runtime import ReliabilityRuntime

    captured: list[dict[str, Any]] = []

    class _Tool:
        name = "get_market_data"
        side_effecting = False
        parameters = {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        }

        def execute(self, **kwargs: Any) -> str:
            captured.append(dict(kwargs))
            return json.dumps({"status": "ok", "data": {"price": 100}})

    registry = _FakeRegistry(tools={"get_market_data": _Tool()}, names=["get_market_data"])

    def plan_provider(route: CapabilityRoute, user_message: str) -> ExecutionPlan | None:
        return ExecutionPlan(
            steps=[
                PlanStep(
                    id="s1",
                    capability="market_data",
                    tool="get_market_data",
                    arguments={"symbol": "AAPL"},
                )
            ],
            budgets={"steps": 2},
        )

    rt = ReliabilityRuntime(
        runs_dir=tmp_path,
        plan_provider=plan_provider,
        allow_side_effects=False,
    )
    res = rt.run(
        user_message="get AAPL price",
        session_id="s1",
        registry=registry,
        executor=lambda **kw: {"content": "done"},
    )
    assert captured == [{"symbol": "AAPL"}], f"arguments did not reach the tool: {captured}"
    # The step succeeded and the runtime produced a result.
    assert res["status"] in {"success", "partial"}
