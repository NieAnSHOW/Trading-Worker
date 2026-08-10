"""Tests for ResultCache: bounded LRU cache for read-only StepResults.

Covers the brief's Step 1 cases:
  - Normalized keys ignore argument ordering.
  - Settled historical results reused.
  - Short-TTL entries expire.
  - A live/side-effect result is never cached.
  - Bounded size evicts oldest entries.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from src.reliability.cache import ResultCache
from src.reliability.contracts import ErrorCode, StepResult, StepStatus, ToolError


# -- helpers -----------------------------------------------------------------

def _ok(step_id: str = "s1", data: object = "ok") -> StepResult:
    return StepResult(step_id=step_id, status=StepStatus.SUCCESS, data=data)


def _key(tool: str = "get_price", **args: object) -> str:
    return json.dumps({"tool": tool, "args": args})


# -- ordering independence ---------------------------------------------------

def test_normalized_keys_ignore_argument_ordering() -> None:
    cache = ResultCache()
    key_a = '{"tool": "price", "args": {"b": 2, "a": 1}}'
    key_b = '{"args": {"a": 1, "b": 2}, "tool": "price"}'
    res = _ok()
    cache.put(key_a, res, ttl_seconds=60)
    assert cache.get(key_b) is res


def test_deeply_nested_ordering_normalized() -> None:
    cache = ResultCache()
    key_a = '{"tool": "t", "args": {"x": {"z": 3, "y": 2}}}'
    key_b = '{"tool": "t", "args": {"x": {"y": 2, "z": 3}}}'
    res = _ok()
    cache.put(key_a, res, ttl_seconds=60)
    assert cache.get(key_b) is res


# -- reuse -------------------------------------------------------------------

def test_settled_result_reused() -> None:
    cache = ResultCache()
    res = _ok(data={"close": 100})
    cache.put(_key(symbol="AAPL"), res, ttl_seconds=60)
    assert cache.get(_key(symbol="AAPL")) is res


def test_miss_returns_none() -> None:
    cache = ResultCache()
    assert cache.get(_key(symbol="MISS")) is None


# -- TTL expiry --------------------------------------------------------------

def test_short_ttl_expires_with_real_time() -> None:
    cache = ResultCache()
    cache.put(_key(symbol="X"), _ok(), ttl_seconds=0.01)
    time.sleep(0.03)
    assert cache.get(_key(symbol="X")) is None


def test_ttl_expiry_via_injected_now() -> None:
    cache = ResultCache()
    res = _ok()
    cache.put(_key(symbol="Y"), res, ttl_seconds=60)
    # Inject a future wall-clock time beyond TTL.
    future = datetime.now() + timedelta(seconds=61)
    assert cache.get(_key(symbol="Y"), now=future) is None


def test_not_expired_with_injected_now_within_ttl() -> None:
    cache = ResultCache()
    res = _ok()
    cache.put(_key(symbol="Z"), res, ttl_seconds=60)
    near_future = datetime.now() + timedelta(seconds=10)
    assert cache.get(_key(symbol="Z"), now=near_future) is res


# -- never cache side-effecting ----------------------------------------------

def test_unsafe_error_result_not_cached() -> None:
    cache = ResultCache()
    bad = StepResult(
        step_id="s1",
        status=StepStatus.UNSAFE_ERROR,
        error=ToolError(code=ErrorCode.UNSAFE_SIDE_EFFECT, message="no", retryable=False),
    )
    cache.put(_key(tool="get_price"), bad, ttl_seconds=60)
    assert cache.get(_key(tool="get_price")) is None


def test_side_effecting_key_refused() -> None:
    """A key referencing a trading_ tool must not be cached."""
    cache = ResultCache()
    res = _ok()
    cache.put(_key(tool="trading_buy"), res, ttl_seconds=60)
    assert cache.get(_key(tool="trading_buy")) is None


def test_bash_key_refused() -> None:
    cache = ResultCache()
    res = _ok()
    cache.put(_key(tool="bash"), res, ttl_seconds=60)
    assert cache.get(_key(tool="bash")) is None


# -- bounded eviction --------------------------------------------------------

def test_bounded_size_evicts_oldest() -> None:
    cache = ResultCache(max_size=3)
    for i in range(3):
        cache.put(_key(symbol=f"S{i}"), _ok(data=i), ttl_seconds=60)
    # Adding a 4th evicts S0 (oldest).
    cache.put(_key(symbol="S3"), _ok(data=3), ttl_seconds=60)
    assert cache.get(_key(symbol="S0")) is None
    assert cache.get(_key(symbol="S3")) is not None


def test_lru_access_promotes_recency() -> None:
    cache = ResultCache(max_size=2)
    cache.put(_key(symbol="A"), _ok(data="a"), ttl_seconds=60)
    cache.put(_key(symbol="B"), _ok(data="b"), ttl_seconds=60)
    # Access A — promotes it; B is now oldest.
    cache.get(_key(symbol="A"))
    cache.put(_key(symbol="C"), _ok(data="c"), ttl_seconds=60)
    assert cache.get(_key(symbol="A")) is not None  # survived
    assert cache.get(_key(symbol="B")) is None  # evicted


# -- reset -------------------------------------------------------------------

def test_reset_clears_store() -> None:
    cache = ResultCache()
    cache.put(_key(symbol="R"), _ok(), ttl_seconds=60)
    cache.reset()
    assert cache.get(_key(symbol="R")) is None
