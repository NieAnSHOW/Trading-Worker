# agent/tests/test_telemetry_api.py
import pytest
from fastapi.testclient import TestClient
import api_server
from src.telemetry import counters


@pytest.fixture()
def client():
    return TestClient(api_server.app)


def test_sidecar_metrics_route_returns_aggregates(client):
    counters.reset_for_test()
    counters.record_skill_call("technical_analysis")
    counters.record_backtest("china_a", elapsed_ms=500)
    counters.record_error("ValueError")
    resp = client.get("/telemetry/sidecar-metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["skill_calls"] == {"technical_analysis": 1}
    assert body["backtests"]["count"] == 1
    assert body["backtests"]["total_ms"] == 500
    assert body["backtests"]["by_engine"] == {"china_a": 1}
    assert body["errors"]["count"] == 1
    assert body["errors"]["by_type"] == {"ValueError": 1}


def test_sidecar_metrics_no_content_fields(client):
    counters.reset_for_test()
    counters.record_skill_call("x")
    body = client.get("/telemetry/sidecar-metrics").json()
    # 隐私边界：响应键集合受限（reliability 仅含数值时长 + 短事件名）
    assert set(body.keys()) <= {"since", "skill_calls", "backtests", "errors", "reliability"}
    flat = str(body)
    # 不得包含任何查询/标的内容（黑名单抽样）
    for taboo in ["prompt", "query", "symbol", "amount", "600519", "茅台"]:
        assert taboo not in flat


def test_sidecar_metrics_requires_no_auth(client):
    """§6.1：路由无 require_auth，匿名回环可访问。"""
    counters.reset_for_test()
    resp = client.get("/telemetry/sidecar-metrics")  # 不带任何 token / API key
    assert resp.status_code == 200


def test_sidecar_metrics_since_incremental(client):
    """§6.1：?since= 增量窗口——第二次调用仅返回新计数。"""
    counters.reset_for_test()
    counters.record_skill_call("a")
    first = client.get("/telemetry/sidecar-metrics").json()
    # snapshot 后窗口重置
    counters.record_skill_call("b")
    second = client.get("/telemetry/sidecar-metrics", params={"since": first["since"]}).json()
    assert second["skill_calls"] == {"b": 1}
