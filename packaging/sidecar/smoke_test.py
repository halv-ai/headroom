"""Smoke test for the headroom-runtime sidecar.

Boots the runtime (frozen onedir binary, or unfrozen entry.py via
--python <venv-python>), points its OpenAI upstream at a local mock, then:
  1. asserts /livez returns healthy
  2. sends a fat, repetitive chat payload through /v1/chat/completions
  3. asserts the response carries x-headroom-* savings headers OR the mock
     received fewer message chars than we sent (compression happened)
  4. asserts streaming SSE passes through intact

Usage:
  python smoke_test.py dist/headroom-runtime/headroom-runtime
  python smoke_test.py --python .venv/bin/python
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_http(url: str, timeout: float = 120.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise SystemExit(f"timeout waiting for {url}")


def main() -> None:
    if sys.argv[1] == "--python":
        runtime_cmd = [sys.argv[2], os.path.join(HERE, "entry.py")]
        mock_python = sys.argv[2]
    else:
        runtime_cmd = [sys.argv[1]]
        mock_python = sys.executable

    tmp = tempfile.mkdtemp(prefix="headroom-smoke-")
    # Model downloads (HF_HOME) are reused across runs via a cache dir so the
    # smoke test doesn't re-download weights every invocation. Override with
    # HEADROOM_SMOKE_CACHE; defaults to .smoke-cache/ next to this file
    # (gitignored).
    cache_dir = os.environ.get("HEADROOM_SMOKE_CACHE") or os.path.join(HERE, ".smoke-cache")
    os.makedirs(os.path.join(cache_dir, "hf"), exist_ok=True)
    env = {
        **os.environ,
        "HF_HOME": os.path.join(cache_dir, "hf"),
        "HEADROOM_WORKSPACE_DIR": os.path.join(tmp, "ws"),
        "HEADROOM_TELEMETRY": "off",
    }

    mock_port = free_port()
    proxy_port = free_port()
    mock = subprocess.Popen([mock_python, os.path.join(HERE, "mock_upstream.py"), str(mock_port)])
    env["OPENAI_TARGET_API_URL"] = f"http://127.0.0.1:{mock_port}"

    proxy = subprocess.Popen(
        [*runtime_cmd, "proxy", "--port", str(proxy_port), "--host", "127.0.0.1"],
        env=env,
    )
    try:
        wait_http(f"http://127.0.0.1:{proxy_port}/livez")
        print("PASS /livez healthy")

        # Fat, repetitive payload: ideal compression fodder.
        #
        # Shape matters. Headroom's proxy is built for agent traffic and only
        # compresses the "live zone" of a conversation:
        #   * Plain user messages are never candidates — non-trailing ones are
        #     frozen as prefix-cache-stable (CompressionCache.compute_frozen_count
        #     treats user/assistant/system as always stable), and even live
        #     ones are skipped by ContentRouterConfig.skip_user_messages=True.
        #   * A first-sight tool result (role="tool") IS the live zone and is
        #     the designed compression target. A pure JSON dump routes to
        #     SmartCrusher — structural compression, no ML model download.
        #   * The tool name must not be in DEFAULT_EXCLUDE_TOOLS
        #     (Read/Glob/Grep/Write/Edit are protected verbatim).
        # So the payload mimics one agent turn: ask → tool call → fat JSON
        # tool result → follow-up question.
        blob = json.dumps({"rows": [{"id": i, "name": f"user{i}", "role": "admin"} for i in range(400)]})
        messages = [
            {"role": "user", "content": "Run the users dump query and summarize it."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_smoke_1",
                        "type": "function",
                        "function": {"name": "db_query", "arguments": "{\"table\": \"users\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_smoke_1", "content": blob},
            {"role": "user", "content": "Summarize the dump above in one word."},
        ]
        sent_chars = sum(len(m["content"]) for m in messages)

        body = json.dumps(
            {"model": "gpt-5.2", "messages": messages, "stream": False}
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            data=body,
            headers={"content-type": "application/json", "authorization": "Bearer sk-smoke"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
            headers = {k.lower(): v for k, v in r.headers.items()}
        assert resp["choices"][0]["message"]["content"], "no completion content"
        print("PASS completion round-trip")

        saved_hdr = headers.get("x-headroom-tokens-saved")
        with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/__last", timeout=5) as r:
            last = json.loads(r.read())
        forwarded_chars = last["message_chars"]
        print(f"info: sent {sent_chars} chars, upstream saw {forwarded_chars}; "
              f"x-headroom-tokens-saved={saved_hdr}")
        assert (saved_hdr and int(saved_hdr) > 0) or forwarded_chars < sent_chars, (
            "no compression observed"
        )
        print("PASS compression observed")

        # Streaming pass-through.
        body = json.dumps({"model": "gpt-5.2", "messages": messages, "stream": True}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            data=body,
            headers={"content-type": "application/json", "authorization": "Bearer sk-smoke"},
        )
        text = ""
        with urllib.request.urlopen(req, timeout=120) as r:
            for line in r:
                line = line.decode().strip()
                if line.startswith("data:") and "[DONE]" not in line:
                    chunk = json.loads(line[5:])
                    delta = chunk["choices"][0]["delta"].get("content")
                    if delta:
                        text += delta
        assert text == "hello from mock", f"unexpected streamed text: {text!r}"
        print("PASS streaming pass-through")

        stats = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{proxy_port}/stats", timeout=10
            ).read()
        )
        assert "tokens" in stats or "savings" in stats, "stats endpoint malformed"
        print("PASS /stats")
        print("SMOKE OK")
    finally:
        proxy.terminate()
        mock.terminate()


if __name__ == "__main__":
    main()
