"""Typed reliability contracts for the agent runtime.

These are internal, immutable, wire-serializable data objects. They are the
cross-task contract imported by the router, gateway, runtime, and evidence
modules. Keep them deterministic and free of behavior beyond serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    RECOVERABLE_ERROR = "recoverable_error"
    BLOCKED = "blocked"
    UNSAFE_ERROR = "unsafe_error"
    CANCELLED = "cancelled"


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"
    AMBIGUOUS_SYMBOL = "ambiguous_symbol"
    DATA_UNAVAILABLE = "data_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    AUTH_REQUIRED = "auth_required"
    SCHEMA_MISMATCH = "schema_mismatch"
    UNSAFE_SIDE_EFFECT = "unsafe_side_effect"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolError:
    """A redactable, retry-aware error surfaced by a tool or step."""

    code: ErrorCode
    message: str
    retryable: bool = False
    fallback: str | None = None
    repair_hint: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "fallback": self.fallback,
            "repair_hint": self.repair_hint,
        }


@dataclass(frozen=True)
class EvidenceRef:
    """Pointer to an upstream fact a step's output depends on."""

    source_type: str
    source_id: str
    field: str | None = None
    as_of: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "field": self.field,
            "as_of": self.as_of,
        }


def _coerce_data(value: Any) -> Any:
    """Best-effort coercion of arbitrary step data to a JSON-safe form.

    Never raises: unknown types fall back to str().
    """
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce_data(v) for v in value]
    if isinstance(value, dict):
        # keys must be strings for JSON; coerce defensively
        return {str(k): _coerce_data(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        # Deterministic list for JSON; key=str keeps mixed types from raising.
        return sorted((_coerce_data(v) for v in value), key=str)
    return str(value)


@dataclass(frozen=True)
class StepResult:
    """Outcome of one step in the reliability runtime.

    Invariants enforced in __post_init__:
      - RECOVERABLE_ERROR requires an error.
      - evidence, if non-empty, must contain only EvidenceRef instances.
    """

    step_id: str
    status: StepStatus
    data: object | None = None
    error: ToolError | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    provider: str | None = None
    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        if self.status is StepStatus.RECOVERABLE_ERROR and self.error is None:
            raise ValueError(
                "StepResult(recoverable_error) requires an error; "
                "use StepStatus.BLOCKED for an error-free hold."
            )
        # ponytail: tuple-of-EvidenceRef type is enforced at construction so
        # malformed evidence fails fast at the boundary, not deep in to_wire.
        if self.evidence:
            bad = [e for e in self.evidence if not isinstance(e, EvidenceRef)]
            if bad:
                raise TypeError(
                    "StepResult.evidence must be a tuple of EvidenceRef; "
                    f"got {type(bad[0]).__name__}"
                )

    def to_wire(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status.value,
            "data": _coerce_data(self.data),
            "error": self.error.to_wire() if self.error is not None else None,
            "evidence": [e.to_wire() for e in self.evidence],
            "provider": self.provider,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True)
class CapabilityRoute:
    """Static routing rule: which tools/capabilities serve an intent."""

    intent: str
    capabilities: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    complexity: str
    budgets: dict[str, int]

    def to_wire(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "capabilities": list(self.capabilities),
            "allowed_tools": list(self.allowed_tools),
            "complexity": self.complexity,
            "budgets": dict(self.budgets),
        }


__all__ = [
    "CapabilityRoute",
    "ErrorCode",
    "EvidenceRef",
    "StepResult",
    "StepStatus",
    "ToolError",
]
