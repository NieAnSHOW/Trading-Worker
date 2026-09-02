"""Pi sidecar 工具执行网关桥。

设计约束（design §Idempotency + Global Constraints）：
- 幂等键 = session_id:assistantEntryId:toolCallId；已完成重复调用回放记录结果。
- 所有执行穿过 ToolGateway（唯一安全 choke point）；side-effecting 永不自动重试
  （ToolGateway._is_side_effecting + allow_side_effects 语义保持）。
- 写与未知分类串行；只读并发受 Semaphore 限界。
- 连接/进程失败时写类结果未知 → outcome_unknown；只读 → error。
- 返回给 Pi 前截断（≤10000 chars）并 redact（src.tools.redaction）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from src.reliability.contracts import StepStatus
from src.reliability.gateway import GatewayPolicy, ToolGateway
from src.tools.redaction import redact_payload

logger = logging.getLogger(__name__)

TOOL_RESULT_LIMIT = 10_000
_TRUNCATION_MARKER = "[truncated]"


def build_gateway_policy(allowed_names: frozenset[str]) -> GatewayPolicy:
    """Pi 引擎统一策略（跨任务契约）：manifest 工具全放行、side effects 放行
    （mandate/order gate 在工具内部 fail-closed）、retry 走 gateway 只读恢复。"""
    return GatewayPolicy(
        allowed_tools=allowed_names,
        retry_limit=2,
        fallback_tools={},
        timeout_seconds=1800.0,
        allow_side_effects=True,
    )


class GatewayBridge:
    def __init__(
        self,
        registry: Any,
        *,
        gateway: Any | None = None,
        max_parallel_reads: int = 4,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._gateway: Any = gateway  # 测试注入；生产 = ToolGateway(registry)
        self._read_sem = asyncio.Semaphore(max_parallel_reads)
        self._write_lock = asyncio.Lock()
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, asyncio.Event] = {}
        self._now = now

    def build_policy(self) -> GatewayPolicy:
        names = frozenset(getattr(self._registry, "_tools", {}).keys()) if self._registry is not None else frozenset()
        return build_gateway_policy(names)

    def _gw(self) -> Any:
        if self._gateway is None:
            self._gateway = ToolGateway(self._registry)
        return self._gateway

    def _tool_meta(self, tool_name: str) -> dict[str, Any]:
        tool = self._registry.get(tool_name) if self._registry is not None else None
        if tool is None:
            return {"is_readonly": True, "side_effecting": False}
        return {
            "is_readonly": bool(getattr(tool, "is_readonly", True)),
            "side_effecting": bool(getattr(tool, "side_effecting", True)),
        }

    def _record(self, key: str, payload: dict[str, Any]) -> None:
        self._idempotency[key] = payload

    @staticmethod
    def _encode_step_result(step: Any) -> str:
        """StepResult/dict → 面向模型的 JSON 信封（截断 + redact）。"""
        if hasattr(step, "to_wire"):
            wire = step.to_wire()
        elif isinstance(step, dict):
            wire = step
        else:
            wire = {"status": "error", "error": str(step)}
        text = json.dumps(redact_payload(wire), ensure_ascii=False, default=str)
        if len(text) > TOOL_RESULT_LIMIT:
            text = text[: TOOL_RESULT_LIMIT - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        return text

    async def handle_invoke(self, params: dict[str, Any]) -> dict[str, Any]:
        key = str(params.get("idempotencyKey") or "")
        if key and key in self._idempotency:
            return dict(self._idempotency[key])  # 已完成重复：回放，不执行（Global Constraint）

        tool_name = str(params.get("toolName") or "")
        tool = self._registry.get(tool_name) if self._registry is not None else None
        # 仅在 registry 明确判定未知时短路；registry 缺失（无注册表可判）时放行到
        # gateway——ToolGateway 内部对未注册工具返回 UNKNOWN StepResult（唯一 choke point）。
        if self._registry is not None and tool is None:
            return self._finish(
                key,
                {
                    "content": json.dumps({"status": "error", "error": f"unknown tool: {tool_name}"}),
                    "isError": True,
                    "outcome": "error",
                },
            )

        meta = self._tool_meta(tool_name)
        side_effecting = bool(params.get("side_effecting", meta["side_effecting"]))
        timeout_s = float(params.get("timeout_seconds") or 1800.0)

        async def _execute() -> dict[str, Any]:
            loop = asyncio.get_running_loop()
            gw = self._gw()
            step_id = str(params.get("call_id") or f"pi-{int(self._now() * 1000)}")
            args = dict(params.get("arguments") or {})

            def _sync() -> Any:
                return gw.execute(
                    tool_name,
                    args,
                    step_id=step_id,
                    policy=self.build_policy(),
                    session_id=str(params.get("idempotencyKey", "").split(":")[0] or ""),
                )

            if side_effecting:
                async with self._write_lock:
                    return await asyncio.wait_for(loop.run_in_executor(None, _sync), timeout=timeout_s)
            async with self._read_sem:
                return await asyncio.wait_for(loop.run_in_executor(None, _sync), timeout=timeout_s)

        try:
            step = await _execute()
            status_ok = getattr(step, "status", None)
            if isinstance(step, dict):
                status_ok = step.get("status")
            is_error = status_ok is not None and status_ok != StepStatus.SUCCESS and status_ok != "success"
            return self._finish(
                key,
                {
                    "content": self._encode_step_result(step),
                    "isError": bool(is_error),
                    "outcome": "error" if is_error else "ok",
                },
            )
        except asyncio.TimeoutError:
            outcome = "outcome_unknown" if side_effecting else "error"
            return self._finish(
                key,
                {
                    "content": json.dumps({"status": "error", "error": "tool_timeout", "outcome": outcome}),
                    "isError": True,
                    "outcome": outcome,
                },
            )
        except asyncio.CancelledError:
            # 用户取消（design §Idempotency）：写类取消 = 结果未知
            outcome = "outcome_unknown" if side_effecting else "error"
            return self._finish(
                key,
                {
                    "content": json.dumps({"status": "error", "error": "tool_cancelled", "outcome": outcome}),
                    "isError": True,
                    "outcome": outcome,
                },
            )
        except Exception as exc:  # noqa: BLE001 — 连接/进程层失败统一转 envelope
            logger.warning("tool %s invoke failed: %s", tool_name, exc)
            outcome = "outcome_unknown" if side_effecting else "error"
            return self._finish(
                key,
                {
                    "content": json.dumps({"status": "error", "error": str(exc), "outcome": outcome}),
                    "isError": True,
                    "outcome": outcome,
                },
            )

    def _finish(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        if key:
            self._record(key, payload)
        return payload

    def handle_cancel(self, call_id: str) -> None:
        ev = self._pending.get(call_id)
        if ev is not None:
            ev.set()
