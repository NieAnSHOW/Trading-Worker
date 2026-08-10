"""Provider health tracking with a bounded circuit for fallback selection.

Process-local, instance-based state. Each provider has a consecutive-failure
counter; after ``threshold`` failures the provider is considered unhealthy
and ``choose_fallback`` will skip it in favour of a healthy candidate.

Only redacted data is recorded: provider names are short codes, and
``ErrorCode`` is an enum — both safe. No prompt content, arguments, or
credential material passes through this module.
"""

from __future__ import annotations

from collections.abc import Sequence

from src.reliability.contracts import ErrorCode

_DEFAULT_THRESHOLD = 3


class ProviderHealth:
    """Per-provider failure tracking with bounded circuit + fallback."""

    def __init__(self, *, threshold: int = _DEFAULT_THRESHOLD) -> None:
        self._failures: dict[str, int] = {}
        self._threshold = threshold

    def record_success(self, provider: str, elapsed_ms: int) -> None:
        self._failures[provider] = 0

    def record_failure(self, provider: str, code: ErrorCode) -> None:
        self._failures[provider] = self._failures.get(provider, 0) + 1

    def _is_healthy(self, provider: str) -> bool:
        return self._failures.get(provider, 0) < self._threshold

    def choose_fallback(self, provider: str, candidates: Sequence[str]) -> str | None:
        """Return the provider if healthy, else the first healthy candidate.

        If all candidates are unhealthy, return None.
        """
        if self._is_healthy(provider):
            return provider
        for cand in candidates:
            if self._is_healthy(cand):
                return cand
        return None

    def reset(self) -> None:
        """Clear all state (for tests)."""
        self._failures.clear()


__all__ = ["ProviderHealth"]
