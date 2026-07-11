"""Tests for batch quotes + sparkline (见 Design Doc §3.3 / §7 / §8).

本任务覆盖 get_quotes_batch 的批量拉取/字段映射 + get_sparkline_batch 的日 K
收盘截取；TTL/路由用例在 Task 4/6 扩展。
"""

from __future__ import annotations

from backtest.loaders import eastmoney_client


def _ulist_payload(rows: list[dict]) -> dict:
    return {"data": {"diff": rows}}


def test_get_quotes_batch_maps_fields_and_joins_secids(monkeypatch) -> None:
    payload = _ulist_payload([{
        "f12": "600519", "f14": "贵州茅台",
        "f2": 1680.0, "f3": 1.23, "f4": 20.4,
        "f5": 12345, "f6": 207000000, "f8": 0.98,
    }])
    monkeypatch.setattr(eastmoney_client, "get_json", lambda url, *, params: payload)

    rows = eastmoney_client.get_quotes_batch(["600519.SH"])
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "600519.SH"
    assert r["code"] == "600519" and r["name"] == "贵州茅台"
    assert r["price"] == 1680.0 and r["change_pct"] == 1.23
    assert r["volume"] == 12345 and r["amount"] == 207000000
    assert r["turnover_rate"] == 0.98


def test_get_quotes_batch_skips_unresolvable_and_dash_sentinels(monkeypatch) -> None:
    # f2="-" 的行 price→None；secid 解析失败的 symbol 不在返回中
    payload = _ulist_payload([{"f12": "000001", "f14": "平安银行", "f2": "-"}])
    monkeypatch.setattr(eastmoney_client, "get_json", lambda url, *, params: payload)

    rows = eastmoney_client.get_quotes_batch(["BADSYMBOL.XX", "000001.SZ"])
    assert [r["symbol"] for r in rows] == ["000001.SZ"]
    assert rows[0]["price"] is None


def test_get_sparkline_batch_takes_last_n_closes(monkeypatch) -> None:
    # fetch_kline 返回升序 OHLCV dict；get_sparkline_batch 取近 days 个 close
    klines = [
        {"trade_date": f"2026-07-0{i}", "open": 1.0, "close": float(i), "high": 2.0, "low": 0.5, "volume": 1.0, "amount": 1.0}
        for i in range(1, 11)  # 10 个交易日,close=1..10
    ]

    def fake_fetch_kline(secid, *, klt, **_kw):
        assert klt == eastmoney_client.KLT_BY_INTERVAL["1D"]  # 日 K
        return klines

    monkeypatch.setattr(eastmoney_client, "fetch_kline", fake_fetch_kline)
    # 清缓存避免跨用例污染
    monkeypatch.setattr(eastmoney_client, "_SPARKLINE_CACHE", {})

    out = eastmoney_client.get_sparkline_batch(["600519.SH"], days=7)
    assert list(out.keys()) == ["600519.SH"]
    assert out["600519.SH"] == [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]  # 近 7 日


def test_get_sparkline_batch_skips_unresolvable(monkeypatch) -> None:
    monkeypatch.setattr(eastmoney_client, "fetch_kline", lambda *a, **kw: [])
    monkeypatch.setattr(eastmoney_client, "_SPARKLINE_CACHE", {})
    out = eastmoney_client.get_sparkline_batch(["BADSYMBOL.XX"], days=7)
    assert out == {}
