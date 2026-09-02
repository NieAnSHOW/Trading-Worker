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
from src.pi_sidecar.protocol import FrameError

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


#: 单帧导入净荷上限（final-review finding 1）：legacy 会话消息总量可远超协议 1MiB
#: 单帧上限（~600+ 条中文消息即触顶）。分块把每次 import_messages 的净荷控制在
#: 该预算内（900KB < 1MiB，留协议头/消息头余量），append-additive 逐块导入，语义不变。
_MAX_IMPORT_FRAME_BYTES = 900_000


def _frame_messages_chunks(msgs: list[dict]) -> list[list[dict]]:
    """按序列化 UTF-8 字节长度把 msgs 切成 ≤ _MAX_IMPORT_FRAME_BYTES 的子批（时间序保持）。"""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_size = 0
    for m in msgs:
        size = len(json.dumps(m, ensure_ascii=False).encode("utf-8"))
        if cur and cur_size + size > _MAX_IMPORT_FRAME_BYTES:
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(m)
        cur_size += size
    if cur:
        chunks.append(cur)
    return chunks


async def _import_messages_chunked(client: PiSidecarClient, session_id: str, msgs: list[dict]) -> None:
    """append-additive 分块导入；任一块失败向上抛（调用方不写 marker）。"""
    for sub in _frame_messages_chunks(msgs):
        await client.request("import_messages", {"session_id": session_id, "messages": sub})


async def _count_pi_session_messages(client: PiSidecarClient, session_id: str) -> int:
    """翻页统计既有 Pi 会话里的 user/assistant 消息数（重试续传起点）。

    仅用于「open_session 成功但未打 marker + 旧消息在」的补迁移路径——前次迁移可能
    已把旧消息前缀导入该会话；从既有数继续导入，避免重复。新会话（new_session 刚建）
    恒 0，跳过本调用。
    """
    total = 0
    before: str | None = None
    while True:
        r = await client.request("get_messages", {"session_id": session_id, "limit": 500, "before_cursor": before})
        batch = r.get("messages") or []
        total += sum(1 for m in batch if m.get("role") in ("user", "assistant"))
        nxt = r.get("next_cursor")
        if not batch or not nxt:
            return total
        before = nxt


async def migrate_session_if_needed(service: Any, client: PiSidecarClient, session_id: str) -> bool:
    if is_migrated(session_id):
        return False
    if not old_session_exists(service.store, session_id):
        return False  # 新会话：调用方 new_session
    # 1) 只读读旧会话（get_messages 只读打开 messages.jsonl；不写任何旧文件）
    try:
        old_messages = service.store.get_messages(session_id, limit=100_000)
    except Exception as exc:  # noqa: BLE001
        logger.error("migration read failed for %s: %s", session_id, exc)
        return False
    # 2) 时间序 user/assistant 文本；attempt/run 引用仅作 marker metadata，不进模型上下文
    msgs = [
        {"role": m.role, "content": m.content, "timestamp": m.created_at}
        for m in old_messages
        if m.role in ("user", "assistant") and m.content
    ]
    # 3) 打开（或新建，同 ID）Pi 会话 + append-additive 分块导入 + 原子 marker。
    #    final-review finding 1：migrate 自身幂等可重试——先 open_session；若成功（前次
    #    new_session 已建文件但导入失败、无 marker）则从既有消息数续传，绝不重复；not_found
    #    才 new_session。整段失败 → 旧文件不动、无 marker（不宣告），下次 attempt 重试。
    try:
        try:
            await client.request("open_session", {"session_id": session_id}, timeout=30)
            resumed = await _count_pi_session_messages(client, session_id)
        except SidecarError as exc:
            if exc.code != "not_found":
                raise
            await client.request("new_session", {"session_id": session_id}, timeout=30)
            resumed = 0
        if resumed < len(msgs):
            await _import_messages_chunked(client, session_id, msgs[resumed:])
    except (SidecarError, FrameError) as exc:
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
