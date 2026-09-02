"""事件归一化单测：Pi 事件 → 完整 Vibe glossary（recon §Event name glossary）。"""

import json

from src.pi_sidecar.events import NormState, normalize_event


def test_text_delta():
    st = NormState()
    st.iter = 3
    out = normalize_event(
        "message_update", {"assistantMessageEvent": {"type": "text_delta", "delta": "你好"}}, state=st
    )
    assert out == [("text_delta", {"delta": "你好", "iter": 3})]


def test_thinking_delta_accumulates_chars():
    st = NormState()
    st.iter = 1
    out = normalize_event(
        "message_update", {"assistantMessageEvent": {"type": "thinking_delta", "delta": "abc"}}, state=st
    )
    assert out == [("reasoning_delta", {"iter": 1, "chars": 3})]
    out = normalize_event(
        "message_update", {"assistantMessageEvent": {"type": "thinking_delta", "delta": "de"}}, state=st
    )
    assert out == [("reasoning_delta", {"iter": 1, "chars": 5})]  # 累计，非文本（recon glossary）


def test_message_end_synthesizes_thinking_done_and_llm_usage():
    st = NormState()
    st.iter = 2
    st.provider = "openai"
    st.model = "gpt-4o"
    out = normalize_event(
        "message_end",
        {
            "message": {
                "role": "assistant",
                "provider": "openai",
                "model": "gpt-4o",
                "content": [
                    {"type": "thinking", "thinking": "thought " * 100},
                    {"type": "text", "text": "answer text"},
                ],
                "usage": {"input": 100, "output": 20, "cacheRead": 0, "cacheWrite": 0},
                "stopReason": "stop",
            }
        },
        state=st,
    )
    names = [n for n, _ in out]
    assert "thinking_done" in names
    assert "llm_usage" in names
    td = dict(out[names.index("thinking_done")][1])
    assert td["iter"] == 2
    assert len(td["content"]) <= 500 and td["content"].startswith("thought")
    lu = dict(out[names.index("llm_usage")][1])
    assert lu["iter"] == 2 and lu["provider"] == "openai" and lu["model"] == "gpt-4o"
    assert lu["input"] == 100 and lu["output"] == 20 and lu["metering_eligible"] is False
    # assistant 最终文本经 text_delta 已流出；message_end 不重复发 text_delta


def test_tool_execution_start_maps_to_tool_call():
    st = NormState()
    st.iter = 4
    out = normalize_event(
        "tool_execution_start",
        {"toolName": "web_search", "toolCallId": "c1", "args": {"query": "AAPL", "api_key": "sk-secret"}},
        state=st,
    )
    assert out == [
        (
            "tool_call",
            {
                "tool": "web_search",
                "arguments": {"query": "AAPL", "api_key": "[redacted]"},
                "iter": 4,
                "tool_call_id": "c1",
            },
        )
    ]
    assert st.tool_started["c1"] > 0  # 记录开始时间


def test_tool_execution_end_maps_to_tool_result_with_redacted_preview():
    st = NormState()
    st.iter = 4
    st.tool_started["c1"] = 1000.0
    body = json.dumps({"result": "x" * 500, "audit_id": "la_abc123"})
    out = normalize_event(
        "tool_execution_end",
        {
            "toolName": "web_search",
            "toolCallId": "c1",
            "isError": False,
            "result": {"content": [{"type": "text", "text": body}]},
        },
        state=st,
    )
    name, data = out[0]
    assert name == "tool_result"
    assert data["tool"] == "web_search"
    assert data["status"] == "ok"
    assert data["elapsed_ms"] >= 0
    assert len(data["preview"]) <= 200
    assert "la_abc123" in data["preview"]  # sessions_routes 依赖 preview 匹配派生 mandate/live 帧


def test_tool_execution_update_maps_to_tool_progress():
    st = NormState()
    st.iter = 1
    out = normalize_event(
        "tool_execution_update",
        {
            "toolName": "backtest_runner",
            "toolCallId": "c2",
            "partial": {"message": "step 2/10", "current": 2, "total": 10},
        },
        state=st,
    )
    name, data = out[0]
    assert name == "tool_progress"
    assert data["tool"] == "backtest_runner"
    assert data["stage"] == "progress"
    assert data["message"] == "step 2/10"
    assert data["current"] == 2 and data["total"] == 10
    assert "ts" in data


def test_compaction_events():
    st = NormState()
    out = normalize_event("auto_compaction_start", {"tokensBefore": 42000}, state=st)
    assert out == [("compact", {"phase": "start", "tokens_before": 42000})]
    out = normalize_event("auto_compaction_end", {"tokensBefore": 42000, "summary": "s" * 300}, state=st)
    assert out == [("compact", {"phase": "end", "tokens_before": 42000, "summary": "s" * 200})]  # summary[:200]


def test_retry_events_map_to_stream_reset_and_notice():
    st = NormState()
    st.iter = 5
    st.provider = "openai"
    st.model = "gpt-4o"
    out = normalize_event("auto_retry_start", {"attempt": 1}, state=st)
    assert out[0][0] == "stream_reset"
    assert out[0][1]["reason"] == "provider_stream_retry"
    assert out[0][1]["iter"] == 5 and out[0][1]["provider"] == "openai" and out[0][1]["model"] == "gpt-4o"
    out = normalize_event("retry_fallback_succeeded", {"from": "openai/gpt-4o", "to": "openai/gpt-4o-mini"}, state=st)
    assert out[0][0] == "pi.notice"
    assert out[0][1]["kind"] == "retry_fallback_succeeded"


def test_agent_end_terminal_emits_nothing():
    st = NormState()
    assert normalize_event("agent_end", {"isTerminal": True}, state=st) == []
    assert normalize_event("agent_end", {}, state=st) == []  # absent = terminal（SDK facts）


def test_agent_end_non_terminal_emits_nothing_but_client_handles():
    st = NormState()
    assert normalize_event("agent_end", {"isTerminal": False}, state=st) == []


def test_model_changed_emits_notice():
    st = NormState()
    out = normalize_event("model_changed", {"model": "openai/gpt-4o"}, state=st)
    assert out == [("pi.notice", {"kind": "model_changed", "message": "model changed to openai/gpt-4o"})]
