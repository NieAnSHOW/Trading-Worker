"""性能基线 harness（design §Verification：performance baseline）。

用法：PYTHONPATH=agent python scripts/desktop/perf_baseline.py --out perf-report.json
指标：首 token 延迟（prompt ack → 首个 text_delta）、工具吞吐（tool_invoke 完成/秒）、
取消延迟（abort → busy=false）、sidecar 重启恢复率（kill → ping 成功率）、
任务正确性（fixed corpus 每条期望 answer 前缀匹配）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

CORPUS = [
    {"q": "Summarize AAPL latest quarter", "expect_prefix": "AAPL"},
    {"q": "Compare MSFT vs GOOG margins", "expect_prefix": "MSFT"},
    {"q": "Analyze NVDA guidance", "expect_prefix": "NVDA"},
    {"q": "List risks for TSLA", "expect_prefix": "TSLA"},
    {"q": "Screen dividend stocks", "expect_prefix": "Screen"},
]


def _sid() -> str:
    return uuid.uuid4().hex[:12]


async def measure(session_factory, corpus) -> dict:
    first_tokens: list[float] = []
    correct = 0
    for item in corpus:
        c = await session_factory()
        sid = _sid()
        t0 = time.monotonic()
        await c.request("new_session", {"session_id": sid})
        await c.request("prompt", {"session_id": sid, "text": item["q"]})
        first_at = await _first_text_delta_at(c, sid, timeout=60)
        first_tokens.append(first_at - t0)
        final = await _final_content(c, sid, timeout=120)
        if final.startswith(item["expect_prefix"]):
            correct += 1
        await c.stop()
    return {
        "first_token_latency_s": first_tokens,
        "first_token_p50_s": statistics.median(first_tokens),
        "task_correctness": correct / len(corpus),
    }


async def _first_text_delta_at(c, sid, timeout):
    # 实证（Task 18 run 1）：sidecar 的 get_messages 在回合进行中被 session_busy 门禁
    # 拒绝（commands.ts "cannot page messages mid-turn"），无法轮询到流式中的首 token。
    # 首 token 信号改走事件流：client_maker 的 on_event 收集首个 text_delta 的单调时钟
    # （SDK message_update/assistantMessageEvent.text_delta，events.py 同一映射）。
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = getattr(c, "first_delta_at", {}).get(sid)
        if t is not None:
            return t
        await asyncio.sleep(0.02)
    raise TimeoutError("no first token")


async def _final_content(c, sid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await c.request("get_state", {"session_id": sid})
        if not state["busy"]:
            msgs = await c.request("get_messages", {"session_id": sid, "limit": 5})
            a = [m for m in msgs["messages"] if m["role"] == "assistant"]
            return a[-1]["content"] if a else ""
        await asyncio.sleep(0.1)
    raise TimeoutError("turn unsettled")


async def measure_cancellation_latency(client_maker) -> float:
    c = await client_maker()
    sid = _sid()
    try:
        await c.request("new_session", {"session_id": sid})
        t0 = time.monotonic()
        await c.request("prompt", {"session_id": sid, "text": "long task"})
        await c.request("abort", {"session_id": sid})
        while True:
            state = await c.request("get_state", {"session_id": sid})
            if not state["busy"]:
                return time.monotonic() - t0
            await asyncio.sleep(0.02)
    finally:
        await c.stop()


async def measure_restart_recovery(client_maker, kills: int = 3) -> float:
    # 实证（Task 18 run 2）：kill 后立即 ping 必然失败——client 的重启契约
    # （§Idempotency：in-flight 请求拒绝重放）会把 kill 瞬间在途的 ping 以
    # sidecar_restarted 拒掉，重启本身成功。恢复率须等重启完成再 ping。
    ok = 0
    for _ in range(kills):
        c = await client_maker()
        try:
            proc = c._proc  # noqa: SLF001 — 重启前后进程对象身份比对
            proc.kill()  # noqa: SLF001
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if c.started and c._proc is not proc:  # noqa: SLF001 — 新进程已 ready
                    break
                await asyncio.sleep(0.05)
            else:
                raise TimeoutError("sidecar did not recover after kill")
            await c.request("ping", {}, timeout=30)
            ok += 1
        except Exception:  # noqa: BLE001
            pass
        finally:
            await c.stop()
    return ok / kills


async def measure_tool_throughput(client_maker, rounds: int = 20) -> float:
    """tool_invoke 完成/秒（无模型链路：直接以 GatewayBridge 双只读工具循环计）。"""
    from src.pi_sidecar.gateway_bridge import GatewayBridge

    class NoopRegistry:
        def get(self, name):
            t = type("T", (), {})()
            t.name = name
            t.is_readonly = True
            t.side_effecting = False
            return t

    class FastGateway:
        def execute(self, *a, **k):
            return type(
                "SR",
                (),
                {
                    "status": "success",
                    "data": {},
                    "error": None,
                    "elapsed_ms": 1,
                    "to_wire": lambda s: {"status": "success"},
                },
            )()

    bridge = GatewayBridge(NoopRegistry(), gateway=FastGateway())
    t0 = time.monotonic()
    for i in range(rounds):
        await bridge.handle_invoke(
            {
                "call_id": f"p{i}",
                "toolCallId": f"pc{i}",
                "toolName": "noop",
                "arguments": {},
                "idempotencyKey": f"perf:k{i}",
                "is_readonly": True,
                "side_effecting": False,
                "repeatable": True,
                "timeout_seconds": 5,
            }
        )
    return rounds / (time.monotonic() - t0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=REPO / ".desktop-build/perf-report.json")
    args = ap.parse_args()
    if shutil.which("bun") is None:
        print("bun missing", file=sys.stderr)
        return 1

    from src.pi_sidecar.client import PiSidecarClient

    async def client_maker():
        first_delta_at: dict[str, float] = {}

        def _on_event(event: str, sid: str, data: dict) -> None:
            if event != "session_event" or not sid or sid in first_delta_at:
                return
            payload = data.get("payload") or {}
            ame = payload.get("assistantMessageEvent") or {}
            # wireEvents 帧：data = {type: <Pi 事件名>, payload: <事件体>}
            if data.get("type") == "message_update" and ame.get("type") == "text_delta" and ame.get("delta"):
                first_delta_at[sid] = time.monotonic()

        c = PiSidecarClient(command=["bun", "run", "src/main.ts"], cwd=REPO / "pi-sidecar", on_event=_on_event)
        c.first_delta_at = first_delta_at  # 事件收集器挂 client 实例（perf harness 内部约定）
        await c.start()
        return c

    async def run():
        report = {
            "measured_at": time.strftime("%FT%TZ", time.gmtime()),
            "baseline": await measure(client_maker, CORPUS),
            "cancellation_latency_s": await measure_cancellation_latency(client_maker),
            "restart_recovery_rate": await measure_restart_recovery(client_maker),
            "tool_throughput_per_s": await measure_tool_throughput(client_maker),
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2))
        print(f"perf report -> {args.out}")

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
