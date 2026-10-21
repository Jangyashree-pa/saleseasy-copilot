"""Adapter for any OpenAI-compatible Chat Completions endpoint.

Use this if your key is for OpenAI or another provider that exposes the same
API (set OPENAI_BASE_URL and OPENAI_MODEL). ``OPENAI_JSON_MODE`` selects strict
JSON-schema output (preferred), plain JSON mode, or none; the pipeline
validates and repairs the output either way.
"""

from __future__ import annotations

import time

import httpx2

from .base import LLMError, LLMRequest


class OpenAICompatClient:
    provider = "openai_compatible"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        json_mode: str = "json_schema",
        timeout_s: float = 25.0,
        retry_backoff_s: float = 1.0,
        http_client: httpx2.Client | None = None,
    ):
        self.model = model
        self._api_key = api_key
        self._backoff = retry_backoff_s
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._json_mode = json_mode
        self._client = http_client or httpx2.Client(timeout=timeout_s)

    def _body(self, request: LLMRequest) -> dict:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system}, *request.messages],
        }
        if self._json_mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "triage_draft", "strict": True, "schema": request.schema},
            }
        elif self._json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    def _post(self, body: dict) -> httpx2.Response:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        last_error = LLMError("unavailable")
        for attempt in range(2):  # one retry for transient failures
            try:
                response = self._client.post(self._url, json=body, headers=headers)
            except httpx2.TimeoutException:
                last_error = LLMError("timeout", "LLM request timed out")
            except httpx2.HTTPError:
                last_error = LLMError("unavailable", "Could not reach the LLM endpoint")
            else:
                if response.status_code == 429:
                    last_error = LLMError("rate_limited", "LLM rate limit")
                elif response.status_code >= 500:
                    last_error = LLMError("unavailable", f"LLM endpoint error (HTTP {response.status_code})")
                elif response.status_code in (401, 403):
                    raise LLMError("auth", "LLM authentication failed (check OPENAI_API_KEY)")
                elif response.status_code >= 400:
                    raise LLMError("bad_request", f"LLM endpoint rejected the request (HTTP {response.status_code})")
                else:
                    return response
            if attempt == 0 and self._backoff:
                time.sleep(self._backoff)
        raise last_error

    def generate(self, request: LLMRequest) -> str:
        response = self._post(self._body(request))
        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("empty", "Unexpected response shape from LLM endpoint") from None
        if message.get("refusal"):
            raise LLMError("refused", "Model declined to answer")
        content = message.get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not content.strip():
            raise LLMError("empty", "No content in LLM response")
        return content
