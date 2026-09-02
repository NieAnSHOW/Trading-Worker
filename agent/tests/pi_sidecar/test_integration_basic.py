"""基础集成：真实 sidecar + fake provider。skipif 无 bun。"""

import asyncio
import shutil
from pathlib import Path

import pytest

from tests.pi_sidecar.fake_provider import FakeOpenAIProvider  # noqa: E402 — pythonpath=agent，tests.pi_sidecar 有 __init__
from src.pi_sidecar.client import PiSidecarClient, SidecarError

pytestmark = pytest.mark.skipif(shutil.which("bun") is None, reason="bun not installed")

REPO = Path(__file__).resolve().parents[3]
SESSION = "a1b2c3d4e5f6"


def _client(provider: FakeOpenAIProvider, tmp_path: Path, events: list, on_host_request=None) -> PiSidecarClient:
    # Task 16 deviation (SDK 18.0.11 实证)：Pi 原生 openai provider 的目录模型全部是
    # openai-responses api（/v1/responses），且 registry 只认真实目录模型 id——openai/fake-model
    # 与 chat.completions stub 无法组合。改用 plan §model wiring 的正式路径：vibe-providers
    # extension 按 env 表注册 deepseek（openai-completions + baseUrl 覆盖到本地 stub），
    # 模型取目录内真实 openai-completions id（deepseek-v4-flash）。
    env = {
        "DEEPSEEK_BASE_URL": provider.base_url,
        "DEEPSEEK_API_KEY": "test",
        "LANGCHAIN_PROVIDER": "deepseek",
        "LANGCHAIN_MODEL_NAME": "deepseek-v4-flash",
        "VIBE_PI_AGENT_DIR": str(tmp_path / "agent"),
        "VIBE_PI_SESSIONS_DIR": str(tmp_path / "sessions"),
    }
    return PiSidecarClient(
        command=["bun", "run", "src/main.ts"],
        cwd=REPO / "pi-sidecar",
        agent_dir=tmp_path / "agent",
        sessions_dir=tmp_path / "sessions",
        env=env,  # B6: 显式传给 sidecar 进程（client._spawn 合并 os.environ），隔离真实 provider 配置
        on_event=lambda ev, sid, data: events.append((ev, sid, data)),
        on_host_request=on_host_request,  # B3: 入站 host op 处理器
    )


class TestBasicFlow:
    def test_multi_turn_with_text_response(self, tmp_path):
        async def run():
            provider = FakeOpenAIProvider([{"text": "first answer"}, {"text": "second answer"}])
            provider.start()
            events: list = []
            c = _client(provider, tmp_path, events)
            await c.start()
            try:
                await c.request("new_session", {"session_id": SESSION})
                await c.request("prompt", {"session_id": SESSION, "text": "hi"})
                r1 = await self._wait_terminal(c, SESSION, timeout=60)
                assert r1["content"] == "first answer"
                await c.request("prompt", {"session_id": SESSION, "text": "again"})
                r2 = await self._wait_terminal(c, SESSION, timeout=60)
                assert r2["content"] == "second answer"
                # Pi JSONL 会话文件存在且 header id 匹配；B6 目录隔离生效（落在 tmp 而非真实 ~/.vibe-trading/pi）
                state = await c.request("get_state", {"session_id": SESSION})
                session_file = Path(state["session_file"])
                assert session_file.exists()
                assert str(session_file).startswith(str(tmp_path)), "sidecar must honor VIBE_PI_SESSIONS_DIR"
            finally:
                await c.stop()
                provider.stop()

        asyncio.run(run())

    def test_tool_call_roundtrip_through_python_gateway(self, tmp_path, monkeypatch):
        async def run():
            import json as _json

            from src.pi_sidecar.gateway_bridge import GatewayBridge
            from src.pi_sidecar.manifest import build_manifest_tool
            from src.agent.tools import BaseTool, ToolRegistry

            class Doubler(BaseTool):
                name = "doubler"
                description = "doubles a number"
                parameters = {"type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"]}
                is_readonly = True
                side_effecting = False
                repeatable = True

                def execute(self, **kw):
                    return _json.dumps({"status": "ok", "result": kw["n"] * 2})

            manifest = [build_manifest_tool(Doubler())]
            provider = FakeOpenAIProvider(
                [
                    {"tool_calls": [{"id": "call_1", "name": "doubler", "arguments": {"n": 21}}]},
                    {"text": "answer is 42"},
                ]
            )
            provider.start()
            events: list = []
            reg = ToolRegistry()
            reg.register(Doubler())
            bridge = GatewayBridge(reg)

            # B3 集成接线：host op → 按注册表分发（tool_invoke → GatewayBridge.handle_invoke）
            async def dispatch_host(op: str, params: dict) -> dict:
                if op == "tool_invoke":
                    return await bridge.handle_invoke(params)
                raise SidecarError("unknown_op", f"unhandled host op {op}")

            c = _client(provider, tmp_path, events, on_host_request=dispatch_host)

            async def main():
                await c.start()
                await c.request("new_session", {"session_id": SESSION})
                await c.request("set_tool_manifest", {"tools": manifest})
                await c.request("prompt", {"session_id": SESSION, "text": "double 21"})
                final = await self._wait_terminal(c, SESSION, timeout=60)
                assert final["content"] == "answer is 42"
                # SSE 事件含 tool_call 与 tool_result（形状与 glossary 一致）
                names = [d.get("type") for ev, sid, d in events if ev == "session_event"]
                assert "tool_execution_start" in names
                assert "tool_execution_end" in names
                # provider 收到第二轮请求（带 tool result）
                assert len(provider.requests) >= 2

            try:
                await main()
            finally:
                await c.stop()
                provider.stop()

        asyncio.run(run())

    @staticmethod
    async def _wait_terminal(c: PiSidecarClient, session_id: str, timeout: float) -> dict:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await c.request("get_state", {"session_id": session_id})
            if not state["busy"]:
                msgs = await c.request("get_messages", {"session_id": session_id, "limit": 5})
                assistant = [m for m in msgs["messages"] if m["role"] == "assistant"]
                return {"content": assistant[-1]["content"] if assistant else "", "state": state}
            await asyncio.sleep(0.1)
        raise TimeoutError("turn did not settle")
