"""Adapter tests against mocked HTTP transports (no network, no API key).

They pin down the exact request each provider receives and how provider
errors are normalised, which is the part that cannot be exercised without a key.
"""

import json

import httpx2
import pytest

from app.llm import LLMError, LLMRequest
from app.llm.anthropic_client import AnthropicClient
from app.llm.openai_compat import OpenAICompatClient
from app.prompts import SYSTEM_PROMPT
from app.schemas import draft_json_schema
from app.signals import analyze
from tests.conftest import draft_json


def _request(data, text="Metro Fresh has a stockout on Cola 500ml."):
    return LLMRequest(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "prompt"}],
        schema=draft_json_schema(data.row_ids),
        text=text,
        pre=analyze(text, data),
    )


def _anthropic_message(content, stop_reason="end_turn"):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def test_anthropic_request_uses_structured_outputs(data):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["key"] = request.headers.get("x-api-key")
        content = [{"type": "thinking", "thinking": "...", "signature": "sig"}, {"type": "text", "text": draft_json()}]
        return httpx2.Response(200, json=_anthropic_message(content))

    client = AnthropicClient("test-key", "claude-haiku-5-5", effort="low", max_retries=0,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    out = client.generate(_request(data))
    assert json.loads(out)["issue_category"] == "other"  # thinking block ignored, text returned
    body = captured["body"]
    assert captured["url"].endswith("/v1/messages")
    assert captured["key"] == "test-key"
    assert body["model"] == "claude-haiku-5-5"
    assert body["system"] == SYSTEM_PROMPT
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"][0] == "issue_category"
    assert body["output_config"]["effort"] == "low"
    assert "temperature" not in body


def test_schema_is_already_in_api_accepted_form(data):
    transform = pytest.importorskip("anthropic.lib._parse._transform").transform_schema
    schema = draft_json_schema(data.row_ids)
    assert transform(schema) == schema


@pytest.mark.parametrize(
    "status, kind",
    [(429, "rate_limited"), (401, "auth"), (400, "bad_request"), (529, "unavailable"), (500, "unavailable")],
)
def test_anthropic_errors_normalised(data, status, kind):
    handler = lambda request: httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "nope"}})
    client = AnthropicClient("test-key", "claude-haiku-5-5", max_retries=0,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(LLMError) as exc:
        client.generate(_request(data))
    assert exc.value.kind == kind
    assert "test-key" not in str(exc.value)


def test_anthropic_timeout_normalised(data):
    def handler(request):
        raise httpx2.ReadTimeout("slow", request=request)

    client = AnthropicClient("test-key", "claude-haiku-5-5", max_retries=0,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(LLMError) as exc:
        client.generate(_request(data))
    assert exc.value.kind == "timeout"


def test_anthropic_refusal(data):
    handler = lambda request: httpx2.Response(200, json=_anthropic_message([], stop_reason="refusal"))
    client = AnthropicClient("test-key", "claude-haiku-5-5", max_retries=0,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(LLMError) as exc:
        client.generate(_request(data))
    assert exc.value.kind == "refused"


def test_openai_compatible_request_shape(data):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, json={"choices": [{"message": {"role": "assistant", "content": draft_json()}}]})

    client = OpenAICompatClient("test-key", "some-model", base_url="https://llm.example/v1", retry_backoff_s=0,
                                http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    assert json.loads(client.generate(_request(data)))["urgency"] == "low"
    body = captured["body"]
    assert captured["auth"] == "Bearer test-key"
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True


def test_openai_compatible_retries_then_fails(data):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx2.Response(503, json={"error": "down"})

    client = OpenAICompatClient("test-key", "some-model", retry_backoff_s=0,
                                http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(LLMError) as exc:
        client.generate(_request(data))
    assert exc.value.kind == "unavailable"
    assert len(calls) == 2
