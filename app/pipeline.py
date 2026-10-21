"""Orchestration: pre-analysis -> LLM draft -> validate/repair -> evidence -> policy -> result.

Failure handling, in order of preference:
1. Model output valid on the first try.
2. Output invalid -> one repair request listing the exact violations.
3. Still invalid, or the provider failed -> rules-only triage, ``meta.degraded``
   set, human review forced. The API still returns schema-valid JSON.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from pydantic import ValidationError

from . import baseline
from .evidence import build_evidence
from .guards import Violation, check_draft
from .llm import FaultInjectingClient, LLMClient, LLMError, LLMRequest
from .policy import apply_policy
from .prompts import SYSTEM_PROMPT, build_repair_prompt, build_user_prompt, new_nonce
from .schemas import LLMDraft, Meta, NextAction, TriageResult, draft_json_schema
from .signals import analyze
from .verified_data import VerifiedData, get_verified_data

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_draft(raw: str) -> tuple[LLMDraft | None, str | None]:
    """Strictly validate the model's JSON. Tolerates code fences and leading prose, nothing else."""
    s = _FENCE_RE.sub("", (raw or "").strip())
    if not s.startswith("{"):
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            return None, "Response is not JSON."
        s = s[start : end + 1]
    try:
        return LLMDraft.model_validate_json(s), None
    except ValidationError as exc:
        problems = []
        for err in exc.errors()[:4]:
            loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
            problems.append(f"{loc}: {err.get('msg', 'invalid')}")
        return None, "Schema validation failed: " + "; ".join(problems)
    except (ValueError, json.JSONDecodeError):
        return None, "Response is not valid JSON."


class TriagePipeline:
    def __init__(self, llm: LLMClient, data: VerifiedData | None = None):
        self.llm = llm
        self.data = data or get_verified_data()
        self.schema = draft_json_schema(self.data.row_ids)

    def run(self, text: str, *, request_id: str | None = None, simulate_failure: str | None = None) -> TriageResult:
        started = time.perf_counter()
        request_id = request_id or uuid.uuid4().hex[:12]
        pre = analyze(text, self.data)
        client = FaultInjectingClient(self.llm, simulate_failure) if simulate_failure else self.llm

        messages = [{"role": "user", "content": build_user_prompt(text, pre, self.data, new_nonce())}]
        guard_notes: list[str] = []
        draft: LLMDraft | None = None
        degraded_reason: str | None = None
        attempts = 0

        for attempt in (1, 2):
            attempts = attempt
            try:
                raw = client.generate(
                    LLMRequest(
                        system=SYSTEM_PROMPT,
                        messages=list(messages),
                        schema=self.schema,
                        text=text,
                        pre=pre,
                        attempt=attempt,
                    )
                )
            except LLMError as exc:
                degraded_reason = f"LLM {exc.kind}"
                guard_notes.append(f"Attempt {attempt}: provider error ({exc.kind}).")
                break

            parsed, parse_error = parse_draft(raw)
            violations = [Violation("schema_invalid", parse_error or "invalid")] if parsed is None else check_draft(parsed, text, self.data)
            # On the final attempt, ungrounded quotes are dropped rather than rejecting the whole draft.
            blocking = violations if attempt == 1 else [v for v in violations if v.severity != "droppable"]
            if parsed is not None and not blocking:
                draft = parsed
                if violations:
                    guard_notes.append("Claims whose quotes could not be found in the update were dropped.")
                break

            codes = ", ".join(sorted({v.code for v in violations}))
            if attempt == 1:
                guard_notes.append(f"Attempt 1 rejected ({codes}); repair requested.")
                messages = [
                    *messages,
                    {"role": "assistant", "content": (raw or "")[:6000] or "(empty response)"},
                    {"role": "user", "content": build_repair_prompt(violations)},
                ]
            else:
                guard_notes.append(f"Attempt 2 rejected ({codes}); using rules-only triage.")
                degraded_reason = f"model output failed validation: {codes}"

        degraded = draft is None
        if draft is None:
            draft = baseline.triage(text, pre)

        evidence = build_evidence(text, pre, self.data, draft.claims)
        outcome = apply_policy(draft, evidence, pre, self.data, degraded=degraded, degraded_reason=degraded_reason)

        return TriageResult(
            issue_category=outcome.issue_category,
            urgency=outcome.urgency,
            evidence=evidence.all,
            recommended_next_action=NextAction(type=outcome.action.type, details=outcome.action.details),
            missing_information=outcome.missing_information,
            needs_human_review=outcome.needs_human_review,
            review_reason=outcome.review_reason,
            meta=Meta(
                request_id=request_id,
                provider=getattr(self.llm, "provider", "unknown"),
                model=getattr(self.llm, "model", None),
                degraded=degraded,
                llm_attempts=attempts,
                rules_triggered=outcome.rules_triggered,
                policy_overrides=outcome.overrides,
                guardrail_notes=[*guard_notes, *evidence.notes, *outcome.notes],
                latency_ms=int((time.perf_counter() - started) * 1000),
                analyzed_text=text,
            ),
        )
