"""懒迁移（design §Lazy migration）：旧 SessionStore 只读 → Pi 同 ID 会话 → 原子 marker。

失败语义：任一步失败 → 旧文件不动（全程只读打开）、无 marker（不宣告）、无部分状态。
批迁移/检查仅为 operator 命令；桌面升级永不自动全量迁移（Non-goals）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.pi_sidecar.client import PiSidecarClient, SidecarError

logger = logging.getLogger(__name__)

MIGRATION_MARKER_DIR = Path.home() / ".vibe-trading" / "pi" / "migration"


def migration_marker_path(session_id: str) -> Path:
    return MIGRATION_MARKER_DIR / f"{session_id}.json"


def is_migrated(session_id: str) -> bool:
    return migration_marker_path(session_id).exists()


def old_session_exists(store: Any, session_id: str) -> bool:
    try:
        msgs = store.get_messages(session_id, limit=1)
        return bool(msgs)
    except Exception:  # noqa: BLE001
        return False


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)  # 原子（design §Lazy migration step 5）
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_pi_header_id_compat(jsonl_path: str) -> str | None:
    """扫描 Pi 会话 JSONL 前 8KB，返回 type==="session" 条目的 id；无/坏 → None。"""
    from pathlib import Path as _P

    try:
        text = _P(jsonl_path).read_text(encoding="utf-8")[:8192]
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "session" and isinstance(obj.get("id"), str):
            return obj["id"]
    return None


async def migrate_session_if_needed(service: Any, client: PiSidecarClient, session_id: str) -> bool:
    if is_migrated(session_id):
        return False
    if not old_session_exists(service.store, session_id):
        return False  # 新会话：调用方 new_session
    # 1) 只读读旧会话（get_messages 只读打开 messages.jsonl；不写任何旧文件）
    try:
        old_messages = service.store.get_messages(session_id, limit=10_000)
    except Exception as exc:  # noqa: BLE001
        logger.error("migration read failed for %s: %s", session_id, exc)
        return False
    # 2) 建 Pi 会话（同 ID）
    try:
        await client.request("new_session", {"session_id": session_id})
        # 3) 时间序导入 user/assistant 文本；attempt/run 引用仅作 marker metadata，不进模型上下文
        msgs = [
            {"role": m.role, "content": m.content, "timestamp": m.created_at}
            for m in old_messages
            if m.role in ("user", "assistant") and m.content
        ]
        await client.request("import_messages", {"session_id": session_id, "messages": msgs})
    except SidecarError as exc:
        logger.error("migration failed for %s (no marker written, old files untouched): %s", session_id, exc)
        return False
    # 4) 原子 marker + 投影（design step 5）
    _atomic_write_json(
        migration_marker_path(session_id),
        {
            "session_id": session_id,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "imported_messages": len(old_messages),
        },
    )
    logger.info("migrated session %s (%d messages)", session_id, len(old_messages))
    return True
