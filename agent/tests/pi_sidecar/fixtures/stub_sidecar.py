"""协议桩 sidecar：client 单测用（不依赖 bun）。

env 控制行为：
  STUB_CRASH_AFTER=<n>     处理第 n 个请求后进程退出（模拟 sidecar 崩溃）
  STUB_SEND_HOST_REQ=<op>  处理完第 1 个请求后，向 stdout 发出一个入站 host 请求
                           （op ∈ tool_invoke/memory_context），并校验收到的响应帧
  VIBE_TEST_ENV_MARKER     原样回显进 ready 帧 sdk 字段（B6：env 透传断言）
"""

from __future__ import annotations

import os
import sys

from src.pi_sidecar.protocol import LineSplitter, decode_frame, encode_frame


def main() -> None:
    crash_after = int(os.environ.get("STUB_CRASH_AFTER", "0"))
    send_host_req = os.environ.get("STUB_SEND_HOST_REQ", "")
    marker = os.environ.get("VIBE_TEST_ENV_MARKER", "stub")
    out = sys.stdout
    out.write(encode_frame({"v": 1, "event": "ready", "data": {"protocol": 1, "pid": os.getpid(), "sdk": marker}}))
    out.flush()
    splitter = LineSplitter()
    handled = 0
    host_req_sent = False
    for raw in iter(sys.stdin.readline, ""):
        for line in splitter.push(raw):
            try:
                frame = decode_frame(line)
            except Exception:  # noqa: BLE001
                continue
            handled += 1
            if crash_after and handled >= crash_after:
                out.flush()
                os._exit(9)
            op = frame.get("op")
            if op == "ping":
                out.write(
                    encode_frame({"v": 1, "id": frame["id"], "ok": True, "result": {"pong": True, "protocol": 1}})
                )
            elif op == "open_session":
                out.write(
                    encode_frame({"v": 1, "id": frame["id"], "ok": True, "result": {"session_file": "/tmp/stub.jsonl"}})
                )
            else:
                out.write(
                    encode_frame(
                        {"v": 1, "id": frame["id"], "ok": False, "error": {"code": "unknown_op", "message": "stub"}}
                    )
                )
            out.flush()
            # 首个请求处理后发一个入站 host 请求（B3），等待并校验响应帧
            if send_host_req and not host_req_sent:
                host_req_sent = True
                out.write(
                    encode_frame(
                        {
                            "v": 1,
                            "id": "s-host-1",
                            "op": send_host_req,
                            "params": {
                                "call_id": "t-1",
                                "toolName": "web_search",
                                "arguments": {"q": "x"},
                                "idempotencyKey": "s:e:c1",
                            },
                        }
                    )
                )
                out.flush()
                resp_raw = sys.stdin.readline()
                resp = decode_frame(resp_raw)
                assert resp.get("id") == "s-host-1" and resp.get("ok") is True, resp
                assert "result" in resp, resp
                # 把校验结果作为事件发回给测试进程观测
                out.write(
                    encode_frame(
                        {"v": 1, "event": "host_roundtrip", "data": {"ok": True, "result": resp.get("result")}}
                    )
                )
                out.flush()


if __name__ == "__main__":
    main()
