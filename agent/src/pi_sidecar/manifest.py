"""ToolRegistry → Pi sidecar 工具 manifest（design §Sidecar and RPC Protocol）。"""

from __future__ import annotations

import os
from typing import Any

from src.agent.tools import BaseTool, ToolRegistry

#: 与 agent/src/agent/loop.py 的 TOOL_TIMEOUT_SECONDS 同源（recon §1.1）。
TOOL_TIMEOUT_SECONDS = float(os.getenv("VIBE_TRADING_TOOL_TIMEOUT_SECONDS", "1800"))

#: 与 ToolGateway._SIDE_EFFECTING_NAMES 对齐（recon §3.3）——manifest 侧保持同一强制分类，
#: 避免 sidecar 依据错误的 is_readonly 并行调度写工具。
_FORCED_SIDE_EFFECTING_NAMES = frozenset({"bash"})
_FORCED_SIDE_EFFECTING_PREFIXES = ("trading_",)

_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "required": []}


def build_manifest_tool(tool: BaseTool) -> dict[str, Any]:
    side_effecting = bool(tool.side_effecting)
    if tool.name in _FORCED_SIDE_EFFECTING_NAMES or tool.name.startswith(_FORCED_SIDE_EFFECTING_PREFIXES):
        side_effecting = True
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters or _EMPTY_OBJECT_SCHEMA,
        "is_readonly": bool(tool.is_readonly) and not side_effecting,
        "side_effecting": side_effecting,
        "repeatable": bool(tool.repeatable),
        "timeout_seconds": TOOL_TIMEOUT_SECONDS,
    }


def build_tool_manifest(registry: ToolRegistry) -> list[dict[str, Any]]:
    return [build_manifest_tool(t) for t in registry._tools.values()]  # noqa: SLF001 — 同包内受控访问
