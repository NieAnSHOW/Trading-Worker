"""懒迁移单测：原子性、失败不动旧文件、marker 语义、CLI check。"""

import asyncio
import json

import pytest

from src.pi_sidecar import migration
from src.pi_sidecar.client import SidecarError
from src.pi_sidecar.migration import (
    _read_pi_header_id_compat,
    is_migrated,
    migrate_session_if_needed,
    old_session_exists,
)
from src.session.models import Message, Session
from src.session.store import SessionStore


class FakeStore(SessionStore):
    pass


class FakeClient:
    def __init__(self, *, fail_import=False):
        self.ops: list[tuple[str, dict]] = []
        self.fail_import = fail_import

    async def request(self, op, params, *, timeout=30.0):
        self.ops.append((op, params))
        if op == "new_session":
            if self.fail_import:
                raise SidecarError("internal", "sdk exploded")
            return {"session_file": "/tmp/pi/x.jsonl"}
        if op == "import_messages":
            return {"imported": len(params["messages"])}
        if op == "open_session":
            raise SidecarError("not_found", "missing")
        raise SidecarError("unknown_op", op)


class FakeService:
    def __init__(self, store):
        self.store = store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    marker_dir = tmp_path / "markers"
    monkeypatch.setattr(migration, "MIGRATION_MARKER_DIR", marker_dir)
    store = SessionStore(base_dir=tmp_path / "sessions")
    return store, marker_dir


def _seed_old(store, session_id="a1b2c3d4e5f6"):
    s = store.create_session(Session(title="old"))  # M5：普通赋值
    # 用固定 id 的 session
    s.session_id = session_id
    store.update_session(s)
    store._session_dir(session_id).mkdir(parents=True, exist_ok=True)
    for i, (role, content) in enumerate([("user", "hello"), ("assistant", "hi"), ("user", "buy?")]):
        store.append_message(
            Message(session_id=session_id, role=role, content=content, created_at=f"2026-08-0{i + 1}T00:00:00")
        )
    return s


def test_old_session_exists_and_migrated_false(env):
    store, _ = env
    _seed_old(store)
    assert old_session_exists(store, "a1b2c3d4e5f6") is True
    assert is_migrated("a1b2c3d4e5f6") is False


def test_migrate_success_writes_marker_and_imports_chronologically(env):
    store, marker_dir = env
    _seed_old(store)
    client = FakeClient()
    ok = asyncio.run(migrate_session_if_needed(FakeService(store), client, "a1b2c3d4e5f6"))
    assert ok is True
    assert is_migrated("a1b2c3d4e5f6")
    assert (marker_dir / "a1b2c3d4e5f6.json").exists()
    import_op = [p for op, p in client.ops if op == "import_messages"][0]
    assert [m["role"] for m in import_op["messages"]] == ["user", "assistant", "user"]  # 时间序
    assert [m["content"] for m in import_op["messages"]] == ["hello", "hi", "buy?"]
    # 第二次调用幂等：不再迁移
    client2 = FakeClient()
    assert asyncio.run(migrate_session_if_needed(FakeService(store), client2, "a1b2c3d4e5f6")) is False
    assert client2.ops == []


def test_migrate_failure_leaves_no_marker_and_no_partial_state(env):
    store, marker_dir = env
    _seed_old(store)
    messages_before = (store._session_dir("a1b2c3d4e5f6") / "messages.jsonl").read_bytes()
    client = FakeClient(fail_import=True)
    ok = asyncio.run(migrate_session_if_needed(FakeService(store), client, "a1b2c3d4e5f6"))
    assert ok is False
    assert list(marker_dir.glob("*")) == []  # 无 marker = 不宣告
    assert (store._session_dir("a1b2c3d4e5f6") / "messages.jsonl").read_bytes() == messages_before  # 旧文件不变


def test_new_session_returns_false_no_marker(env):
    store, marker_dir = env
    client = FakeClient()
    assert asyncio.run(migrate_session_if_needed(FakeService(store), client, "ffffffffffff")) is False
    assert list(marker_dir.glob("*")) == []


def test_check_detects_orphan_marker(env, capsys):
    store, marker_dir = env
    marker_dir.mkdir(parents=True)
    (marker_dir / "deadbeef1234.json").write_text("{}")
    from src.pi_sidecar.migrate_cli import cmd_check

    rc = cmd_check(store)
    assert rc == 2  # marker 存在但 Pi 会话缺失


def test_read_pi_header_id_compat(tmp_path):
    """B2 兼容：扫描前 8KB 找 type==="session" 条目的 id（与 TS readSessionHeaderId 同语义）。"""
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"type": "session", "id": "a1b2c3d4e5f6"}) + "\n")
    assert _read_pi_header_id_compat(str(p)) == "a1b2c3d4e5f6"
    assert _read_pi_header_id_compat(str(tmp_path / "missing.jsonl")) is None
