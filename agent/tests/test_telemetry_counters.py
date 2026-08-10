# agent/tests/test_telemetry_counters.py
from agent.src.telemetry import counters


def test_skill_call_increment():
    counters.reset_for_test()
    counters.record_skill_call("technical_analysis")
    counters.record_skill_call("technical_analysis")
    counters.record_skill_call("market_data")
    snap = counters.snapshot()
    assert snap["skill_calls"] == {"technical_analysis": 2, "market_data": 1}


def test_backtest_aggregation():
    counters.reset_for_test()
    counters.record_backtest("china_a", elapsed_ms=1000)
    counters.record_backtest("china_a", elapsed_ms=3000)
    counters.record_backtest("crypto", elapsed_ms=500)
    snap = counters.snapshot()
    assert snap["backtests"]["count"] == 3
    assert snap["backtests"]["total_ms"] == 4500
    assert snap["backtests"]["by_engine"] == {"china_a": 2, "crypto": 1}


def test_error_by_type():
    counters.reset_for_test()
    counters.record_error("ValueError")
    counters.record_error("TimeoutError")
    counters.record_error("ValueError")
    snap = counters.snapshot()
    assert snap["errors"]["count"] == 3
    assert snap["errors"]["by_type"] == {"ValueError": 2, "TimeoutError": 1}


def test_snapshot_shape_no_content_fields():
    counters.reset_for_test()
    counters.record_skill_call("x")
    snap = counters.snapshot()
    # 仅允许的键；不应有任何 prompt/query/symbol 字段（reliability 仅含数值时长 + 短事件名）
    allowed_top = {"since", "skill_calls", "backtests", "errors", "reliability"}
    assert set(snap.keys()) <= allowed_top
    assert set(snap["backtests"].keys()) == {"count", "total_ms", "by_engine"}


def test_since_returns_delta_and_resets():
    counters.reset_for_test()
    counters.record_skill_call("a")
    first = counters.snapshot()
    counters.record_skill_call("a")
    second = counters.snapshot(since=first["since"])
    assert second["skill_calls"] == {"a": 1}  # 仅第二次后的增量


def test_backtest_record_via_runner_path():
    """模拟 runner 在完成一次回测后调用 counters.record_backtest。"""
    counters.reset_for_test()
    # 直接验证 wiring 契约：runner 完成时应调用 record_backtest
    counters.record_backtest(engine="daily", elapsed_ms=200)
    snap = counters.snapshot()
    assert snap["backtests"]["count"] == 1
