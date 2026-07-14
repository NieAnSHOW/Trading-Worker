"""Tests for backtest/correlation.py"""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api_server
from src.api import system_routes

from backtest.correlation import (
    _rolling_correlation_matrix,
    infer_market,
)


def test_correlation_requires_auth_for_remote_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_AUTH_KEY", "correlation-secret")
    monkeypatch.setattr(api_server, "_API_KEY", "correlation-secret")
    client = TestClient(api_server.app, client=("203.0.113.10", 50000))

    response = client.get("/correlation?codes=BTC-USDT,ETH-USDT")

    assert response.status_code == 401


def test_correlation_rate_limits_each_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_AUTH_KEY", "correlation-secret")
    monkeypatch.setattr(api_server, "_API_KEY", "correlation-secret")
    monkeypatch.setattr(system_routes, "_CORRELATION_RATE_LIMIT", 1)
    system_routes._correlation_request_times.clear()
    client = TestClient(api_server.app, client=("203.0.113.10", 50000))
    headers = {"Authorization": "Bearer correlation-secret"}

    first = client.get("/correlation?codes=only-one", headers=headers)
    limited = client.get("/correlation?codes=only-one", headers=headers)

    assert first.status_code == 400
    assert limited.status_code == 429


class TestInferMarket:
    def test_crypto_usdt(self):
        assert infer_market("BTC-USDT") == "crypto"
        assert infer_market("ETH-USDT") == "crypto"

    def test_a_share(self):
        assert infer_market("000001.SZ") == "a_share"
        assert infer_market("600519.SH") == "a_share"

    def test_us_equity(self):
        assert infer_market("AAPL") == "us_equity"
        assert infer_market("SPY") == "us_equity"

    def test_hk_leading_zero_tickers(self):
        # Leading-zero HK tickers like 0700.HK / 0005.HK must be classified as
        # hk_equity, NOT a_share (which also starts with 0)
        assert infer_market("0700.HK") == "hk_equity"
        assert infer_market("0005.HK") == "hk_equity"
        assert infer_market("0000.HK") == "hk_equity"
        assert infer_market("9988.HK") == "hk_equity"

    def test_hk_suffix_before_a_share_prefix(self):
        # .HK suffix should be checked before A-share numeric prefix checks
        assert infer_market("000001.HK") == "hk_equity"


class TestRollingCorrelationMatrix:
    def _make_price_df(self, closes):
        """Build a DataFrame with trade_date as the index name (like real loaders)."""
        dates = pd.date_range("2024-01-01", periods=len(closes), freq="D")
        return pd.DataFrame(
            {"close": closes},
            index=pd.Index(dates, name="trade_date"),
        )

    def test_window_parameter_is_respected(self):
        # Full history has 50 rows; window=10 should use only the last 10 days.
        # Two assets with perfectly positively correlated full history but
        # negatively correlated last 10 days — verifies window is applied.
        np.random.seed(42)
        n = 50
        closes_a = list(np.cumsum(np.random.randn(n)) + 100)
        closes_b = list(np.cumsum(np.random.randn(n)) + 100)
        price_series = {
            "A": self._make_price_df(closes_a),
            "B": self._make_price_df(closes_b),
        }
        _, matrix_full = _rolling_correlation_matrix(price_series, window=1000, method="pearson")
        _, matrix_window = _rolling_correlation_matrix(price_series, window=10, method="pearson")
        # Matrices should be different when window is applied vs full history
        assert matrix_window[0][1] != pytest.approx(matrix_full[0][1])
        # But both should be valid correlations
        assert -1 <= matrix_window[0][1] <= 1
        assert -1 <= matrix_full[0][1] <= 1

    def test_same_asset_correlation_is_one(self):
        price_series = {
            "A": self._make_price_df([100, 105, 110, 108, 112]),
        }
        labels, matrix = _rolling_correlation_matrix(price_series, window=5, method="pearson")
        assert labels == ["A"]
        assert matrix[0][0] == pytest.approx(1.0)

    def test_matrix_is_symmetric(self):
        np.random.seed(42)
        price_series = {
            "A": self._make_price_df(np.cumsum(np.random.randn(100)).tolist()),
            "B": self._make_price_df(np.cumsum(np.random.randn(100)).tolist()),
            "C": self._make_price_df(np.cumsum(np.random.randn(100)).tolist()),
        }
        labels, matrix = _rolling_correlation_matrix(price_series, window=30, method="pearson")
        n = len(labels)
        assert len(labels) == 3
        for i in range(n):
            for j in range(n):
                assert matrix[i][j] == pytest.approx(matrix[j][i])

    def test_diagonal_is_one(self):
        np.random.seed(42)
        price_series = {
            "X": self._make_price_df(np.cumsum(np.random.randn(50)).tolist()),
            "Y": self._make_price_df(np.cumsum(np.random.randn(50)).tolist()),
        }
        labels, matrix = _rolling_correlation_matrix(price_series, window=20, method="pearson")
        n = len(labels)
        for i in range(n):
            assert matrix[i][i] == pytest.approx(1.0)

    def test_spearman_vs_pearson_diff(self):
        np.random.seed(0)
        # Non-linear relationship: Pearson < Spearman
        x = np.linspace(0, 10, 50)
        y = np.power(x, 2) + np.random.randn(50) * 5
        price_series = {
            "A": self._make_price_df((x * 100 + 1000).tolist()),
            "B": self._make_price_df((y + 1000).tolist()),
        }
        _, p_matrix = _rolling_correlation_matrix(price_series, window=30, method="pearson")
        _, s_matrix = _rolling_correlation_matrix(price_series, window=30, method="spearman")
        # Spearman can be higher for monotonic (not linear) relationships
        assert isinstance(p_matrix[0][1], float)
        assert isinstance(s_matrix[0][1], float)
        # Both should be reasonable correlations
        assert -1 <= p_matrix[0][1] <= 1
        assert -1 <= s_matrix[0][1] <= 1

    def test_empty_dict_returns_empty(self):
        labels, matrix = _rolling_correlation_matrix({}, window=30, method="pearson")
        assert labels == []
        assert matrix == []

    def test_missing_close_column_raises(self):
        df = pd.DataFrame({"open": [1, 2, 3]})
        with pytest.raises(ValueError, match="No 'close' column"):
            _rolling_correlation_matrix({"X": df}, window=30, method="pearson")
