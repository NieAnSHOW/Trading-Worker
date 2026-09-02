"""性能基线 harness（design §Verification：performance baseline）——双引擎 A/B。

用法：
  PYTHONPATH=agent python scripts/desktop/perf_baseline.py [--engine {pi,legacy,both}] [--out perf-report.json]

对比目标：Agent 执行层更换（legacy Python AgentLoop vs Pi sidecar）。同一 corpus、
同一脚本化模型响应——pi 侧经 FakeOpenAIProvider（SSE，按用户问题 resolver 取脚本），
legacy 侧以同接口 duck-typed LLM 流式相同文本——两边模型耗时一致，差值即执行层开销。

指标（两引擎同口径）：
- first_token_p50_s        prompt 提交 → 首个 text delta（TTFT，热态：引擎预先就绪）
- turn_e2e_p50_s           prompt 提交 → 回合终止
- cancellation_latency_s   cancel 指令 → 回合终止
- task_correctness         期望 answer 前缀匹配（正确性 sanity，非性能）
- rss_mb                   执行层进程常驻内存（best-effort，psutil 缺失时 null）

pi 专属（legacy 无 sidecar 进程，不构成对比）：
- restart_recovery_rate    kill sidecar → 重启后 ping 成功率
- tool_throughput_per_s    GatewayBridge 只读工具循环吞吐

密闭性：provider env 只注入 sidecar 子进程（PiSidecarClient env=，B6 合并），
VIBE_PI_* 状态目录指向 tmp；不读真实 ~/.vibe-trading 状态、不出外网。

历史说明：Task 18 版本未注入 provider env（靠 shell 环境碰运气）、TTFT 含 sidecar
冷启动。本版密闭化 + 热态口径。模型 id 必须取 SDK 目录内真实 openai-completions
条目（deepseek-v4-flash；Task 16 实证 "fake-model" 解析失败）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
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

# 逐条内容实质不同：SDK 有 thinking_loop_detected 守卫（近重复回复会触发
# system-interrupt 并追加续跑请求），基准脚本若形状雷同会被守卫劫持。
_SCRIPTED_TEXT = {
    item["q"]: (
        f"{item['expect_prefix']} — scripted answer {i} for: {item['q']}. "
        + " ".join(f"{w}{j}" for j, w in enumerate(item["q"].split() * 14))
        + f". End of report {i}."
    )
    for i, item in enumerate(CORPUS)
}


def _answer_for(q: str) -> str:
    # sidecar 会在 user content 里内嵌 <system-reminder> 前缀（日期/cwd），须子串匹配。
    q = q.strip()
    if q in _SCRIPTED_TEXT:
        return _SCRIPTED_TEXT[q]
    for question, text in _SCRIPTED_TEXT.items():
        if question in q:
            return text
    return "unknown — scripted benchmark answer."


def _sid() -> str:
    return uuid.uuid4().hex[:12]


def _p50(xs: list[float]) -> float:
    return round(statistics.median(xs), 4) if xs else float("nan")


def _rss_mb(pid: int | None = None) -> float | None:
    try:
        import psutil

        p = psutil.Process(pid) if pid else psutil.Process(os.getpid())
        return round(p.memory_info().rss / (1024 * 1024), 1)
    except Exception:
        return None


def _load_fake_provider():
    spec = importlib.util.spec_from_file_location(
        "perf_fake_provider", REPO / "agent/tests/pi_sidecar/fake_provider.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.FakeOpenAIProvider


def _last_user(body: dict) -> str:
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):  # openai content-part 数组：拼 text 片段
                return "".join(p.get("text", "") for p in c if isinstance(p, dict))
            return json.dumps(c, ensure_ascii=False)
    return ""


# ---------------------------------------------------------------- pi 引擎 ----


async def bench_pi(tmp: Path) -> dict:
    from src.pi_sidecar.client import PiSidecarClient

    FakeOpenAIProvider = _load_fake_provider()
    provider = FakeOpenAIProvider(resolver=lambda body: {"text": _answer_for(_last_user(body))})
    provider.start()

    async def client_maker() -> PiSidecarClient:
        first_delta_at: dict[str, float] = {}

        def _on_event(event: str, sid: str, data: dict) -> None:
            if event != "session_event" or not sid or sid in first_delta_at:
                return
            payload = data.get("payload") or {}
            ame = payload.get("assistantMessageEvent") or {}
            if data.get("type") == "message_update" and ame.get("type") == "text_delta" and ame.get("delta"):
                first_delta_at[sid] = time.monotonic()

        c = PiSidecarClient(
            command=["bun", "run", "src/main.ts"],
            cwd=REPO / "pi-sidecar",
            env={
                # Task 16 实证：registry 只认目录内真实 openai-completions 模型 id。
                "DEEPSEEK_BASE_URL": provider.base_url,
                "DEEPSEEK_API_KEY": "perf-key",
                "LANGCHAIN_PROVIDER": "deepseek",
                "LANGCHAIN_MODEL_NAME": "deepseek-v4-flash",
                "VIBE_PI_AGENT_DIR": str(tmp / "pi-agent"),
                "VIBE_PI_SESSIONS_DIR": str(tmp / "pi-sessions"),
            },
            sessions_dir=tmp / "pi-sessions",
            agent_dir=tmp / "pi-agent",
            on_event=_on_event,
        )
        c.first_delta_at = first_delta_at  # harness 内部约定：事件收集器挂 client 实例
        await c.start()
        return c

    out: dict = {}
    client = await client_maker()  # 热态口径：spawn/ready 不计入 TTFT
    ttfts: list[float] = []
    e2es: list[float] = []
    correct = 0
    try:
        for item in CORPUS:
            sid = _sid()
            t0 = time.monotonic()
            await client.request("new_session", {"session_id": sid})
            await client.request("prompt", {"session_id": sid, "text": item["q"]})
            while sid not in client.first_delta_at:
                if time.monotonic() - t0 > 60:
                    raise TimeoutError("no first token")
                await asyncio.sleep(0.001)
            ttfts.append(client.first_delta_at[sid] - t0)
            await _final_content_pi(client, sid, timeout=120)
            e2es.append(time.monotonic() - t0)
            r = await client.request("get_messages", {"session_id": sid, "limit": 5})
            last_assistant = [m for m in r.get("messages", []) if m.get("role") == "assistant"]
            content = last_assistant[-1].get("content", "") if last_assistant else ""
            if content.startswith(item["expect_prefix"]):
                correct += 1
        out["first_token_p50_s"] = _p50(ttfts)
        out["turn_e2e_p50_s"] = _p50(e2es)
        out["task_correctness"] = round(correct / len(CORPUS), 3)
        out["cancellation_latency_s"] = await measure_cancellation_latency(client)
        out["tool_throughput_per_s"] = round(await measure_tool_throughput(), 1)
        out["rss_python_mb"] = _rss_mb()
        out["rss_sidecar_mb"] = _rss_mb(client._proc.pid if client._proc else None)  # noqa: SLF001
        out["restart_recovery_rate"] = await measure_restart_recovery(client_maker)
    finally:
        await client.stop()
        provider.stop()
    return out


async def _final_content_pi(c, sid: str, timeout: float) -> str:
    # 回合进行中 get_messages 被 session_busy 门禁（commands.ts），轮询直到可通过。
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = await c.request("get_messages", {"session_id": sid, "limit": 1}, timeout=10)
            msgs = r.get("messages") or []
            if msgs and msgs[-1].get("role") == "assistant":
                return msgs[-1].get("content", "")
        except Exception:
            pass
        await asyncio.sleep(0.005)
    raise TimeoutError("turn unsettled")


async def measure_cancellation_latency(client) -> float:
    sid = _sid()
    await client.request("new_session", {"session_id": sid})
    await client.request("prompt", {"session_id": sid, "text": CORPUS[0]["q"]})
    t0 = time.monotonic()
    await client.request("abort", {"session_id": sid})
    while time.monotonic() - t0 < 30:
        st = await client.request("get_state", {"session_id": sid})
        if not st.get("busy"):
            return round(time.monotonic() - t0, 4)
        await asyncio.sleep(0.001)
    raise TimeoutError("abort unsettled")


async def measure_restart_recovery(client_maker, kills: int = 3) -> float:
    # max_restarts=1 预算：每次 kill 用全新 client（实证自 Task 18 run 2）。
    # kill 后在途 ping 按 §Idempotency 以 sidecar_restarted 拒绝，等新进程 ready 再 ping。
    ok = 0
    for _ in range(kills):
        c = await client_maker()
        try:
            proc = c._proc  # noqa: SLF001 — 重启前后进程对象身份比对
            proc.kill()  # noqa: SLF001
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if c.started and c._proc is not proc:  # noqa: SLF001
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


async def measure_tool_throughput(rounds: int = 20) -> float:
    """tool_invoke 完成/秒（无模型链路：GatewayBridge 只读工具循环）。"""
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


# ------------------------------------------------------------ legacy 引擎 ----


def _mk_scripted_llm(recorder: dict | None = None):
    """与 tests 同款 duck-typed LLM：流式吐出与 FakeOpenAIProvider 相同文本。

    recorder 非 None 时把首个 text-delta 的单调时刻写入 recorder["first"]。
    """
    from src.providers.chat import LLMResponse

    class ScriptedLLM:
        def stream_chat(
            self, messages, tools=None, on_text_chunk=None, on_reasoning_chunk=None, timeout=None, should_cancel=None
        ):
            text = _answer_for(_last_user({"messages": messages}))
            if on_text_chunk:
                on_text_chunk(text)
                if recorder is not None and "first" not in recorder:
                    recorder["first"] = time.monotonic()
            return LLMResponse(content=text)

        def chat(self, messages, **_):
            return LLMResponse(content=_answer_for(_last_user({"messages": messages})))

    return ScriptedLLM()


def _mk_slow_llm(recorder: dict):
    """取消测量用：首 delta 后持续小步流式，给 cancel 留窗口。"""
    from src.providers.chat import LLMResponse

    class SlowLLM:
        def stream_chat(
            self, messages, tools=None, on_text_chunk=None, on_reasoning_chunk=None, timeout=None, should_cancel=None
        ):
            text = _answer_for(_last_user({"messages": messages}))
            if on_text_chunk:
                on_text_chunk(text)
                recorder["first"] = time.monotonic()
            for _ in range(400):
                if should_cancel and should_cancel():
                    return LLMResponse(content="")
                time.sleep(0.005)
            return LLMResponse(content=text)

        def chat(self, messages, **_):
            return LLMResponse(content=_answer_for(_last_user({"messages": messages})))

    return SlowLLM()


def _mk_legacy_agent(llm, run_dir: Path):
    from src.agent.loop import AgentLoop
    from src.tools import build_registry

    agent = AgentLoop(registry=build_registry(include_shell_tools=False), llm=llm)
    agent.memory.run_dir = str(run_dir)
    return agent


async def bench_legacy(tmp: Path) -> dict:
    loop = asyncio.get_running_loop()
    run_dir = tmp / "legacy-run"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 热态口径：AgentLoop 构造在计时外（对应 pi 侧 client 已 ready）。
    agent = _mk_legacy_agent(_mk_scripted_llm(), run_dir)

    ttfts: list[float] = []
    e2es: list[float] = []
    correct = 0
    for item in CORPUS:
        sid = _sid()
        recorder: dict = {}
        agent.llm = _mk_scripted_llm(recorder)

        t0 = time.monotonic()
        res = await loop.run_in_executor(None, lambda q=item["q"], s=sid: agent.run(user_message=q, session_id=s))
        ttfts.append(recorder.get("first", t0) - t0)
        e2es.append(time.monotonic() - t0)
        if res.get("status") == "success" and str(res.get("content", "")).startswith(item["expect_prefix"]):
            correct += 1

    cancel_s = await loop.run_in_executor(None, lambda: _legacy_cancel_latency(run_dir))

    return {
        "first_token_p50_s": _p50(ttfts),
        "turn_e2e_p50_s": _p50(e2es),
        "task_correctness": round(correct / len(CORPUS), 3),
        "cancellation_latency_s": cancel_s,
        "rss_python_mb": _rss_mb(),
    }


def _legacy_cancel_latency(run_dir: Path) -> float:
    recorder: dict = {}
    agent = _mk_legacy_agent(_mk_slow_llm(recorder), run_dir)
    done = threading.Event()

    def _work():
        agent.run(user_message=CORPUS[0]["q"], session_id=_sid())
        done.set()

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    deadline = time.monotonic() + 30
    while "first" not in recorder and time.monotonic() < deadline:
        time.sleep(0.001)
    cancel_t = time.monotonic()
    agent.cancel()
    th.join(timeout=30)
    return round(time.monotonic() - cancel_t, 4)


# ------------------------------------------------------------------ main ----


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["pi", "legacy", "both"], default="both")
    ap.add_argument("--out", type=Path, default=REPO / ".desktop-build/perf-report.json")
    args = ap.parse_args()
    if args.engine in ("pi", "both") and shutil.which("bun") is None:
        print("bun missing", file=sys.stderr)
        return 1

    async def run():
        report: dict = {"measured_at": time.strftime("%FT%TZ", time.gmtime())}
        with tempfile.TemporaryDirectory(prefix="vibe-perf-") as td:
            tmp = Path(td)
            if args.engine in ("pi", "both"):
                print("measuring pi engine ...", file=sys.stderr)
                report["pi"] = await bench_pi(tmp)
            if args.engine in ("legacy", "both"):
                print("measuring legacy engine ...", file=sys.stderr)
                report["legacy"] = await bench_legacy(tmp)
        if "pi" in report and "legacy" in report:
            report["delta_pi_minus_legacy"] = {
                k: round(report["pi"][k] - report["legacy"][k], 4)
                for k in ("first_token_p50_s", "turn_e2e_p50_s", "cancellation_latency_s")
                if k in report["pi"] and k in report["legacy"]
            }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2))
        print(f"perf report -> {args.out}")
        for eng in ("legacy", "pi"):
            if eng in report:
                r = report[eng]
                extra = f"  +sidecar={r.get('rss_sidecar_mb')}MB" if "rss_sidecar_mb" in r else ""
                print(
                    f"[{eng:6}] ttft_p50={r.get('first_token_p50_s')}s  e2e_p50={r.get('turn_e2e_p50_s')}s  "
                    f"cancel={r.get('cancellation_latency_s')}s  correct={r.get('task_correctness')}  "
                    f"python_rss={r.get('rss_python_mb')}MB{extra}"
                )
        if "delta_pi_minus_legacy" in report:
            print(f"[delta  ] pi-legacy: {report['delta_pi_minus_legacy']}")
        if "pi" in report:
            print(
                f"[pi only] restart_recovery={report['pi'].get('restart_recovery_rate')}  "
                f"tool_throughput={report['pi'].get('tool_throughput_per_s')}/s"
            )

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
