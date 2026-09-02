"""Pi 事件 → Vibe SSE glossary 归一化（design §Events + Global Constraints 事件契约）。

形状铁律：前端与 sessions_routes 派生逻辑（mandate.proposal/live.action 依赖
tool_result.preview 正则匹配）逐字段依赖这些 payload 形状，不得改名/增删关键字段。
"""

from __future__ import annotations

import re
import time
from typing import Any

from src.tools.redaction import redact_payload, redact_log_text

PREVIEW_CHARS = 200
THINKING_DONE_CHARS = 500
COMPACT_SUMMARY_CHARS = 200

#: sessions_routes 派生 mandate.proposal / live.action 帧所用的同款 relay 正则
#: （agent/src/api/sessions_routes.py 的 `_PROPOSAL_ID_RE` / `_LIVE_ACTION_ID_RE`）。
#: 预览窗口按这些锚点构造，保证 relay id 必在 preview 内；改动必须与
#: sessions_routes 保持同步。
_PROPOSAL_ID_RE = re.compile(r'"proposal_id"\s*:\s*"(mp_[0-9a-f]{32})"')
_LIVE_ACTION_ID_RE = re.compile(r'"audit_id"\s*:\s*"(la_[0-9a-zA-Z]+)"')

#: 锚段上下文余量：audit 段向前 30 字符容纳 `"live_action": {` 前缀（live.action
#: 派生同时要求 '"live_action"' 出现在 preview），向后 40 字符保留记录后续字段；
#: proposal 段向后 20 字符。两锚段加 2 个 '...' 分隔符最坏 197 字符 ≤ PREVIEW_CHARS。
_AUDIT_CTX_BEFORE = 30
_AUDIT_CTX_AFTER = 40
_PROPOSAL_CTX_AFTER = 20


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
    """Redact 全文后构造 ≤PREVIEW_CHARS 预览，保证 relay id 必在其中。

    sessions_routes 用正则从 tool_result.preview 派生 mandate.proposal 与
    live.action 帧：propose 工具 JSON 的 proposal_id（mp_…）位于头部，而
    order-guard 结果末尾嵌入的 live_action 记录中 audit_id（la_…）是记录
    首键、距 JSON 末尾可达数百字符（broker 转发内容与记录其余字段在其后）
    ——固定头尾窗口（legacy result[:200] / head+tail）会让 id 落在两窗之外，
    relay 静默失效。这里按 sessions_routes 同款正则锚定首个 mp_ 与末个
    la_，拼出含全部命中 id 的窗口（'...' 分隔，id 永不截断）；无命中时
    回退头尾窗口。relay id 优先于头部上下文，头部吃剩余预算。
    """
    # redact_log_text 默认 limit=2000 会截尾，导致长结果的 live_action 记录
    # 在窗口化前就丢失；预览需要全文锚定，limit 放宽到文本全长（redaction 对
    # JSON 结构只缩不扩，+2048 仅容 default=str 的少量膨胀）。
    redacted = redact_log_text(text, limit=max(2000, len(text) + 2048))
    if len(redacted) <= PREVIEW_CHARS:
        return redacted

    proposal = _PROPOSAL_ID_RE.search(redacted)
    audits = list(_LIVE_ACTION_ID_RE.finditer(redacted))
    audit = audits[-1] if audits else None

    if proposal is None and audit is None:
        head_len = PREVIEW_CHARS - 3 - 77  # 尾部 77 字符容纳 `"live_action": {"audit_id": "la_…"`
        return redacted[:head_len] + "..." + redacted[-77:]

    core: list[str] = []
    if audit is not None:
        start = max(0, audit.start() - _AUDIT_CTX_BEFORE)
        end = min(len(redacted), audit.end() + _AUDIT_CTX_AFTER)
        core.append(redacted[start:end])
    if proposal is not None:
        end = min(len(redacted), proposal.end() + _PROPOSAL_CTX_AFTER)
        core.append(redacted[proposal.start() : end])

    core_chars = sum(len(seg) for seg in core)
    head_chars = max(0, PREVIEW_CHARS - core_chars - 3 * len(core))
    return "...".join([redacted[:head_chars], *core])


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
