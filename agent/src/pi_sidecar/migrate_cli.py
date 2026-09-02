"""Operator 批迁移 + 完整性检查。

用法（仓库根，PYTHONPATH=agent）：
  PYTHONPATH=agent python -m src.pi_sidecar.migrate_cli --session <id>
  PYTHONPATH=agent python -m src.pi_sidecar.migrate_cli --all
  PYTHONPATH=agent python -m src.pi_sidecar.migrate_cli --check
退出码：0 全成功；1 任一迁移失败；2 --check 发现不一致。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from src.pi_sidecar import migration
from src.pi_sidecar.client import PiSidecarClient, SidecarError


def _get_service():
    from src.api.state import _get_session_service  # recon §6.1 单例装配

    return _get_session_service()


def cmd_migrate_one(client: PiSidecarClient, service, session_id: str) -> bool:
    return asyncio.run(migration.migrate_session_if_needed(service, client, session_id))


def cmd_all(client: PiSidecarClient, service) -> int:
    failed = 0
    for s in service.store.list_sessions(limit=1000):
        try:
            if not cmd_migrate_one(client, service, s.session_id):
                continue
        except SidecarError as exc:
            print(f"FAIL {s.session_id}: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def cmd_check(store) -> int:
    """marker 存在但 Pi 会话不可用（索引无文件/header 不符）→ 不一致。"""
    inconsistent = 0
    if not migration.MIGRATION_MARKER_DIR.exists():
        print("no markers; nothing to check")
        return 0
    for marker in migration.MIGRATION_MARKER_DIR.glob("*.json"):
        session_id = marker.stem
        if not resolve_pi_session(session_id):
            print(f"INCONSISTENT {session_id}: marker without usable Pi session")
            inconsistent += 1
    return 2 if inconsistent else 0


def resolve_pi_session(session_id: str) -> str | None:
    """读 sidecar 索引文件（pi-sidecar/src/session-index.ts 写的同一路径，B2 格式）。

    条目形如 {"path": ..., "headerId": ...}（兼容旧纯路径格式）；
    同时校验文件 header id 仍等于记录的 headerId（替换检测，与 TS resolve 同语义）。
    """
    idx = Path.home() / ".vibe-trading" / "pi" / "session-index.json"
    try:
        import json

        mapping = json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = mapping.get(session_id)
    if entry is None:
        return None
    if isinstance(entry, str):  # 旧格式兼容
        path, header_id = entry, session_id
    else:
        path, header_id = str(entry.get("path")), str(entry.get("headerId"))
    from src.pi_sidecar.migration import _read_pi_header_id_compat

    return path if _read_pi_header_id_compat(path) == header_id else None


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m src.pi_sidecar.migrate_cli")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--session", metavar="ID")
    g.add_argument("--all", action="store_true")
    g.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        service = _get_service()
        return cmd_check(service.store)
    client = PiSidecarClient()
    asyncio.run(client.start())
    try:
        service = _get_service()
        if args.session:
            return 0 if cmd_migrate_one(client, service, args.session) else 1
        return cmd_all(client, service)
    finally:
        asyncio.run(client.stop())


if __name__ == "__main__":
    raise SystemExit(main())
