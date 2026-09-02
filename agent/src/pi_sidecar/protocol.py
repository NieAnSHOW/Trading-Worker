"""Pi sidecar JSONL 帧协议（与 pi-sidecar/src/protocol.ts 镜像）。

严格 LF 分隔 JSONL；单帧序列化后 ≤ MAX_FRAME_BYTES 字节（UTF-8）；
v1 超限不做分块，直接 FrameError("frame_too_large")。
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1_048_576


class FrameError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def encode_frame(frame: dict[str, Any]) -> str:
    line = json.dumps(frame, ensure_ascii=False)
    if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
        raise FrameError("frame_too_large", f"frame exceeds {MAX_FRAME_BYTES} bytes")
    return line + "\n"


def _nonempty_str(x: Any) -> bool:
    return isinstance(x, str) and len(x) > 0


def decode_frame(line: str) -> dict[str, Any]:
    trimmed = line.strip()
    if not trimmed:
        raise FrameError("bad_json", "empty frame")
    try:
        obj = json.loads(trimmed)
    except json.JSONDecodeError as exc:
        raise FrameError("bad_json", f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise FrameError("bad_frame", "frame is not an object")
    if obj.get("v") != PROTOCOL_VERSION:
        raise FrameError("bad_frame", f"unsupported v: {obj.get('v')!r}")
    if "event" in obj:
        if not _nonempty_str(obj.get("event")):
            raise FrameError("bad_frame", "event must be a non-empty string")
        sid = obj.get("session_id")
        if sid is not None and not _nonempty_str(sid):
            raise FrameError("bad_frame", "bad session_id")
        return obj
    if not _nonempty_str(obj.get("id")):
        raise FrameError("bad_frame", "id must be a non-empty string")
    if "op" in obj:
        if not _nonempty_str(obj.get("op")):
            raise FrameError("bad_frame", "op must be a non-empty string")
        params = obj.get("params")
        if params is not None and not isinstance(params, dict):
            raise FrameError("bad_frame", "params must be an object")
        return obj
    if "ok" in obj:
        if obj["ok"] is True:
            return obj
        if obj["ok"] is False:
            err = obj.get("error")
            if (
                not isinstance(err, dict)
                or not _nonempty_str(err.get("code"))
                or not isinstance(err.get("message"), str)
            ):
                raise FrameError("bad_frame", "error frame needs {code,message}")
            return obj
        raise FrameError("bad_frame", "ok must be boolean")
    raise FrameError("bad_frame", "frame is neither request, response nor event")


class LineSplitter:
    def __init__(self) -> None:
        self._buf = ""

    def push(self, chunk: str) -> list[str]:
        self._buf += chunk
        if "\n" not in self._buf and len(self._buf.encode("utf-8", "ignore")) > MAX_FRAME_BYTES:
            self._buf = ""
            raise FrameError("frame_too_large", f"line exceeds {MAX_FRAME_BYTES} bytes")
        out: list[str] = []
        while True:
            idx = self._buf.find("\n")
            if idx < 0:
                break
            out.append(self._buf[:idx])
            self._buf = self._buf[idx + 1 :]
        return out
