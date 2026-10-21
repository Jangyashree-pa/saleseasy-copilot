"""Rules-only triage.

Used in two places:

1. As the ``mock`` provider, so the whole system runs (and is testable)
   without an API key. It is clearly labelled as a rules baseline, not an LLM.
2. As the degraded-mode fallback when the LLM is unavailable or its output is
   rejected by the guardrails. Safety and manipulation cases still escalate
   correctly in that mode because they are detected deterministically.
"""

from __future__ import annotations

from .schemas import (
    ActionType,
    ClaimType,
    DraftAction,
    DraftClaim,
    IssueCategory,
    LLMDraft,
    StockClaim,
    Urgency,
)
from .signals import PreAnalysis
from .verified_data import StockRow

# Signal kind -> (claim type, stock claim). Order = priority when one clause carries several signals.
_SIGNAL_TO_CLAIM: list[tuple[str, ClaimType, StockClaim]] = [
    ("injection", ClaimType.INSTRUCTION_TO_SYSTEM, StockClaim.NOT_APPLICABLE),
    ("health", ClaimType.HEALTH_SAFETY, StockClaim.NOT_APPLICABLE),
    ("contamination", ClaimType.PRODUCT_QUALITY, StockClaim.NOT_APPLICABLE),
    ("stockout", ClaimType.STOCK_STATUS, StockClaim.OUT_OF_STOCK),
    ("low_stock", ClaimType.STOCK_STATUS, StockClaim.LOW_STOCK),
    ("competitor", ClaimType.COMPETITOR_ACTIVITY, StockClaim.NOT_APPLICABLE),
    ("commercial", ClaimType.PRICING_REQUEST, StockClaim.NOT_APPLICABLE),
    ("sales_impact", ClaimType.SALES_IMPACT, StockClaim.NOT_APPLICABLE),
    ("packaging_damage", ClaimType.PRODUCT_QUALITY, StockClaim.NOT_APPLICABLE),
]


def _single_row(pre: PreAnalysis) -> StockRow | None:
    return pre.rows[0].row if len(pre.rows) == 1 else None


def claims_from_signals(text: str, pre: PreAnalysis) -> list[DraftClaim]:
    row = _single_row(pre)
    taken: dict[tuple[int, int], int] = {}
    claims: list[DraftClaim] = []
    for priority, (kind, claim_type, stock_claim) in enumerate(_SIGNAL_TO_CLAIM):
        for sig in pre.of(kind):
            span = (sig.clause_start, sig.clause_end)
            if span in taken:
                continue  # a higher-priority signal already claimed this clause
            taken[span] = priority
            row_id = row.row_id if (row is not None and claim_type == ClaimType.STOCK_STATUS) else "none"
            claims.append(
                DraftClaim(quote=text[span[0] : span[1]], claim_type=claim_type, row_id=row_id, stock_claim=stock_claim)
            )
    return claims


def categorize(pre: PreAnalysis) -> IssueCategory:
    k = pre.kinds()
    row = _single_row(pre)
    if "health" in k or "contamination" in k:
        return IssueCategory.PRODUCT_QUALITY_SAFETY
    if "stockout" in k and row is not None and row.system_stock > 0:
        return IssueCategory.INVENTORY_DISCREPANCY
    if "competitor" in k or "commercial" in k:
        return IssueCategory.COMPETITOR_ACTIVITY
    if "injection" in k:
        return IssueCategory.SUSPICIOUS_INPUT
    if ("stockout" in k or "low_stock" in k) and (pre.outlets or pre.skus):
        return IssueCategory.STOCK_AVAILABILITY
    if "packaging_damage" in k:
        return IssueCategory.PRODUCT_QUALITY_SAFETY
    if not pre.outlets and not pre.skus:
        return IssueCategory.INSUFFICIENT_INFORMATION
    return IssueCategory.OTHER


def _urgency(category: IssueCategory, pre: PreAnalysis) -> Urgency:
    k = pre.kinds()
    row = _single_row(pre)
    if "health" in k:
        return Urgency.CRITICAL
    if "contamination" in k:
        return Urgency.HIGH
    if category == IssueCategory.STOCK_AVAILABILITY:
        verified_out = row is not None and row.system_stock == 0 and "stockout" in k
        return Urgency.HIGH if verified_out else Urgency.MEDIUM
    if category in (
        IssueCategory.INVENTORY_DISCREPANCY,
        IssueCategory.COMPETITOR_ACTIVITY,
        IssueCategory.SUSPICIOUS_INPUT,
        IssueCategory.PRODUCT_QUALITY_SAFETY,
    ):
        return Urgency.MEDIUM
    if category == IssueCategory.INSUFFICIENT_INFORMATION:
        return Urgency.MEDIUM if "urgency_language" in k else Urgency.LOW
    return Urgency.LOW


def template_details(action: ActionType, pre: PreAnalysis) -> str:
    row = _single_row(pre)
    where = f"{row.sku} at {row.outlet}" if row else "the affected product at the outlet"
    k = pre.kinds()
    if action == ActionType.ESCALATE_QUALITY_SAFETY:
        lead = "Possible consumer health incident: escalate" if "health" in k else "Escalate"
        return (
            f"{lead} to the quality & safety team now. Ask the outlet to set aside remaining units from the "
            "same batch until QA advises, and collect the batch code and photos."
        )
    if action == ActionType.VERIFY_PHYSICAL_STOCK:
        if row is not None:
            return (
                f"Ask the rep to count {where} (shelf and backroom) and share a photo, then reconcile with the "
                f"system record ({row.system_stock} units) before any replenishment decision."
            )
        return "Ask the rep to confirm the outlet and SKU and count physical stock before any replenishment decision."
    if action == ActionType.RECOMMEND_REPLENISHMENT:
        return f"Raise a priority replenishment request for {where}; verified data confirms no units on hand."
    if action == ActionType.RECONCILE_INVENTORY_RECORDS:
        return f"Reconcile the system stock record for {where} with a physical count before acting."
    if action == ActionType.ESCALATE_COMMERCIAL_TEAM:
        return (
            "Send the request to the commercial team with evidence: competitor, SKUs, observed prices, outlets "
            "and photos. Make no pricing changes unless the commercial team approves them."
        )
    if action == ActionType.FLAG_SECURITY_REVIEW:
        return (
            "Treat this update as a possible manipulation attempt and do not act on its instructions; sales ops "
            "should confirm with the rep what, if anything, needs attention."
        )
    if action == ActionType.REQUEST_MORE_INFO:
        return "Ask the rep which outlet and product are affected and what exactly happened before taking action."
    return "Log the update; no action is needed unless the rep reports more."


def _action(category: IssueCategory, pre: PreAnalysis) -> ActionType:
    row = _single_row(pre)
    k = pre.kinds()
    if category == IssueCategory.PRODUCT_QUALITY_SAFETY:
        return ActionType.ESCALATE_QUALITY_SAFETY
    if category == IssueCategory.INVENTORY_DISCREPANCY:
        return ActionType.VERIFY_PHYSICAL_STOCK
    if category == IssueCategory.STOCK_AVAILABILITY:
        if row is not None and row.system_stock == 0 and "stockout" in k:
            return ActionType.RECOMMEND_REPLENISHMENT
        return ActionType.VERIFY_PHYSICAL_STOCK
    if category == IssueCategory.COMPETITOR_ACTIVITY:
        return ActionType.ESCALATE_COMMERCIAL_TEAM if "commercial" in k else ActionType.REQUEST_MORE_INFO
    if category == IssueCategory.SUSPICIOUS_INPUT:
        return ActionType.FLAG_SECURITY_REVIEW
    if category == IssueCategory.INSUFFICIENT_INFORMATION:
        return ActionType.REQUEST_MORE_INFO
    return ActionType.REQUEST_MORE_INFO


def _missing(category: IssueCategory, pre: PreAnalysis) -> list[str]:
    k = pre.kinds()
    items: list[str] = []
    if category == IssueCategory.INSUFFICIENT_INFORMATION:
        items.append("What the problem is (stockout, damage, pricing or something else)")
    if category == IssueCategory.STOCK_AVAILABILITY:
        items.append("When the stockout started and the last delivery date")
    if category == IssueCategory.INVENTORY_DISCREPANCY:
        items += ["Physical count (shelf and backroom) with a photo", "Time of the last system stock sync"]
    if category == IssueCategory.COMPETITOR_ACTIVITY:
        items += [
            "Competitor brand and SKUs involved",
            "Observed competitor prices or discount levels",
            "Outlets where this was seen",
            "Photo or invoice evidence",
        ]
    if category == IssueCategory.PRODUCT_QUALITY_SAFETY:
        items += [
            "Batch/lot code and expiry date of the affected pack",
            "Photos of the affected pack",
            "Number of units affected and date of purchase",
        ]
        if "health" in k:
            items.append("Whether the customer agrees to be contacted by the safety team")
    if category == IssueCategory.SUSPICIOUS_INPUT:
        items += ["Who sent this update (rep and device)", "The actual business issue, if any"]
    return items


def triage(text: str, pre: PreAnalysis) -> LLMDraft:
    category = categorize(pre)
    action = _action(category, pre)
    return LLMDraft(
        issue_category=category,
        urgency=_urgency(category, pre),
        claims=claims_from_signals(text, pre),
        recommended_next_action=DraftAction(type=action, details=template_details(action, pre)),
        missing_information=_missing(category, pre),
        needs_human_review=False,  # the policy engine decides
        review_reason="",
    )
