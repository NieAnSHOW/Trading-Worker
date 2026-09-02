"""Pi sidecar 协议编解码单测。与 pi-sidecar/src/protocol.test.ts 镜像。"""

import pytest

from src.pi_sidecar import LEGACY_ONLY_PROVIDERS, get_agent_engine
from src.pi_sidecar.protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    LineSplitter,
    decode_frame,
    encode_frame,
)
from src.pi_sidecar.protocol import FrameError


class TestEncodeFrame:
    def test_appends_lf_and_round_trips(self):
        req = {"v": 1, "id": "r-1", "op": "ping"}
        line = encode_frame(req)
        assert line.endswith("\n")
        assert decode_frame(line) == req

    def test_rejects_frames_over_1mib(self):
        big = {"v": 1, "id": "r-1", "op": "x", "params": {"blob": "a" * MAX_FRAME_BYTES}}
        with pytest.raises(FrameError) as ei:
            encode_frame(big)
        assert ei.value.code == "frame_too_large"


class TestDecodeFrame:
    def test_bad_json(self):
        with pytest.raises(FrameError) as ei:
            decode_frame("{nope")
        assert ei.value.code == "bad_json"

    def test_empty_line(self):
        with pytest.raises(FrameError) as ei:
            decode_frame("   ")
        assert ei.value.code == "bad_json"

    @pytest.mark.parametrize(
        "bad",
        [
            '{"v":2,"id":"x","op":"ping"}',
            '{"id":"x","op":"ping"}',
            '{"v":1,"id":1,"op":"ping"}',
            '{"v":1,"event":42}',
            '{"v":1,"id":"x","ok":false,"error":{"code":"x"}}',
        ],
    )
    def test_wrong_shape_is_bad_frame(self, bad):
        with pytest.raises(FrameError) as ei:
            decode_frame(bad)
        assert ei.value.code == "bad_frame"

    def test_accepts_all_four_frame_kinds(self):
        assert decode_frame('{"v":1,"id":"r-1","op":"ping","params":{"a":1}}')["op"] == "ping"
        assert decode_frame('{"v":1,"id":"r-1","ok":true,"result":{}}')["ok"] is True
        assert decode_frame('{"v":1,"id":"r-1","ok":false,"error":{"code":"x","message":"y"}}')["ok"] is False
        assert decode_frame('{"v":1,"event":"ready","data":{"protocol":1}}')["event"] == "ready"


class TestLineSplitter:
    def test_splits_incremental_chunks_on_lf(self):
        s = LineSplitter()
        assert s.push('{"v":1,"id":"a') == []
        assert s.push('","op":"ping"}\n{"v":1,"id":"b","op":"p"}\n') == [
            '{"v":1,"id":"a","op":"ping"}',
            '{"v":1,"id":"b","op":"p"}',
        ]

    def test_oversized_line(self):
        s = LineSplitter()
        with pytest.raises(FrameError) as ei:
            s.push("x" * (MAX_FRAME_BYTES + 1))
        assert ei.value.code == "frame_too_large"

    def test_protocol_version_constant(self):
        assert PROTOCOL_VERSION == 1


class TestEngineFlag:
    def test_default_is_pi(self, monkeypatch):
        monkeypatch.delenv("VIBE_AGENT_ENGINE", raising=False)
        assert get_agent_engine() == "pi"

    def test_legacy_explicit(self, monkeypatch):
        monkeypatch.setenv("VIBE_AGENT_ENGINE", "legacy")
        assert get_agent_engine() == "legacy"

    def test_invalid_value_falls_back_to_pi_never_legacy(self, monkeypatch):
        monkeypatch.setenv("VIBE_AGENT_ENGINE", "bogus")
        assert get_agent_engine() == "pi"

    def test_legacy_only_providers(self):
        assert LEGACY_ONLY_PROVIDERS == frozenset({"vip_server", "openai-codex"})
