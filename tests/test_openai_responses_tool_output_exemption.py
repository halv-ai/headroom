from __future__ import annotations

import json
from types import MethodType, SimpleNamespace

from headroom.proxy.handlers.openai import OpenAIHandlerMixin
from headroom.transforms.content_router import (
    CompressionStrategy,
    ContentRouter,
    RouterCompressionResult,
)

EXEMPT_TRANSFORM = "router:openai:responses:function_call_output:exempt"


class TokenCounter:
    def count_text(self, text: str) -> int:
        return len(text.split())


def _handler() -> OpenAIHandlerMixin:
    router = ContentRouter()

    def compress(self, content: str, **_kwargs):
        return RouterCompressionResult(
            compressed="compressed output <<ccr:test>>",
            original=content,
            strategy_used=CompressionStrategy.KOMPRESS,
        )

    router.compress = MethodType(compress, router)
    handler = OpenAIHandlerMixin()
    handler.openai_pipeline = SimpleNamespace(transforms=[router])
    handler.openai_provider = SimpleNamespace(
        get_token_counter=lambda _model: TokenCounter(),
    )
    handler.config = SimpleNamespace(
        compress_responses_messages=False,
        compress_user_messages=False,
        protect_recent=0,
    )
    return handler


def _large_output() -> str:
    return " ".join(f"caller_{index}" for index in range(180))


def _payload(tool_name: str | None, *, call_id: str = "call_1") -> dict:
    items = []
    if tool_name is not None:
        items.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": tool_name,
                "arguments": "{}",
            }
        )
    items.append(
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": _large_output(),
        }
    )
    return {"model": "gpt-5", "input": items}


def _compress(payload: dict):
    return _handler()._compress_openai_responses_payload(
        payload,
        model="gpt-5",
        request_id="req_tool_output_exemption",
    )


def test_scip_tool_output_is_byte_identical_and_tagged(monkeypatch):
    monkeypatch.delenv("HEADROOM_COMPRESS_TOOL_OUTPUT_EXCLUDE", raising=False)
    payload = _payload("scip_map")
    original_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    new_payload, modified, saved, transforms, *_ = _compress(payload)

    new_bytes = json.dumps(new_payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert new_bytes == original_bytes
    assert modified is False
    assert saved == 0
    assert EXEMPT_TRANSFORM in transforms


def test_shell_tool_output_still_compresses(monkeypatch):
    monkeypatch.delenv("HEADROOM_COMPRESS_TOOL_OUTPUT_EXCLUDE", raising=False)
    payload = _payload("shell")

    new_payload, modified, saved, transforms, *_ = _compress(payload)

    assert modified is True
    assert saved > 0
    assert new_payload["input"][1]["output"] == "compressed output <<ccr:test>>"
    assert EXEMPT_TRANSFORM not in transforms
    assert "router:openai:responses:function_call_output:kompress" in transforms


def test_unresolvable_call_id_still_compresses(monkeypatch):
    monkeypatch.delenv("HEADROOM_COMPRESS_TOOL_OUTPUT_EXCLUDE", raising=False)
    payload = _payload(None, call_id="missing_call")

    new_payload, modified, saved, transforms, *_ = _compress(payload)

    assert modified is True
    assert saved > 0
    assert new_payload["input"][0]["output"] == "compressed output <<ccr:test>>"
    assert EXEMPT_TRANSFORM not in transforms
    assert "router:openai:responses:function_call_output:kompress" in transforms


def test_custom_regex_override_and_empty_value(monkeypatch):
    payload = _payload("precision_lookup")
    monkeypatch.setenv("HEADROOM_COMPRESS_TOOL_OUTPUT_EXCLUDE", r"^precision_")

    exempt_payload, exempt_modified, exempt_saved, exempt_transforms, *_ = _compress(payload)

    assert exempt_payload == payload
    assert exempt_modified is False
    assert exempt_saved == 0
    assert EXEMPT_TRANSFORM in exempt_transforms

    monkeypatch.setenv("HEADROOM_COMPRESS_TOOL_OUTPUT_EXCLUDE", "")
    compressed_payload, compressed_modified, compressed_saved, compressed_transforms, *_ = (
        _compress(payload)
    )

    assert compressed_modified is True
    assert compressed_saved > 0
    assert compressed_payload["input"][1]["output"] == "compressed output <<ccr:test>>"
    assert EXEMPT_TRANSFORM not in compressed_transforms
