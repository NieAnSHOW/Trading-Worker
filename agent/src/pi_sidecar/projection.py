"""最终 result 投影（design §Events：保留 AgentLoop result 契约 + Pi metadata）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.session.models import Message


def build_attempt_result(
    *,
    status: str,
    content: str,
    run_dir: str,
    react_trace: list[dict],
    iterations: int,
    metrics: dict[str, Any] | None,
    reliability: dict[str, Any] | None = None,
    pi_meta: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "run_dir": run_dir,
        "run_id": Path(run_dir).name,
        "content": content,
        "react_trace": react_trace,
        "iterations": iterations,
        "max_iterations": 50,  # 与 SessionService._run_with_agent 的 AgentLoop(max_iterations=50) 一致
    }
    if metrics is not None:
        result["metrics"] = metrics
    if reliability is not None:
        result["reliability"] = reliability
    if pi_meta is not None:
        result["pi"] = pi_meta
    if reason is not None:
        result["reason"] = reason
    return result


def pi_messages_to_store_messages(session_id: str, entries: list[dict]) -> list[Message]:
    """sidecar get_messages → Message 内存投影（不落盘；design §Session Persistence）。"""
    out: list[Message] = []
    for e in entries:
        out.append(
            Message(
                session_id=session_id,
                role=str(e.get("role") or "user"),
                content=str(e.get("content") or ""),
                created_at=str(e.get("timestamp") or datetime.utcnow().isoformat()),
                metadata={"pi_entry_id": e.get("entry_id")},
            )
        )
    return out
