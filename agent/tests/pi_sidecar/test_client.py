"""PiSidecarClient 单测：ready 关联、请求/响应、事件分发、restart-once、不可用状态。"""

import asyncio
import sys
from pathlib import Path

import pytest

from src.pi_sidecar.client import PiSidecarClient, SidecarError

STUB = str(Path(__file__).parent / "fixtures" / "stub_sidecar.py")


def _command() -> list[str]:
    return [sys.executable, STUB]


class TestBasics:
    def test_ping_roundtrip(self):
        async def run():
            c = PiSidecarClient(command=_command())
            await c.start()
            try:
                assert await c.request("ping", {}) == {"pong": True, "protocol": 1}
            finally:
                await c.stop()

        asyncio.run(run())

    def test_unknown_op_maps_to_sidecar_error(self):
        async def run():
            c = PiSidecarClient(command=_command())
            await c.start()
            try:
                with pytest.raises(SidecarError) as ei:
                    await c.request("frobnicate", {})
                assert ei.value.code == "unknown_op"
            finally:
                await c.stop()

        asyncio.run(run())

    def test_events_dispatched_to_callback(self):
        got = []

        async def run():
            c = PiSidecarClient(command=_command(), on_event=lambda ev, sid, data: got.append((ev, sid, data)))
            await c.start()
            try:
                # 桩不发事件；client 自身 ready 事件也算分发路径验证
                await c.request("ping", {})
                assert any(ev == "ready" for ev, _, _ in got)
            finally:
                await c.stop()

        asyncio.run(run())

    def test_chatty_stderr_drained_and_bounded(self):
        """stderr 不 drain 会撑满 64KB 管道缓冲反向阻塞 sidecar（start/request 挂起）。

        桩先写 5000 行诊断（≈230KB，远超管道缓冲）再发 ready；drain 正常则
        start()/request() 完成，尾部有界（deque maxlen=200）且不含协议污染。
        """

        async def run():
            c = PiSidecarClient(command=_command(), env={"STUB_CHATTY_STDERR": "5000"})
            await c.start()
            try:
                assert await c.request("ping", {}) == {"pong": True, "protocol": 1}
            finally:
                await c.stop()
            tail = c.stderr_tail
            assert len(tail) <= 200
            assert tail[-1] == "diag line 4999: sidecar diagnostics lorem ipsum"
            assert tail[0] == "diag line 4800: sidecar diagnostics lorem ipsum"

        asyncio.run(run())


class TestHostRequests:
    def test_inbound_host_request_dispatched_and_response_framed(self):
        """B3：sidecar→Python 的 op 请求帧（id+op，无 ok）→ on_host_request → 回响应帧。"""

        async def run():
            events = []

            async def handler(op: str, params: dict) -> dict:
                return {"content": '{"status":"ok"}', "isError": False, "outcome": "ok"}

            c = PiSidecarClient(
                command=_command(),
                env={"STUB_SEND_HOST_REQ": "tool_invoke"},
                on_event=lambda ev, sid, d: events.append((ev, d)),
                on_host_request=handler,
            )
            await c.start()
            try:
                # 桩在首个请求处理后发 tool_invoke 请求并等待响应帧；client 自动回帧后
                # 桩发出 host_roundtrip 事件确认闭环
                await c.request("ping", {})
                deadline = asyncio.get_running_loop().time() + 10
                while asyncio.get_running_loop().time() < deadline:
                    if any(ev == "host_roundtrip" for ev, _ in events):
                        break
                    await asyncio.sleep(0.05)
                frame = next(d for ev, d in events if ev == "host_roundtrip")
                assert frame["ok"] is True
                assert frame["result"] == {"content": '{"status":"ok"}', "isError": False, "outcome": "ok"}
            finally:
                await c.stop()

        asyncio.run(run())

    def test_host_request_handler_error_yields_error_frame(self):
        """handler 抛错 → client 回 ok:false 帧（桩收到非 ok 帧 → 其 assert 失败 →
        桩进程退出 → client 读到 EOF）。本用例断言：错误 handler 不产生 host_roundtrip
        成功事件，且 client 在桩退出后仍可重启服务后续请求。"""

        async def run():
            events = []

            async def handler(op: str, params: dict) -> dict:
                return {"_host_error_": {"code": "internal", "message": "gateway exploded"}}

            c = PiSidecarClient(
                command=_command(),
                env={"STUB_SEND_HOST_REQ": "tool_invoke"},
                on_event=lambda ev, sid, d: events.append((ev, d)),
                on_host_request=handler,
            )
            await c.start()
            try:
                await c.request("ping", {})
                await asyncio.sleep(1.0)  # 给桩发 host 请求 + 收到 ok:false 帧的时间
                # 桩收到 ok:false 帧 → 其 assert 失败 → os._exit；client 不应崩溃且可继续服务
                r = await c.request("ping", {}, timeout=15)
                assert r == {"pong": True, "protocol": 1}
                assert not any(ev == "host_roundtrip" for ev, _ in events)
            finally:
                await c.stop()

        asyncio.run(run())

    def test_respond_host_error_frame_shape(self):
        """handler 抛错 → respond_host 发 ok:false 帧（桩收到后 assert 失败 → 事件缺失）。"""

        async def run():
            from src.pi_sidecar.protocol import decode_frame

            sent: list[bytes] = []

            class FakeStdin:
                def write(self, data):
                    sent.append(data)

                async def drain(self):
                    return None

            c = PiSidecarClient(command=_command())
            c._proc = type("P", (), {"stdin": FakeStdin()})()  # noqa: SLF001 — 注入假管道
            await c.respond_host("s-host-9", {"_host_error_": {"code": "unknown_op", "message": "nope"}})
            frame = decode_frame(sent[0].decode())
            assert frame["id"] == "s-host-9" and frame["ok"] is False
            assert frame["error"] == {"code": "unknown_op", "message": "nope"}

        asyncio.run(run())


class TestEnvPassthrough:
    def test_env_dict_reaches_sidecar_process(self):
        """B6：client env 参数必须透传到子进程（桩回显进 ready 帧 sdk 字段）。"""
        got = []

        async def run():
            c = PiSidecarClient(
                command=_command(),
                env={"VIBE_TEST_ENV_MARKER": "env-probe-123"},
                on_event=lambda ev, sid, data: got.append(data),
            )
            await c.start()
            try:
                ready = next(d for d in got if d.get("protocol") == 1)
                assert ready["sdk"] == "env-probe-123"
            finally:
                await c.stop()

        asyncio.run(run())


class TestRestart:
    def test_crash_restarts_once_and_reopens_sessions(self):
        async def run():
            opened: list[list[str]] = []
            c = PiSidecarClient(command=_command() + [], max_restarts=1)
            # 用 open_session 注册活跃会话（桩返回固定 result）
            await c.start()
            await c.request("open_session", {"session_id": "a1b2c3d4e5f6"})
            c._active_sessions.add("a1b2c3d4e5f6")  # noqa: SLF001 — 单测注入活跃集
            original_open = c.request

            async def spy(op, params, **kw):
                if op == "open_session":
                    opened.append([str(params.get("session_id"))])
                return await original_open(op, params, **kw)

            c.request = spy  # type: ignore[method-assign]
            # 强杀进程（模拟崩溃）
            assert c._proc is not None  # noqa: SLF001
            c._proc.kill()  # noqa: SLF001
            await asyncio.sleep(0.2)
            # 下一个请求应触发重启 + 活跃会话重开
            r = await c.request("ping", {}, timeout=15)
            assert r == {"pong": True, "protocol": 1}
            assert "a1b2c3d4e5f6" in opened[0]
            await c.stop()

        asyncio.run(run())

    def test_second_crash_permanently_unavailable(self):
        async def run():
            c = PiSidecarClient(command=_command(), max_restarts=1)
            await c.start()
            for _ in range(2):
                assert c._proc is not None  # noqa: SLF001
                c._proc.kill()  # noqa: SLF001
                await asyncio.sleep(0.2)
                try:
                    await c.request("ping", {}, timeout=15)
                except SidecarError:
                    pass
            assert c.unavailable is True
            with pytest.raises(SidecarError) as ei:
                await c.request("ping", {})
            assert ei.value.code == "pi_sidecar_unavailable"
            await c.stop()

        asyncio.run(run())

    def test_unready_start_counts_against_budget(self):
        async def run():
            c = PiSidecarClient(command=[sys.executable, "-c", "import sys; sys.exit(3)"], max_restarts=1)
            with pytest.raises(SidecarError):
                await c.start()
            assert c.unavailable is True

        asyncio.run(run())
