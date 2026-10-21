"""Human-review policy: deterministic, monotonic, auditable.

* Rules can only ADD review and RAISE urgency. Nothing in a field update, and
  nothing the model says, can remove a case from review once a rule fires.
* Rules are driven by deterministic signals and code-computed evidence, with
  the model's own flags as an extra (never sufficient-to-clear) input.
* A vague report with no risk signal is NOT sent to review: the cheapest fix
  is asking the rep, and a noisy queue teaches reviewers to ignore it.

| Rule                     | Trigger                                              | Urgency floor |
|--------------------------|------------------------------------------------------|---------------|
| health_safety_signal     | health keywords (unwell, vomiting, hospital, ...)    | critical      |
| contamination_signal     | contamination / expiry / tampering keywords          | high          |
| instruction_like_input   | prompt-injection patterns or model says manipulation | medium        |
| commercial_request       | price match / discount / refund / credit / scheme    | medium        |
| conflicting_evidence     | rep's stock claim contradicts the verified table     | medium        |
| degraded_mode            | LLM unavailable or its output rejected               | medium        |
| model_requested_review   | the model asked for review                           | (none)        |
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .baseline import template_details
from .evidence import EvidenceBuild
from .schemas import (
    MAX_ACTION_DETAILS_CHARS,
    MAX_MISSING_ITEM_CHARS,
    MAX_MISSING_ITEMS,
    MAX_REVIEW_REASON_CHARS,
    ActionType,
    ClaimType,
    EvidenceStatus,
    IssueCategory,
    LLMDraft,
    NextAction,
    Urgency,
    max_urgency,
)
from .signals import PreAnalysis
from .verified_data import VerifiedData


@dataclass(frozen=True)
class RuleHit:
    rule: str
    reason: str
    urgency_floor: Urgency | None


@dataclass
class PolicyOutcome:
    issue_category: IssueCategory
    urgency: Urgency
    action: NextAction
    missing_information: list[str]
    needs_human_review: bool
    review_reason: str | None
    rules_triggered: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _short(s: str, n: int = 80) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _first_quote(ev: EvidenceBuild, claim_type: ClaimType, pre: PreAnalysis, kind: str) -> str:
    """Short phrase to cite in the review reason: the matched signal words if any, else the model's quote."""
    sigs = pre.of(kind)
    if sigs:
        return _short(sigs[0].match)
    for e in ev.field_items:
        if e.claim_type == claim_type:
            return _short(e.quote)
    return ""


def evaluate_rules(
    draft: LLMDraft,
    ev: EvidenceBuild,
    pre: PreAnalysis,
    data: VerifiedData,
    *,
    degraded: bool,
    degraded_reason: str | None,
) -> list[RuleHit]:
    hits: list[RuleHit] = []
    claim_types = {e.claim_type for e in ev.field_items}

    # Escalation listens to BOTH the deterministic signals and the claims the model
    # extracted (grounded in the text). Either can add review; neither can remove it.
    if pre.has("health") or ClaimType.HEALTH_SAFETY in claim_types:
        q = _first_quote(ev, ClaimType.HEALTH_SAFETY, pre, "health")
        hits.append(RuleHit(
            "health_safety_signal",
            f'Possible consumer health incident reported ("{q}"). Urgent human review; the assistant does not assess medical causes or severity.',
            Urgency.CRITICAL,
        ))
    if pre.has("contamination"):
        q = _first_quote(ev, ClaimType.PRODUCT_QUALITY, pre, "contamination")
        hits.append(RuleHit(
            "contamination_signal",
            f'Possible contamination, tampering or expiry reported ("{q}"). Quality team review required.',
            Urgency.HIGH,
        ))
    if pre.has("injection") or ClaimType.INSTRUCTION_TO_SYSTEM in claim_types or draft.issue_category == IssueCategory.SUSPICIOUS_INPUT:
        q = _first_quote(ev, ClaimType.INSTRUCTION_TO_SYSTEM, pre, "injection")
        detail = f' ("{q}")' if q else ""
        hits.append(RuleHit(
            "instruction_like_input",
            f"Field update contains instruction-like text{detail} that tries to steer the system. It was treated as data and not followed.",
            Urgency.MEDIUM,
        ))
    if (
        pre.has("commercial")
        or ClaimType.PRICING_REQUEST in claim_types
        or draft.recommended_next_action.type == ActionType.ESCALATE_COMMERCIAL_TEAM
    ):
        hits.append(RuleHit(
            "commercial_request",
            "Commercial request (price match, discount, refund or similar). Only the commercial team can approve pricing changes.",
            Urgency.MEDIUM,
        ))
    if ev.has_status(EvidenceStatus.CONFLICTING):
        parts = []
        for row_id in ev.conflicting_rows():
            row = data.get(row_id)
            if row:
                parts.append(f"{row.outlet} / {row.sku}: system shows {row.system_stock} units")
        hits.append(RuleHit(
            "conflicting_evidence",
            f"Field report conflicts with verified stock ({'; '.join(parts) or 'see evidence'}). Do not act until reconciled.",
            Urgency.MEDIUM,
        ))
    if degraded:
        hits.append(RuleHit(
            "degraded_mode",
            f"Automated analysis unavailable ({degraded_reason or 'unknown error'}); rules-only triage was used.",
            Urgency.MEDIUM,
        ))
    if draft.needs_human_review and not degraded:
        reason = _short(draft.review_reason, 200) if draft.review_reason.strip() else "Model marked the case as uncertain."
        hits.append(RuleHit("model_requested_review", f"Model flagged for review: {reason}", None))
    return hits


def _fit_details(details: str, action: ActionType, pre: PreAnalysis, notes: list[str]) -> str:
    details = " ".join((details or "").split())
    if not details:
        notes.append("Empty action details replaced with a template.")
        return template_details(action, pre)
    if len(details) > MAX_ACTION_DETAILS_CHARS:
        cut = details[: MAX_ACTION_DETAILS_CHARS - 1]
        sentence_end = cut.rfind(". ")
        cut = cut[: sentence_end + 1] if sentence_end > 120 else cut.rsplit(" ", 1)[0] + "…"
        notes.append("Action details truncated to 300 characters.")
        return cut
    return details


def _mentions(items: list[str], *words: str) -> bool:
    low = " ".join(items).lower()
    return any(w in low for w in words)


def _fit_missing(items: list[str], category: IssueCategory, pre: PreAnalysis, ev: EvidenceBuild, notes: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        item = " ".join(item.split())
        if not item:
            continue
        if len(item) > MAX_MISSING_ITEM_CHARS:
            item = item[: MAX_MISSING_ITEM_CHARS - 1].rstrip() + "…"
        key = item.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(item)

    needs_location = category in (
        IssueCategory.STOCK_AVAILABILITY,
        IssueCategory.INVENTORY_DISCREPANCY,
        IssueCategory.PRODUCT_QUALITY_SAFETY,
        IssueCategory.INSUFFICIENT_INFORMATION,
    )
    front: list[str] = []
    if needs_location and not pre.outlets and not _mentions(cleaned, "outlet", "store", "shop", "retailer"):
        front.append("Outlet name (as listed in the outlet master)")
    if needs_location and not pre.skus and not _mentions(cleaned, "sku", "product", "pack size", "item", "brand"):
        front.append("Product / SKU affected, with pack size")
    claimed_impact = pre.has("sales_impact") or any(e.claim_type == ClaimType.SALES_IMPACT for e in ev.field_items)
    if claimed_impact and not _mentions(cleaned, "sales", "loss", "impact", "revenue", "value"):
        front.append("Lost-sales estimate (units or value): the report claims an impact but gives no figure")
    if front:
        notes.append("Added missing-information items: " + "; ".join(front))
    merged = front + cleaned
    if len(merged) > MAX_MISSING_ITEMS:
        notes.append(f"missing_information trimmed to {MAX_MISSING_ITEMS} items.")
    return merged[:MAX_MISSING_ITEMS]


def apply_policy(
    draft: LLMDraft,
    ev: EvidenceBuild,
    pre: PreAnalysis,
    data: VerifiedData,
    *,
    degraded: bool,
    degraded_reason: str | None = None,
) -> PolicyOutcome:
    notes: list[str] = []
    overrides: list[str] = []
    hits = evaluate_rules(draft, ev, pre, data, degraded=degraded, degraded_reason=degraded_reason)
    rule_names = {h.rule for h in hits}

    # Category consistency: a stock report that contradicts the table is a discrepancy by definition.
    category = draft.issue_category
    if "conflicting_evidence" in rule_names and category == IssueCategory.STOCK_AVAILABILITY:
        category = IssueCategory.INVENTORY_DISCREPANCY
        overrides.append("issue_category stock_availability -> inventory_discrepancy (report conflicts with verified stock)")

    urgency = draft.urgency
    for h in hits:
        if h.urgency_floor is not None:
            raised = max_urgency(urgency, h.urgency_floor)
            if raised != urgency:
                overrides.append(f"urgency {urgency.value} -> {raised.value} ({h.rule})")
                urgency = raised

    action_type = draft.recommended_next_action.type
    details = draft.recommended_next_action.details
    if "conflicting_evidence" in rule_names and action_type in (ActionType.RECOMMEND_REPLENISHMENT, ActionType.NO_ACTION):
        overrides.append(f"action {action_type.value} -> verify_physical_stock (never act on conflicting stock data)")
        action_type, details = ActionType.VERIFY_PHYSICAL_STOCK, template_details(ActionType.VERIFY_PHYSICAL_STOCK, pre)
    if "health_safety_signal" in rule_names and action_type in (ActionType.NO_ACTION, ActionType.RECOMMEND_REPLENISHMENT):
        overrides.append(f"action {action_type.value} -> escalate_quality_safety (health signal)")
        action_type, details = ActionType.ESCALATE_QUALITY_SAFETY, template_details(ActionType.ESCALATE_QUALITY_SAFETY, pre)
    if "instruction_like_input" in rule_names and action_type == ActionType.NO_ACTION:
        overrides.append("action no_action -> flag_security_review (instruction-like input)")
        action_type, details = ActionType.FLAG_SECURITY_REVIEW, template_details(ActionType.FLAG_SECURITY_REVIEW, pre)

    action = NextAction(type=action_type, details=_fit_details(details, action_type, pre, notes))
    missing = _fit_missing(draft.missing_information, category, pre, ev, notes)

    needs_review = bool(hits)
    review_reason = None
    if needs_review:
        review_reason = " ".join(h.reason for h in hits)
        if len(review_reason) > MAX_REVIEW_REASON_CHARS:
            review_reason = review_reason[: MAX_REVIEW_REASON_CHARS - 1].rstrip() + "…"

    return PolicyOutcome(
        issue_category=category,
        urgency=urgency,
        action=action,
        missing_information=missing,
        needs_human_review=needs_review,
        review_reason=review_reason,
        rules_triggered=[h.rule for h in hits],
        overrides=overrides,
        notes=notes,
    )
