"""Guarded tool gateway: validation, retry, and fallback policy (Task 4).

The gateway sits in front of tool execution as a defense-in-layer. It does
NOT replace the mandate / order-gate / kill-switch / audit-ledger gates for
live writes — it adds schema validation, conservative normalization, typed
exception classification, bounded read-only retry, and read-only fallback in
front of the existing :class:`ToolRegistry`.

Non-negotiable safety property
------------------------------
A side-effecting tool is **never** retried and **never** used as a fallback
source, even if its error is flagged retryable. Retry and fallback are gated
on TWO conditions conjunctively:

  1. the error is retryable / fallback-eligible by policy, AND
  2. the tool is provably read-only.

When ``GatewayPolicy.allow_side_effects=False`` a side-effecting tool is
rejected before execution with an ``unsafe_side_effect`` error and is never
invoked.

Side-effecting classification
-----------------------------
A tool is treated as side-effecting when ANY of:

  * its name starts with ``trading_`` (the live-trading namespace), or
  * its name is ``bash`` (the shell), or
  * it does not explicitly declare ``side_effecting = False``.

The default is therefore conservative (side-effecting). This is intentional:
the gateway must not retry what it cannot prove is read-only. A mislabeled
trading tool cannot escape the namespace rule.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.reliability.contracts import ErrorCode, StepResult, StepStatus, ToolError
from src.tools.redaction import redact_internal_paths

if TYPE_CHECKING:
    from src.reliability.cache import ResultCache
    from src.reliability.providers import ProviderHealth

# Exponential backoff base for retry. Tests monkeypatch this to zero; prod
# keeps a small base so a transient blip is covered without stalling the loop.
# ponytail: a module constant is the minimum knob; expose per-policy if needed.
_RETRY_BACKOFF_BASE = 0.1
_RETRY_BACKOFF_CAP = 2.0

# TTL for cached read-only results in the reliability path. Conservative: most
# market data is stable enough for a 5-minute window within a single research
# run. ponytail: a single constant; expose per-policy if needed.
_CACHE_TTL_SECONDS = 300.0

# Defense-in-depth denylist: these names are side-effecting regardless of any
# per-tool attribute, so a mislabeled tool cannot become retryable by accident.
_SIDE_EFFECTING_NAME_PREFIXES = ("trading_",)
_SIDE_EFFECTING_NAMES = frozenset({"bash"})

# Conservative JSON-Schema type → python tuple. ``bool`` is excluded from
# integer/number (bool is an int subclass in Python; that is never an int arg).
_JSON_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}

# Argument fields the normalizer is allowed to touch. Kept explicit so the
# gateway never silently mangles a field it does not understand.
_NORMALIZE_SYMBOL_FIELDS = ("symbol", "ticker", "code")
_NORMALIZE_DATE_FIELDS = ("date", "start_date", "end_date", "from_date", "to_date")


class GatewayPolicy(BaseModel):
    """Policy for one gateway ``execute`` call (or a run).

    Attributes:
        allowed_tools: Tools the gateway may invoke. Disallowed tools are
            rejected before execution.
        retry_limit: Max additional attempts after the first. ``0`` (default)
            means no retry — one attempt total.
        fallback_tools: Mapping of primary tool name → fallback tool name.
            Fallback is invoked only on ``data_unavailable`` /
            ``provider_timeout`` and only for read-only tools.
        timeout_seconds: Advisory per-attempt timeout hint (the hard timeout
            is still the AgentLoop's worker watchdog).
        allow_side_effects: When False, side-effecting tools are rejected
            before execution with ``unsafe_side_effect``.
    """

    allowed_tools: frozenset[str]
    retry_limit: int = 0
    fallback_tools: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 30.0
    allow_side_effects: bool = False


def _is_side_effecting(tool_name: str, tool: Any) -> bool:
    """Conservative side-effect classification.

    Namespace rule first (cannot be overridden), then the per-tool attribute
    (default True when absent).
    """
    if tool_name in _SIDE_EFFECTING_NAMES or tool_name.startswith(_SIDE_EFFECTING_NAME_PREFIXES):
        return True
    return bool(getattr(tool, "side_effecting", True))


def _redact(text: str) -> str:
    """Strip internal filesystem roots and credentials before emitting."""
    if not text:
        return ""
    return redact_internal_paths(text)


class ToolGateway:
    """Validate, classify, and (for read-only tools) recover tool calls."""

    def __init__(
        self,
        registry: Any,
        *,
        cache: ResultCache | None = None,
        health: ProviderHealth | None = None,
    ) -> None:
        """Initialize the gateway.

        Args:
            registry: Tool registry with ``get(name)`` returning a BaseTool-like
                object (or None) whose ``execute(**kwargs)`` returns a JSON
                string and may raise.
            cache: Optional result cache for read-only tool results. When
                provided, the gateway checks the cache before execution and
                caches successful read-only results. When None (default), no
                caching occurs — existing behavior is untouched.
            health: Optional provider health tracker. When provided, the
                gateway records success/failure after execution.
        """
        self._registry = registry
        self._cache = cache
        self._health = health

    # -- public API ---------------------------------------------------------

    def normalize_arguments(self, tool_name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """Conservatively normalize known argument fields.

        Only touches explicitly-supported field names; never raises. Symbol
        fields are trimmed; date fields have ``/`` separators normalized to
        ``-`` and are trimmed.
        """
        out: dict[str, object] = dict(arguments) if isinstance(arguments, Mapping) else {}
        for key in _NORMALIZE_SYMBOL_FIELDS:
            val = out.get(key)
            if isinstance(val, str):
                out[key] = val.strip()
        for key in _NORMALIZE_DATE_FIELDS:
            val = out.get(key)
            if isinstance(val, str):
                out[key] = val.strip().replace("/", "-")
        return out

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        step_id: str,
        policy: GatewayPolicy,
        session_id: str = "",
    ) -> StepResult:
        """Execute a tool under the gateway policy.

        Order of gates: unknown → disallowed → side-effect → normalize →
        schema-validate → execute (with bounded read-only retry / fallback).
        """
        tool = self._lookup(tool_name)
        if tool is None:
            return self._blocked(
                step_id, ErrorCode.UNKNOWN, f"tool '{tool_name}' is not registered", retryable=False
            )
        if tool_name not in policy.allowed_tools:
            return self._blocked(
                step_id,
                ErrorCode.UNKNOWN,
                f"tool '{tool_name}' is not in allowed_tools",
                retryable=False,
                repair_hint="route through an allowed tool or widen allowed_tools",
            )
        if _is_side_effecting(tool_name, tool) and not policy.allow_side_effects:
            return StepResult(
                step_id=step_id,
                status=StepStatus.UNSAFE_ERROR,
                error=ToolError(
                    code=ErrorCode.UNSAFE_SIDE_EFFECT,
                    message=f"tool '{tool_name}' is side-effecting and allow_side_effects=False",
                    retryable=False,
                    repair_hint="confirm the action with the user; do not auto-run side-effecting tools",
                ),
            )

        arguments = self.normalize_arguments(tool_name, arguments)
        hint = self._validate_schema(tool, arguments)
        if hint is not None:
            return self._blocked(
                step_id, ErrorCode.INVALID_ARGUMENT, hint, retryable=False, repair_hint=hint
            )

        # Reliability cache: check before execution (read-only tools only).
        read_only = not _is_side_effecting(tool_name, tool)
        if self._cache is not None and read_only:
            cache_key = json.dumps({"tool": tool_name, "args": arguments}, default=str)
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._record_telemetry("cache_hit")
                return cached
            self._record_telemetry("cache_miss")

        result = self._execute_with_recovery(tool_name, tool, arguments, step_id, policy, session_id)

        # Record provider health (redacted: only tool name + error code).
        if self._health is not None:
            self._record_health(tool_name, result)

        # Cache successful read-only results only.
        if self._cache is not None and read_only and result.status is StepStatus.SUCCESS:
            self._cache.put(cache_key, result, ttl_seconds=_CACHE_TTL_SECONDS)

        return result

    @staticmethod
    def _record_telemetry(name: str) -> None:
        try:
            from src.telemetry import counters
            counters.record_reliability_event(name)
        except Exception:  # noqa: BLE001 - telemetry must not break execution
            pass

    def _record_health(self, tool_name: str, result: StepResult) -> None:
        try:
            if result.status is StepStatus.SUCCESS:
                self._health.record_success(tool_name, result.elapsed_ms)
            elif result.error is not None:
                self._health.record_failure(tool_name, result.error.code)
        except Exception:  # noqa: BLE001 - health tracking must not break execution
            pass

    # -- internals ----------------------------------------------------------

    def _lookup(self, tool_name: str) -> Any:
        get_tool = getattr(self._registry, "get", None)
        if not callable(get_tool):
            return None
        try:
            return get_tool(tool_name)
        except Exception:  # noqa: BLE001 - unknown classification is not executable
            return None

    @staticmethod
    def _blocked(
        step_id: str,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool,
        repair_hint: str | None = None,
    ) -> StepResult:
        return StepResult(
            step_id=step_id,
            status=StepStatus.RECOVERABLE_ERROR,
            error=ToolError(code=code, message=_redact(message), retryable=retryable, repair_hint=repair_hint),
        )

    def _validate_schema(self, tool: Any, arguments: dict[str, object]) -> str | None:
        """Minimal JSON-Schema object validator. Returns a hint string or None."""
        schema = getattr(tool, "parameters", {}) or {}
        if not isinstance(schema, Mapping):
            return None
        required = schema.get("required") or []
        if isinstance(required, list):
            for field in required:
                if field not in arguments:
                    return f"missing required field '{field}'"
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, value in arguments.items():
            spec = properties.get(key) if isinstance(properties, Mapping) else None
            if spec is None:
                if additional is False:
                    return f"unexpected field '{key}'"
                continue
            expected = spec.get("type") if isinstance(spec, Mapping) else None
            hint = self._check_type(expected, value)
            if hint is not None:
                return f"field '{key}' {hint}"
        return None

    @staticmethod
    def _check_type(expected: Any, value: object) -> str | None:
        if not expected or expected not in _JSON_TYPE_MAP:
            return None
        # bool is an int subclass; reject it for numeric types explicitly.
        if expected in ("integer", "number") and isinstance(value, bool):
            return f"expected {expected}, got boolean"
        types = _JSON_TYPE_MAP[expected]
        if not isinstance(value, types):
            return f"expected {expected}, got {type(value).__name__}"
        return None

    def _execute_with_recovery(
        self,
        tool_name: str,
        tool: Any,
        arguments: dict[str, object],
        step_id: str,
        policy: GatewayPolicy,
        session_id: str,
    ) -> StepResult:
        read_only = not _is_side_effecting(tool_name, tool)
        last = self._execute_once(tool_name, tool, arguments, step_id)
        attempts = 1
        # Retry: ONLY read-only tools with a retryable error, bounded by retry_limit.
        while (
            read_only
            and last.status is StepStatus.RECOVERABLE_ERROR
            and last.error is not None
            and last.error.retryable
            and attempts <= policy.retry_limit
        ):
            time.sleep(min(_RETRY_BACKOFF_BASE * (2 ** (attempts - 1)), _RETRY_BACKOFF_CAP))
            last = self._execute_once(tool_name, tool, arguments, step_id)
            attempts += 1
        # Fallback: ONLY read-only tools, ONLY on data_unavailable / provider_timeout.
        if (
            read_only
            and last.status is StepStatus.RECOVERABLE_ERROR
            and last.error is not None
            and last.error.code in (ErrorCode.DATA_UNAVAILABLE, ErrorCode.PROVIDER_TIMEOUT)
        ):
            fb = self._try_fallback(tool_name, arguments, step_id, policy, session_id)
            if fb is not None:
                return fb
        return last

    def _execute_once(self, tool_name: str, tool: Any, arguments: dict[str, object], step_id: str) -> StepResult:
        t0 = time.perf_counter()
        try:
            raw = tool.execute(**arguments)
        except Exception as exc:  # noqa: BLE001 - classify, do not crash the agent
            err = self._classify_exception(exc)
            return StepResult(
                step_id=step_id,
                status=StepStatus.RECOVERABLE_ERROR,
                error=err,
                elapsed_ms=int((time.perf_counter() - t0) * 1000),
            )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        envelope_code, empty = self._inspect_envelope(raw)
        if envelope_code is None and not empty:
            return StepResult(step_id=step_id, status=StepStatus.SUCCESS, data=raw, elapsed_ms=elapsed_ms)
        code = ErrorCode.DATA_UNAVAILABLE if empty else envelope_code or ErrorCode.UNKNOWN
        retryable = code in (ErrorCode.DATA_UNAVAILABLE, ErrorCode.PROVIDER_TIMEOUT)
        return StepResult(
            step_id=step_id,
            status=StepStatus.RECOVERABLE_ERROR,
            error=ToolError(
                code=code,
                message=_redact(self._envelope_message(raw)),
                retryable=retryable,
            ),
            elapsed_ms=elapsed_ms,
        )

    def _try_fallback(
        self,
        tool_name: str,
        arguments: dict[str, object],
        step_id: str,
        policy: GatewayPolicy,
        session_id: str,
    ) -> StepResult | None:
        fb_name = policy.fallback_tools.get(tool_name)
        if not fb_name or fb_name == tool_name:
            return None
        fb_tool = self._lookup(fb_name)
        if fb_tool is None or fb_name not in policy.allowed_tools:
            return None
        if _is_side_effecting(fb_name, fb_tool):
            # A write tool is never a valid fallback for a read-only failure.
            return None
        # Re-validate arguments against the FALLBACK tool's schema: the fallback
        # is a tool execute call, so invalid arguments must never reach it.
        if self._validate_schema(fb_tool, arguments) is not None:
            return None
        # One attempt, no further retry/fallback chaining (bounded).
        return self._execute_once(fb_name, fb_tool, arguments, step_id)

    @staticmethod
    def _classify_exception(exc: BaseException) -> ToolError:
        """Map a Python exception to a typed, redacted ToolError."""
        name = type(exc).__name__
        msg = _redact(str(exc))
        if isinstance(exc, (TimeoutError,)):
            return ToolError(ErrorCode.PROVIDER_TIMEOUT, msg or "provider timed out", retryable=True)
        if isinstance(exc, (ConnectionError,)):
            return ToolError(ErrorCode.DATA_UNAVAILABLE, msg or "provider unavailable", retryable=True)
        if isinstance(exc, (PermissionError,)):
            return ToolError(ErrorCode.AUTH_REQUIRED, msg or "authentication required", retryable=False)
        if isinstance(exc, (KeyError,)):
            return ToolError(
                ErrorCode.INVALID_ARGUMENT,
                f"missing field {msg}",
                retryable=False,
                repair_hint="a required argument was not provided",
            )
        if isinstance(exc, (ValueError, TypeError)):
            return ToolError(ErrorCode.SCHEMA_MISMATCH, msg or "validation failed", retryable=False)
        # Heuristic: transient availability issues often surface as RuntimeError.
        low = name.lower() + " " + msg.lower()
        if "timeout" in low or "timed out" in low:
            return ToolError(ErrorCode.PROVIDER_TIMEOUT, msg or "provider timed out", retryable=True)
        if "auth" in low or "unauthorized" in low or "api key" in low:
            return ToolError(ErrorCode.AUTH_REQUIRED, msg or "authentication required", retryable=False)
        return ToolError(ErrorCode.UNKNOWN, msg or name, retryable=False)

    @staticmethod
    def _inspect_envelope(raw: object) -> tuple[ErrorCode | None, bool]:
        """Inspect a tool result envelope.

        Returns ``(mapped_error_code_or_None, empty_data_flag)``. ``empty`` is
        True when the envelope explicitly signals empty/unavailable data,
        regardless of status — that maps to ``DATA_UNAVAILABLE`` so a read-only
        caller can fall back.
        """
        if not isinstance(raw, str):
            return None, False
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None, False
        if not isinstance(data, dict):
            return None, False
        empty = bool(data.get("empty")) or bool(data.get("data_unavailable"))
        if empty:
            return ErrorCode.DATA_UNAVAILABLE, True
        if data.get("status") != "error":
            return None, False
        code_str = data.get("error_code")
        if isinstance(code_str, str):
            mapping = {
                "tool_timeout": ErrorCode.PROVIDER_TIMEOUT,
                "provider_timeout": ErrorCode.PROVIDER_TIMEOUT,
                "auth_required": ErrorCode.AUTH_REQUIRED,
                "auth": ErrorCode.AUTH_REQUIRED,
                "budget_exceeded": ErrorCode.BUDGET_EXCEEDED,
                "invalid_argument": ErrorCode.INVALID_ARGUMENT,
                "data_unavailable": ErrorCode.DATA_UNAVAILABLE,
            }
            mapped = mapping.get(code_str)
            if mapped is not None:
                return mapped, False
        return None, False

    @staticmethod
    def _envelope_message(raw: object) -> str:
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                return raw[:500]
            if isinstance(data, dict):
                if isinstance(data.get("error"), str):
                    return data["error"]
                return json.dumps(data, ensure_ascii=False)[:500]
        return str(raw)[:500]


__all__ = ["GatewayPolicy", "ToolGateway"]
