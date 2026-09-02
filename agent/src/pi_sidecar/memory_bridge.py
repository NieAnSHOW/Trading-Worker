"""PersistentMemory ↔ sidecar host op 桥（design §Long-Term Memory）。"""

from __future__ import annotations

from typing import Any


class MemoryBridge:
    def __init__(self, memory: Any | None = None) -> None:
        # memory=None → 服务不可用：block 返回空串，sidecar 发 degraded 诊断继续
        self._memory = memory

    async def handle_memory_context(self, params: dict) -> dict:
        if self._memory is None:
            return {"block": ""}
        snapshot = getattr(self._memory, "snapshot", "") or ""
        query = str(params.get("query") or "")
        lines: list[str] = []
        if snapshot:
            lines.append(snapshot)
        relevant = self._memory.find_relevant(query, max_results=5) if query else []
        if relevant:
            lines.append("## relevant memories")
            for entry in relevant[:5]:
                body = (getattr(entry, "body", "") or "")[:500]
                lines.append(f"- {entry.title}: {getattr(entry, 'description', '')}\n  {body}")
        return {"block": "\n".join(lines)}

    async def handle_remember(self, params: dict) -> dict:
        if self._memory is None:
            return {"ok": False, "error": "memory unavailable"}
        self._memory.add(
            str(params.get("name") or ""),
            str(params.get("content") or ""),
            memory_type=str(params.get("memory_type") or "project"),
            description=str(params.get("description") or ""),
        )
        return {"ok": True}

    async def handle_memory_search(self, params: dict) -> dict:
        if self._memory is None:
            return {"ok": False, "entries": []}
        entries = self._memory.find_relevant(str(params.get("query") or ""), max_results=5)
        return {
            "ok": True,
            "entries": [
                {
                    "name": e.title,
                    "description": getattr(e, "description", ""),
                    "body": (getattr(e, "body", "") or "")[:500],
                }
                for e in entries
            ],
        }

    async def handle_memory_remove(self, params: dict) -> dict:
        if self._memory is None:
            return {"removed": False, "ok": False}
        removed = self._memory.remove(str(params.get("name") or ""))
        return {"removed": bool(removed), "ok": True}
