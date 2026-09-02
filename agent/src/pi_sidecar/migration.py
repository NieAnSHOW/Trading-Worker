"""旧 SessionStore 会话懒迁移到 Pi（Task 13 落地完整实现；本文件为 Task 12 的最小导入 seam）。

Task 12 的 pi 引擎执行路径（SessionService）引用 `is_migrated` 与
`migrate_session_if_needed`。在 Task 13 之前提供恒 False 的最小实现，保证 cutover
流程可运行且不宣告任何会话已迁移：open_session not_found 且旧 store 有该会话时
`migrate_session_if_needed` 返回 False → `new_session` 新建（旧数据只读，绝不部分
迁移）。Task 13 将替换为真实实现。
"""

from __future__ import annotations


def is_migrated(session_id: str) -> bool:
    """该会话是否已完成到 Pi 的迁移（Task 13 语义）。最小 seam：恒 False。"""
    return False


async def migrate_session_if_needed(service, client, session_id: str) -> bool:
    """懒迁移旧 store 会话到 Pi；成功返回 True。最小 seam：无迁移能力，恒 False。"""
    return False
