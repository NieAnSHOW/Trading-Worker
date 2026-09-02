"""GatewayBridge 单测：幂等、串行/并行、outcome_unknown、截断/redact、gateway 路由。"""

import asyncio
from typing import Any

from src.pi_sidecar.gateway_bridge import GatewayBridge


class FakeRegistry:
    def __init__(self, tools: dict[str, Any]):
        self._tools = tools

    def get(self, name):
        return self._tools.get(name)

    def execute(self, name, params):
        tool = self._tools[name]
        return tool["run"](params)


def make_registry(entries: dict[str, dict]) -> FakeRegistry:
    class T:
        pass

    tools = {}
    for name, spec in entries.items():
        t = T()
        t.name = name
        t.is_readonly = spec.get("readonly", True)
        t.side_effecting = spec.get("side_effecting", False)
        t.repeatable = spec.get("repeatable", True)
        t.parameters = {"type": "object", "properties": {}}
        t.description = spec.get("description", name)
        tools[name] = t
    return FakeRegistry({k: {"run": v.get("run", lambda p: "{}")} for k, v in entries.items()}), tools


class FakeGateway:
    """记录调用并返回预置 StepResult-like dict。"""

    def __init__(self, responses: dict[str, Any]):
        self.calls: list[tuple[str, dict, str]] = []
        self.responses = responses

    def execute(self, tool_name, arguments, *, step_id, policy, session_id=""):
        self.calls.append((tool_name, dict(arguments), step_id))
        import time

        time.sleep(self.responses.get("_delay", 0))
        return self.responses.get(tool_name) or {
            "status": "success",
            "data": {"result": "ok"},
            "error": None,
            "elapsed_ms": 1,
        }


class FakeStepResult:
    def __init__(self, status="success", data=None, error=None):
        self.status = status
        self.data = data or {}
        self.error = error
        self.elapsed_ms = 1

    def to_wire(self):
        return {"status": self.status, "data": self.data, "error": self.error}


def _params(**over):
    base = {
        "call_id": "t-1",
        "toolCallId": "tc-1",
        "toolName": "web_search",
        "arguments": {"query": "aapl"},
        "idempotencyKey": "sess1:assist1:tc-1",
        "is_readonly": True,
        "side_effecting": False,
        "repeatable": True,
        "timeout_seconds": 5,
    }
    base.update(over)
    return base


class TestHandleInvoke:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_success_returns_content_and_records_idempotency(self):
        gw = FakeGateway({})
        bridge = GatewayBridge(None, gateway=gw)
        result = self._run(bridge.handle_invoke(_params()))
        assert result["outcome"] == "ok"
        assert result["isError"] is False
        assert '"result"' in result["content"] or "ok" in result["content"]
        # 重复调用（同幂等键）不再执行
        gw.calls.clear()
        again = self._run(bridge.handle_invoke(_params()))
        assert again["outcome"] == "ok"
        assert gw.calls == []
        assert again["content"] == result["content"]

    def test_duplicate_suppressed_even_with_different_arguments(self):
        gw = FakeGateway({})
        bridge = GatewayBridge(None, gateway=gw)
        first = self._run(bridge.handle_invoke(_params()))
        second = self._run(bridge.handle_invoke(_params(arguments={"query": "other"})))
        assert second["content"] == first["content"]
        assert len(gw.calls) == 1

    def test_gateway_error_is_error_outcome(self):
        gw = FakeGateway({"web_search": FakeStepResult(status="recoverable_error", error={"message": "boom"})})
        bridge = GatewayBridge(None, gateway=gw)
        result = self._run(bridge.handle_invoke(_params()))
        assert result["outcome"] == "error"
        assert result["isError"] is True

    def test_write_unknown_outcome_on_gateway_exception(self):
        class BoomGateway:
            def execute(self, *a, **k):
                raise RuntimeError("connection lost")

        bridge = GatewayBridge(None, gateway=BoomGateway())
        result = self._run(
            bridge.handle_invoke(_params(toolName="trading_place_order", side_effecting=True, is_readonly=False))
        )
        assert result["outcome"] == "outcome_unknown"
        assert result["isError"] is True

    def test_readonly_exception_is_plain_error(self):
        class BoomGateway:
            def execute(self, *a, **k):
                raise RuntimeError("provider down")

        bridge = GatewayBridge(None, gateway=BoomGateway())
        result = self._run(bridge.handle_invoke(_params()))
        assert result["outcome"] == "error"
        assert result["isError"] is True

    def test_output_truncated_to_10k_chars(self):
        big = {"data": "x" * 20_000}

        class BigGateway:
            def execute(self, *a, **k):
                return FakeStepResult(data=big)

        bridge = GatewayBridge(None, gateway=BigGateway())
        result = self._run(bridge.handle_invoke(_params()))
        assert len(result["content"]) <= 10_000
        assert result["content"].endswith("[truncated]") or len(result["content"]) == 10_000

    def test_sensitive_keys_redacted(self):
        class LeakGateway:
            def execute(self, *a, **k):
                return FakeStepResult(data={"api_key": "sk-secret", "result": "fine"})

        bridge = GatewayBridge(None, gateway=LeakGateway())
        result = self._run(bridge.handle_invoke(_params()))
        assert "sk-secret" not in result["content"]

    def test_unknown_tool_is_error(self):
        class NoopGateway:
            def execute(self, *a, **k):
                raise AssertionError("should not be called")

        reg, _ = make_registry({})
        bridge = GatewayBridge(reg, gateway=NoopGateway())
        result = self._run(bridge.handle_invoke(_params(toolName="nonexistent")))
        assert result["outcome"] == "error"
        assert "unknown tool" in result["content"]


class TestConcurrency:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_writes_are_serialized_reads_may_parallel(self):
        # 写/读各用独立计数器：写互斥锁只约束写-写重叠，只读并发可叠加在写之上。
        active = {"n": 0, "w": 0, "max_write_overlap": 0, "max_read_overlap": 0}

        class GW:
            def execute(self, tool_name, arguments, *, step_id, policy, session_id=""):
                import time

                if tool_name == "writer":
                    active["w"] += 1
                    active["max_write_overlap"] = max(active["max_write_overlap"], active["w"])
                    time.sleep(0.05)
                    active["w"] -= 1
                else:
                    active["n"] += 1
                    active["max_read_overlap"] = max(active["max_read_overlap"], active["n"])
                    time.sleep(0.05)
                    active["n"] -= 1
                return FakeStepResult()

        bridge = GatewayBridge(None, gateway=GW())

        async def scenario():
            calls = [_params(call_id=f"r{i}", toolCallId=f"tc-r{i}", idempotencyKey=f"k-r{i}") for i in range(3)]
            calls += [
                _params(
                    call_id="w1",
                    toolCallId="tc-w1",
                    idempotencyKey="k-w1",
                    toolName="writer",
                    side_effecting=True,
                    is_readonly=False,
                ),
                _params(
                    call_id="w2",
                    toolCallId="tc-w2",
                    idempotencyKey="k-w2",
                    toolName="writer",
                    side_effecting=True,
                    is_readonly=False,
                ),
            ]
            await asyncio.gather(*(bridge.handle_invoke(c) for c in calls))

        self._run(scenario())
        assert active["max_write_overlap"] == 1  # 写严格串行
        assert active["max_read_overlap"] >= 2  # 只读并行

    def test_cancel_marks_pending_write_cancelled(self):
        bridge = GatewayBridge(None, gateway=FakeGateway({}))
        bridge.handle_cancel("t-9")  # 不在 flight → no-op 不抛
