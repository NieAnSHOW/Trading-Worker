"""高级集成：design §Verification integration 全项覆盖。"""

import asyncio
import shutil
from pathlib import Path

import pytest

from src.pi_sidecar.client import SidecarError
from src.session.models import Message, Session
from src.session.store import SessionStore

pytestmark = pytest.mark.skipif(shutil.which("bun") is None, reason="bun not installed")

REPO = Path(__file__).resolve().parents[3]
SESSION = "b2c3d4e5f6a1"

# _client/_wait_terminal 复用 test_integration_basic 的 helper（import 复用，不重复实现）：
# Task 16 落地时 _wait_terminal 是 TestBasicFlow 的静态方法（非模块级自由函数）。
# 经模块别名取用——不能把 TestBasicFlow 绑进本模块命名空间（pytest 会重复收集其
# Test* 用例，本文件将多跑 basic 的两个集成测试）。
from tests.pi_sidecar import test_integration_basic as _basic  # noqa: E402

_client = _basic._client
_wait_terminal = _basic.TestBasicFlow._wait_terminal
FakeOpenAIProvider = _basic.FakeOpenAIProvider


@pytest.fixture(autouse=True)
def _marker_dir_isolated(tmp_path, monkeypatch):
    """Hermetic：懒迁移 marker 落 tmp（默认写真实 ~/.vibe-trading/pi/migration）。"""
    from src.pi_sidecar import migration

    monkeypatch.setattr(migration, "MIGRATION_MARKER_DIR", tmp_path / "markers")


class TestCompactionAndReopen:
    def test_manual_compaction_and_session_reopen(self, tmp_path):
        async def run():
            provider = FakeOpenAIProvider([{"text": "answer one"}])
            provider.start()
            events: list = []
            c = _client(provider, tmp_path, events)
            await c.start()
            try:
                await c.request("new_session", {"session_id": SESSION})
                await c.request("prompt", {"session_id": SESSION, "text": "hello"})
                await _wait_terminal(c, SESSION, timeout=60)
                state_before = await c.request("get_state", {"session_id": SESSION})
                assert await c.request("compact", {"session_id": SESSION}) == {"started": True}
                # compact 完成后 session 仍可用
                await c.request("prompt", {"session_id": SESSION, "text": "still there?"})
                await _wait_terminal(c, SESSION, timeout=60)
                # 重开（close + open 新 client）
                await c.stop()
                c2 = _client(provider, tmp_path, events)
                await c2.start()
                r = await c2.request("open_session", {"session_id": SESSION})
                assert Path(r["session_file"]) == Path(state_before["session_file"])
                msgs = await c2.request("get_messages", {"session_id": SESSION, "limit": 50})
                assert len(msgs["messages"]) >= 2
                await c2.stop()
            finally:
                provider.stop()

        asyncio.run(run())


class TestSteerAbort:
    def test_steer_and_abort(self, tmp_path):
        async def run():
            # 脚本两个回复；第一个后 steering 窗口
            provider = FakeOpenAIProvider([{"text": "working"}, {"text": "done"}])
            provider.start()
            events: list = []
            c = _client(provider, tmp_path, events)
            await c.start()
            try:
                await c.request("new_session", {"session_id": SESSION})
                await c.request("prompt", {"session_id": SESSION, "text": "start"})
                await asyncio.sleep(0.5)
                assert await c.request("steer", {"session_id": SESSION, "text": "pivot"}) == {"queued": True}
                await _wait_terminal(c, SESSION, timeout=60)
                # abort 中断在飞回合
                await c.request("prompt", {"session_id": SESSION, "text": "long one"})
                await asyncio.sleep(0.2)
                assert await c.request("abort", {"session_id": SESSION}) == {"aborted": True}
                await asyncio.sleep(0.5)
                state = await c.request("get_state", {"session_id": SESSION})
                assert state["busy"] is False
            finally:
                await c.stop()
                provider.stop()

        asyncio.run(run())


class TestRestart:
    def test_crash_restart_reopens_session(self, tmp_path):
        async def run():
            provider = FakeOpenAIProvider([{"text": "survived"}])
            provider.start()
            events: list = []
            c = _client(provider, tmp_path, events)
            await c.start()
            try:
                await c.request("new_session", {"session_id": SESSION})
                # 杀进程 → client 重启一次并重开活跃会话
                c._proc.kill()  # noqa: SLF001
                await asyncio.sleep(0.3)
                r = await c.request("ping", {}, timeout=30)
                assert r["pong"] is True
                r2 = await c.request("open_session", {"session_id": SESSION})
                assert Path(r2["session_file"]).exists()
            finally:
                await c.stop()
                provider.stop()

        asyncio.run(run())


class TestDuplicateAndUnknownOutcome:
    def test_duplicate_tool_call_suppressed(self, tmp_path):
        # 幂等逻辑在 Python GatewayBridge（Task 8 单测已覆盖语义）；
        # 此处端到端验证：同一 assistant turn 中相同 idempotencyKey 只触发一次 execute
        class CountingGateway:
            def __init__(self):
                self.n = 0

            def execute(self, tool_name, arguments, *, step_id, policy, session_id=""):
                import time

                self.n += 1
                time.sleep(0.05)
                return type(
                    "SR",
                    (),
                    {
                        "status": "success",
                        "data": {"n": self.n},
                        "error": None,
                        "elapsed_ms": 1,
                        "to_wire": lambda: {"status": "success", "data": {"n": self.n}},
                    },
                )()

        # 语义级端到端（不依赖 fake provider 时序）：直接以 handle_invoke 双发同键
        from src.pi_sidecar.gateway_bridge import GatewayBridge

        bridge = GatewayBridge(None, gateway=CountingGateway())

        async def run():
            params = {
                "call_id": "t1",
                "toolCallId": "c1",
                "toolName": "x",
                "arguments": {},
                "idempotencyKey": "s:e:c",
                "is_readonly": True,
                "side_effecting": False,
                "repeatable": True,
                "timeout_seconds": 5,
            }
            r1 = await bridge.handle_invoke(params)
            r2 = await bridge.handle_invoke(params)
            assert r1["content"] == r2["content"]
            assert bridge._gw().n == 1  # noqa: SLF001 — 只执行一次

        asyncio.run(run())

    def test_unknown_write_outcome_marked(self, tmp_path):
        class DyingGateway:
            def execute(self, *a, **k):
                raise ConnectionError("sidecar pipe broke mid-write")

        from src.pi_sidecar.gateway_bridge import GatewayBridge

        bridge = GatewayBridge(None, gateway=DyingGateway())

        async def run():
            r = await bridge.handle_invoke(
                {
                    "call_id": "t2",
                    "toolCallId": "c2",
                    "toolName": "trading_place_order",
                    "arguments": {},
                    "idempotencyKey": "s:e:c2",
                    "is_readonly": False,
                    "side_effecting": True,
                    "repeatable": False,
                    "timeout_seconds": 5,
                }
            )
            assert r["outcome"] == "outcome_unknown"
            assert r["isError"] is True

        asyncio.run(run())


class TestMigrationProjection:
    def test_old_session_lazy_migration_and_projection(self, tmp_path):
        async def run():
            store = SessionStore(base_dir=tmp_path / "old-sessions")
            s = store.create_session(Session(title="old"))
            s.session_id = SESSION
            store.update_session(s)
            store._session_dir(SESSION).mkdir(parents=True, exist_ok=True)
            for role, content in [("user", "old q"), ("assistant", "old a")]:
                store.append_message(Message(session_id=SESSION, role=role, content=content))

            provider = FakeOpenAIProvider([{"text": "post-migration answer"}])
            provider.start()
            events: list = []
            c = _client(provider, tmp_path, events)
            await c.start()
            try:
                from src.pi_sidecar.migration import is_migrated, migrate_session_if_needed

                class FakeSvc:
                    """service duck-type：migrate_session_if_needed 只读 service.store。

                    brief 原写法 `class FakeSvc: store = store` 在类体内自引用同名
                    赋值 → NameError（类体作用域不回落闭包）；与 test_migration 的
                    FakeService 一致改为 __init__ 持有。
                    """

                    def __init__(self, store):
                        self.store = store

                # not_found → migrate → 同 ID Pi 会话含旧消息
                with pytest.raises(SidecarError) as ei:
                    await c.request("open_session", {"session_id": SESSION})
                assert ei.value.code == "not_found"
                assert await migrate_session_if_needed(FakeSvc(store), c, SESSION) is True
                assert is_migrated(SESSION)
                msgs = await c.request("get_messages", {"session_id": SESSION, "limit": 10})
                roles = [m["role"] for m in msgs["messages"]]
                assert "user" in roles and "assistant" in roles
                contents = " ".join(m["content"] for m in msgs["messages"])
                assert "old q" in contents and "old a" in contents
                # 迁移后可继续 prompt
                await c.request("prompt", {"session_id": SESSION, "text": "new q"})
                final = await _wait_terminal(c, SESSION, timeout=60)
                assert final["content"] == "post-migration answer"
            finally:
                await c.stop()
                provider.stop()

        asyncio.run(run())
