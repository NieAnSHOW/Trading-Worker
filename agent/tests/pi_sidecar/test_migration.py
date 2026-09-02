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


def _seed_large_old(store, session_id="a1b2c3d4e5f6", n=400):
    """n 条中文消息，单条 ~3KB，总量远超 1MiB 单帧上限（final-review finding 1 复现前提）。"""
    s = store.create_session(Session(title="old"))
    s.session_id = session_id
    store.update_session(s)
    store._session_dir(session_id).mkdir(parents=True, exist_ok=True)
    body = "行情快讯，投资者请注意风险。" * 300  # ~ 3000 字节
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        store.append_message(
            Message(
                session_id=session_id,
                role=role,
                content=f"{body} seq={i}",
                created_at=f"2026-08-01T{i // 60:02d}:{i % 60:02d}:00",
            )
        )
    return s


def test_migrate_chunks_over_1MiB_session_into_small_frames(env):
    """final-review finding 1：>1MiB legacy 会话迁移必须分块，任一块帧 ≤ 900KB，语义不变。"""
    store, marker_dir = env
    _seed_large_old(store)

    class ChunkClient(FakeClient):
        async def request(self, op, params, *, timeout=30.0):
            self.ops.append((op, params))
            if op == "new_session":
                return {"session_file": "/tmp/pi/x.jsonl"}
            if op == "open_session":
                raise SidecarError("not_found", "missing")
            if op == "import_messages":
                return {"imported": len(params["messages"])}
            raise SidecarError("unknown_op", op)

    client = ChunkClient()
    ok = asyncio.run(migrate_session_if_needed(FakeService(store), client, "a1b2c3d4e5f6"))
    assert ok is True
    assert is_migrated("a1b2c3d4e5f6")
    import_ops = [p for op, p in client.ops if op == "import_messages"]
    assert len(import_ops) >= 2  # 总 >1MiB → 必然分块
    total = 0
    for p in import_ops:
        # 协议实际编码 ensure_ascii=False（protocol.encode_frame）；度量真实线上帧字节
        frame_bytes = len(
            json.dumps({"session_id": "a1b2c3d4e5f6", "messages": p["messages"]}, ensure_ascii=False).encode("utf-8")
        )
        assert frame_bytes <= 900_000  # 单帧净荷上限
        total += len(p["messages"])
    assert total == 400  # 无丢失、无重复


def test_migrate_resumes_half_created_pi_session_without_duplicating(env):
    """final-review finding 1：open_session 成功但无 marker + 旧消息在（前次导入中断）→
    续传补齐（只导既有数之后的消息），绝不重复，最终打 marker。"""
    store, marker_dir = env
    _seed_old(store)  # user/assistant/user = 3 条旧消息

    class ExistingClient(FakeClient):
        def __init__(self, existing_pi_count):
            super().__init__()
            self.existing_pi_count = existing_pi_count

        async def request(self, op, params, *, timeout=30.0):
            self.ops.append((op, params))
            if op == "open_session":
                return {"session_file": "/tmp/pi/x.jsonl"}
            if op == "get_messages":
                return {"messages": [{"role": "user"} for _ in range(self.existing_pi_count)], "next_cursor": None}
            if op == "import_messages":
                return {"imported": len(params["messages"])}
            if op == "new_session":
                raise SidecarError("internal", "should not recreate existing session")
            raise SidecarError("unknown_op", op)

    client = ExistingClient(existing_pi_count=2)  # 前 2 条已导入
    ok = asyncio.run(migrate_session_if_needed(FakeService(store), client, "a1b2c3d4e5f6"))
    assert ok is True
    assert is_migrated("a1b2c3d4e5f6")
    import_ops = [p for op, p in client.ops if op == "import_messages"]
    assert len(import_ops) == 1
    # 只续传既有 2 条之后的 1 条（buy?）——不重复前缀
    assert [m["content"] for m in import_ops[0]["messages"]] == ["buy?"]
    assert any(op == "open_session" for op, _ in client.ops)
    assert all(op != "new_session" for op, _ in client.ops)
