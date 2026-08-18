"""Unit 3: WebSocket session lifecycle + deterministic relay cancellation.

These tests exercise the Codex WS handler with a fake upstream and a
fake client WebSocket so we can drive the relay halves through their
real code paths (not mocked) and assert on registry / task state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import headroom.proxy.handlers.openai as openai_module
from headroom.proxy.handlers.openai import OpenAIHandlerMixin
from headroom.proxy.ws_session_registry import WebSocketSessionRegistry
from headroom.transforms.content_router import (
    CompressionStrategy,
    ContentRouter,
    RouterCompressionResult,
)

CODEX_WS_EXEMPT_TRANSFORM = "router:openai:responses:function_call_output:codex_ws_exempt"
CONTENT_EXEMPT_TRANSFORM = "router:openai:responses:content_exempt"

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _TokenCounter:
    def count_text(self, text: str) -> int:
        return len(text.split())


class _DummyMetrics:
    def __init__(self) -> None:
        self.active_ws_sessions = 0
        self.active_ws_sessions_max = 0
        self.active_relay_tasks = 0
        self.ws_session_durations: list[float] = []
        self.stage_timings: list[tuple[str, dict[str, float]]] = []
        self.termination_causes: list[str] = []
        self.recorded_requests: list[dict] = []
        self.codex_ws_frames: list[dict] = []

    async def record_request(self, **kwargs):  # pragma: no cover
        self.recorded_requests.append(dict(kwargs))
        return None

    async def record_stage_timings(self, path: str, timings: dict[str, float]) -> None:
        self.stage_timings.append((path, dict(timings)))

    def inc_active_ws_sessions(self) -> None:
        self.active_ws_sessions += 1
        self.active_ws_sessions_max = max(self.active_ws_sessions_max, self.active_ws_sessions)

    def dec_active_ws_sessions(self) -> None:
        self.active_ws_sessions = max(0, self.active_ws_sessions - 1)

    def inc_active_relay_tasks(self, n: int = 1) -> None:
        self.active_relay_tasks += n

    def dec_active_relay_tasks(self, n: int = 1) -> None:
        self.active_relay_tasks = max(0, self.active_relay_tasks - n)

    def record_ws_session_duration(self, duration_ms: float, cause: str) -> None:
        self.ws_session_durations.append(duration_ms)
        self.termination_causes.append(cause)

    def record_codex_ws_frame(self, **kwargs) -> None:
        self.codex_ws_frames.append(dict(kwargs))


class _DummyOpenAIHandler(OpenAIHandlerMixin):
    OPENAI_API_URL = "https://api.openai.com"

    def __init__(self, ws_sessions: WebSocketSessionRegistry | None = None) -> None:
        self.rate_limiter = None
        self.metrics = _DummyMetrics()
        self.config = SimpleNamespace(
            optimize=False,
            retry_max_attempts=1,
            retry_base_delay_ms=1,
            retry_max_delay_ms=1,
            connect_timeout_seconds=10,
            openai_extra_headers=None,
        )
        self.usage_reporter = None
        self.openai_provider = SimpleNamespace(
            get_context_limit=lambda model: 128_000,
            get_token_counter=lambda model: _TokenCounter(),
        )
        self.openai_pipeline = SimpleNamespace(apply=MagicMock())
        self.anthropic_backend = None
        self.cost_tracker = None
        self.memory_handler = None
        self.ws_sessions = ws_sessions or WebSocketSessionRegistry()
        self.compression_executor_calls = 0
        self.compression_executor_timeouts: list[float] = []

    async def _next_request_id(self) -> str:
        return "req-lifecycle-test"

    async def _run_compression_in_executor(self, fn, *, timeout: float):
        self.compression_executor_calls += 1
        self.compression_executor_timeouts.append(timeout)
        return fn()

    async def _record_request_outcome(self, outcome) -> None:
        # Mirror of ``HeadroomProxy._record_request_outcome`` for the
        # mixin tests. Delegates to the free funnel function so the
        # wire shape is identical to production.
        from headroom.proxy.outcome import emit_request_outcome

        await emit_request_outcome(self, outcome)


class _FakeWebSocketDisconnect(Exception):
    """Mirrors the ``WebSocketDisconnect`` type-name check in the handler.

    The production code identifies "normal client gone" by
    ``"WebSocketDisconnect" in type(e).__name__`` — so the fake exception
    type name must start with ``WebSocketDisconnect``.
    """


# Force the type-name substring match in the handler.
_FakeWebSocketDisconnect.__name__ = "WebSocketDisconnect_Fake"


class _FakeUpstreamClose(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _FakeWebSocket:
    """Scripted client WebSocket that can delay / disconnect mid-stream."""

    def __init__(
        self,
        frames: list[str] | None = None,
        *,
        headers: dict[str, str] | None = None,
        disconnect_after_n_sends: int | None = None,
        hold_after_initial: bool = False,
        call_log: list[str] | None = None,
    ) -> None:
        self.headers = dict(headers or {"authorization": "Bearer test"})
        self._frames = list(frames or [])
        self._hold_after_initial = hold_after_initial
        self._disconnect_after_n_sends = disconnect_after_n_sends
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.accepted_subprotocol: str | None = None
        self.accepted_headers: list[tuple[bytes, bytes]] | None = None
        self.accepted_event = asyncio.Event()
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._call_log = call_log
        # "client" can trip this event to simulate mid-stream disconnect.
        self._disconnect_event = asyncio.Event()
        self.client = SimpleNamespace(host="127.0.0.1", port=12345)

    async def accept(self, subprotocol=None, headers=None) -> None:
        self.accepted_subprotocol = subprotocol
        self.accepted_headers = list(headers) if headers is not None else None
        self.accepted_event.set()
        if self._call_log is not None:
            self._call_log.append("accept")

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        if self._hold_after_initial:
            # Wait for simulated client disconnect.
            await self._disconnect_event.wait()
        # Use an exception type whose name starts with ``WebSocketDisconnect``
        # so the handler's ``type(e).__name__`` check classifies this as a
        # normal client exit (not a ``client_error``).
        raise _FakeWebSocketDisconnect("client closed")

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)
        if (
            self._disconnect_after_n_sends is not None
            and len(self.sent_text) >= self._disconnect_after_n_sends
        ):
            # Trigger the "client gone" signal the next receive_text will see.
            self._disconnect_event.set()

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = True
        if code is not None or self.close_code is None:
            self.close_code = code
        if reason is not None or self.close_reason is None:
            self.close_reason = reason

    def trigger_disconnect(self) -> None:
        self._disconnect_event.set()


class _FakeHeaders:
    """Minimal stand-in for websockets' handshake ``Headers``.

    Exposes both ``raw_items()`` (preferred by the production header
    extractor to survive duplicate names like ``set-cookie``) and
    ``items()``.
    """

    def __init__(self, pairs) -> None:
        if isinstance(pairs, dict):
            pairs = list(pairs.items())
        self._pairs = [(str(k), str(v)) for k, v in pairs]

    def raw_items(self):
        return list(self._pairs)

    def items(self):
        return list(self._pairs)


class _FakeUpstream:
    """Upstream that streams scripted events then optionally blocks.

    ``hold_after_events`` makes the async iterator wait forever after the
    scripted events are exhausted — that mirrors a real upstream that
    keeps the connection open after a ``response.completed`` event. The
    handler's ``_upstream_to_client`` will block on it, so the only way
    the outer ``asyncio.wait`` can progress is via the client-side task
    completing — which is exactly the cancel-partner path we want to
    test.
    """

    def __init__(
        self,
        events: list[str],
        *,
        hold_after_events: bool = False,
        wait_for_sent_count: int = 0,
        raise_mid_stream: Exception | None = None,
        response_headers=None,
    ) -> None:
        self._events = list(events)
        self._hold_after_events = hold_after_events
        self._wait_for_sent_count = wait_for_sent_count
        self._raise_mid_stream = raise_mid_stream
        self.sent: list[str] = []
        self.closed = False
        # Mirror websockets' ClientConnection.response.headers, which is the
        # only place OpenAI delivers the Codex x-codex-* subscription window.
        self.response = SimpleNamespace(headers=_FakeHeaders(response_headers or []))

    async def __aenter__(self) -> _FakeUpstream:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.closed = True

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        while len(self.sent) < self._wait_for_sent_count:
            await asyncio.sleep(0)
        for ev in self._events:
            yield ev
        if self._raise_mid_stream is not None:
            raise self._raise_mid_stream
        if self._hold_after_events:
            # Wait forever — until the task is cancelled by the handler.
            await asyncio.Event().wait()


def _make_fake_websockets_module(
    upstream: _FakeUpstream | None,
    *,
    call_log: list[str] | None = None,
    connect_calls: list[tuple[tuple, dict]] | None = None,
    connect_error: Exception | None = None,
):
    """Build a fake ``websockets`` module.

    Production now does ``upstream = await websockets.connect(...)`` (then
    ``async with upstream``), so ``connect`` must return an awaitable that
    resolves to the connection. ``connect_error`` makes the await raise to
    simulate an upstream handshake failure.
    """
    module = MagicMock()

    async def _connect(*args, **kwargs):
        if call_log is not None:
            call_log.append("connect")
        if connect_calls is not None:
            connect_calls.append((args, dict(kwargs)))
        if connect_error is not None:
            raise connect_error
        return upstream

    module.connect = _connect
    module.Subprotocol = str
    return module


def _first_frame() -> str:
    return json.dumps(
        {
            "type": "response.create",
            "response": {"model": "gpt-5.4", "input": "hi"},
        }
    )


def _tool_call_frame(tool_name: str, call_id: str) -> str:
    return json.dumps(
        {
            "type": "response.create",
            "response": {
                "model": "gpt-5.4",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": tool_name,
                        "arguments": "{}",
                    }
                ],
            },
        }
    )


def _tool_output_frame(output: str, call_id: str) -> str:
    return json.dumps(
        {
            "type": "response.create",
            "response": {
                "model": "gpt-5.4",
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    }
                ],
            },
        }
    )


def _custom_tool_output_frame(outputs: list[tuple[str, str]]) -> str:
    return json.dumps(
        {
            "type": "response.create",
            "response": {
                "model": "gpt-5.4",
                "input": [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": call_id,
                        "output": output,
                    }
                    for call_id, output in outputs
                ],
            },
        }
    )


def _large_tool_output() -> str:
    return " ".join(f"tool_output_{index}" for index in range(180))


def _completed_response_event() -> str:
    return json.dumps(
        {
            "type": "response.completed",
            "response": {
                "id": "r_tool_output",
                "usage": {"input_tokens": 200, "output_tokens": 1},
            },
        }
    )


def _redundant_response_create_frame() -> str:
    repeated_line = (
        "The WebSocket request repeats this exact sentence for deterministic "
        "compression while preserving the useful request."
    )
    repeated_text = "\n".join([repeated_line] * 12)
    return json.dumps(
        {
            "type": "response.create",
            "response": {
                "model": "gpt-5.4",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": repeated_text}],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": repeated_text}],
                    },
                ],
            },
        }
    )


def _scip_tools() -> list[dict]:
    tool_names = ["scip_index", "scip_find", "scip_map", "scip_outline", "scip_expand"]
    return [
        {
            "type": "function",
            "name": name,
            "description": "  Find symbols with the SCIP index.\nPreserve this text.  ",
            "parameters": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": f"{name}Parameters",
                "type": "object",
                "properties": {
                    "query": {
                        "title": "Query",
                        "type": "string",
                        "examples": ["HeadroomProxy"],
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }
        for name in tool_names
    ]


def _redundant_response_create_frame_with_tools() -> str:
    frame = json.loads(_redundant_response_create_frame())
    frame["response"]["tools"] = _scip_tools()
    return json.dumps(frame)


def _codex_lite_headers(*, chatgpt: bool) -> dict[str, str]:
    headers = {
        "authorization": "Bearer test",
        "X-OpenAI-Internal-Codex-Responses-Lite": "true",
        "X-OpenAI-Debug": "keep-me",
    }
    if chatgpt:
        headers["ChatGPT-Account-ID"] = "acct-123"
    return headers


@pytest.mark.asyncio
async def test_ws_first_frame_output_shaper_rewrites_without_compression(monkeypatch):
    monkeypatch.setenv("HEADROOM_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HEADROOM_VERBOSITY_LEVEL", "2")
    monkeypatch.delenv("HEADROOM_OUTPUT_HOLDOUT", raising=False)
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "r_1",
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            }
        ),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)
    client_ws = _FakeWebSocket(frames=[_first_frame()])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = False
    outcomes = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    handler._record_request_outcome = _record_request_outcome

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    sent = json.loads(upstream.sent[0])
    payload = sent["response"]
    assert "<headroom_output_shaping>" in payload["instructions"]
    assert payload["text"]["verbosity"] == "low"
    assert any(t == "output_shaper:verbosity:L2" for t in outcomes[-1].transforms_applied)


@pytest.mark.asyncio
async def test_ws_output_shaper_stratum_uses_frame_input_tokens(monkeypatch):
    monkeypatch.setenv("HEADROOM_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HEADROOM_VERBOSITY_LEVEL", "2")
    long_input = " ".join(f"word{i}" for i in range(2500))
    first_frame = json.dumps(
        {
            "type": "response.create",
            "response": {"model": "gpt-5.4", "input": long_input},
        }
    )
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "r_1",
                    "usage": {"input_tokens": 3000, "output_tokens": 1},
                },
            }
        ),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)
    client_ws = _FakeWebSocket(frames=[first_frame])
    handler = _DummyOpenAIHandler()
    outcomes = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    handler._record_request_outcome = _record_request_outcome

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    transforms = outcomes[-1].transforms_applied
    assert any(t.startswith("output_shaper:stratum:gpt|new_user_ask|s|") for t in transforms)
    assert not any(t.startswith("output_shaper:stratum:gpt|new_user_ask|xs|") for t in transforms)


@pytest.mark.asyncio
async def test_ws_output_shaper_respects_bypass(monkeypatch):
    monkeypatch.setenv("HEADROOM_OUTPUT_SHAPER", "1")
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)
    first = _first_frame()
    client_ws = _FakeWebSocket(frames=[first])
    client_ws.headers = {
        "authorization": "Bearer test",
        "x-headroom-bypass": "true",
    }
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent[0] == first


@pytest.mark.asyncio
async def test_ws_output_shaper_holdout_labels_without_rewrite(monkeypatch):
    monkeypatch.setenv("HEADROOM_OUTPUT_SHAPER", "1")
    monkeypatch.setenv("HEADROOM_OUTPUT_HOLDOUT", "1")
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "r_1",
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            }
        ),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)
    first = _first_frame()
    client_ws = _FakeWebSocket(frames=[first])
    handler = _DummyOpenAIHandler()
    outcomes = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    handler._record_request_outcome = _record_request_outcome

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent[0] == first
    transforms = outcomes[-1].transforms_applied
    assert any(t.startswith("output_shaper:control:") for t in transforms)
    assert not any(t == "output_shaper:verbosity:L2" for t in transforms)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_response_create_compresses_and_records_shared_savings_once(monkeypatch):
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    original_frame = _redundant_response_create_frame_with_tools()
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "r_1",
                    "usage": {"input_tokens": 200, "output_tokens": 3},
                },
            }
        ),
    ]
    upstream = _FakeUpstream(upstream_events)
    client_ws = _FakeWebSocket(
        frames=[original_frame],
        headers={
            "authorization": "Bearer test",
            "X-Headroom-Compress-Messages": "true",
            "X-Headroom-Protect-Recent": "0",
        },
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.openai_pipeline = SimpleNamespace(transforms=[ContentRouter()])

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    forwarded = json.loads(upstream.sent[0])
    original = json.loads(original_frame)
    assert (
        forwarded["response"]["input"][0]["content"][0]["text"]
        != original["response"]["input"][0]["content"][0]["text"]
    )
    assert forwarded["response"]["tools"] == original["response"]["tools"]
    assert [tool["name"] for tool in forwarded["response"]["tools"]] == [
        "scip_index",
        "scip_find",
        "scip_map",
        "scip_outline",
        "scip_expand",
    ]
    assert handler.metrics.codex_ws_frames[0]["tokens_saved"] > 0
    assert len(handler.metrics.codex_ws_frames) == 1
    assert len(handler.metrics.recorded_requests) == 1
    shared_record = handler.metrics.recorded_requests[0]
    assert shared_record["tokens_saved"] == handler.metrics.codex_ws_frames[0]["tokens_saved"]


@pytest.mark.asyncio
async def test_ws_response_create_is_byte_identical_when_compression_is_disabled(
    caplog,
    monkeypatch,
):
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    original_frame = _redundant_response_create_frame()
    upstream = _FakeUpstream(
        [json.dumps({"type": "response.completed", "response": {"id": "r_1"}})]
    )
    client_ws = _FakeWebSocket(frames=[original_frame])
    handler = _DummyOpenAIHandler()
    caplog.set_level(logging.INFO, logger="headroom.proxy")

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent[0] == original_frame
    assert "reason=disabled" in caplog.text


@pytest.mark.asyncio
async def test_ws_response_create_without_eligible_content_is_byte_identical(
    caplog,
    monkeypatch,
):
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    original_frame = _first_frame()
    upstream = _FakeUpstream(
        [json.dumps({"type": "response.completed", "response": {"id": "r_1"}})]
    )
    client_ws = _FakeWebSocket(frames=[original_frame])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.openai_pipeline = SimpleNamespace(transforms=[ContentRouter()])
    caplog.set_level(logging.INFO, logger="headroom.proxy")

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent[0] == original_frame
    assert "reason=nothing_eligible" in caplog.text
    assert "reason=router_no_compression" not in caplog.text


@pytest.mark.asyncio
async def test_ws_response_create_error_rolls_back_to_original_frame(caplog, monkeypatch):
    monkeypatch.setenv("HEADROOM_OUTPUT_SHAPER", "1")
    original_frame = _redundant_response_create_frame()
    upstream = _FakeUpstream(
        [json.dumps({"type": "response.completed", "response": {"id": "r_1"}})]
    )
    client_ws = _FakeWebSocket(frames=[original_frame])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True

    def _raise_after_mutation(payload, **_kwargs):
        payload.clear()
        raise RuntimeError("simulated transform failure")

    handler._compress_openai_responses_payload = _raise_after_mutation  # type: ignore[method-assign]
    caplog.set_level(logging.INFO, logger="headroom.proxy")

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent[0] == original_frame
    assert "reason=error_rollback" in caplog.text
    assert len(handler.metrics.codex_ws_frames) == 1
    assert handler.metrics.codex_ws_frames[0]["failed"] is True


@pytest.mark.asyncio
async def test_ws_later_response_create_frame_uses_shared_compression(monkeypatch):
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    later_frame = _redundant_response_create_frame()
    upstream = _FakeUpstream([], hold_after_events=True)
    client_ws = _FakeWebSocket(
        frames=[_first_frame(), later_frame],
        headers={
            "authorization": "Bearer test",
            "X-Headroom-Compress-Messages": "true",
            "X-Headroom-Protect-Recent": "0",
        },
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.openai_pipeline = SimpleNamespace(transforms=[ContentRouter()])

    async def _disconnect() -> None:
        await asyncio.sleep(0.05)
        client_ws.trigger_disconnect()

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        trigger = asyncio.create_task(_disconnect())
        try:
            await asyncio.wait_for(handler.handle_openai_responses_ws(client_ws), timeout=2.0)
        finally:
            trigger.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await trigger

    assert upstream.sent[-1] != later_frame
    forwarded = json.loads(upstream.sent[-1])
    assert forwarded["type"] == "response.create"
    assert handler.metrics.codex_ws_frames[-1]["tokens_saved"] > 0


@pytest.mark.asyncio
async def test_ws_scip_tool_output_skips_cached_shell_variant(monkeypatch):
    monkeypatch.delenv("HEADROOM_INCREMENTAL_LOSSLESS", raising=False)
    monkeypatch.delenv("HEADROOM_COMPRESS_TOOL_OUTPUT_EXCLUDE", raising=False)
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    large_output = _large_tool_output()
    shell_call_frame = _tool_call_frame("shell", "call_shell_cache_seed")
    shell_output_frame = _tool_output_frame(large_output, "call_shell_cache_seed")
    scip_call_frame = _tool_call_frame("scip_map", "call_scip_map")
    scip_output_frame = _tool_output_frame(large_output, "call_scip_map")
    upstream = _FakeUpstream(
        [_completed_response_event()],
        wait_for_sent_count=4,
    )
    client_ws = _FakeWebSocket(
        frames=[shell_call_frame, shell_output_frame, scip_call_frame, scip_output_frame],
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.protect_recent = 0
    router = ContentRouter()
    router.config.exclude_tools = set()
    outcomes = []
    compressed_contents = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    handler._record_request_outcome = _record_request_outcome

    def _compress(self, content: str, **_kwargs):
        compressed_contents.append(content)
        return RouterCompressionResult(
            compressed="compressed shell output",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(_compress, router)
    handler.openai_pipeline = SimpleNamespace(transforms=[router])

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await asyncio.wait_for(handler.handle_openai_responses_ws(client_ws), timeout=2.0)

    assert json.loads(upstream.sent[1])["response"]["input"][0]["output"] == large_output
    assert upstream.sent[3] == scip_output_frame
    assert compressed_contents == []
    assert outcomes
    assert CODEX_WS_EXEMPT_TRANSFORM in outcomes[-1].transforms_applied


@pytest.mark.asyncio
async def test_ws_shell_tool_output_is_lossless_across_frames(monkeypatch):
    monkeypatch.delenv("HEADROOM_INCREMENTAL_LOSSLESS", raising=False)
    monkeypatch.delenv("HEADROOM_COMPRESS_TOOL_OUTPUT_EXCLUDE", raising=False)
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    call_id = "call_shell"
    call_frame = _tool_call_frame("shell", call_id)
    output_frame = _tool_output_frame(_large_tool_output(), call_id)
    upstream = _FakeUpstream(
        [_completed_response_event()],
        wait_for_sent_count=2,
    )
    client_ws = _FakeWebSocket(
        frames=[call_frame, output_frame],
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.protect_recent = 0
    router = ContentRouter()
    router.config.exclude_tools = set()
    outcomes = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    handler._record_request_outcome = _record_request_outcome

    def _compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="compressed shell output",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(_compress, router)
    handler.openai_pipeline = SimpleNamespace(transforms=[router])

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await asyncio.wait_for(handler.handle_openai_responses_ws(client_ws), timeout=2.0)

    forwarded_output = json.loads(upstream.sent[1])["response"]["input"][0]["output"]
    assert forwarded_output == _large_tool_output()
    assert outcomes[-1].tokens_saved == 0
    assert CODEX_WS_EXEMPT_TRANSFORM not in outcomes[-1].transforms_applied


@pytest.mark.asyncio
async def test_ws_custom_tool_output_content_signature_is_byte_identical(monkeypatch):
    monkeypatch.delenv("HEADROOM_INCREMENTAL_LOSSLESS", raising=False)
    monkeypatch.delenv("HEADROOM_COMPRESS_CONTENT_EXCLUDE", raising=False)
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    crux_output = "\n".join(
        [
            "resolved make_model_tuple → `x`/make_model_tuple().",
            "## `x`",
            "definition: path.py:12",
            *(f"symbol detail {index}: preserve exact navigation data" for index in range(80)),
        ]
    )
    original_frame = _custom_tool_output_frame([("call_exec", crux_output)])
    upstream = _FakeUpstream([_completed_response_event()])
    client_ws = _FakeWebSocket(frames=[original_frame])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.protect_recent = 0
    router = ContentRouter()
    router.config.exclude_tools = set()
    outcomes = []
    debug_events = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    def _unexpected_compression(self, content: str, **_kwargs):
        raise AssertionError(f"content-exempt output reached the router: {content[:40]}")

    def _capture_debug(event: str, **payload):
        debug_events.append((event, payload))

    handler._record_request_outcome = _record_request_outcome
    router.compress = MethodType(_unexpected_compression, router)
    handler.openai_pipeline = SimpleNamespace(transforms=[router])
    monkeypatch.setattr(openai_module, "_log_codex_compression_debug", _capture_debug)

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent[0] == original_frame
    assert outcomes
    assert CONTENT_EXEMPT_TRANSFORM in outcomes[-1].transforms_applied
    assert not hasattr(handler, "_openai_responses_unit_result_cache")
    unit_events = [
        payload for event, payload in debug_events if event == "codex_compression_unit_result"
    ]
    assert len(unit_events) == 1
    assert unit_events[0]["original"] == crux_output
    assert unit_events[0]["compressed"] == crux_output
    assert unit_events[0]["transforms_applied"] == [CONTENT_EXEMPT_TRANSFORM]


@pytest.mark.asyncio
async def test_ws_custom_tool_output_uses_incremental_lossless_mode(monkeypatch):
    monkeypatch.delenv("HEADROOM_INCREMENTAL_LOSSLESS", raising=False)
    monkeypatch.delenv("HEADROOM_COMPRESS_CONTENT_EXCLUDE", raising=False)
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    build_output = "\n".join(
        [
            "Script completed successfully.",
            *(f"build log line {index}: compiled target module_{index}" for index in range(400)),
        ]
    )
    assert len(build_output.encode("utf-8")) > 512
    original_frame = _custom_tool_output_frame([("call_exec", build_output)])
    upstream = _FakeUpstream([_completed_response_event()])
    client_ws = _FakeWebSocket(frames=[original_frame])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.protect_recent = 0
    router = ContentRouter()
    router.config.exclude_tools = set()
    outcomes = []
    lossy_calls = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    def _compress(self, content: str, **_kwargs):
        lossy_calls.append(content)
        return RouterCompressionResult(
            compressed="compressed build log",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    handler._record_request_outcome = _record_request_outcome
    router.compress = MethodType(_compress, router)
    handler.openai_pipeline = SimpleNamespace(transforms=[router])

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    forwarded = json.loads(upstream.sent[0])
    forwarded_output = forwarded["response"]["input"][0]["output"]
    assert forwarded_output == build_output
    assert "<<ccr:" not in forwarded_output
    assert lossy_calls == []
    assert outcomes[-1].tokens_saved == 0
    assert CONTENT_EXEMPT_TRANSFORM not in outcomes[-1].transforms_applied


@pytest.mark.asyncio
async def test_ws_content_signature_env_override_changes_exempt_output(monkeypatch):
    monkeypatch.delenv("HEADROOM_INCREMENTAL_LOSSLESS", raising=False)
    monkeypatch.setenv("HEADROOM_COMPRESS_CONTENT_EXCLUDE", r"^KEEP EXACT:")
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    override_output = "KEEP EXACT:\n" + "\n".join(
        f"override detail {index}: preserve this line" for index in range(80)
    )
    default_signature_output = "resolved make_model_tuple → `x`/make_model_tuple().\n" + "\n".join(
        f"default detail {index}: this unit can compress" for index in range(80)
    )
    original_frame = _custom_tool_output_frame(
        [
            ("call_override", override_output),
            ("call_default", default_signature_output),
        ]
    )
    upstream = _FakeUpstream([_completed_response_event()])
    client_ws = _FakeWebSocket(frames=[original_frame])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.protect_recent = 0
    router = ContentRouter()
    router.config.exclude_tools = set()
    compressed_contents = []
    outcomes = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    def _compress(self, content: str, **_kwargs):
        compressed_contents.append(content)
        return RouterCompressionResult(
            compressed="compressed default-signature output",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    handler._record_request_outcome = _record_request_outcome
    router.compress = MethodType(_compress, router)
    handler.openai_pipeline = SimpleNamespace(transforms=[router])

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    forwarded_items = json.loads(upstream.sent[0])["response"]["input"]
    assert forwarded_items[0]["output"] == override_output
    assert forwarded_items[1]["output"] == default_signature_output
    assert compressed_contents == []
    assert CONTENT_EXEMPT_TRANSFORM in outcomes[-1].transforms_applied


@pytest.mark.asyncio
async def test_ws_empty_content_signature_disables_guard(monkeypatch):
    monkeypatch.delenv("HEADROOM_INCREMENTAL_LOSSLESS", raising=False)
    monkeypatch.setenv("HEADROOM_COMPRESS_CONTENT_EXCLUDE", "")
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    output = "resolved make_model_tuple → `x`/make_model_tuple().\n" + "\n".join(
        f"symbol detail {index}: compress after guard disable" for index in range(80)
    )
    original_frame = _custom_tool_output_frame([("call_exec", output)])
    upstream = _FakeUpstream([_completed_response_event()])
    client_ws = _FakeWebSocket(frames=[original_frame])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.protect_recent = 0
    router = ContentRouter()
    router.config.exclude_tools = set()
    outcomes = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    def _compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="compressed disabled-guard output",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    handler._record_request_outcome = _record_request_outcome
    router.compress = MethodType(_compress, router)
    handler.openai_pipeline = SimpleNamespace(transforms=[router])

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    forwarded = json.loads(upstream.sent[0])
    assert forwarded["response"]["input"][0]["output"] == output
    assert CONTENT_EXEMPT_TRANSFORM not in outcomes[-1].transforms_applied


@pytest.mark.asyncio
async def test_ws_invalid_content_signature_warns_and_uses_default(
    caplog,
    monkeypatch,
):
    monkeypatch.delenv("HEADROOM_INCREMENTAL_LOSSLESS", raising=False)
    monkeypatch.setenv("HEADROOM_COMPRESS_CONTENT_EXCLUDE", "[")
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    output = "definition: path.py:12\n" + "\n".join(
        f"definition detail {index}: preserve fallback output" for index in range(80)
    )
    original_frame = _custom_tool_output_frame([("call_exec", output)])
    upstream = _FakeUpstream([_completed_response_event()])
    client_ws = _FakeWebSocket(frames=[original_frame])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.protect_recent = 0
    router = ContentRouter()
    router.config.exclude_tools = set()
    outcomes = []

    async def _record_request_outcome(outcome):
        outcomes.append(outcome)

    def _unexpected_compression(self, content: str, **_kwargs):
        raise AssertionError(f"fallback-exempt output reached the router: {content[:40]}")

    handler._record_request_outcome = _record_request_outcome
    router.compress = MethodType(_unexpected_compression, router)
    handler.openai_pipeline = SimpleNamespace(transforms=[router])
    caplog.set_level(logging.WARNING, logger="headroom.proxy")

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent[0] == original_frame
    assert CONTENT_EXEMPT_TRANSFORM in outcomes[-1].transforms_applied
    assert "Invalid HEADROOM_COMPRESS_CONTENT_EXCLUDE regex" in caplog.text
    assert "using the default" in caplog.text


@pytest.mark.asyncio
async def test_ws_other_input_frame_uses_frame_header_overrides(monkeypatch):
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    response_frame = json.loads(_redundant_response_create_frame_with_tools())["response"]
    later_frame = json.dumps(
        {
            "type": "request.update",
            "headers": {
                "X-Headroom-Compress-Messages": "true",
                "X-Headroom-Protect-Recent": "0",
            },
            **response_frame,
        }
    )
    upstream = _FakeUpstream([], hold_after_events=True)
    client_ws = _FakeWebSocket(
        frames=[_first_frame(), later_frame],
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.openai_pipeline = SimpleNamespace(transforms=[ContentRouter()])

    async def _disconnect() -> None:
        await asyncio.sleep(0.05)
        client_ws.trigger_disconnect()

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        trigger = asyncio.create_task(_disconnect())
        try:
            await asyncio.wait_for(handler.handle_openai_responses_ws(client_ws), timeout=2.0)
        finally:
            trigger.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await trigger

    forwarded = json.loads(upstream.sent[-1])
    assert forwarded["type"] == "request.update"
    assert forwarded["tools"] == response_frame["tools"]
    assert upstream.sent[-1] != later_frame
    assert handler.metrics.codex_ws_frames[-1]["tokens_saved"] > 0


@pytest.mark.asyncio
async def test_ws_first_frame_compression_uses_bounded_executor(monkeypatch):
    """Codex WS compression must not run synchronously on the event loop."""
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(
        frames=[_first_frame()],
        headers={
            "authorization": "Bearer test",
            "X-Headroom-Compress-Messages": "true",
        },
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    monkeypatch.setattr(openai_module, "COMPRESSION_TIMEOUT_SECONDS", 30.0)
    expected_timeout = getattr(
        openai_module,
        "_CODEX_WS_COMPRESSION_TIMEOUT_SECONDS",
        5.0,
    )
    handler._compress_openai_responses_payload = MagicMock(
        return_value=(
            {"model": "gpt-5.4", "input": "hi"},
            False,
            0,
            [],
            "router_no_compression",
            10,
            10,
        )
    )

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert handler.compression_executor_calls == 1
    assert handler.compression_executor_timeouts == [expected_timeout]
    handler._compress_openai_responses_payload.assert_called_once()
    assert (
        handler._compress_openai_responses_payload.call_args.kwargs["compress_responses_messages"]
        is True
    )
    assert handler._compress_openai_responses_payload.call_args.kwargs["preserve_tools"] is True


@pytest.mark.asyncio
async def test_ws_first_frame_timeout_uses_error_rollback_reason(caplog, monkeypatch):
    """A Codex WS timeout must stay bounded and roll back safely."""
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(frames=[_first_frame()])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    monkeypatch.setattr(openai_module, "COMPRESSION_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(
        openai_module,
        "_CODEX_WS_COMPRESSION_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )

    async def _timeout_run(fn, *, timeout: float):
        handler.compression_executor_calls += 1
        handler.compression_executor_timeouts.append(timeout)
        raise asyncio.TimeoutError("simulated timeout")

    handler._run_compression_in_executor = _timeout_run  # type: ignore[method-assign]
    caplog.set_level(logging.INFO, logger="headroom.proxy")

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert handler.compression_executor_timeouts == [0.01]
    assert "reason=error_rollback" in caplog.text


@pytest.mark.asyncio
async def test_ws_first_frame_non_timeout_exception_uses_error_rollback_reason(
    caplog,
    monkeypatch,
):
    """A non-timeout compression failure must log an error rollback."""
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(frames=[_first_frame()])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    monkeypatch.setattr(openai_module, "COMPRESSION_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(
        openai_module,
        "_CODEX_WS_COMPRESSION_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )

    async def _error_run(fn, *, timeout: float):
        handler.compression_executor_calls += 1
        handler.compression_executor_timeouts.append(timeout)
        raise RuntimeError("simulated failure")

    handler._run_compression_in_executor = _error_run  # type: ignore[method-assign]
    caplog.set_level(logging.INFO, logger="headroom.proxy")

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert handler.compression_executor_timeouts == [0.01]
    assert "reason=error_rollback" in caplog.text


@pytest.mark.asyncio
async def test_ws_later_frame_timeout_records_failed_frame(caplog, monkeypatch):
    """Later Codex WS compression timeout records failed frame metrics."""
    second_frame = _first_frame()
    upstream = _FakeUpstream([], hold_after_events=True)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(
        frames=[_first_frame(), second_frame],
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    monkeypatch.setattr(openai_module, "COMPRESSION_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(
        openai_module,
        "_CODEX_WS_COMPRESSION_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )

    def _noop_compress(payload, *, model, request_id, timing=None):
        return payload, False, 0, [], "test_noop", 10, 10, 0

    calls = 0

    async def _run(fn, *, timeout: float):
        nonlocal calls
        calls += 1
        handler.compression_executor_calls += 1
        handler.compression_executor_timeouts.append(timeout)
        if calls == 2:
            raise asyncio.TimeoutError("simulated later-frame timeout")
        return fn()

    async def _trigger() -> None:
        await asyncio.sleep(0.05)
        client_ws.trigger_disconnect()

    handler._compress_openai_responses_payload = _noop_compress  # type: ignore[method-assign]
    handler._run_compression_in_executor = _run  # type: ignore[method-assign]
    caplog.set_level(logging.INFO, logger="headroom.proxy")

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        trigger_task = asyncio.create_task(_trigger())
        try:
            await asyncio.wait_for(handler.handle_openai_responses_ws(client_ws), timeout=2.0)
        finally:
            trigger_task.cancel()
            try:
                await trigger_task
            except asyncio.CancelledError:
                pass

    failed_frames = [frame for frame in handler.metrics.codex_ws_frames if frame.get("failed")]
    assert handler.compression_executor_timeouts == [0.01, 0.01]
    assert upstream.sent[-1] == second_frame
    assert failed_frames and failed_frames[-1]["elapsed_ms"] > 0
    assert "reason=error_rollback" in caplog.text


@pytest.mark.asyncio
async def test_happy_path_registry_empty_after_response_completed():
    """Normal session completes — both relay tasks done, registry empty."""
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(frames=[_first_frame()])
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert handler.ws_sessions.active_count() == 0
    assert handler.metrics.active_ws_sessions == 0
    # termination_cause captured
    assert handler.metrics.termination_causes
    # Either "response_completed" or "client_disconnect" — both are
    # acceptable here depending on which relay half exited first; the
    # important thing is we recorded one.
    assert handler.metrics.termination_causes[-1] in {
        "response_completed",
        "client_disconnect",
        "upstream_disconnect",
    }


@pytest.mark.asyncio
async def test_ws_session_metrics_include_response_completed_usage():
    """Codex WS sessions should report real upstream usage, not zero-token sessions."""

    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "r_1",
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": {"cached_tokens": 75},
                        "output_tokens": 12,
                    },
                },
            }
        ),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(frames=[_first_frame()])
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert handler.metrics.recorded_requests
    recorded = handler.metrics.recorded_requests[-1]
    assert recorded["input_tokens"] == 100
    assert recorded["output_tokens"] == 12
    assert recorded["cache_read_tokens"] == 75
    assert recorded["cache_write_tokens"] == 25
    assert recorded["uncached_input_tokens"] == 25


@pytest.mark.asyncio
async def test_ws_session_metrics_include_dashboard_performance_timings():
    """Codex WS response metrics should feed the dashboard Performance tab."""

    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "r_1",
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": {"cached_tokens": 75},
                        "output_tokens": 12,
                    },
                },
            }
        ),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(frames=[_first_frame()])
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True

    def _noop_compress(
        payload,
        *,
        model,
        request_id,
        timing=None,
        compress_responses_messages=False,
    ):
        assert compress_responses_messages is False
        if timing is not None:
            timing["compression_live_unit_extraction"] = 2.0
            timing["compression_unit_router_strategy_passthrough"] = 3.0
        return payload, False, 0, [], "test_noop", 10, 10, 0

    handler._compress_openai_responses_payload = _noop_compress  # type: ignore[method-assign]

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert handler.metrics.recorded_requests
    recorded = handler.metrics.recorded_requests[-1]
    assert recorded["overhead_ms"] > 0
    assert recorded["ttfb_ms"] > 0
    assert recorded["pipeline_timing"]["codex_ws.compression"] > 0
    assert recorded["pipeline_timing"]["codex_ws.upstream_first_event"] > 0
    assert recorded["pipeline_timing"]["codex_ws.compression_preflight_serialization"] > 0
    assert recorded["pipeline_timing"]["codex_ws.compression_executor_wait_run"] > 0
    assert recorded["pipeline_timing"]["codex_ws.compression_live_unit_extraction"] == 2.0
    assert (
        recorded["pipeline_timing"]["codex_ws.compression_unit_router_strategy_passthrough"] == 3.0
    )


@pytest.mark.asyncio
async def test_ws_opt_in_flattens_response_create_for_openai_compatible_upstream(monkeypatch):
    """Some OpenAI-compatible WS gateways expect top-level response.create payloads."""

    monkeypatch.setenv("HEADROOM_OPENAI_WS_FLATTEN_RESPONSE_CREATE", "1")
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    first = json.dumps(
        {
            "type": "response.create",
            "event_id": "evt_flatten",
            "response": {
                "model": "gpt-5.4",
                "input": "hello",
                "instructions": "be concise",
                "tools": [{"type": "function", "name": "shell"}],
            },
        }
    )
    client_ws = _FakeWebSocket(frames=[first])
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert upstream.sent
    sent = json.loads(upstream.sent[0])
    assert sent == {
        "model": "gpt-5.4",
        "input": "hello",
        "instructions": "be concise",
        "tools": [{"type": "function", "name": "shell"}],
        "type": "response.create",
        "event_id": "evt_flatten",
    }


@pytest.mark.asyncio
async def test_ws_opt_in_propagates_upstream_close_code_and_reason(monkeypatch):
    """Expose upstream close details to Codex instead of swallowing them in debug logs."""

    monkeypatch.setenv("HEADROOM_OPENAI_WS_PROPAGATE_UPSTREAM_CLOSE", "1")
    upstream = _FakeUpstream(
        [],
        raise_mid_stream=_FakeUpstreamClose(4001, "bad request shape"),
    )
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(
        frames=[_first_frame()],
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await asyncio.wait_for(
            handler.handle_openai_responses_ws(client_ws),
            timeout=2.0,
        )

    assert client_ws.closed
    assert client_ws.close_code == 4001
    assert client_ws.close_reason == "bad request shape"
    assert handler.metrics.termination_causes[-1] == "upstream_error"


@pytest.mark.asyncio
async def test_client_disconnect_cancels_upstream_relay_within_100ms():
    """**Failing-test-first** scenario from the plan.

    When the client side exits (``receive_text`` raises
    ``WebSocketDisconnect``) while upstream is still open and iterating,
    the upstream relay task must be cancelled and become ``done()``
    quickly. The registry must report no active sessions afterwards.
    """
    # Upstream keeps iterating forever after one event, forcing the
    # upstream-to-client task to block on the iterator. The only way
    # out is a cancel from the handler's orchestration.
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events, hold_after_events=True)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    # Client has one initial frame, then disconnects after the server
    # sends the first forwarded event to us.
    client_ws = _FakeWebSocket(
        frames=[_first_frame()],
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()

    # Trigger disconnect shortly after the handler accepts.
    async def _trigger() -> None:
        await asyncio.sleep(0.05)
        client_ws.trigger_disconnect()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        trigger_task = asyncio.create_task(_trigger())
        try:
            await asyncio.wait_for(
                handler.handle_openai_responses_ws(client_ws),
                timeout=2.0,
            )
        finally:
            trigger_task.cancel()
            try:
                await trigger_task
            except asyncio.CancelledError:
                pass

    # Registry must be empty — the finally block deregistered the session.
    assert handler.ws_sessions.active_count() == 0, (
        "session leaked — deregister did not run in outermost finally"
    )
    assert handler.metrics.active_ws_sessions == 0
    # We recorded a session duration (came through deregister path).
    assert handler.metrics.ws_session_durations, (
        "record_ws_session_duration never fired — deregister path broken"
    )
    # And we tagged the cause. For a client-side exit it should be one
    # of: client_disconnect, client_error, upstream_disconnect (if
    # upstream iteration happened to end first in a race).
    cause = handler.metrics.termination_causes[-1]
    assert cause in {
        "client_disconnect",
        "client_error",
        "upstream_disconnect",
    }, f"unexpected cause: {cause}"

    # No codex-ws-* named task should still be running.
    leaked = [
        t
        for t in asyncio.all_tasks()
        if (t.get_name() or "").startswith("codex-ws-") and not t.done()
    ]
    assert leaked == [], f"relay tasks leaked: {[t.get_name() for t in leaked]}"


@pytest.mark.asyncio
async def test_upstream_closes_first_cancels_client_task():
    """Upstream iterator ends naturally; client task should be cancelled.

    The client is set to block on ``receive_text`` indefinitely; only a
    cancel from the handler's orchestration releases it.
    """
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events, hold_after_events=False)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(
        frames=[_first_frame()],
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await asyncio.wait_for(
            handler.handle_openai_responses_ws(client_ws),
            timeout=2.0,
        )

    assert handler.ws_sessions.active_count() == 0
    # We must still have recorded exactly one session duration.
    assert len(handler.metrics.ws_session_durations) == 1


@pytest.mark.asyncio
async def test_upstream_error_mid_stream_classifies_as_upstream_error():
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(
        upstream_events,
        raise_mid_stream=RuntimeError("boom from upstream"),
    )
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(
        frames=[_first_frame()],
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await asyncio.wait_for(
            handler.handle_openai_responses_ws(client_ws),
            timeout=2.0,
        )

    assert handler.ws_sessions.active_count() == 0
    assert handler.metrics.termination_causes
    assert handler.metrics.termination_causes[-1] == "upstream_error"


@pytest.mark.asyncio
async def test_response_cancel_frame_is_logged_as_client_cancel_lifecycle():
    """A Codex Ctrl-C maps to response.cancel on the WS stream.

    The proxy should relay it upstream and classify the lifecycle as a
    client-side cancel when no response.completed event follows.
    """
    cancel_frame = json.dumps({"type": "response.cancel", "response_id": "r_1"})
    upstream = _FakeUpstream([], hold_after_events=True)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(
        frames=[_first_frame(), cancel_frame],
        hold_after_initial=True,
    )
    handler = _DummyOpenAIHandler()

    async def _trigger() -> None:
        await asyncio.sleep(0.05)
        client_ws.trigger_disconnect()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        trigger_task = asyncio.create_task(_trigger())
        try:
            await asyncio.wait_for(
                handler.handle_openai_responses_ws(client_ws),
                timeout=2.0,
            )
        finally:
            trigger_task.cancel()
            try:
                await trigger_task
            except asyncio.CancelledError:
                pass

    assert cancel_frame in upstream.sent
    assert handler.metrics.termination_causes[-1] == "client_cancel"
    assert handler.ws_sessions.active_count() == 0


@pytest.mark.asyncio
async def test_upstream_connect_failure_still_deregisters_cleanly():
    """Handshake-phase leak must be impossible: if upstream connect
    raises before relay tasks are created, the session is still
    registered+deregistered cleanly (or never registered). Either way,
    no leak.
    """
    fake_ws_mod = _make_fake_websockets_module(None, connect_error=RuntimeError("upstream refused"))

    client_ws = _FakeWebSocket(frames=[_first_frame()])
    handler = _DummyOpenAIHandler()

    async def _fallback(*args, **kwargs):
        return None

    handler._ws_http_fallback = _fallback  # type: ignore[assignment]

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert handler.ws_sessions.active_count() == 0


@pytest.mark.asyncio
async def test_ws_connect_failure_falls_back_to_http():
    """When every ChatGPT-auth upstream connect attempt fails, the client
    still gets its local 101 immediately, then the request is served via
    the HTTP POST fallback with the first frame after retries exhaust.
    """
    fake_ws_mod = _make_fake_websockets_module(
        None, connect_error=RuntimeError("HTTP 500 from upstream")
    )

    first = _first_frame()
    client_ws = _FakeWebSocket(
        frames=[first],
        headers=_codex_lite_headers(chatgpt=True),
    )
    handler = _DummyOpenAIHandler()

    fallback_calls: list[tuple] = []

    async def _fallback(websocket, body, first_msg_raw, upstream_headers, request_id):
        fallback_calls.append((body, first_msg_raw))

    handler._ws_http_fallback = _fallback  # type: ignore[assignment]

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    # Client was accepted with no upstream window to forward.
    assert client_ws.accepted_headers is None
    # Fallback ran with the first frame.
    assert len(fallback_calls) == 1
    _body, _first_raw = fallback_calls[0]
    expected = json.loads(first)
    expected["response"]["store"] = False
    assert json.loads(_first_raw) == expected
    assert _body == expected
    # Clean teardown.
    assert handler.ws_sessions.active_count() == 0


@pytest.mark.asyncio
async def test_chatgpt_ws_accepts_before_stalled_upstream_connect():
    """ChatGPT-auth sessions must send the local 101 before a stalled
    upstream opening handshake is released.
    """
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    first_attempt_started = asyncio.Event()
    release_first_attempt = asyncio.Event()
    connect_calls: list[tuple[tuple, dict]] = []

    async def _connect(*args, **kwargs):
        connect_calls.append((args, dict(kwargs)))
        if len(connect_calls) == 1:
            first_attempt_started.set()
            await release_first_attempt.wait()
            raise RuntimeError("first opening handshake stalled")
        return _FakeUpstream(list(upstream_events))

    fake_ws_mod = MagicMock()
    fake_ws_mod.connect = _connect
    fake_ws_mod.Subprotocol = str

    client_ws = _FakeWebSocket(
        frames=[_first_frame()],
        headers=_codex_lite_headers(chatgpt=True),
    )
    handler = _DummyOpenAIHandler()
    handler.config.retry_max_attempts = 3

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        task = asyncio.create_task(handler.handle_openai_responses_ws(client_ws))
        try:
            await asyncio.wait_for(first_attempt_started.wait(), timeout=0.5)
            await asyncio.wait_for(client_ws.accepted_event.wait(), timeout=0.2)
            assert len(connect_calls) == 1
            assert client_ws.accepted_headers is None
            release_first_attempt.set()
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            release_first_attempt.set()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert handler.ws_sessions.active_count() == 0


@pytest.mark.asyncio
async def test_ws_codex_responses_lite_header_is_not_forwarded_upstream():
    """The WS upstream handshake must drop the Codex lite header only."""
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    connect_calls: list[tuple[tuple, dict]] = []
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream, connect_calls=connect_calls)

    client_ws = _FakeWebSocket(
        frames=[_first_frame()],
        headers=_codex_lite_headers(chatgpt=True),
    )
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert len(connect_calls) == 1
    connect_args, connect_kwargs = connect_calls[0]
    assert connect_args[0] == "wss://chatgpt.com/backend-api/codex/responses"
    forwarded_headers = connect_kwargs["additional_headers"]
    assert "X-OpenAI-Internal-Codex-Responses-Lite" not in forwarded_headers
    assert forwarded_headers["ChatGPT-Account-ID"] == "acct-123"
    assert forwarded_headers["X-OpenAI-Debug"] == "keep-me"


@pytest.mark.asyncio
async def test_ws_codex_responses_lite_header_is_not_forwarded_to_fallback():
    """HTTP fallback must inherit the sanitized upstream header copy."""
    fake_ws_mod = _make_fake_websockets_module(
        None,
        connect_error=RuntimeError("HTTP 500 from upstream"),
    )

    client_ws = _FakeWebSocket(
        frames=[_first_frame()],
        headers=_codex_lite_headers(chatgpt=True),
    )
    handler = _DummyOpenAIHandler()

    fallback_calls: list[dict[str, str]] = []

    async def _fallback(websocket, body, first_msg_raw, upstream_headers, request_id):
        fallback_calls.append(dict(upstream_headers))

    handler._ws_http_fallback = _fallback  # type: ignore[assignment]

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert len(fallback_calls) == 1
    forwarded_headers = fallback_calls[0]
    assert "X-OpenAI-Internal-Codex-Responses-Lite" not in forwarded_headers
    assert forwarded_headers["ChatGPT-Account-ID"] == "acct-123"
    assert forwarded_headers["X-OpenAI-Debug"] == "keep-me"


@pytest.mark.asyncio
async def test_ws_without_codex_lite_preserves_adjacent_headers_and_api_key_route():
    """Requests without the lite header keep adjacent OpenAI headers intact."""
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    connect_calls: list[tuple[tuple, dict]] = []
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream, connect_calls=connect_calls)

    client_ws = _FakeWebSocket(
        frames=[_first_frame()],
        headers={
            "authorization": "Bearer test",
            "OpenAI-Beta": "responses=v1",
            "X-OpenAI-Debug": "keep-me",
        },
    )
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert len(connect_calls) == 1
    connect_args, connect_kwargs = connect_calls[0]
    assert connect_args[0] == "wss://api.openai.com/v1/responses"
    forwarded_headers = connect_kwargs["additional_headers"]
    assert "responses=v1" in forwarded_headers["OpenAI-Beta"]
    assert "responses_websockets=2026-02-06" in forwarded_headers["OpenAI-Beta"]
    assert forwarded_headers["X-OpenAI-Debug"] == "keep-me"
    assert "ChatGPT-Account-ID" not in forwarded_headers


@pytest.mark.asyncio
async def test_ws_first_frame_strips_codex_lite_metadata_mirror():
    """Codex mirrors the lite header into response.create's client_metadata
    (regression for #1523): stripping the handshake header alone is not
    enough, upstream rejects gpt-5.x when the frame-body mirror survives.
    """
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    first_frame = json.dumps(
        {
            "type": "response.create",
            "response": {
                "model": "gpt-5.5",
                "input": "hi",
                "client_metadata": {
                    "thread_id": "t_1",
                    "ws_request_header_x_openai_internal_codex_responses_lite": True,
                },
            },
        }
    )
    client_ws = _FakeWebSocket(
        frames=[first_frame],
        headers=_codex_lite_headers(chatgpt=True),
    )
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert len(upstream.sent) == 1
    sent_body = json.loads(upstream.sent[0])
    client_metadata = sent_body["response"]["client_metadata"]
    assert "ws_request_header_x_openai_internal_codex_responses_lite" not in client_metadata
    # Sibling metadata must survive the strip.
    assert client_metadata["thread_id"] == "t_1"


@pytest.mark.asyncio
async def test_api_key_ws_connect_happens_before_accept():
    """API-key sessions keep the upstream connect before the client 101,
    so OpenAI's x-codex-* handshake headers remain attachable there.
    """
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    call_log: list[str] = []
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream, call_log=call_log)

    client_ws = _FakeWebSocket(frames=[_first_frame()], call_log=call_log)
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert "connect" in call_log and "accept" in call_log
    assert call_log.index("connect") < call_log.index("accept"), (
        f"connect must precede accept, got {call_log}"
    )


@pytest.mark.asyncio
async def test_ws_forwards_codex_headers_to_client_accept():
    """OpenAI's x-codex-* subscription window from the upstream WS
    handshake must be forwarded onto the client-facing 101 (and only
    that subset — never set-cookie/authorization), and Python /stats
    state must be refreshed.
    """
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    # Include duplicate set-cookie to ensure raw_items() is used (a plain
    # dict-style .items() on real websockets Headers raises on dupes).
    handshake_headers = [
        ("x-codex-primary-used-percent", "42"),
        ("X-Codex-Primary-Window-Minutes", "300"),
        ("set-cookie", "a=1"),
        ("set-cookie", "b=2"),
        ("authorization", "Bearer leak"),
    ]
    upstream = _FakeUpstream(upstream_events, response_headers=handshake_headers)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(frames=[_first_frame()])
    handler = _DummyOpenAIHandler()

    captured: dict = {}

    def _fake_state():
        class _S:
            def update_from_headers(self, headers):
                captured.update(headers)

        return _S()

    with (
        patch.dict(sys.modules, {"websockets": fake_ws_mod}),
        patch(
            "headroom.subscription.codex_rate_limits.get_codex_rate_limit_state",
            _fake_state,
        ),
    ):
        await handler.handle_openai_responses_ws(client_ws)

    assert client_ws.accepted_headers is not None
    names = {name.decode("latin-1").lower() for name, _ in client_ws.accepted_headers}
    assert names == {"x-codex-primary-used-percent", "x-codex-primary-window-minutes"}
    assert "set-cookie" not in names
    assert "authorization" not in names
    # Original-case names preserved on the wire.
    sent = {name.decode("latin-1") for name, _ in client_ws.accepted_headers}
    assert "X-Codex-Primary-Window-Minutes" in sent
    # Python /stats state refreshed with the same x-codex-* subset.
    assert captured == {
        "x-codex-primary-used-percent": "42",
        "X-Codex-Primary-Window-Minutes": "300",
    }


@pytest.mark.asyncio
async def test_ws_first_frame_timeout_after_connect_closes_upstream():
    """If the client never sends its first frame after we connected, the
    upstream WS must be closed (no leak) and the session deregistered.
    """
    upstream = _FakeUpstream([], hold_after_events=True)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    # No frames + hold => receive_text blocks until disconnect; we force a
    # short first-frame timeout so the handler hits the timeout branch.
    client_ws = _FakeWebSocket(frames=[], hold_after_initial=True)
    handler = _DummyOpenAIHandler()

    with (
        patch.dict(sys.modules, {"websockets": fake_ws_mod}),
        patch(
            "headroom.proxy.handlers.openai.WS_FIRST_FRAME_TIMEOUT_SECONDS",
            0.05,
        ),
    ):
        await asyncio.wait_for(
            handler.handle_openai_responses_ws(client_ws),
            timeout=2.0,
        )

    assert upstream.closed, "upstream not closed on first-frame timeout"
    assert client_ws.closed and client_ws.close_code == 1001
    assert handler.ws_sessions.active_count() == 0


@pytest.mark.asyncio
async def test_many_concurrent_sessions_cleanly_drained():
    """50 concurrent sessions: all drain; registry and named tasks go to 0."""
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]

    async def run_one() -> None:
        upstream = _FakeUpstream(list(upstream_events))
        fake_ws_mod = _make_fake_websockets_module(upstream)
        client_ws = _FakeWebSocket(frames=[_first_frame()])
        handler = _DummyOpenAIHandler()
        with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
            await handler.handle_openai_responses_ws(client_ws)
        assert handler.ws_sessions.active_count() == 0

    await asyncio.gather(*[run_one() for _ in range(50)])

    # Global check: no codex-ws-* named task remains.
    leaked = [
        t
        for t in asyncio.all_tasks()
        if (t.get_name() or "").startswith("codex-ws-") and not t.done()
    ]
    assert leaked == []


@pytest.mark.asyncio
async def test_ws_upstream_connect_allows_large_frames_and_no_pong_deadline():
    """The upstream WS must accept arbitrarily large frames and never impose a
    pong deadline.

    Image-generation turns expose two failure modes the relay was previously
    blind to: (1) the render phase goes silent for 20-60s with no data frames,
    so a 20s pong deadline false-kills the healthy upstream mid-render; and
    (2) the finished image arrives inline as a single base64 frame larger than
    the websockets default 1 MiB cap, raising ``PayloadTooBig`` just as it
    lands. Pin the connect kwargs so neither regresses.
    """
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    captured: dict = {}
    inner_connect = fake_ws_mod.connect

    async def _capturing_connect(*args, **kwargs):
        captured.update(kwargs)
        return await inner_connect(*args, **kwargs)

    fake_ws_mod.connect = _capturing_connect

    client_ws = _FakeWebSocket(frames=[_first_frame()])
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    assert captured.get("max_size") is None, "upstream frame size must be uncapped"
    assert captured.get("ping_timeout") is None, "upstream must not impose a pong deadline"


@pytest.mark.asyncio
async def test_ws_recognized_client_with_real_path_is_not_restamped():
    """A WS caller that already classifies on a real request path is not stamped."""
    upstream_events = [
        json.dumps({"type": "response.created", "response": {"id": "r_1"}}),
        json.dumps({"type": "response.completed", "response": {"id": "r_1"}}),
    ]
    upstream = _FakeUpstream(upstream_events)
    fake_ws_mod = _make_fake_websockets_module(upstream)

    client_ws = _FakeWebSocket(frames=[_first_frame()])
    # A non-empty url path (so the handler does not fall back to the default)
    # and a recognized codex UA (so should_stamp_codex_client returns False).
    client_ws.url = SimpleNamespace(path="/v1/responses")
    client_ws.headers = {"authorization": "Bearer test", "user-agent": "codex-cli/0.5"}
    handler = _DummyOpenAIHandler()

    with patch.dict(sys.modules, {"websockets": fake_ws_mod}):
        await handler.handle_openai_responses_ws(client_ws)

    # The forwarded handshake headers must not carry a proxy-injected x-client:
    # the caller already self-identifies via its User-Agent.
    assert "x-client" not in {k.lower() for k in client_ws.headers}
    assert handler.ws_sessions.active_count() == 0


def test_incremental_ccr_env_opts_out_of_the_lossless_swap(monkeypatch):
    from headroom.proxy.handlers import openai as handler_module

    monkeypatch.delenv("HEADROOM_INCREMENTAL_CCR", raising=False)
    assert not handler_module._openai_responses_incremental_ccr_enabled()
    monkeypatch.setenv("HEADROOM_INCREMENTAL_CCR", "1")
    assert handler_module._openai_responses_incremental_ccr_enabled()
    monkeypatch.setenv("HEADROOM_INCREMENTAL_CCR", "off")
    assert not handler_module._openai_responses_incremental_ccr_enabled()
