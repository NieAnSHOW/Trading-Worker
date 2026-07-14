"""Security regression tests for alpha-bench pickle cache authentication."""

from __future__ import annotations

import hashlib
import pickle
import stat
from pathlib import Path

import pandas as pd

from src.tools import alpha_bench_tool


def _panel() -> dict[str, pd.DataFrame]:
    return {"close": pd.DataFrame({"AAPL": [100.0, 101.0]})}


def test_rejects_pickle_with_forged_sha256_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    """A matching bare SHA-256 digest must never authorize unpickling."""
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    cache_path = tmp_path / "forged.pkl"
    blob = pickle.dumps(_panel(), protocol=pickle.HIGHEST_PROTOCOL)
    cache_path.write_bytes(blob)
    cache_path.with_suffix(".pkl.sha256").write_text(
        hashlib.sha256(blob).hexdigest(), encoding="utf-8"
    )

    assert alpha_bench_tool._read_pickle_cache(cache_path) is None


def test_generated_cache_round_trips_with_authenticated_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    cache_path = tmp_path / "panel.pkl"
    panel = _panel()

    alpha_bench_tool._write_pickle_cache(tmp_path, cache_path, panel)

    cached = alpha_bench_tool._read_pickle_cache(cache_path)
    assert cached is not None
    pd.testing.assert_frame_equal(cached["close"], panel["close"])


def test_api_auth_key_is_used_when_configured(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_KEY", "configured-cache-secret")

    assert alpha_bench_tool._cache_hmac_key(tmp_path) == b"configured-cache-secret"
    assert not (tmp_path / ".hmac_key").exists()


def test_fallback_hmac_key_is_owner_readable_only(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("API_AUTH_KEY", raising=False)

    key = alpha_bench_tool._cache_hmac_key(tmp_path)
    key_path = tmp_path / ".hmac_key"

    assert key_path.read_bytes() == key
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
