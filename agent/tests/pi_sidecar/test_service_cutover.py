"""SessionService pi 引擎 cutover 单测（fake sidecar client，无 bun 依赖）。"""

import asyncio
from pathlib import Path

import pytest

from src.pi_sidecar.client import SidecarError
from src.session.models import Attempt
from src.session.service import SessionService


class _DummyIndex:
    def index_session(self, session_id: str, title: str) -> None:
        del session_id, title

    def index_message(self, session_id: str, role: str, content: str) -> None:
        del session_id, role, content


class FakeClient:
    """duck-type PiSidecarClient：started/unavailable 语义与真实 client 一致（B4）。"""

    def __init__(self, *, unavailable=False, fail_start=False):
        self.unavailable = unavailable
        self.fail_start = fail_start
        # fail_start 场景由 start() 翻转：启动失败即标记永久不可用（与真实 client 一致）。
        self.started = not fail_start
        self.requests: list[tuple[str, dict]] = []

    async def start(self):
        if self.fail_start:
            self.unavailable = True  # 真实 client：启动失败即标记永久不可用
            raise SidecarError("pi_sidecar_unavailable", "spawn failed")
        self.started = True

    async def request(self, op, params, *, timeout=30.0):
        import src.session.service as svc_mod

        self.requests.append((op, params))
        if op == "open_session":
            raise SidecarError("not_found", "no session")
        if op == "new_session":
            return {"session_file": "/tmp/pi/fake.jsonl"}
        if op == "set_tool_manifest":
            return {"count": len(params.get("tools", []))}
        if op == "prompt":
            # 模拟 sidecar 事件流（经模块级 listener 注册表派发，B4/B5）
            sid = params["session_id"]
            listener = svc_mod._PI_EVENT_LISTENERS.get(sid)
            assert listener is not None, "service must register an event listener before prompt"
            listener("session_event", sid, {"type": "message_start", "payload": {"entryId": "assist1"}})
            listener(
                "session_event",
                sid,
                {
                    "type": "message_update",
                    "payload": {"assistantMessageEvent": {"type": "text_delta", "delta": "最终答案内容"}},
                },
            )
            listener(
                "session_event",
                sid,
                {
                    "type": "message_end",
                    "payload": {
                        "entryId": "assist1",
                        "message": {
                            "role": "assistant",
                            "provider": "openai",
                            "model": "gpt-4o",
                            "content": [{"type": "text", "text": "最终答案内容"}],
                            "usage": {"input": 10, "output": 5},
                        },
                    },
                },
            )
            listener("session_event", sid, {"type": "agent_end", "payload": {"isTerminal": True}})
            return {"accepted": True}
        if op == "get_messages":
            return {
                "messages": [
                    {"entry_id": "e1", "role": "user", "content": "q", "timestamp": "t1"},
                    {"entry_id": "e2", "role": "assistant", "content": "最终答案内容", "timestamp": "t2"},
                ],
                "next_cursor": None,
            }
        if op == "abort":
            return {"aborted": True}
        raise SidecarError("unknown_op", op)


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    from src.session.store import SessionStore
    from src.session.events import EventBus
    import src.session.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_shared_index", lambda: _DummyIndex())
    store = SessionStore(base_dir=tmp_path / "sessions")
    bus = EventBus()
    service = SessionService(store, bus, runs_dir=tmp_path / "runs")
    return service


def _wire_pi(svc, fake, monkeypatch):
    """monkeypatch client 单例与 pi-loop 桥（测试线程无运行 loop → asyncio.run 直跑）。"""
    import src.session.service as svc_mod

    monkeypatch.setattr(svc_mod, "_get_pi_client", lambda: fake)
    monkeypatch.setattr(svc_mod, "_run_on_pi_loop", lambda coro, timeout=30.0: asyncio.run(coro))
    return fake


def test_pi_engine_full_attempt_flow(svc, monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_AGENT_ENGINE", "pi")
    fake = _wire_pi(svc, FakeClient(), monkeypatch)
    session = svc.create_session(title="t")
    result = svc._run_with_pi(
        Attempt(session_id=session.session_id, prompt="research AAPL", created_at="2026-08-30T00:00:00"),
        include_shell_tools=False,
        session_config=None,
    )
    assert result["status"] == "success"
    assert result["content"] == "最终答案内容"
    assert result["pi"]["pi_session_id"] == session.session_id
    assert result["pi"]["entry_ids"] == ["assist1"]  # M2：entry_ids 来自 message_start/end payload
    assert Path(result["run_dir"]).exists()
    ops = [op for op, _ in fake.requests]
    assert ops[:3] == ["open_session", "new_session", "set_tool_manifest"]
    assert ("prompt", {"session_id": session.session_id, "text": "research AAPL"}) in fake.requests
    # SSE 事件已经过归一化（text_delta/llm_usage 形状）
    events = svc.event_bus.replay(session.session_id, replay_all=True)
    names = [e.event_type for e in events]
    assert "text_delta" in names and "llm_usage" in names


def test_pi_unavailable_fails_without_legacy_fallback(svc, monkeypatch):
    monkeypatch.setenv("VIBE_AGENT_ENGINE", "pi")
    _wire_pi(svc, FakeClient(fail_start=True), monkeypatch)
    session = svc.create_session(title="t")
    attempt = Attempt(session_id=session.session_id, prompt="x", created_at="2026-08-30T00:00:00")
    result = svc._run_with_pi(attempt, include_shell_tools=False, session_config=None)
    assert result["status"] == "failed"
    assert result["reason"] == "pi_sidecar_unavailable"
    assert result.get("_legacy_fallback") is None  # 绝不回退


def test_legacy_engine_flag_uses_old_path(svc, monkeypatch):
    monkeypatch.setenv("VIBE_AGENT_ENGINE", "legacy")
    import src.session.service as svc_mod

    called = {"pi": 0, "legacy": 0}
    monkeypatch.setattr(
        svc_mod.SessionService,
        "_run_with_pi",
        lambda self, attempt, **k: called.__setitem__("pi", called["pi"] + 1) or {},
    )
    monkeypatch.setattr(
        svc_mod.SessionService,
        "_run_with_agent",
        lambda self, attempt, **k: (
            called.__setitem__("legacy", called["legacy"] + 1)
            or {"status": "success", "content": "", "run_dir": "", "react_trace": [], "iterations": 0}
        ),
    )
    session = svc.create_session(title="t")
    # monkeypatch 后 _run_with_agent 是同步 lambda（非协程），直接调用
    svc._run_with_agent(Attempt(session_id=session.session_id, prompt="x", created_at="2026-08-30T00:00:00"))
    assert called == {"pi": 0, "legacy": 1}


class _DummyAgentLoop:
    def __init__(self, **kwargs):
        del kwargs

    def run(self, **kwargs):
        del kwargs
        return {"status": "success", "content": "legacy 答案", "run_dir": "", "react_trace": [], "iterations": 0}


def _stub_legacy_deps(monkeypatch):
    """stub _run_with_agent legacy 函数体依赖，让真实 legacy 执行体可跑完。"""
    monkeypatch.setattr("src.tools.build_registry", lambda **kw: object())
    monkeypatch.setattr("src.providers.chat.ChatLLM", lambda: object())
    monkeypatch.setattr("src.memory.persistent.PersistentMemory", lambda: object())
    monkeypatch.setattr("src.agent.loop.AgentLoop", _DummyAgentLoop)
    monkeypatch.setattr("src.config.loader.load_runtime_agent_config", lambda overrides=None: object())
    monkeypatch.setattr("src.config.loader.sanitize_session_overrides", lambda overrides: dict(overrides or {}))


async def _send_and_drain(svc, session_id: str, text: str) -> str:
    """走真实交互路径 send_message → 后台 _run_attempt task，并等 task 跑完。"""
    resp = await svc.send_message(session_id, text)
    await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
    return resp["attempt_id"]


@pytest.mark.parametrize("engine_env", ["pi", None])  # None = VIBE_AGENT_ENGINE 未设，get_agent_engine 默认 "pi"
def test_legacy_only_provider_gate_routes_interactive_path_to_legacy(svc, monkeypatch, engine_env):
    """vip_server 会话经 send_message 真实交互路径：pi 分支入口 gate 拦截，
    (a) 不触达 sidecar，(b) 走 legacy 执行体，(c) 发 pi.notice。"""
    if engine_env is not None:
        monkeypatch.setenv("VIBE_AGENT_ENGINE", engine_env)
    else:
        monkeypatch.delenv("VIBE_AGENT_ENGINE", raising=False)
    monkeypatch.setenv("LANGCHAIN_PROVIDER", "vip_server")

    fake = _wire_pi(svc, FakeClient(), monkeypatch)  # 若 gate 失效会记录 sidecar 请求
    _stub_legacy_deps(monkeypatch)
    session = svc.create_session(title="t")

    attempt_id = asyncio.run(_send_and_drain(svc, session.session_id, "hello legacy-only"))

    # (a) sidecar 绝未触达：client 零请求、_run_with_pi 从未执行
    assert fake.requests == []
    # (b) legacy 执行体真实跑完：attempt 完成且 assistant 回复落 store（pi 路径从不写 store 消息）
    attempt = svc.store.get_attempt(session.session_id, attempt_id)
    assert attempt is not None and attempt.status.value == "completed"
    msgs = svc.store.get_messages(session.session_id)
    assert any(m.role == "assistant" for m in msgs)
    # (c) pi.notice 诊断事件（kind=legacy_provider_fallback）
    notices = [e for e in svc.event_bus.replay(session.session_id, replay_all=True) if e.event_type == "pi.notice"]
    assert any(n.data.get("kind") == "legacy_provider_fallback" for n in notices)


def test_legacy_engine_flag_gate_inert_for_legacy_only_provider(svc, monkeypatch):
    """engine=legacy 时 gate 必须惰性：vip_server 直接走 legacy，不发 pi.notice、不触达 sidecar。"""
    monkeypatch.setenv("VIBE_AGENT_ENGINE", "legacy")
    monkeypatch.setenv("LANGCHAIN_PROVIDER", "vip_server")

    fake = _wire_pi(svc, FakeClient(), monkeypatch)
    _stub_legacy_deps(monkeypatch)
    session = svc.create_session(title="t")

    attempt_id = asyncio.run(_send_and_drain(svc, session.session_id, "hello legacy"))

    assert fake.requests == []
    attempt = svc.store.get_attempt(session.session_id, attempt_id)
    assert attempt is not None and attempt.status.value == "completed"
    msgs = svc.store.get_messages(session.session_id)
    assert any(m.role == "assistant" for m in msgs)
    notices = [e for e in svc.event_bus.replay(session.session_id, replay_all=True) if e.event_type == "pi.notice"]
    assert notices == []


def test_cancel_current_pi_engine_aborts(svc, monkeypatch):
    monkeypatch.setenv("VIBE_AGENT_ENGINE", "pi")
    fake = _wire_pi(svc, FakeClient(), monkeypatch)
    session = svc.create_session(title="t")
    svc._active_pi_sessions.add(session.session_id)
    assert svc.cancel_current(session.session_id) is True
    assert ("abort", {"session_id": session.session_id}) in fake.requests


def test_get_messages_routes_to_pi_for_pi_sessions(svc, monkeypatch):
    monkeypatch.setenv("VIBE_AGENT_ENGINE", "pi")
    fake = _wire_pi(svc, FakeClient(), monkeypatch)
    session = svc.create_session(title="t")
    svc._pi_sessions[session.session_id] = {"migrated": True}
    msgs = svc.get_messages(session.session_id, limit=10)
    assert len(msgs) == 2
    assert msgs[1].metadata.get("pi_entry_id") == "e2"
    assert any(op == "get_messages" for op, _ in fake.requests)
