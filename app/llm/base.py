"""Provider-agnostic LLM interface.

Every adapter returns the raw text of the model's JSON draft. Parsing and
validation happen in one place (the pipeline), so swapping providers cannot
weaken the guardrails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from ..signals import PreAnalysis


@dataclass
class LLMRequest:
    system: str
    messages: list[dict[str, str]]
    schema: dict
    # Offline clients (rules baseline, scripted test doubles) use these instead of the prompt.
    text: str
    pre: "PreAnalysis"
    attempt: int = 1


class LLMError(Exception):
    """A provider failure, normalised. ``kind`` never contains secrets or raw payloads."""

    KINDS = ("timeout", "unavailable", "rate_limited", "refused", "auth", "bad_request", "empty")

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


class LLMClient(Protocol):
    provider: str
    model: str | None

    def generate(self, request: LLMRequest) -> str: ...
