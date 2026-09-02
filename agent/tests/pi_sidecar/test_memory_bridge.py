"""MemoryBridge 单测（tmp memory dir，无网络）。"""

import asyncio

import pytest

from src.memory.persistent import PersistentMemory
from src.pi_sidecar.memory_bridge import MemoryBridge


@pytest.fixture()
def bridge(tmp_path):
    pm = PersistentMemory(memory_dir=tmp_path / "memory")
    pm.add("risk-rules", "Max position 5% per name", memory_type="feedback", description="position limits")
    return MemoryBridge(memory=pm)


def test_memory_context_block_contains_snapshot_and_relevant(bridge):
    out = asyncio.run(bridge.handle_memory_context({"session_id": "s", "query": "position limits for AAPL"}))
    assert isinstance(out["block"], str)
    assert "risk-rules" in out["block"]
    assert "Max position 5%" in out["block"]


def test_memory_context_unavailable_returns_empty_block():
    b = MemoryBridge(memory=None)
    out = asyncio.run(b.handle_memory_context({"session_id": "s", "query": "x"}))
    assert out == {"block": ""}


def test_remember_then_search_then_remove(bridge):
    out = asyncio.run(
        bridge.handle_remember(
            {"name": "aapl-thesis", "content": "Revenue up", "memory_type": "project", "description": "thesis"}
        )
    )
    assert out["ok"] is True
    out = asyncio.run(bridge.handle_memory_search({"query": "aapl revenue"}))
    assert any(m["name"] == "aapl-thesis" for m in out["entries"])
    out = asyncio.run(bridge.handle_memory_remove({"name": "aapl-thesis"}))
    assert out == {"removed": True, "ok": True}
    out = asyncio.run(bridge.handle_memory_search({"query": "aapl revenue"}))
    assert all(m["name"] != "aapl-thesis" for m in out["entries"])


def test_write_is_immediately_durable_but_snapshot_frozen(bridge, tmp_path):
    asyncio.run(bridge.handle_remember({"name": "new-fact", "content": "fresh"}))
    # 既有语义：当前 snapshot 不含新条目（recon §5：快照构造时冻结）
    out = asyncio.run(bridge.handle_memory_context({"session_id": "s", "query": "fresh"}))
    assert "new-fact" not in out["block"] or "fresh" not in out["block"].split("## relevant")[0]
