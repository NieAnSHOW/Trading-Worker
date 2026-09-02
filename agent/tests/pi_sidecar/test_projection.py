"""投影单测：AgentLoop.run() 兼容 result + Message 内存投影。"""

from src.pi_sidecar.projection import build_attempt_result, pi_messages_to_store_messages
from src.session.models import Message


class TestBuildAttemptResult:
    def test_full_shape(self):
        r = build_attempt_result(
            status="success",
            content="done",
            run_dir="/runs/20260830_1_abc123",
            react_trace=[{"type": "answer", "content": "done"}],
            iterations=3,
            metrics={"sharpe": 1.2},
            reliability=None,
            pi_meta={
                "session_file": "/pi/s.jsonl",
                "pi_session_id": "a1b2c3d4e5f6",
                "entry_ids": ["e1"],
                "tool_call_ids": ["c1"],
            },
        )
        assert r["status"] == "success"
        assert r["run_id"] == "20260830_1_abc123"
        assert r["max_iterations"] == 50
        assert r["metrics"] == {"sharpe": 1.2}
        assert r["pi"]["pi_session_id"] == "a1b2c3d4e5f6"
        assert "reliability" not in r

    def test_reliability_included_when_present(self):
        r = build_attempt_result(
            status="success",
            content="x",
            run_dir="/r/a_b",
            react_trace=[],
            iterations=1,
            metrics=None,
            reliability={"mode": "shadow"},
        )
        assert r["reliability"] == {"mode": "shadow"}

    def test_failure_with_reason_passthrough(self):
        r = build_attempt_result(
            status="failed",
            content="",
            run_dir="/r/a_b",
            react_trace=[],
            iterations=2,
            metrics=None,
            reason="model exploded",
        )
        assert r["status"] == "failed"
        assert r["reason"] == "model exploded"


class TestPiMessagesToStoreMessages:
    def test_projects_entries_to_messages_without_disk_write(self):
        entries = [
            {"entry_id": "e1", "role": "user", "content": "hello", "timestamp": "2026-08-30T00:00:00Z"},
            {"entry_id": "e2", "role": "assistant", "content": "hi there", "timestamp": "2026-08-30T00:00:01Z"},
        ]
        msgs = pi_messages_to_store_messages("sess123", entries)
        assert len(msgs) == 2
        assert all(isinstance(m, Message) for m in msgs)
        assert msgs[0].session_id == "sess123"
        assert msgs[0].role == "user" and msgs[0].content == "hello"
        assert msgs[1].metadata == {"pi_entry_id": "e2"}

    def test_empty_entries(self):
        assert pi_messages_to_store_messages("s", []) == []
