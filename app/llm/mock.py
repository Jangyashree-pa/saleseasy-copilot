"""Offline clients: the rules baseline, fault injection, and a scripted test double.

None of these call a network service. The API and UI label the baseline as
"MOCK (rules baseline)" so nobody mistakes it for an LLM.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from .. import baseline
from .base import LLMError, LLMRequest

FAILURE_MODES = ("timeout", "unavailable", "invalid_json", "schema_violation", "flaky_json")


class RulesBaselineClient:
    """Deterministic stand-in for an LLM: returns the rules-only triage as JSON."""

    provider = "mock"
    model = "rules-baseline"

    def generate(self, request: LLMRequest) -> str:
        draft = baseline.triage(request.text, request.pre)
        return draft.model_dump_json()


class FaultInjectingClient:
    """Wraps a client and simulates a failure mode.

    * timeout / unavailable: the provider call fails.
    * invalid_json:          the model returns truncated, non-JSON text (every attempt).
    * schema_violation:      valid JSON with an out-of-contract value (every attempt).
    * flaky_json:            the first attempt is malformed, the repair attempt succeeds.
    """

    def __init__(self, inner, mode: str):
        if mode not in FAILURE_MODES:
            raise ValueError(f"unknown failure mode {mode!r}")
        self.inner = inner
        self.mode = mode
        self.provider = getattr(inner, "provider", "unknown")
        self.model = getattr(inner, "model", None)

    def generate(self, request: LLMRequest) -> str:
        if self.mode == "timeout":
            raise LLMError("timeout", "Simulated LLM timeout")
        if self.mode == "unavailable":
            raise LLMError("unavailable", "Simulated LLM outage (HTTP 503)")
        if self.mode == "invalid_json" or (self.mode == "flaky_json" and request.attempt == 1):
            return 'Sure! Here is the triage you asked for: {"issue_category": "stock_availability", "urgency": '
        if self.mode == "schema_violation":
            return json.dumps(
                {
                    "issue_category": "stock_availability",
                    "urgency": "super-urgent",
                    "claims": [],
                    "recommended_next_action": {"type": "mark_resolved", "details": "Done."},
                    "missing_information": [],
                    "needs_human_review": False,
                    "review_reason": "",
                }
            )
        return self.inner.generate(request)


class ScriptedClient:
    """Test double that returns pre-programmed responses (strings or LLMError) in order."""

    provider = "scripted"
    model = "scripted"

    def __init__(self, responses: Iterable):
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> str:
        self.requests.append(request)
        if not self._responses:
            raise LLMError("empty", "ScriptedClient ran out of responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item
