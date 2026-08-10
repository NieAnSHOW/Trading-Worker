"""Session lifecycle orchestration for message flow, attempt creation, and execution scheduling.

V5: Uses AgentLoop instead of the fixed pipeline behind the generate skill.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Dedicated thread pool limited to four concurrent agents to avoid exhausting the default executor.
_AGENT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")

from src.session.events import EventBus
from src.session.models import (
    Attempt,
    AttemptStatus,
    Message,
    Session,
)
from src.session.search import get_shared_index
from src.session.store import SessionStore


class SessionService:
    """Session lifecycle service.

    Attributes:
        store: Session persistence store.
        event_bus: SSE event bus.
        runs_dir: Root runs directory.
    """

    def __init__(
        self,
        store: SessionStore,
        event_bus: EventBus,
        runs_dir: Path,
    ) -> None:
        """Initialize the session service.

        Args:
            store: Session persistence store.
            event_bus: SSE event bus.
            runs_dir: Root runs directory.
        """
        self.store = store
        self.event_bus = event_bus
        self.runs_dir = runs_dir
        self._active_loops: Dict[str, "AgentLoop"] = {}
        # ponytail: runtime cancel handles keyed by session_id. Only populated
        # on the enforce path; off/shadow cancel via _active_loops.
        self._active_cancels: Dict[str, Any] = {}
        self._search_index = get_shared_index()

    def create_session(self, title: str = "", config: Optional[Dict[str, Any]] = None) -> Session:
        """Create a new session.

        Args:
            title: Session title.
            config: Session configuration.

        Returns:
            The newly created Session.
        """
        session = Session(title=title, config=config or {})
        self.store.create_session(session)
        self._search_index.index_session(session.session_id, title)
        self.event_bus.emit(session.session_id, "session.created", {"session_id": session.session_id, "title": title})
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Return a session by ID."""
        return self.store.get_session(session_id)

    def list_sessions(self, limit: int = 50) -> list[Session]:
        """List all sessions."""
        return self.store.list_sessions(limit)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        self.event_bus.clear(session_id)
        return self.store.delete_session(session_id)

    async def send_message(
        self,
        session_id: str,
        content: str,
        role: str = "user",
        *,
        include_shell_tools: bool = False,
    ) -> Dict[str, Any]:
        """Send a message to a session and trigger execution.

        Args:
            session_id: Session ID.
            content: Message content.
            role: Message role.
            include_shell_tools: Whether this attempt may use shell tools.

        Returns:
            Dictionary containing message_id and attempt_id.
        """
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        message = Message(session_id=session_id, role=role, content=content)
        self.store.append_message(message)
        self._search_index.index_message(session_id, role, content)
        self.event_bus.emit(session_id, "message.received", {"message_id": message.message_id, "role": role, "content": content})

        if role != "user":
            return {"message_id": message.message_id}

        attempt = Attempt(session_id=session_id, parent_attempt_id=session.last_attempt_id, prompt=content)
        self.store.create_attempt(attempt)
        session.config["include_shell_tools"] = include_shell_tools
        session.last_attempt_id = attempt.attempt_id
        session.updated_at = datetime.now().isoformat()
        self.store.update_session(session)
        self.event_bus.emit(session_id, "attempt.created", {"attempt_id": attempt.attempt_id, "prompt": content})

        asyncio.create_task(self._run_attempt(session, attempt, include_shell_tools=include_shell_tools))
        return {"message_id": message.message_id, "attempt_id": attempt.attempt_id}

    def get_messages(self, session_id: str, limit: int = 100) -> list[Message]:
        """Return the message history."""
        return self.store.get_messages(session_id, limit)

    def cancel_current(self, session_id: str) -> bool:
        """Cancel the currently running AgentLoop for a session.

        Args:
            session_id: Session ID.

        Returns:
            Whether cancellation succeeded. True means an active loop or
            runtime cancel handle existed and received a cancel signal.
        """
        cancelled = False
        loop = self._active_loops.get(session_id)
        if loop is not None:
            loop.cancel()
            cancelled = True
        cancel_event = self._active_cancels.get(session_id)
        if cancel_event is not None:
            cancel_event.set()
            cancelled = True
        return cancelled

    async def _run_attempt(self, session: Session, attempt: Attempt, *, include_shell_tools: bool = False) -> None:
        """Execute an Attempt in the background."""
        attempt.mark_running()
        self.store.update_attempt(attempt)
        self.event_bus.emit(session.session_id, "attempt.started", {"attempt_id": attempt.attempt_id})

        try:
            messages = self.store.get_messages(session.session_id)
            result = await self._run_with_agent(
                attempt,
                messages=messages,
                include_shell_tools=include_shell_tools,
                session_config=dict(session.config),
            )
            if result.get("status") == "success":
                attempt.mark_completed(summary=result.get("content", ""))
            else:
                attempt.mark_failed(error=result.get("reason", "unknown"))
            attempt.run_dir = result.get("run_dir")

            self.store.update_attempt(attempt)
            reply_metadata = {}
            if attempt.run_dir:
                reply_metadata["run_id"] = Path(attempt.run_dir).name
            reply_metadata["status"] = attempt.status.value
            if attempt.metrics:
                reply_metadata["metrics"] = attempt.metrics

            reply = Message(
                session_id=session.session_id, role="assistant",
                content=self._format_result_message(attempt),
                linked_attempt_id=attempt.attempt_id,
                metadata=reply_metadata,
            )
            self.store.append_message(reply)
            self._search_index.index_message(session.session_id, "assistant", reply.content)
            self.event_bus.emit(
                session.session_id,
                "attempt.completed" if attempt.status == AttemptStatus.COMPLETED else "attempt.failed",
                {"attempt_id": attempt.attempt_id, "status": attempt.status.value,
                 "summary": attempt.summary, "error": attempt.error, "run_dir": attempt.run_dir},
            )

        except Exception as exc:
            attempt.mark_failed(error=str(exc))
            self.store.update_attempt(attempt)
            self.event_bus.emit(session.session_id, "attempt.failed", {"attempt_id": attempt.attempt_id, "error": str(exc)})

    async def _run_with_agent(
        self,
        attempt: Attempt,
        messages: list = None,
        *,
        include_shell_tools: bool = False,
        session_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute an attempt with the V5 AgentLoop.

        Args:
            attempt: Current execution attempt.
            messages: Session message history.
            include_shell_tools: Whether the registry may include shell tools.
            session_config: Optional session-level config overrides. MCP server
                definitions under the ``mcpServers`` key are merged on top of
                the user config file via ``load_runtime_agent_config`` so each
                session can extend or override the global MCP server list.

        Returns:
            Result dictionary containing status, run_dir, run_id, metrics, and related fields.
        """
        from src.tools import build_registry
        from src.providers.chat import ChatLLM
        from src.agent.loop import AgentLoop
        from src.memory.persistent import PersistentMemory
        from src.config.loader import load_runtime_agent_config, sanitize_session_overrides
        from src.config.schema import get_reliability_runtime_mode
        from src.telemetry import counters

        # ponytail: branch at construction. off runs the EXACT current path
        # below (zero reliability code on the hot path); shadow/enforce
        # delegate to _run_with_reliability so the off path is untouched.
        mode = get_reliability_runtime_mode()
        if mode != "off":
            return await self._run_with_reliability(
                mode,
                attempt,
                messages=messages,
                include_shell_tools=include_shell_tools,
                session_config=session_config,
            )

        llm = ChatLLM()
        pm = PersistentMemory()

        session_id = attempt.session_id
        attempt_id = attempt.attempt_id
        loop = asyncio.get_running_loop()

        safe_overrides = sanitize_session_overrides(session_config) if session_config else session_config
        agent_config = load_runtime_agent_config(overrides=safe_overrides)

        def event_callback(event_type: str, data: Dict[str, Any]) -> None:
            """Forward AgentLoop events to the SSE event bus."""
            data["attempt_id"] = attempt_id
            self.event_bus.emit(session_id, event_type, data)

        def _mcp_collision_warn(msg: str) -> None:
            """Forward MCP server-name collision warnings to the operator event channel."""
            self.event_bus.emit(session_id, "mcp.warning", {"attempt_id": attempt_id, "message": msg})

        _reg_t0 = time.perf_counter()
        registry = await loop.run_in_executor(
            _AGENT_EXECUTOR,
            lambda: build_registry(
                persistent_memory=pm,
                include_shell_tools=include_shell_tools,
                agent_config=agent_config,
                session_id=session_id,
                event_callback=event_callback,
                warn_callback=_mcp_collision_warn,
            ),
        )
        # ponytail: best-effort telemetry — failure must not affect agent
        try:
            counters.record_reliability_phase(
                "registry_build", int((time.perf_counter() - _reg_t0) * 1000)
            )
        except Exception:
            pass

        agent = AgentLoop(
            registry=registry,
            llm=llm,
            event_callback=event_callback,
            max_iterations=50,
            persistent_memory=pm,
        )
        self._active_loops[session_id] = agent

        # Build the message history context.
        history = self._convert_messages_to_history(messages) if messages else None

        try:
            _loop_t0 = time.perf_counter()
            result = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: agent.run(
                    user_message=attempt.prompt,
                    history=history,
                    session_id=session_id,
                ),
            )
        finally:
            # ponytail: best-effort telemetry — record even on exception.
            try:
                counters.record_reliability_phase(
                    "agent_loop", int((time.perf_counter() - _loop_t0) * 1000)
                )
            except Exception:
                pass
            self._active_loops.pop(session_id, None)

        # Load metrics from the run output when available.
        if result.get("run_dir"):
            metrics = self._load_metrics(Path(result["run_dir"]))
            if metrics:
                result["metrics"] = metrics

        return result

    async def _run_with_reliability(
        self,
        mode: str,
        attempt: Attempt,
        *,
        messages: list = None,
        include_shell_tools: bool = False,
        session_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run the reliability runtime alongside / over the AgentLoop.

        Builds the SAME registry + AgentLoop + persistent-memory stack as the
        off path, then:

          * ``shadow`` — the AgentLoop owns the attempt; the runtime runs as an
            observer (fast path, stub executor returning the AgentLoop's own
            synthesis) so its route + verification decisions are recorded
            WITHOUT duplicating provider/tool calls and WITHOUT replacing the
            AgentLoop result.
          * ``enforce`` — the runtime owns the attempt. Fast-path rollout wires
            ``plan_provider=None`` (no LLM planner yet) and
            ``allow_side_effects=False`` so the gateway blocks writes and the
            no-fallback-after-writes invariant holds trivially. The AgentLoop
            remains the synthesis engine; the runtime grades its output and
            its terminal verdict is what the caller sees. A runtime FAULT
            (unexpected exception, not a legitimate verdict) falls back to the
            AgentLoop result — safe because allow_side_effects=False means no
            side effect can have begun. A future task wires a real planner so
            the runtime drives tools through the gateway itself.
        """
        from src.tools import build_registry
        from src.providers.chat import ChatLLM
        from src.agent.loop import AgentLoop
        from src.memory.persistent import PersistentMemory
        from src.config.loader import load_runtime_agent_config, sanitize_session_overrides
        from src.reliability.runtime import ReliabilityRuntime
        from src.reliability.evidence import Claim
        from src.telemetry import counters

        llm = ChatLLM()
        pm = PersistentMemory()

        session_id = attempt.session_id
        attempt_id = attempt.attempt_id
        loop = asyncio.get_running_loop()

        safe_overrides = sanitize_session_overrides(session_config) if session_config else session_config
        agent_config = load_runtime_agent_config(overrides=safe_overrides)

        def event_callback(event_type: str, data: Dict[str, Any]) -> None:
            """Forward AgentLoop events (incl. mandate/live relays) to SSE."""
            data["attempt_id"] = attempt_id
            self.event_bus.emit(session_id, event_type, data)

        def runtime_event_callback(event_type: str, data: Dict[str, Any]) -> None:
            """Forward runtime tool-lifecycle events only.

            The service (_run_attempt) owns attempt.created/started/completed/
            failed; the runtime's attempt.* emissions are dropped here to keep
            a single source of truth and one attempt_id space.
            """
            if event_type.startswith("attempt."):
                return
            data["attempt_id"] = attempt_id
            self.event_bus.emit(session_id, event_type, data)

        def _mcp_collision_warn(msg: str) -> None:
            self.event_bus.emit(session_id, "mcp.warning", {"attempt_id": attempt_id, "message": msg})

        _reg_t0 = time.perf_counter()
        registry = await loop.run_in_executor(
            _AGENT_EXECUTOR,
            lambda: build_registry(
                persistent_memory=pm,
                include_shell_tools=include_shell_tools,
                agent_config=agent_config,
                session_id=session_id,
                event_callback=event_callback,
                warn_callback=_mcp_collision_warn,
            ),
        )
        try:  # ponytail: best-effort telemetry — failure must not affect the attempt
            counters.record_reliability_phase("registry_build", int((time.perf_counter() - _reg_t0) * 1000))
        except Exception:  # noqa: BLE001
            pass

        agent = AgentLoop(
            registry=registry,
            llm=llm,
            event_callback=event_callback,
            max_iterations=50,
            persistent_memory=pm,
        )
        self._active_loops[session_id] = agent

        history = self._convert_messages_to_history(messages) if messages else None

        _loop_t0 = time.perf_counter()
        try:
            agentloop_result = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: agent.run(
                    user_message=attempt.prompt,
                    history=history,
                    session_id=session_id,
                ),
            )
        finally:
            try:
                counters.record_reliability_phase("agent_loop", int((time.perf_counter() - _loop_t0) * 1000))
            except Exception:  # noqa: BLE001
                pass
            # AgentLoop done; enforce will register its own cancel handle below.
            self._active_loops.pop(session_id, None)

        synthesis = self._coerce_synthesis(agentloop_result, Claim)

        if mode == "shadow":
            # Observer: record reliability decisions; do not replace the result.
            shadow_summary = self._observe_reliability(
                registry=registry,
                user_message=attempt.prompt,
                session_id=session_id,
                synthesis=synthesis,
                agentloop_result=agentloop_result,
            )
            agentloop_result["reliability"] = shadow_summary
            return agentloop_result

        # enforce: runtime owns the attempt.
        import threading

        cancel_event = threading.Event()
        self._active_cancels[session_id] = cancel_event

        runtime = ReliabilityRuntime(
            runs_dir=self.runs_dir,
            # Reuse the AgentLoop's run_dir so runtime grading lands next to the
            # artifacts the loop already wrote (trace, llm_usage, metrics); a
            # fresh empty dir would orphan downstream run-artifact consumers.
            run_dir=Path(agentloop_result["run_dir"]) if agentloop_result.get("run_dir") else None,
            allow_side_effects=False,  # fast-path rollout: writes blocked
            cancel_event=cancel_event,
        )

        def executor(*, user_message: str, session_id: str, route, evidence, run_dir: str) -> Dict[str, Any]:
            """Fast-path rollout: synthesis reuses the AgentLoop output.

            The runtime's plan_provider is None (no LLM planner wired yet), so
            the fast path calls the executor once for synthesis. Reusing the
            AgentLoop result avoids a duplicate LLM/provider call. A future
            task swaps in a dedicated synthesizer + planner.
            """
            del user_message, session_id, route, evidence, run_dir  # synthesis already computed
            return synthesis

        try:
            result = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: runtime.run(
                    user_message=attempt.prompt,
                    session_id=session_id,
                    registry=registry,
                    executor=executor,
                    event_callback=runtime_event_callback,
                ),
            )
        except Exception:  # noqa: BLE001 - runtime fault, not a legitimate verdict
            # allow_side_effects=False guarantees no side effect began, so
            # falling back to the AgentLoop result is safe. Mark the fallback
            # as un-graded: terminal status stays the AgentLoop's, but
            # reliability.faulted tells operators no verdict was produced.
            rel = agentloop_result.get("reliability")
            if isinstance(rel, dict):
                rel["faulted"] = True
            else:
                agentloop_result["reliability"] = {"faulted": True}
            return agentloop_result
        finally:
            self._active_cancels.pop(session_id, None)

        if result.get("run_dir"):
            metrics = self._load_metrics(Path(result["run_dir"]))
            if metrics:
                result["metrics"] = metrics
        return result

    @staticmethod
    def _coerce_synthesis(agentloop_result: Dict[str, Any], claim_cls) -> Dict[str, Any]:
        """Shape an AgentLoop result as a runtime synthesis dict."""
        synth: Dict[str, Any] = {
            "content": str(agentloop_result.get("content", "")),
            "claims": [c for c in (agentloop_result.get("claims") or []) if isinstance(c, claim_cls)],
        }
        for key in ("usage", "prompt_tokens", "completion_tokens", "total_tokens"):
            if key in agentloop_result:
                synth[key] = agentloop_result[key]
        return synth

    def _observe_reliability(
        self,
        *,
        registry: Any,
        user_message: str,
        session_id: str,
        synthesis: Dict[str, Any],
        agentloop_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run the runtime as a silent observer and return its redacted summary.

        The stub executor returns the AgentLoop's own synthesis so no
        provider/tool call is duplicated. The runtime's event callback is
        silenced (shadow records, it does not emit).
        """
        from src.reliability.runtime import ReliabilityRuntime

        runtime = ReliabilityRuntime(runs_dir=self.runs_dir, allow_side_effects=False)

        def stub_executor(**_kwargs: Any) -> Dict[str, Any]:
            return synthesis

        try:
            observed = runtime.run(
                user_message=user_message,
                session_id=session_id,
                registry=registry,
                executor=stub_executor,
                event_callback=lambda _et, _data: None,
            )
        except Exception:  # noqa: BLE001 - observer must never break the attempt
            return {}
        return dict(observed.get("reliability") or {})  # type: ignore[arg-type]

    @staticmethod
    def _convert_messages_to_history(messages: list) -> list[Dict[str, Any]]:
        """Convert Session messages into OpenAI-format history.

        Keeps the readable ``[prev_run: {run_id}]`` marker instead of removing it
        completely, and trims by character budget instead of a hard six-message cap
        so the LLM can still see previous artifact paths and strategy content during
        iterative updates.

        Args:
            messages: Session message list without the current turn.

        Returns:
            OpenAI-format messages trimmed from the newest items within the token budget.
        """
        import re
        from pathlib import Path

        def _shorten_run_dir(match: re.Match) -> str:
            path_str = match.group(0).replace("Run directory:", "").strip()
            run_id = Path(path_str).name if path_str else ""
            return f"[prev_run: {run_id}]" if run_id else ""

        history = []
        for msg in messages[:-1]:
            role = msg.role if hasattr(msg, "role") else msg.get("role", "user")
            content = msg.content if hasattr(msg, "content") else msg.get("content", "")
            if not content.strip() or role not in ("user", "assistant"):
                continue
            content = re.sub(r"Run directory:\s*\S+", _shorten_run_dir, content).strip()
            if content:
                history.append({"role": role, "content": content})

        # Trim from the newest messages within a character budget of roughly 3000 tokens.
        MAX_HISTORY_CHARS = 12000
        total_chars = 0
        trimmed: list = []
        for msg in reversed(history):
            msg_len = len(msg.get("content", ""))
            if total_chars + msg_len > MAX_HISTORY_CHARS:
                break
            trimmed.append(msg)
            total_chars += msg_len
        return list(reversed(trimmed))

    @staticmethod
    def _load_metrics(run_dir: Path) -> Optional[Dict[str, Any]]:
        """Load metrics.csv from a run directory."""
        import csv
        metrics_path = run_dir / "artifacts" / "metrics.csv"
        if not metrics_path.exists():
            return None
        try:
            with open(metrics_path, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                if rows:
                    return {k: float(v) for k, v in rows[0].items() if v}
        except Exception:
            pass
        return None

    @staticmethod
    def _format_result_message(attempt: Attempt) -> str:
        """Format the final execution result message."""
        if attempt.status == AttemptStatus.COMPLETED:
            return attempt.summary or "Strategy execution completed."
        return f"Execution failed: {attempt.error or 'unknown error'}"
