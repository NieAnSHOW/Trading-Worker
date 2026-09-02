"""Pi 事件 → Vibe SSE glossary 归一化（design §Events + Global Constraints 事件契约）。

形状铁律：前端与 sessions_routes 派生逻辑（mandate.proposal/live.action 依赖
tool_result.preview 正则匹配）逐字段依赖这些 payload 形状，不得改名/增删关键字段。
"""

from __future__ import annotations

import time
from typing import Any

from src.tools.redaction import redact_payload, redact_log_text

PREVIEW_CHARS = 200
THINKING_DONE_CHARS = 500
COMPACT_SUMMARY_CHARS = 200


class NormState:
    """每 attempt 一个。"""

    def __init__(self) -> None:
        self.iter: int = 0
        self.reasoning_chars: int = 0
        self.tool_started: dict[str, float] = {}
        self.provider: str = ""
        self.model: str = ""


def _redact_args(args: Any) -> dict[str, str]:
    if not isinstance(args, dict):
        return {}
    safe = redact_payload(args)
    return {k: str(v)[:200] for k, v in safe.items()}


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _thinking_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(b.get("thinking", "") for b in content if isinstance(b, dict) and b.get("type") == "thinking")
    return ""


def _result_text(payload: dict) -> str:
    result = payload.get("result")
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(content, str):
            return content
    return ""


def _result_preview(text: str) -> str:
    """Redact 全文后保留 ≤PREVIEW_CHARS 的头尾窗口。

    sessions_routes 用正则从 tool_result.preview 派生 mandate.proposal 与
    live.action 帧：propose 工具 JSON 的 proposal_id 位于头部，order-guard
    结果末尾嵌入的 live_action 记录（audit_id）位于尾部——纯头部截断（legacy
    result[:200]）会丢尾部标识符，故头尾各保留一段。
    """
    redacted = redact_log_text(text)
    if len(redacted) <= PREVIEW_CHARS:
        return redacted
    head_len = PREVIEW_CHARS - 3 - 77  # 尾部 77 字符容纳 `"live_action": {"audit_id": "la_…"`
    return redacted[:head_len] + "..." + redacted[-77:]


def normalize_event(pi_type: str, payload: dict, *, state: NormState) -> list[tuple[str, dict]]:
    if pi_type == "message_update":
        ame = payload.get("assistantMessageEvent") or {}
        kind = ame.get("type")
        if kind == "text_delta":
            return [("text_delta", {"delta": str(ame.get("delta") or ""), "iter": state.iter})]
        if kind == "thinking_delta":
            state.reasoning_chars += len(str(ame.get("delta") or ""))
            return [("reasoning_delta", {"iter": state.iter, "chars": state.reasoning_chars})]
        return []

    if pi_type == "message_end":
        out: list[tuple[str, dict]] = []
        msg = payload.get("message") or {}
        if msg.get("role") != "assistant":
            return []
        state.provider = str(msg.get("provider") or state.provider)
        state.model = str(msg.get("model") or state.model)
        thinking = _thinking_of(msg)
        if thinking:
            out.append(("thinking_done", {"iter": state.iter, "content": thinking[:THINKING_DONE_CHARS]}))
        usage = msg.get("usage")
        if isinstance(usage, dict):
            data = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
            data.update(
                {"iter": state.iter, "provider": state.provider, "model": state.model, "metering_eligible": False}
            )
            out.append(("llm_usage", data))
        return out

    if pi_type == "tool_execution_start":
        call_id = str(payload.get("toolCallId") or "")
        state.tool_started[call_id] = time.monotonic()
        return [
            (
                "tool_call",
                {
                    "tool": str(payload.get("toolName") or ""),
                    "arguments": _redact_args(payload.get("args")),
                    "iter": state.iter,
                    "tool_call_id": call_id,
                },
            )
        ]

    if pi_type == "tool_execution_update":
        partial = payload.get("partial") or {}
        data: dict[str, Any] = {
            "tool": str(payload.get("toolName") or ""),
            "stage": str(partial.get("stage") or "progress"),
            "current": partial.get("current"),
            "total": partial.get("total"),
            "message": str(partial.get("message") or ""),
            "elapsed_s": round(
                time.monotonic() - state.tool_started.get(str(payload.get("toolCallId") or ""), time.monotonic()), 3
            ),
            "ts": time.time(),
        }
        return [("tool_progress", data)]

    if pi_type == "tool_execution_end":
        call_id = str(payload.get("toolCallId") or "")
        started = state.tool_started.pop(call_id, None)
        elapsed_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
        text = _result_text(payload)
        preview = _result_preview(text)
        return [
            (
                "tool_result",
                {
                    "tool": str(payload.get("toolName") or ""),
                    "status": "error" if payload.get("isError") else "ok",
                    "elapsed_ms": elapsed_ms,
                    "preview": preview,
                    "tool_call_id": call_id,
                },
            )
        ]

    if pi_type == "auto_compaction_start":
        return [("compact", {"phase": "start", "tokens_before": payload.get("tokensBefore")})]
    if pi_type == "auto_compaction_end":
        summary = str(payload.get("summary") or "")[:COMPACT_SUMMARY_CHARS]
        data = {"phase": "end", "tokens_before": payload.get("tokensBefore")}
        if summary:
            data["summary"] = summary
        return [("compact", data)]

    if pi_type == "auto_retry_start":
        return [
            (
                "stream_reset",
                {
                    "iter": state.iter,
                    "reason": "provider_stream_retry",
                    "provider": state.provider,
                    "model": state.model,
                },
            )
        ]

    if pi_type in (
        "retry_fallback_applied",
        "retry_fallback_succeeded",
        "model_changed",
        "thinking_level_changed",
        "notice",
    ):
        if pi_type == "model_changed":
            msg = f"model changed to {payload.get('model')}"
        elif pi_type == "thinking_level_changed":
            msg = f"thinking level changed to {payload.get('thinkingLevel')}"
        else:
            msg = str(payload.get("message") or str(payload))
        state.model = str(payload.get("model") or state.model)
        return [("pi.notice", {"kind": pi_type, "message": msg})]

    if pi_type == "agent_start":
        state.iter += 1
        state.reasoning_chars = 0
        return []
    if pi_type in ("agent_end", "turn_start", "turn_end", "message_start"):
        if pi_type == "message_start" and isinstance(payload.get("entryId"), str):
            pass  # entry id 供 tool-bridge 幂等键使用；无 SSE 输出
        return []

    return []  # 未知 Pi 事件不外泄（design：Pi 原始事件流永不外露）
