import asyncio
import base64
import json
import sys
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anyio
import pytest
from fastapi import Request

from headroom.proxy.handlers.openai import (
    OpenAIHandlerMixin,
    _count_openai_responses_payload_tokens,
    _is_allowed_websocket_origin,
    _openai_responses_unit_cache_key,
    _resolve_codex_routing_headers,
    _responses_input_to_token_messages,
)
from headroom.tokenizers import CharacterCounter, EstimatingTokenCounter
from headroom.transforms.content_router import ContentRouter


def _jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode(header)}.{encode(payload)}."


def test_resolve_codex_routing_prefers_explicit_header():
    headers, is_chatgpt = _resolve_codex_routing_headers(
        {
            "Authorization": "Bearer sk-test",
            "ChatGPT-Account-ID": "acct-explicit",
        }
    )

    assert is_chatgpt is True
    assert headers["ChatGPT-Account-ID"] == "acct-explicit"


def test_resolve_codex_routing_derives_account_id_from_oauth_jwt():
    token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-from-jwt",
            }
        }
    )

    headers, is_chatgpt = _resolve_codex_routing_headers(
        {
            "authorization": f"Bearer {token}",
        }
    )

    assert is_chatgpt is True
    assert headers["ChatGPT-Account-ID"] == "acct-from-jwt"


def test_resolve_codex_routing_leaves_regular_openai_bearer_tokens_unchanged():
    token = _jwt({"aud": ["https://api.openai.com/v1"]})

    headers, is_chatgpt = _resolve_codex_routing_headers(
        {
            "authorization": f"Bearer {token}",
        }
    )

    assert is_chatgpt is False
    assert "ChatGPT-Account-ID" not in headers


def test_resolve_codex_routing_returns_none_without_bearer_auth():
    headers, is_chatgpt = _resolve_codex_routing_headers({})

    assert is_chatgpt is False
    assert headers == {}


def test_resolve_codex_routing_ignores_non_jwt_bearer_tokens():
    headers, is_chatgpt = _resolve_codex_routing_headers(
        {
            "authorization": "Bearer not-a-jwt",
        }
    )

    assert is_chatgpt is False
    assert headers["authorization"] == "Bearer not-a-jwt"


def test_resolve_codex_routing_ignores_invalid_jwt_payloads():
    invalid_payload = base64.urlsafe_b64encode(b"not-json").decode("ascii").rstrip("=")
    token = f"test-header.{invalid_payload}.signature"

    headers, is_chatgpt = _resolve_codex_routing_headers(
        {
            "authorization": f"Bearer {token}",
        }
    )

    assert is_chatgpt is False
    assert headers["authorization"] == f"Bearer {token}"


def test_openai_responses_unit_cache_key_includes_recoverability_inputs() -> None:
    unit = SimpleNamespace(
        text="large tool output",
        provider="openai",
        endpoint="responses",
        role="tool",
        item_type="function_call_output",
        cache_zone="live",
        mutable=True,
        min_bytes=100,
        context=None,
        question=None,
        bias=None,
        metadata={},
    )

    default_key = _openai_responses_unit_cache_key(unit, model="gpt-5.4")
    aggressive_key = _openai_responses_unit_cache_key(
        unit,
        model="gpt-5.4",
        target_ratio=0.10,
    )
    balanced_key = _openai_responses_unit_cache_key(
        unit,
        model="gpt-5.4",
        target_ratio=0.50,
    )
    unit.retrieval_available = True
    retrieval_key = _openai_responses_unit_cache_key(unit, model="gpt-5.4")
    unit.retrieval_available = False
    no_retrieval_key = _openai_responses_unit_cache_key(unit, model="gpt-5.4")

    assert aggressive_key != default_key
    assert aggressive_key != balanced_key
    assert retrieval_key != no_retrieval_key


def test_responses_message_env_config_keeps_user_gate_independent(monkeypatch) -> None:
    from headroom.proxy.server import _proxy_config_from_env

    monkeypatch.delenv("HEADROOM_PROXY_CONFIG_JSON", raising=False)
    monkeypatch.setenv("HEADROOM_COMPRESS_RESPONSES_MESSAGES", "1")
    monkeypatch.setenv("HEADROOM_COMPRESS_USER_MESSAGES", "1")
    monkeypatch.setenv("HEADROOM_PROTECT_RECENT", "0")

    config = _proxy_config_from_env()

    assert config.compress_responses_messages is True
    assert config.compress_user_messages is True
    assert config.protect_recent == 0


class _DummyMetrics:
    def __init__(self) -> None:
        self.recorded_requests: list[dict] = []
        self.tokens_saved_total = 0
        self.requests_by_model: dict[str, int] = {}
        self.requests_by_provider: dict[str, int] = {}

    async def record_request(self, **kwargs):  # noqa: ANN003
        self.recorded_requests.append(kwargs)
        self.tokens_saved_total += int(kwargs.get("tokens_saved", 0))
        model = str(kwargs.get("model", "unknown"))
        provider = str(kwargs.get("provider", "unknown"))
        self.requests_by_model[model] = self.requests_by_model.get(model, 0) + 1
        self.requests_by_provider[provider] = self.requests_by_provider.get(provider, 0) + 1
        return None

    async def record_failed(self, **kwargs):  # noqa: ANN003
        return None


class _DummyTokenizer:
    def count_text(self, text):
        return len(str(text).split())

    def count_messages(self, messages):
        return len(messages)


class _ResponseStub:
    status_code = 200
    headers = {"content-type": "application/json", "content-length": "42"}
    content = b'{"id":"resp_123","output":[{"type":"message"}]}'

    def json(self):
        return {"usage": {"input_tokens": 2, "output_tokens": 1}}


class _ResponseWithoutUsageStub(_ResponseStub):
    content = b'{"id":"resp_123","output":[{"type":"message"}]}'

    def json(self):
        return {"id": "resp_123", "output": [{"type": "message"}]}


class _ResponseWithZeroUsageStub(_ResponseStub):
    def json(self):
        return {"usage": {"input_tokens": 0, "output_tokens": 1}}


class _DummyOpenAIHandler(OpenAIHandlerMixin):
    OPENAI_API_URL = "https://api.openai.com"

    def __init__(self) -> None:
        self.rate_limiter = None
        self.metrics = _DummyMetrics()
        self.config = SimpleNamespace(
            optimize=False,
            mode="balanced",
            retry_max_attempts=3,
            retry_base_delay_ms=10,
            retry_max_delay_ms=50,
            connect_timeout_seconds=10,
            openai_extra_headers=None,
        )
        self.usage_reporter = None
        self.openai_provider = SimpleNamespace(get_context_limit=lambda model: 128_000)
        self.openai_pipeline = SimpleNamespace(apply=MagicMock())
        self.anthropic_backend = None
        self.cost_tracker = None
        self.memory_handler = None
        self.traffic_learner = None
        # PR-A6 wires session-sticky `OpenAI-Beta` merging into the
        # responses HTTP handler — it reads `compute_session_id` to key
        # the SessionBetaTracker. The routing tests don't exercise the
        # tracker semantics themselves, so a fixed-id stub is enough.
        self.session_tracker_store = SimpleNamespace(
            compute_session_id=lambda *a, **k: "sess-openai-1",
        )
        self.captured_request: tuple[str, str, dict, dict] | None = None
        self.captured_stream_request: tuple[str, dict, dict] | None = None
        self.recorded_outcomes: list = []

    async def _next_request_id(self) -> str:
        return "req-1"

    def _extract_tags(self, headers: dict[str, str]) -> dict[str, str]:
        return {}

    async def _retry_request(self, method: str, url: str, headers: dict, body: dict, **kwargs):
        self.captured_request = (method, url, headers, body)
        return _ResponseStub()

    async def _run_compression_in_executor(self, fn, *, timeout: float):
        # Test stub for HeadroomProxy._run_compression_in_executor.
        # The real implementation runs `fn` on a bounded thread pool with
        # a wall-clock timeout; tests just need the callable invoked
        # synchronously so MagicMock call_count assertions fire.
        return fn()

    async def _count_tokens_offloaded(self, model, messages):  # noqa: ANN001, ANN201
        # Test stub for HeadroomProxy._count_tokens_offloaded: resolve the
        # tokenizer and count inline (the real method offloads to the executor).
        from headroom.tokenizers import get_tokenizer

        tokenizer = get_tokenizer(model)
        return tokenizer, tokenizer.count_messages(messages)

    async def _record_request_outcome(self, outcome) -> None:
        # Test stub: delegates to the production funnel so wire shape
        # matches HeadroomProxy._record_request_outcome.
        from headroom.proxy.outcome import emit_request_outcome

        self.recorded_outcomes.append(outcome)
        await emit_request_outcome(self, outcome)

    async def _stream_response(
        self,
        url: str,
        headers: dict,
        body: dict,
        provider: str,
        model: str,
        request_id: str,
        original_tokens: int,
        optimized_tokens: int,
        tokens_saved: int,
        transforms_applied: list[str],
        tags: dict[str, str],
        optimization_latency: float,
        memory_user_id: str | None = None,
        **kwargs,
    ):
        self.captured_stream_request = (url, headers, body)
        return SimpleNamespace(
            status_code=200,
            url=url,
            headers=headers,
            body=body,
            memory_user_id=memory_user_id,
        )


class _MemoryToolsOnlyHandler:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            inject_context=False,
            inject_tools=True,
            project_root_override="",
        )
        self.compute_calls = 0

    def compute_memory_tool_definitions(self, provider: str) -> list[dict]:
        self.compute_calls += 1
        assert provider == "openai"
        return [
            {
                "type": "function",
                "function": {
                    "name": "memory_search",
                    "description": "Search memory.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def has_memory_tool_calls(self, response: dict, provider: str) -> bool:
        return False


def _build_request(body: dict, headers: dict[str, str]) -> Request:
    payload = json.dumps(body).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "query_string": b"",
        "headers": [
            (key.lower().encode("utf-8"), value.encode("utf-8")) for key, value in headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }
    return Request(scope, receive)


def _use_response_without_usage(handler: _DummyOpenAIHandler) -> None:
    async def retry_request(method, url, headers, body, **kwargs):  # noqa: ANN001, ANN003
        handler.captured_request = (method, url, headers, body)
        return _ResponseWithoutUsageStub()

    handler._retry_request = retry_request


def _use_response_with_zero_usage(handler: _DummyOpenAIHandler) -> None:
    async def retry_request(method, url, headers, body, **kwargs):  # noqa: ANN001, ANN003
        handler.captured_request = (method, url, headers, body)
        return _ResponseWithZeroUsageStub()

    handler._retry_request = retry_request


def test_responses_array_accounting_includes_multipart_messages_tool_output_and_arguments(
    monkeypatch,
):
    payload = {
        "model": "gpt-5.5",
        "instructions": "Reply with one sentence.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "A" * 400},
                    {"type": "output_text", "text": "B" * 300},
                    {"type": "text", "text": "C" * 200},
                    {"type": "input_image", "text": "ignored-image-text"},
                ],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{"path":"src/large_file.py","line_end":400}',
            },
            {
                "type": "function_call_output",
                "id": "out_1",
                "call_id": "call_1",
                "output": {"stdout": "D" * 600, "exit_code": 0},
            },
            {
                "type": "computer_call_output",
                "id": "out_2",
                "call_id": "call_2",
                "output": "E" * 300,
            },
        ],
    }
    counter = CharacterCounter(chars_per_token=1.0)
    accounting_messages = _responses_input_to_token_messages(
        payload["instructions"], payload["input"]
    )

    assert [message["role"] for message in accounting_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert accounting_messages[1]["content"] == f"{'A' * 400}\n{'B' * 300}\n{'C' * 200}"
    assert "ignored-image-text" not in accounting_messages[1]["content"]
    assert accounting_messages[2]["tool_calls"][0]["function"]["arguments"] == (
        '{"path":"src/large_file.py","line_end":400}'
    )
    assert '"stdout":"' in accounting_messages[3]["content"]
    assert accounting_messages[4]["content"] == "E" * 300

    handler = _DummyOpenAIHandler()
    _use_response_without_usage(handler)
    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda _model: counter)

    response = anyio.run(
        handler.handle_openai_responses,
        _build_request(payload, {"Authorization": "Bearer sk-test"}),
    )

    expected = _count_openai_responses_payload_tokens(counter, payload)
    shortened = json.loads(json.dumps(payload))
    shortened["input"][2]["output"] = "ok"
    assert response.status_code == 200
    assert expected > 1_500
    assert _count_openai_responses_payload_tokens(counter, shortened) < expected
    assert handler.recorded_outcomes[-1].original_tokens == expected
    assert handler.recorded_outcomes[-1].optimized_tokens == expected
    assert handler.recorded_outcomes[-1].tokens_saved == 0
    assert handler.metrics.recorded_requests[-1]["input_tokens"] == expected


def test_responses_zero_upstream_usage_keeps_nonempty_payload_fallback(monkeypatch):
    payload = {
        "model": "gpt-5.5",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "payload text " * 200}],
            }
        ],
    }
    counter = CharacterCounter(chars_per_token=4.0)
    handler = _DummyOpenAIHandler()
    _use_response_with_zero_usage(handler)
    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda _model: counter)

    response = anyio.run(
        handler.handle_openai_responses,
        _build_request(payload, {"Authorization": "Bearer sk-test"}),
    )

    expected = _count_openai_responses_payload_tokens(counter, payload)
    outcome = handler.recorded_outcomes[-1]
    assert response.status_code == 200
    assert expected > 0
    assert outcome.original_tokens == expected
    assert outcome.optimized_tokens == expected
    assert outcome.tokens_saved == 0


def test_handle_openai_responses_routes_chatgpt_auth_to_backend_api(monkeypatch):
    token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-from-jwt",
            }
        }
    )
    request = _build_request(
        {"model": "gpt-5.4", "input": "hello"},
        {"Authorization": f"Bearer {token}"},
    )
    handler = _DummyOpenAIHandler()

    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())

    response = anyio.run(handler.handle_openai_responses, request)

    assert handler.captured_request is not None
    method, url, headers, body = handler.captured_request
    assert method == "POST"
    assert url == "https://chatgpt.com/backend-api/codex/responses"
    assert headers["ChatGPT-Account-ID"] == "acct-from-jwt"
    assert body["input"] == "hello"
    assert body["store"] is False
    assert response.status_code == 200


def test_handle_openai_responses_strips_codex_lite_header_upstream(monkeypatch):
    # OpenAI rejects newer Codex models when the client-only lite header leaks
    # upstream. The HTTP POST path must drop it like the WS handler does, while
    # leaving adjacent headers intact.
    token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-from-jwt",
            }
        }
    )
    request = _build_request(
        {"model": "gpt-5.4", "input": "hello"},
        {
            "Authorization": f"Bearer {token}",
            "X-OpenAI-Internal-Codex-Responses-Lite": "true",
            "X-OpenAI-Debug": "keep-me",
        },
    )
    handler = _DummyOpenAIHandler()

    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())

    response = anyio.run(handler.handle_openai_responses, request)

    assert response.status_code == 200
    assert handler.captured_request is not None
    _method, _url, headers, _body = handler.captured_request
    lowered = {k.lower(): v for k, v in headers.items()}
    assert "x-openai-internal-codex-responses-lite" not in lowered
    assert lowered.get("x-openai-debug") == "keep-me"


def test_handle_openai_responses_chatgpt_auth_skips_memory_tools(monkeypatch):
    token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-from-jwt",
            }
        }
    )
    request = _build_request(
        {"model": "gpt-5.4", "input": "hello", "store": True},
        {"Authorization": f"Bearer {token}", "x-headroom-user-id": "user-1"},
    )
    handler = _DummyOpenAIHandler()
    memory_handler = _MemoryToolsOnlyHandler()
    handler.memory_handler = memory_handler
    handler.session_tracker_store = SimpleNamespace(
        compute_session_id=lambda *a, **k: "sess-chatgpt-no-memory-tools",
    )

    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())

    response = anyio.run(handler.handle_openai_responses, request)

    assert response.status_code == 200
    assert handler.captured_request is not None
    _, url, _, body = handler.captured_request
    assert url == "https://chatgpt.com/backend-api/codex/responses"
    assert body["store"] is False
    assert "tools" not in body
    assert memory_handler.compute_calls == 0


def test_handle_openai_responses_chatgpt_codex_timeout_fails_open(monkeypatch):
    token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-from-jwt",
            }
        }
    )
    request = _build_request(
        {"model": "gpt-5.4", "input": "large context"},
        {"Authorization": f"Bearer {token}"},
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True

    async def timeout_compression(*args, **kwargs):  # noqa: ANN002, ANN003
        raise asyncio.TimeoutError()

    handler._compress_openai_responses_payload_in_executor = timeout_compression
    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())

    response = anyio.run(handler.handle_openai_responses, request)

    assert response.status_code == 200
    assert handler.captured_request is not None
    method, url, headers, body = handler.captured_request
    assert method == "POST"
    assert url == "https://chatgpt.com/backend-api/codex/responses"
    assert body["input"] == "large context"
    assert body["store"] is False


def test_handle_openai_responses_api_auth_store_false_skips_memory_tools(monkeypatch):
    request = _build_request(
        {"model": "gpt-4o-mini", "input": "hello", "store": False},
        {"Authorization": "Bearer sk-test", "x-headroom-user-id": "user-1"},
    )
    handler = _DummyOpenAIHandler()
    memory_handler = _MemoryToolsOnlyHandler()
    handler.memory_handler = memory_handler
    handler.session_tracker_store = SimpleNamespace(
        compute_session_id=lambda *a, **k: "sess-api-memory-tools",
    )

    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())

    response = anyio.run(handler.handle_openai_responses, request)

    assert response.status_code == 200
    assert handler.captured_request is not None
    _, url, _, body = handler.captured_request
    assert url == "https://api.openai.com/v1/responses"
    assert body["store"] is False
    assert "tools" not in body
    assert memory_handler.compute_calls == 1


def test_handle_openai_responses_routes_api_key_auth_direct_to_openai(monkeypatch):
    request = _build_request(
        {"model": "gpt-4o-mini", "input": "hello"},
        {"Authorization": "Bearer sk-test"},
    )
    handler = _DummyOpenAIHandler()

    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())

    response = anyio.run(handler.handle_openai_responses, request)

    assert handler.captured_request is not None
    method, url, headers, body = handler.captured_request
    assert method == "POST"
    assert url == "https://api.openai.com/v1/responses"
    assert headers.get("ChatGPT-Account-ID") is None
    assert body["input"] == "hello"
    assert response.status_code == 200
    outcome = handler.recorded_outcomes[-1]
    assert outcome.original_tokens == 1
    assert outcome.optimized_tokens == 1
    assert outcome.provider_input_tokens == 2
    assert outcome.tokens_saved == 0


def test_handle_openai_responses_internal_compression_headers_override_config_and_are_stripped(
    monkeypatch,
):
    request = _build_request(
        {"model": "gpt-5.4", "input": "hello"},
        {
            "Authorization": "Bearer sk-test",
            "x-headroom-compress-messages": "true",
            "x-headroom-protect-recent": "0",
        },
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.protect_recent = 4
    captured: dict[str, bool | int] = {}

    async def fake_compress(payload, **kwargs):  # noqa: ANN001, ANN003
        captured["compress_responses_messages"] = bool(kwargs.get("compress_responses_messages"))
        captured["protect_recent"] = int(kwargs["protect_recent"])
        return payload, False, 0, [], "router_no_compression", 10, 10, 0, {}

    handler._compress_openai_responses_payload_in_executor = fake_compress

    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())

    response = anyio.run(handler.handle_openai_responses, request)

    assert response.status_code == 200
    assert captured["compress_responses_messages"] is True
    assert captured["protect_recent"] == 0
    assert handler.captured_request is not None
    _method, _url, headers, _body = handler.captured_request
    forwarded_header_names = {key.lower() for key in headers}
    assert "x-headroom-compress-messages" not in forwarded_header_names
    assert "x-headroom-protect-recent" not in forwarded_header_names


def test_handle_openai_responses_replays_large_codex_array_input_with_truthful_accounting(
    monkeypatch,
):
    file_text = "\n".join(
        f"{line:02d}: def process_record_{line}(record): return record.get('field_{line}') "
        "# deterministic fixture content with enough detail to resemble a pasted source file"
        for line in range(40)
    )
    pasted_file = "\n".join([file_text, file_text, file_text])
    payload = {
        "model": "gpt-5.5",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": pasted_file}],
            }
        ],
        "instructions": "Reply ok only.",
    }
    counter = EstimatingTokenCounter()
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.compress_responses_messages = False
    handler.config.compress_user_messages = False
    handler.config.protect_recent = None
    handler.openai_pipeline = SimpleNamespace(transforms=[ContentRouter()])
    handler.openai_provider = SimpleNamespace(
        get_context_limit=lambda _model: 128_000,
        get_token_counter=lambda _model: counter,
    )
    _use_response_without_usage(handler)
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda _model: counter)

    response = anyio.run(
        handler.handle_openai_responses,
        _build_request(
            payload,
            {
                "Authorization": "Bearer sk-test",
                "x-headroom-compress-messages": "true",
            },
        ),
    )

    outcome = handler.recorded_outcomes[-1]
    assert response.status_code == 200
    assert outcome.original_tokens > 2_000
    # The header grants user-message eligibility, but protect_recent defaults
    # to 6. This sole message is the newest turn, so it must remain verbatim.
    assert outcome.optimized_tokens == outcome.original_tokens
    assert outcome.tokens_saved == 0
    assert handler.captured_request is not None
    _method, _url, headers, sent_body = handler.captured_request
    assert sent_body == payload
    assert "x-headroom-compress-messages" not in {key.lower() for key in headers}


def test_handle_openai_responses_message_savings_reach_metrics_log_and_cost_once(monkeypatch):
    old_text = "\n".join(
        f"old segment {idx} contains deterministic message compression context and detail."
        for idx in range(40)
    )
    payload = {
        "model": "gpt-5.5",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": old_text}],
            },
            *[
                {
                    "type": "message",
                    "role": "assistant" if idx % 2 else "user",
                    "content": [{"type": "text", "text": f"recent turn {idx}"}],
                }
                for idx in range(6)
            ],
        ],
    }
    counter = EstimatingTokenCounter()
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True
    handler.config.compress_responses_messages = False
    handler.config.compress_user_messages = False
    handler.config.protect_recent = None
    handler.openai_pipeline = SimpleNamespace(transforms=[ContentRouter()])
    handler.openai_provider = SimpleNamespace(
        get_context_limit=lambda _model: 128_000,
        get_token_counter=lambda _model: counter,
    )
    handler.cost_tracker = SimpleNamespace(record_tokens=MagicMock(), stats=lambda: {})
    logged_requests = []
    handler.logger = SimpleNamespace(_logs=logged_requests, log=logged_requests.append)
    _use_response_without_usage(handler)
    monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda _model: counter)

    response = anyio.run(
        handler.handle_openai_responses,
        _build_request(
            payload,
            {
                "Authorization": "Bearer sk-test",
                "X-Client": "codex",
                "x-headroom-compress-messages": "true",
            },
        ),
    )

    outcome = handler.recorded_outcomes[-1]
    metrics_call = handler.metrics.recorded_requests[-1]
    request_log = logged_requests[-1]
    assert response.status_code == 200
    assert outcome.tokens_saved > 0
    assert outcome.original_tokens - outcome.optimized_tokens == outcome.tokens_saved
    assert metrics_call["tokens_saved"] == outcome.tokens_saved
    assert request_log.tokens_saved == outcome.tokens_saved
    assert request_log.input_tokens_original == outcome.original_tokens
    assert request_log.input_tokens_optimized == outcome.optimized_tokens
    assert handler.cost_tracker.record_tokens.call_count == 1

    from headroom.proxy.cost import build_session_summary
    from headroom.proxy.server import _build_agent_usage_summary

    session_summary = build_session_summary(
        handler,
        handler.metrics,
        {},
        outcome.original_tokens,
    )
    assert session_summary["compression"]["requests_compressed"] == 1
    assert session_summary["compression"]["total_tokens_removed"] == outcome.tokens_saved

    agent_usage = _build_agent_usage_summary(
        [asdict(request_log)],
        requests_by_provider=handler.metrics.requests_by_provider,
        requests_by_model=handler.metrics.requests_by_model,
        global_before_tokens=outcome.original_tokens,
        global_after_tokens=outcome.optimized_tokens,
        global_tokens_saved=outcome.tokens_saved,
        global_output_tokens=outcome.output_tokens,
    )
    codex = next(row for row in agent_usage["agents"] if row["agent"] == "codex")
    assert codex["before_tokens"] == outcome.original_tokens
    assert codex["after_tokens"] == outcome.optimized_tokens
    assert codex["tokens_saved"] == outcome.tokens_saved


def test_handle_openai_responses_stream_skips_python_compression(monkeypatch):
    """PR-C5: Python no longer compresses /v1/responses (Rust handles it
    natively). The streaming forward path must still fire — only the
    Python compression dispatch is retired."""
    request = _build_request(
        {
            "model": "gpt-5.4",
            "stream": True,
            "instructions": "Keep it short",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
        },
        {"Authorization": "Bearer sk-test"},
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True

    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())

    response = anyio.run(handler.handle_openai_responses, request)

    assert response.status_code == 200
    assert handler.captured_stream_request is not None
    assert handler.openai_pipeline.apply.call_count == 0
    assert handler.captured_stream_request[2]["stream"] is True


def test_handle_openai_responses_memory_timeout_fails_open(monkeypatch):
    class _SlowMemoryHandler:
        def __init__(self):
            self.config = SimpleNamespace(inject_context=True, inject_tools=False)

        async def search_and_format_context(self, memory_user_id, messages, **_kwargs):
            return "should not be used"

        def has_memory_tool_calls(self, response, provider):
            return False

    async def _timeout_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    request = _build_request(
        {"model": "gpt-5.4", "input": "hello"},
        {"Authorization": "Bearer sk-test", "x-headroom-user-id": "user-1"},
    )
    handler = _DummyOpenAIHandler()
    handler.memory_handler = _SlowMemoryHandler()

    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())
    monkeypatch.setattr("headroom.proxy.handlers.openai.asyncio.wait_for", _timeout_wait_for)

    response = anyio.run(handler.handle_openai_responses, request)

    assert response.status_code == 200
    assert handler.captured_request is not None
    _, _, _, body = handler.captured_request
    assert body.get("instructions") is None


def test_codex_responses_timeout_fails_open_in_standalone_proxy(monkeypatch):
    """Codex users running only the proxy still get fail-open on timeout."""
    request = _build_request(
        {
            "model": "gpt-5.4",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "large tool output",
                }
            ],
        },
        {"Authorization": "Bearer sk-test", "x-client": "codex"},
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = True

    monkeypatch.setattr("headroom.tokenizers.get_tokenizer", lambda model: _DummyTokenizer())
    monkeypatch.setattr(
        handler,
        "_compress_openai_responses_payload",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    response = anyio.run(handler.handle_openai_responses, request)

    assert response.status_code == 200
    assert handler.captured_request is not None
    _, url, _, body = handler.captured_request
    assert url == "https://api.openai.com/v1/responses"
    assert body["input"][0]["output"] == "large tool output"


class _DummyWebSocket:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers
        self.accepted_subprotocol = None
        self.closed = False
        self.close_code = None
        self.close_reason = None

    async def accept(self, subprotocol=None, headers=None):
        self.accepted_subprotocol = subprotocol

    async def close(self, code=1000, reason=None):
        self.closed = True
        self.close_code = code
        self.close_reason = reason


def test_websocket_origin_policy_allows_native_clients_without_origin(monkeypatch):
    monkeypatch.delenv("HEADROOM_WS_ORIGINS", raising=False)
    monkeypatch.delenv("HEADROOM_CORS_ORIGINS", raising=False)

    assert _is_allowed_websocket_origin({"authorization": "Bearer token"}) is True


def test_websocket_origin_policy_allows_loopback_origins_by_default(monkeypatch):
    monkeypatch.delenv("HEADROOM_WS_ORIGINS", raising=False)
    monkeypatch.delenv("HEADROOM_CORS_ORIGINS", raising=False)

    assert _is_allowed_websocket_origin({"origin": "http://localhost:3000"}) is True
    assert _is_allowed_websocket_origin({"origin": "https://127.0.0.1:8787"}) is True


def test_websocket_origin_policy_requires_config_for_remote_origins(monkeypatch):
    monkeypatch.delenv("HEADROOM_WS_ORIGINS", raising=False)
    monkeypatch.delenv("HEADROOM_CORS_ORIGINS", raising=False)

    assert _is_allowed_websocket_origin({"origin": "https://remote.example"}) is False
    assert _is_allowed_websocket_origin({"origin": "http://"}) is False


def test_websocket_origin_policy_can_be_pinned_with_env(monkeypatch):
    monkeypatch.setenv("HEADROOM_WS_ORIGINS", "https://dash.example.com")
    monkeypatch.delenv("HEADROOM_CORS_ORIGINS", raising=False)

    assert _is_allowed_websocket_origin({"origin": "https://dash.example.com"}) is True
    assert _is_allowed_websocket_origin({"origin": "http://localhost:3000"}) is False


def test_handle_openai_responses_ws_resolves_codex_routing_headers():
    class SentinelError(RuntimeError):
        pass

    handler = _DummyOpenAIHandler()
    websocket = _DummyWebSocket({"authorization": "Bearer token"})

    with patch.dict(sys.modules, {"websockets": MagicMock()}):
        with patch(
            "headroom.proxy.handlers.openai._resolve_codex_routing_headers",
            side_effect=SentinelError("resolved"),
        ):
            with pytest.raises(SentinelError, match="resolved"):
                anyio.run(handler.handle_openai_responses_ws, websocket)


def test_handle_openai_responses_ws_closes_unconfigured_origin(monkeypatch):
    handler = _DummyOpenAIHandler()
    websocket = _DummyWebSocket({"origin": "https://remote.example"})

    monkeypatch.delenv("HEADROOM_WS_ORIGINS", raising=False)
    monkeypatch.delenv("HEADROOM_CORS_ORIGINS", raising=False)

    with patch.dict(sys.modules, {"websockets": MagicMock()}):
        with patch(
            "headroom.proxy.handlers.openai._resolve_codex_routing_headers",
            side_effect=AssertionError("routing should not run"),
        ):
            anyio.run(handler.handle_openai_responses_ws, websocket)

    assert websocket.closed is True
    assert websocket.close_code == 1008
    assert websocket.close_reason == "origin not allowed"
    assert websocket.accepted_subprotocol is None
