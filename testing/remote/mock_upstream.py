"""Deterministic OpenAI-compatible stub upstream, for harness runs.

Runs on the target host so the GitInTheVan instance under test can reach it at
127.0.0.1. Exists so a test run needs no real provider: Endpoint.api_key is
stored in plaintext, so replicating live endpoints would copy billable
credentials onto throwaway machines and leave them in the test database.

Deliberately stdlib-only. It is copied to a freshly cloned tree and started
before the app's dependencies are guaranteed to be importable.

Usage:
    python mock_upstream.py [--port 8199]
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mock-model-v1"

# Echoing part of the prompt back lets pipeline tests assert that injection
# actually reached the upstream, rather than only that a call was made.
REPLY_TEMPLATE = "MOCK_REPLY: {seen}"
MAX_ECHO = 200


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):  # multimodal content blocks
                content = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            return str(content)[:MAX_ECHO]
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        # Quiet by default; the harness captures the app's logs, not ours.
        pass

    def _send(self, code: int, payload: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 - stdlib signature
        if self.path.rstrip("/") in ("/health", ""):
            self._send(200, b'{"status":"ok"}', "application/json")
            return
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({
                "object": "list",
                "data": [{"id": MODEL, "object": "model", "owned_by": "mock"}],
            }).encode()
            self._send(200, body, "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self):  # noqa: N802 - stdlib signature
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"invalid json"}', "application/json")
            return

        reply = REPLY_TEMPLATE.format(seen=_last_user_text(body.get("messages", [])))

        if body.get("stream"):
            self._stream(reply)
        else:
            self._complete(reply)

    def _complete(self, reply: str) -> None:
        payload = json.dumps({
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self._send(200, payload, "application/json")

    def _stream(self, reply: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta: dict, finish=None) -> bytes:
            frame = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return b"data: " + json.dumps(frame).encode() + b"\n\n"

        self.wfile.write(chunk({"role": "assistant"}))
        # Word-at-a-time so the proxy's SSE handling sees several real deltas.
        for word in reply.split(" "):
            self.wfile.write(chunk({"content": word + " "}))
            self.wfile.flush()
        self.wfile.write(chunk({}, finish="stop"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible upstream")
    parser.add_argument("--port", type=int, default=8199)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock upstream listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
