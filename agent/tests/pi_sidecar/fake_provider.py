"""确定性 OpenAI 兼容 SSE stub provider（design §Verification: integration tests）。"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpenAIProvider:
    def __init__(
        self,
        responses: list[dict] | None = None,
        model: str = "fake-model",
        resolver: "Callable[[dict], dict] | None" = None,
    ):
        # resolver: Callable[[request_body], dict] — 按请求内容脚本化响应（perf A/B 用）；
        # 缺省 None 时保持原行为：按 assistant 消息计数在 responses 里线性取。
        self.responses = responses or []
        self.model = model
        self.resolver = resolver
        self.requests: list[dict] = []
        # brief 原文传 self._handler（方法对象）→ 服务器按 (request, client_address, server) 调用
        # 报 "takes 1 positional argument but 4 were given"；须调用 _handler() 取回内部 handler 类。
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]  # type: ignore[return-value]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()

    def _assistant_count(self, messages: list[dict]) -> int:
        return sum(1 for m in messages if m.get("role") == "assistant")

    def _handler(self) -> type[BaseHTTPRequestHandler]:  # noqa: C901 — 紧凑 SSE handler
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                script = (
                    outer.resolver(body)
                    if outer.resolver is not None
                    else outer.responses[
                        min(outer._assistant_count(body.get("messages", [])), len(outer.responses) - 1)
                    ]
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def emit(chunk: dict):
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

                emit(
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion.chunk",
                        "model": outer.model,
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    }
                )
                if "tool_calls" in script:
                    for i, tc in enumerate(script["tool_calls"]):
                        emit(
                            {
                                "id": "chatcmpl-1",
                                "object": "chat.completion.chunk",
                                "model": outer.model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "tool_calls": [
                                                {
                                                    "index": i,
                                                    "id": tc["id"],
                                                    "type": "function",
                                                    "function": {
                                                        "name": tc["name"],
                                                        "arguments": json.dumps(tc["arguments"]),
                                                    },
                                                }
                                            ],
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            }
                        )
                    finish = "tool_calls"
                else:
                    emit(
                        {
                            "id": "chatcmpl-1",
                            "object": "chat.completion.chunk",
                            "model": outer.model,
                            "choices": [
                                {"index": 0, "delta": {"content": script.get("text", "")}, "finish_reason": None}
                            ],
                        }
                    )
                    finish = "stop"
                emit(
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion.chunk",
                        "model": outer.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    }
                )
                self.wfile.write(b"data: [DONE]\n\n")

        return H
