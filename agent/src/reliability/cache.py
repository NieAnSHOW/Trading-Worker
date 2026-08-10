"""Bounded in-memory result cache for the reliability runtime.

Sits in front of tool/provider invocation in the reliability path only. Does
NOT replace the loader caches in ``agent/src/providers/`` — those are
untouched when the reliability runtime is off.

Design:
  - Keys are JSON strings. The cache normalizes them (sorted keys at all
    depths) so argument ordering does not matter: ``{"a":1,"b":2}`` and
    ``{"b":2,"a":1}`` map to the same entry.
  - Expiry uses ``time.monotonic()`` in production (immune to wall-clock
    drift). The ``now`` param on ``get`` is for test injection: when provided,
    wall-clock elapsed time is used instead.
  - Bounded size with LRU eviction (``OrderedDict`` + ``move_to_end``).
  - Never caches a side-effecting result. ``put`` is a silent no-op when the
    StepResult carries an ``UNSAFE_ERROR`` status, an ``UNSAFE_SIDE_EFFECT``
    error code, or the key references a side-effecting tool name (reuses the
    gateway's ``_SIDE_EFFECTING_NAMES`` / ``_SIDE_EFFECTING_NAME_PREFIXES``
    constants — no duplicated classification logic).

No raw prompt or credential values are added to keys by this module. The
caller is responsible for not passing secrets as keys; the cache only
re-normalizes what it receives.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from src.reliability.contracts import ErrorCode, StepResult, StepStatus
from src.reliability.gateway import _SIDE_EFFECTING_NAMES, _SIDE_EFFECTING_NAME_PREFIXES

_DEFAULT_MAX_SIZE = 128


def _normalize_key(key: str) -> str:
    """Re-serialize a JSON key with sorted keys so ordering doesn't matter.

    Non-JSON keys pass through unchanged.
    """
    try:
        parsed = json.loads(key)
    except (TypeError, ValueError):
        return key
    return json.dumps(parsed, sort_keys=True, ensure_ascii=False, default=str)


def _is_side_effecting_key(norm_key: str) -> bool:
    """Defense-in-depth: refuse keys that reference a side-effecting tool.

    Reuses the gateway's name constants (no duplicated classification). The
    key is checked as a raw string — intentionally conservative.
    """
    for prefix in _SIDE_EFFECTING_NAME_PREFIXES:
        if prefix in norm_key:
            return True
    for name in _SIDE_EFFECTING_NAMES:
        if name in norm_key:
            return True
    return False


@dataclass
class _Entry:
    result: StepResult
    mono_inserted: float  # time.monotonic() at put
    wall_inserted: float  # datetime.now().timestamp() at put
    ttl: float


class ResultCache:
    """Bounded LRU cache for read-only StepResults."""

    def __init__(self, *, max_size: int = _DEFAULT_MAX_SIZE) -> None:
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str, *, now: datetime | None = None) -> StepResult | None:
        norm = _normalize_key(key)
        entry = self._store.get(norm)
        if entry is None:
            return None
        if now is not None:
            elapsed = now.timestamp() - entry.wall_inserted
        else:
            elapsed = time.monotonic() - entry.mono_inserted
        if elapsed >= entry.ttl:
            del self._store[norm]
            return None
        self._store.move_to_end(norm)
        return entry.result

    def put(self, key: str, result: StepResult, *, ttl_seconds: float) -> None:
        # Never cache a side-effecting result — defense-in-depth on the result
        # itself (status / error code) plus the key (tool name pattern).
        if result.status is StepStatus.UNSAFE_ERROR:
            return
        if result.error is not None and result.error.code is ErrorCode.UNSAFE_SIDE_EFFECT:
            return
        norm = _normalize_key(key)
        if _is_side_effecting_key(norm):
            return
        self._store[norm] = _Entry(
            result=result,
            mono_inserted=time.monotonic(),
            wall_inserted=datetime.now().timestamp(),
            ttl=ttl_seconds,
        )
        self._store.move_to_end(norm)
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def reset(self) -> None:
        """Clear all entries (for tests)."""
        self._store.clear()


__all__ = ["ResultCache"]
