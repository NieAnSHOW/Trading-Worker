"""Tests for ToolGateway validation, retry, and fallback policy (Task 4).

Safety property (the whole point of this module): a side-effecting tool is
NEVER retried and NEVER used as a fallback source, even when its error is
flagged retryable. The gateway is a defense-in-layer in front of tool
execution; it does not replace the mandate / order-gate / kill-switch gates.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.reliability import gateway as gateway_mod
from src.reliability.contracts import ErrorCode, StepStatus
from src.reliability.gateway import GatewayPolicy, ToolGateway


# -- fakes -------------------------------------------------------------------

class _FakeTool:
    """Minimal BaseTool-like object: gateway only reads .name/.parameters/
    .side_effecting and calls .execute(**kwargs)."""

    def __init__(
        self,
        name: str,
        *,
        parameters: dict[str, Any] | None = None,
        side_effecting: bool = True,
        responses: list[str] | None = None,
        raises: list[BaseException] | None = None,
    ) -> None:
        self.name = name
        self.parameters = parameters or {
            "type": "object",
            "properties": {},
            "required": [],
        }
        self.side_effecting = side_effecting
        self._responses = responses or []
        self._raises = raises or []
        self.call_count = 0
        self.last_kwargs: dict[str, Any] = {}

    def execute(self, **kwargs: Any) -> str:
        self.call_count += 1
        self.last_kwargs = dict(kwargs)
        if self._raises and len(self._raises) >= self.call_count:
            raise self._raises[self.call_count - 1]
        if self._responses:
            idx = min(self.call_count - 1, len(self._responses) - 1)
            return self._responses[idx]
        return json.dumps({"status": "ok"})


class _FakeRegistry:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> _FakeTool | None:
        return self._tools.get(name)

    def execute(self, name: str, params: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"status": "error", "error": f"Tool '{name}' not found"})
        return tool.execute(**params)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry backoff is real time in prod; tests pin it to zero for speed."""
    monkeypatch.setattr(gateway_mod, "_RETRY_BACKOFF_BASE", 0.0)


# -- Step 1a: unknown / disallowed tools are rejected ------------------------


class TestUnknownAndDisallowed:
    def test_unknown_tool_rejected_without_execution(self) -> None:
        reg = _FakeRegistry([])
        gw = ToolGateway(reg)
        policy = GatewayPolicy(allowed_tools=frozenset({"read"}))
        res = gw.execute("nope", {}, step_id="s1", policy=policy)
        assert res.status == StepStatus.RECOVERABLE_ERROR
        assert res.error is not None
        assert res.error.code == ErrorCode.UNKNOWN
        assert res.error.retryable is False

    def test_disallowed_tool_rejected_even_when_registered(self) -> None:
        tool = _FakeTool("write_only", side_effecting=False)
        reg = _FakeRegistry([tool])
        gw = ToolGateway(reg)
        policy = GatewayPolicy(allowed_tools=frozenset({"other_tool"}))
        res = gw.execute("write_only", {}, step_id="s1", policy=policy)
        assert res.status == StepStatus.RECOVERABLE_ERROR
        assert res.error is not None
        assert res.error.code == ErrorCode.UNKNOWN
        assert tool.call_count == 0  # never invoked


# -- Step 1b: invalid schema args never reach execute ------------------------


class TestSchemaValidation:
    def test_missing_required_field_never_invokes_tool(self) -> None:
        tool = _FakeTool(
            "quote",
            side_effecting=False,
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["symbol"],
            },
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(allowed_tools=frozenset({"quote"}), allow_side_effects=False)
        res = gw.execute("quote", {}, step_id="s1", policy=policy)
        assert res.status == StepStatus.RECOVERABLE_ERROR
        assert res.error is not None
        assert res.error.code == ErrorCode.INVALID_ARGUMENT
        assert res.error.retryable is False
        assert "symbol" in (res.error.repair_hint or "")
        assert tool.call_count == 0

    def test_wrong_type_never_invokes_tool(self) -> None:
        tool = _FakeTool(
            "quote",
            side_effecting=False,
            parameters={
                "type": "object",
                "properties": {"symbol": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["symbol"],
            },
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(allowed_tools=frozenset({"quote"}))
        res = gw.execute("quote", {"symbol": "AAPL", "limit": "not-an-int"}, step_id="s1", policy=policy)
        assert res.error is not None
        assert res.error.code == ErrorCode.INVALID_ARGUMENT
        assert "limit" in (res.error.repair_hint or "")
        assert tool.call_count == 0

    def test_boolean_not_accepted_as_integer(self) -> None:
        tool = _FakeTool(
            "q",
            side_effecting=False,
            parameters={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(allowed_tools=frozenset({"q"}))
        res = gw.execute("q", {"n": True}, step_id="s1", policy=policy)
        assert res.error is not None
        assert res.error.code == ErrorCode.INVALID_ARGUMENT
        assert tool.call_count == 0

    def test_additional_properties_false_rejects_unknown_field(self) -> None:
        tool = _FakeTool(
            "q",
            side_effecting=False,
            parameters={
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "additionalProperties": False,
            },
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(allowed_tools=frozenset({"q"}))
        res = gw.execute("q", {"a": "x", "b": 1}, step_id="s1", policy=policy)
        assert res.error is not None
        assert res.error.code == ErrorCode.INVALID_ARGUMENT
        assert tool.call_count == 0


# -- Step 1c: symbol/date normalizers applied -------------------------------


class TestNormalization:
    def test_symbol_whitespace_stripped(self) -> None:
        tool = _FakeTool(
            "quote",
            side_effecting=False,
            parameters={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(allowed_tools=frozenset({"quote"}))
        res = gw.execute("quote", {"symbol": "  AAPL  "}, step_id="s1", policy=policy)
        assert res.status == StepStatus.SUCCESS
        assert tool.last_kwargs["symbol"] == "AAPL"

    def test_date_separators_normalized(self) -> None:
        tool = _FakeTool(
            "px",
            side_effecting=False,
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                },
                "required": ["symbol"],
            },
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(allowed_tools=frozenset({"px"}))
        res = gw.execute(
            "px",
            {"symbol": "AAPL", "start_date": "2024/01/01", "end_date": " 2024-02-01 "},
            step_id="s1",
            policy=policy,
        )
        assert res.status == StepStatus.SUCCESS
        assert tool.last_kwargs["start_date"] == "2024-01-01"
        assert tool.last_kwargs["end_date"] == "2024-02-01"

    def test_normalize_arguments_is_pure_helper(self) -> None:
        gw = ToolGateway(_FakeRegistry([]))
        out = gw.normalize_arguments("quote", {"symbol": " a ", "date": "2024/1/2", "x": 1})
        assert out["symbol"] == "a"
        assert out["date"] == "2024-1-2"
        assert out["x"] == 1


# -- Step 1d: side-effecting tool rejected when allow_side_effects=False -----


class TestSideEffectGate:
    def test_side_effecting_tool_rejected_when_not_allowed(self) -> None:
        tool = _FakeTool("bash", side_effecting=True)
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(allowed_tools=frozenset({"bash"}), allow_side_effects=False)
        res = gw.execute("bash", {"command": "ls"}, step_id="s1", policy=policy)
        assert res.status == StepStatus.UNSAFE_ERROR
        assert res.error is not None
        assert res.error.code == ErrorCode.UNSAFE_SIDE_EFFECT
        assert tool.call_count == 0

    def test_side_effecting_tool_runs_when_allowed(self) -> None:
        tool = _FakeTool("bash", side_effecting=True)
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(allowed_tools=frozenset({"bash"}), allow_side_effects=True)
        res = gw.execute("bash", {"command": "ls"}, step_id="s1", policy=policy)
        assert res.status == StepStatus.SUCCESS
        assert tool.call_count == 1

    def test_trading_namespace_always_treated_side_effecting(self) -> None:
        # Defense-in-depth: even if a trading tool is mislabeled side_effecting=False,
        # the namespace denylist must keep it side-effecting.
        tool = _FakeTool("trading_place_order", side_effecting=False)
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"trading_place_order"}), allow_side_effects=False
        )
        res = gw.execute("trading_place_order", {}, step_id="s1", policy=policy)
        assert res.status == StepStatus.UNSAFE_ERROR
        assert res.error is not None
        assert res.error.code == ErrorCode.UNSAFE_SIDE_EFFECT
        assert tool.call_count == 0


# -- Step 1e: retry only for read-only + retryable --------------------------


class TestRetryPolicy:
    def test_readonly_retryable_succeeds_on_second_attempt(self) -> None:
        tool = _FakeTool(
            "px",
            side_effecting=False,
            raises=[TimeoutError("provider slow")],
            responses=[json.dumps({"status": "ok", "price": 100})],
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"px"}), retry_limit=1, allow_side_effects=False
        )
        res = gw.execute("px", {}, step_id="s1", policy=policy)
        assert res.status == StepStatus.SUCCESS
        assert tool.call_count == 2

    def test_no_retry_when_retry_limit_zero(self) -> None:
        tool = _FakeTool(
            "px",
            side_effecting=False,
            raises=[TimeoutError("provider slow")],
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"px"}), retry_limit=0, allow_side_effects=False
        )
        res = gw.execute("px", {}, step_id="s1", policy=policy)
        assert res.status == StepStatus.RECOVERABLE_ERROR
        assert res.error is not None
        assert res.error.code == ErrorCode.PROVIDER_TIMEOUT
        assert tool.call_count == 1

    def test_non_retryable_error_not_retried(self) -> None:
        tool = _FakeTool(
            "px",
            side_effecting=False,
            raises=[PermissionError("bad api key")],
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"px"}), retry_limit=3, allow_side_effects=False
        )
        res = gw.execute("px", {}, step_id="s1", policy=policy)
        assert res.error is not None
        assert res.error.code == ErrorCode.AUTH_REQUIRED
        assert res.error.retryable is False
        assert tool.call_count == 1


# -- Step 1f: fallback only on data_unavailable/provider_timeout, readonly ---


class TestFallback:
    def test_fallback_invoked_on_data_unavailable_for_readonly(self) -> None:
        primary = _FakeTool(
            "primary",
            side_effecting=False,
            responses=[json.dumps({"status": "error", "error": "no data", "empty": True})],
        )
        fallback = _FakeTool(
            "backup",
            side_effecting=False,
            responses=[json.dumps({"status": "ok", "price": 42})],
        )
        gw = ToolGateway(_FakeRegistry([primary, fallback]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"primary", "backup"}),
            fallback_tools={"primary": "backup"},
        )
        res = gw.execute("primary", {}, step_id="s1", policy=policy)
        assert res.status == StepStatus.SUCCESS
        assert primary.call_count == 1
        assert fallback.call_count == 1

    def test_no_fallback_on_auth_error(self) -> None:
        primary = _FakeTool("primary", side_effecting=False, raises=[PermissionError("nope")])
        fallback = _FakeTool("backup", side_effecting=False)
        gw = ToolGateway(_FakeRegistry([primary, fallback]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"primary", "backup"}),
            fallback_tools={"primary": "backup"},
        )
        res = gw.execute("primary", {}, step_id="s1", policy=policy)
        assert res.error is not None
        assert res.error.code == ErrorCode.AUTH_REQUIRED
        assert fallback.call_count == 0

    def test_fallback_not_executed_when_args_invalid_for_fallback_schema(self) -> None:
        # Global constraint: invalid arguments NEVER reach the actual tool
        # execute call. The fallback IS a tool execute call, so the primary's
        # arguments must be re-validated against the fallback tool's schema.
        primary = _FakeTool(
            "primary",
            side_effecting=False,
            parameters={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
            responses=[json.dumps({"status": "error", "error": "no data", "empty": True})],
        )
        # Fallback requires a DIFFERENT field the primary was not called with.
        fallback = _FakeTool(
            "backup",
            side_effecting=False,
            parameters={
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
            responses=[json.dumps({"status": "ok", "price": 42})],
        )
        gw = ToolGateway(_FakeRegistry([primary, fallback]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"primary", "backup"}),
            fallback_tools={"primary": "backup"},
        )
        res = gw.execute("primary", {"symbol": "AAPL"}, step_id="s1", policy=policy)
        # Fallback must NOT run: its schema rejects the primary's arguments.
        assert fallback.call_count == 0
        # No fallback applied -> the primary's data_unavailable error surfaces.
        assert res.status == StepStatus.RECOVERABLE_ERROR
        assert res.error is not None
        assert res.error.code == ErrorCode.DATA_UNAVAILABLE


# -- Step 1g: WRITE TOOL IS NEVER RETRIED OR FALLBACK -----------------------
# This is the never-retry-writes property — the single most important test.


class TestNeverRetryWrites:
    def test_write_tool_not_retried_even_when_error_is_retryable(self) -> None:
        # side_effecting=True + a TimeoutError (normally retryable) -> ONE attempt only.
        tool = _FakeTool(
            "bash",
            side_effecting=True,
            raises=[TimeoutError("slow")],
        )
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"bash"}),
            retry_limit=5,
            allow_side_effects=True,  # allowed to RUN, but must not RETRY
        )
        res = gw.execute("bash", {"command": "x"}, step_id="s1", policy=policy)
        assert res.status == StepStatus.RECOVERABLE_ERROR
        assert res.error is not None
        assert res.error.code == ErrorCode.PROVIDER_TIMEOUT
        # The error is flagged retryable in the abstract, but the gateway MUST NOT
        # have acted on it for a side-effecting tool.
        assert tool.call_count == 1

    def test_write_tool_not_fallen_back_even_if_configured(self) -> None:
        primary = _FakeTool(
            "trading_place_order",
            side_effecting=False,  # mislabeled; namespace rule still classifies as write
            responses=[json.dumps({"status": "error", "error": "no data", "empty": True})],
        )
        fallback = _FakeTool("backup", side_effecting=False)
        gw = ToolGateway(_FakeRegistry([primary, fallback]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"trading_place_order", "backup"}),
            fallback_tools={"trading_place_order": "backup"},
            allow_side_effects=True,  # allowed to RUN; still must not FALLBACK
        )
        res = gw.execute("trading_place_order", {}, step_id="s1", policy=policy)
        # data_unavailable would normally trigger fallback for a read-only tool,
        # but trading_* is side-effecting -> no fallback.
        assert fallback.call_count == 0
        assert primary.call_count == 1

    def test_unknown_exception_not_retried_for_readonly(self) -> None:
        tool = _FakeTool("px", side_effecting=False, raises=[RuntimeError("???")])
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(
            allowed_tools=frozenset({"px"}), retry_limit=3, allow_side_effects=False
        )
        res = gw.execute("px", {}, step_id="s1", policy=policy)
        assert res.error is not None
        assert res.error.code == ErrorCode.UNKNOWN
        assert res.error.retryable is False
        assert tool.call_count == 1


# -- exception text redaction ------------------------------------------------


class TestRedaction:
    def test_exception_text_redacted_in_error_message(self) -> None:
        # An exception whose text embeds an internal filesystem root must not
        # leak that root into the emitted ToolError message. Use the real agent
        # dir, which redact_internal_paths is guaranteed to know.
        from pathlib import Path

        agent_dir = str(Path(__file__).resolve().parents[1])
        secret_path = f"{agent_dir}/secret/leaked"
        tool = _FakeTool("px", side_effecting=False, raises=[ValueError(f"boom at {secret_path}")])
        gw = ToolGateway(_FakeRegistry([tool]))
        policy = GatewayPolicy(allowed_tools=frozenset({"px"}), retry_limit=0)
        res = gw.execute("px", {}, step_id="s1", policy=policy)
        assert res.error is not None
        msg = res.error.message
        assert agent_dir not in msg
        assert "<redacted>" in msg
