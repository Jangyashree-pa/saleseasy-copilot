"""Anthropic Messages API adapter using structured outputs.

``output_config.format`` (type ``json_schema``) constrains the response to the
draft schema. The SDK retries transient errors once (``max_retries=1``);
everything else is normalised into ``LLMError`` without echoing payloads.
"""

from __future__ import annotations

import anthropic

from .base import LLMError, LLMRequest


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_s: float = 25.0,
        max_tokens: int = 4096,
        effort: str | None = None,
        max_retries: int = 1,
        http_client=None,
    ):
        self.model = model
        self._max_tokens = max_tokens
        self._effort = effort
        kwargs = {"api_key": api_key, "timeout": timeout_s, "max_retries": max_retries}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = anthropic.Anthropic(**kwargs)

    def generate(self, request: LLMRequest) -> str:
        output_config: dict = {"format": {"type": "json_schema", "schema": request.schema}}
        if self._effort:
            output_config["effort"] = self._effort
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=request.system,
                messages=request.messages,
                output_config=output_config,
            )
        except anthropic.APITimeoutError:
            raise LLMError("timeout", "Anthropic request timed out") from None
        except anthropic.RateLimitError:
            raise LLMError("rate_limited", "Anthropic rate limit") from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise LLMError("auth", "Anthropic authentication failed (check ANTHROPIC_API_KEY)") from None
        except anthropic.BadRequestError as exc:
            raise LLMError("bad_request", f"Anthropic rejected the request (HTTP {exc.status_code})") from None
        except anthropic.APIConnectionError:
            raise LLMError("unavailable", "Could not reach the Anthropic API") from None
        except anthropic.APIStatusError as exc:
            raise LLMError("unavailable", f"Anthropic API error (HTTP {exc.status_code})") from None

        if message.stop_reason == "refusal":
            raise LLMError("refused", "Model declined to answer")
        text = "".join(getattr(block, "text", "") for block in message.content if getattr(block, "type", "") == "text")
        if not text.strip():
            raise LLMError("empty", f"No text in response (stop_reason={message.stop_reason})")
        return text
