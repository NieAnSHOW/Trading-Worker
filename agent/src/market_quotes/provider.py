"""Market-quotes capability: Quote model + provider abstraction + TTL cache.

见 Design Doc §3.3。A 股实现走 backtest.loaders.eastmoney_client.get_quotes_batch
（qt/ulist/get）。仅基类 + 单实现,不预造 registry（YAGNI, §6）。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Callable

from backtest.loaders.eastmoney_client import get_quotes_batch


@dataclass
class Quote:
    symbol: str
    name: str
    price: float | None = None
    change_pct: float | None = None
    change: float | None = None
    volume: float | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    missing: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class QuoteProvider(ABC):
    market: str

    @abstractmethod
    def fetch_quotes(self, symbols: list[str]) -> list[Quote]:
        ...


class EastmoneyQuoteProvider(QuoteProvider):
    market = "a_share"

    def fetch_quotes(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            return []
        rows = get_quotes_batch(symbols)
        by_symbol = {r["symbol"]: r for r in rows}
        out: list[Quote] = []
        for symbol in symbols:
            row = by_symbol.get(symbol)
            if row is None:
                out.append(Quote(symbol=symbol, name="", missing=True))
                continue
            out.append(Quote(
                symbol=symbol,
                name=str(row.get("name") or ""),
                price=row.get("price"),
                change_pct=row.get("change_pct"),
                change=row.get("change"),
                volume=row.get("volume"),
                amount=row.get("amount"),
                turnover_rate=row.get("turnover_rate"),
            ))
        return out


class QuoteCache:
    """Process-local TTL cache keyed by (sorted symbols, market). TTL < 10s polling."""

    def __init__(self, ttl_seconds: float = 8.0) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple[tuple[str, ...], str], tuple[list[Quote], float]] = {}

    def get(
        self,
        symbols: list[str],
        *,
        market: str,
        fetch: Callable[[list[str]], list[Quote]],
    ) -> list[Quote]:
        if not symbols:
            return []
        key = (tuple(sorted(symbols)), market)
        now = time.monotonic()
        hit = self._store.get(key)
        if hit and now < hit[1]:
            return hit[0]
        quotes = fetch(symbols)
        self._store[key] = (quotes, now + self._ttl)
        return quotes
