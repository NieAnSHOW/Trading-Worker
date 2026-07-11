"""自选股 SQLite 存储,与 ``GoalStore`` 共用 ``sessions.db`` 但独立表名。"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar

_DEFAULT_DB_PATH = Path.home() / ".vibe-trading" / "sessions.db"
_DB_PATH_ENV = "VIBE_TRADING_WATCHLIST_DB_PATH"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_db_path() -> Path:
    """返回自选股数据库路径(env 优先,与 goal store 同形)。"""
    raw_path = os.getenv(_DB_PATH_ENV, "").strip()
    if raw_path:
        return Path(raw_path).expanduser()
    return _DEFAULT_DB_PATH


class WatchlistItemExists(Exception):
    """当加入的 (symbol, market) 已存在于自选股中时抛出。"""


@dataclass
class WatchlistRow:
    """一条自选股记录。"""

    symbol: str
    market: str
    name: str
    sort_order: int
    created_at: str


F = TypeVar("F", bound=Callable)


def _synchronized(method: F) -> F:
    """串行化对共享 SQLite 连接的访问。"""

    @wraps(method)
    def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class WatchlistStore:
    """SQLite 自选股存储(仅读写自己的 ``watchlist`` 表,不触碰 ``goals*``)。"""

    def __init__(self, db_path: Path | None = None) -> None:
        """初始化自选股存储。

        Args:
            db_path: SQLite 数据库路径。省略时可用
                ``VIBE_TRADING_WATCHLIST_DB_PATH`` 覆盖默认路径。
        """
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        """幂等建表,不修改 ``user_version``(与 goal store 共库互不覆盖)。"""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, market)
                );

                CREATE INDEX IF NOT EXISTS idx_watchlist_order
                    ON watchlist(market, sort_order);
                """
            )
            self._conn.commit()

    @contextmanager
    def _write_transaction(self):
        """开启 immediate 写事务,保证跨连接安全。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @_synchronized
    def list(self, market: str = "a_share") -> list[WatchlistRow]:
        """按 ``sort_order`` 升序返回某市场的自选股。"""
        rows = self._conn.execute(
            "SELECT * FROM watchlist WHERE market = ? ORDER BY sort_order",
            (market,),
        ).fetchall()
        return [self._row_from_sql(row) for row in rows]

    @_synchronized
    def add(self, symbol: str, market: str, name: str) -> WatchlistRow:
        """加入一条自选股,追加到末尾;重复 (symbol, market) 抛 ``WatchlistItemExists``。"""
        now = _now_iso()
        with self._write_transaction():
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM watchlist WHERE market = ?",
                (market,),
            ).fetchone()
            sort_order = int(row[0])
            try:
                self._conn.execute(
                    """
                    INSERT INTO watchlist (symbol, market, name, sort_order, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (symbol, market, name, sort_order, now),
                )
            except sqlite3.IntegrityError as exc:
                raise WatchlistItemExists(
                    f"watchlist already contains {symbol} in market {market}"
                ) from exc
        return WatchlistRow(
            symbol=symbol,
            market=market,
            name=name,
            sort_order=sort_order,
            created_at=now,
        )

    @_synchronized
    def remove(self, symbol: str, market: str = "a_share") -> bool:
        """删除一条自选股;返回是否真的删除了行。"""
        with self._write_transaction():
            cursor = self._conn.execute(
                "DELETE FROM watchlist WHERE symbol = ? AND market = ?",
                (symbol, market),
            )
            return cursor.rowcount > 0

    @_synchronized
    def reorder(self, ordered_symbols: list[str], market: str = "a_share") -> None:
        """按给定顺序重排某市场的自选股 ``sort_order``。"""
        with self._write_transaction():
            for index, symbol in enumerate(ordered_symbols):
                self._conn.execute(
                    "UPDATE watchlist SET sort_order = ? WHERE symbol = ? AND market = ?",
                    (index, symbol, market),
                )

    @staticmethod
    def _row_from_sql(row: sqlite3.Row) -> WatchlistRow:
        return WatchlistRow(
            symbol=row["symbol"],
            market=row["market"],
            name=row["name"],
            sort_order=row["sort_order"],
            created_at=row["created_at"],
        )
