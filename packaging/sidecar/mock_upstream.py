"""Tiny mock OpenAI-compatible upstream for smoke tests.

Serves POST /v1/chat/completions (stream + non-stream). Records the byte size
and token-ish length of each request body at GET /__last so the smoke test can
assert the proxy actually compressed the context it forwarded.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

LAST: dict = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path == "/__last":
            body = json.dumps(LAST).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        LAST.clear()
        LAST.update(
            {
                "path": self.path,
                "bytes": length,
                "message_chars": sum(
                    len(str(m.get("content", ""))) for m in payload.get("messages", [])
                ),
                "stream": bool(payload.get("stream")),
            }
        )
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "hello "}}]},
                {"choices": [{"index": 0, "delta": {"content": "from mock"}}]},
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 42, "completion_tokens": 3},
                },
            ]
            for c in chunks:
                c.setdefault("object", "chat.completion.chunk")
                c.setdefault("model", payload.get("model", "mock"))
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps(
                {
                    "object": "chat.completion",
                    "model": payload.get("model", "mock"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hello from mock"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 42, "completion_tokens": 3},
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9099
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
