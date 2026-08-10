"""Tests for ProviderHealth: bounded circuit + fallback selection.

Covers the brief's Step 1 cases:
  - Provider failures open a bounded circuit.
  - A healthy candidate is chosen when the primary is unhealthy.
  - All-unhealthy candidates return None.
  - record_success resets the failure counter.
  - reset() clears state.
"""

from __future__ import annotations

from src.reliability.contracts import ErrorCode
from src.reliability.providers import ProviderHealth


# -- healthy default ---------------------------------------------------------

def test_healthy_provider_returns_itself() -> None:
    health = ProviderHealth()
    assert health.choose_fallback("alpha", ["beta"]) == "alpha"


def test_few_failures_still_healthy() -> None:
    health = ProviderHealth()
    health.record_failure("alpha", ErrorCode.PROVIDER_TIMEOUT)
    health.record_failure("alpha", ErrorCode.DATA_UNAVAILABLE)
    # Below threshold (3) — still healthy.
    assert health.choose_fallback("alpha", ["beta"]) == "alpha"


# -- circuit opens -----------------------------------------------------------

def test_circuit_opens_after_threshold() -> None:
    health = ProviderHealth(threshold=3)
    for _ in range(3):
        health.record_failure("alpha", ErrorCode.PROVIDER_TIMEOUT)
    assert health.choose_fallback("alpha", ["beta", "gamma"]) == "beta"


def test_circuit_open_returns_none_when_all_unhealthy() -> None:
    health = ProviderHealth(threshold=3)
    for _ in range(3):
        health.record_failure("alpha", ErrorCode.PROVIDER_TIMEOUT)
    for _ in range(3):
        health.record_failure("beta", ErrorCode.PROVIDER_TIMEOUT)
    assert health.choose_fallback("alpha", ["beta"]) is None


def test_fallback_skips_unhealthy_candidate() -> None:
    health = ProviderHealth(threshold=3)
    for _ in range(3):
        health.record_failure("alpha", ErrorCode.PROVIDER_TIMEOUT)
    for _ in range(3):
        health.record_failure("beta", ErrorCode.PROVIDER_TIMEOUT)
    # gamma is healthy.
    assert health.choose_fallback("alpha", ["beta", "gamma"]) == "gamma"


# -- success resets ----------------------------------------------------------

def test_success_resets_failures() -> None:
    health = ProviderHealth(threshold=3)
    health.record_failure("alpha", ErrorCode.PROVIDER_TIMEOUT)
    health.record_failure("alpha", ErrorCode.PROVIDER_TIMEOUT)
    health.record_success("alpha", elapsed_ms=120)
    # Failure count reset to 0.
    assert health.choose_fallback("alpha", ["beta"]) == "alpha"


def test_success_after_open_closes_it() -> None:
    health = ProviderHealth(threshold=3)
    for _ in range(3):
        health.record_failure("alpha", ErrorCode.PROVIDER_TIMEOUT)
    assert health.choose_fallback("alpha", ["beta"]) == "beta"
    health.record_success("alpha", elapsed_ms=100)
    assert health.choose_fallback("alpha", ["beta"]) == "alpha"


# -- different providers independent -----------------------------------------

def test_providers_are_independent() -> None:
    health = ProviderHealth(threshold=3)
    for _ in range(3):
        health.record_failure("alpha", ErrorCode.PROVIDER_TIMEOUT)
    # beta is still healthy.
    assert health.choose_fallback("beta", ["alpha"]) == "beta"


# -- reset -------------------------------------------------------------------

def test_reset_clears_state() -> None:
    health = ProviderHealth(threshold=3)
    for _ in range(3):
        health.record_failure("alpha", ErrorCode.PROVIDER_TIMEOUT)
    health.reset()
    assert health.choose_fallback("alpha", ["beta"]) == "alpha"
