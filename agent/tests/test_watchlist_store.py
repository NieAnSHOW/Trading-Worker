"""Tests for the watchlist SQLite store (见 Design Doc §3.1 / §8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.watchlist.store import WatchlistItemExists, WatchlistStore


def _store(tmp_path: Path) -> WatchlistStore:
    return WatchlistStore(tmp_path / "sessions.db")


def test_init_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store2 = WatchlistStore(tmp_path / "sessions.db")  # 二次打开不报错
    assert store.list() == store2.list() == []


def test_add_and_list_sorted_by_sort_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add("600519.SH", "a_share", "贵州茅台")
    store.add("000001.SZ", "a_share", "平安银行")
    rows = store.list()
    assert [r.symbol for r in rows] == ["600519.SH", "000001.SZ"]
    assert rows[0].sort_order == 0 and rows[1].sort_order == 1


def test_add_duplicate_raises_friendly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add("600519.SH", "a_share", "贵州茅台")
    with pytest.raises(WatchlistItemExists):
        store.add("600519.SH", "a_share", "贵州茅台")


def test_remove(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add("600519.SH", "a_share", "贵州茅台")
    assert store.remove("600519.SH") is True
    assert store.list() == []
    assert store.remove("600519.SH") is False  # 已不存在


def test_reorder(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for s in ("600519.SH", "000001.SZ", "300750.SZ"):
        store.add(s, "a_share", s)
    store.reorder(["000001.SZ", "300750.SZ", "600519.SH"])
    assert [r.symbol for r in store.list()] == ["000001.SZ", "300750.SZ", "600519.SH"]
