"""Data contracts.

Two layers, on purpose:

* ``LLMDraft`` is everything the language model is allowed to produce. It is
  constrained at generation time (JSON schema / structured outputs) and then
  re-validated with Pydantic, because a schema guarantees shape, not truth.
* ``TriageResult`` is what the API returns. It is assembled by code: evidence
  status, table values, the review decision and the review reason are all
  computed deterministically from the draft, the field update and the
  verified table. The model never gets to label something "verified".
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

APP_VERSION = "1.0.0"

MAX_ACTION_DETAILS_CHARS = 300
MAX_MISSING_ITEMS = 5
MAX_MISSING_ITEM_CHARS = 160
MAX_REVIEW_REASON_CHARS = 700


class IssueCategory(str, Enum):
    STOCK_AVAILABILITY = "stock_availability"
    INVENTORY_DISCREPANCY = "inventory_discrepancy"
    COMPETITOR_ACTIVITY = "competitor_activity"
    PRODUCT_QUALITY_SAFETY = "product_quality_safety"
    SUSPICIOUS_INPUT = "suspicious_input"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    OTHER = "other"


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


URGENCY_RANK = {Urgency.LOW: 0, Urgency.MEDIUM: 1, Urgency.HIGH: 2, Urgency.CRITICAL: 3}


def max_urgency(a: Urgency, b: Urgency) -> Urgency:
    return a if URGENCY_RANK[a] >= URGENCY_RANK[b] else b


class ActionType(str, Enum):
    """Allowed next actions.

    There is deliberately no action that approves or executes a commercial
    change (price match, discount, refund, credit) and no "mark resolved":
    the copilot recommends, people decide.
    """

    REQUEST_MORE_INFO = "request_more_info"
    VERIFY_PHYSICAL_STOCK = "verify_physical_stock"
    RECOMMEND_REPLENISHMENT = "recommend_replenishment"
    RECONCILE_INVENTORY_RECORDS = "reconcile_inventory_records"
    ESCALATE_COMMERCIAL_TEAM = "escalate_commercial_team"
    ESCALATE_QUALITY_SAFETY = "escalate_quality_safety"
    FLAG_SECURITY_REVIEW = "flag_security_review"
    NO_ACTION = "no_action"


class ClaimType(str, Enum):
    STOCK_STATUS = "stock_status"
    SALES_IMPACT = "sales_impact"
    COMPETITOR_ACTIVITY = "competitor_activity"
    PRICING_REQUEST = "pricing_request"
    PRODUCT_QUALITY = "product_quality"
    HEALTH_SAFETY = "health_safety"
    INSTRUCTION_TO_SYSTEM = "instruction_to_system"
    OTHER = "other"


class StockClaim(str, Enum):
    OUT_OF_STOCK = "out_of_stock"
    LOW_STOCK = "low_stock"
    IN_STOCK = "in_stock"
    NOT_APPLICABLE = "not_applicable"


class EvidenceStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED_CLAIM = "unverified_claim"
    CONFLICTING = "conflicting"


# --------------------------------------------------------------------------
# What the LLM may produce
# --------------------------------------------------------------------------


class DraftClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str
    claim_type: ClaimType
    row_id: str = "none"
    stock_claim: StockClaim = StockClaim.NOT_APPLICABLE


class DraftAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType
    details: str


class LLMDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_category: IssueCategory
    urgency: Urgency
    claims: list[DraftClaim]
    recommended_next_action: DraftAction
    missing_information: list[str]
    needs_human_review: bool
    review_reason: str = ""


def _enum_schema(enum_cls: type[Enum], description: str) -> dict:
    return {"type": "string", "enum": [m.value for m in enum_cls], "description": description}


def draft_json_schema(row_ids: list[str]) -> dict:
    """JSON schema sent to the model (structured outputs).

    Written by hand so it stays inside the subset that both Anthropic
    structured outputs and OpenAI strict mode accept: every property
    required, ``additionalProperties: false``, string enums, no length
    keywords (those are enforced afterwards in code).
    """

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "issue_category": _enum_schema(IssueCategory, "Primary issue in the field update."),
            "urgency": _enum_schema(Urgency, "How quickly someone must act."),
            "claims": {
                "type": "array",
                "description": "Key claims made in the field update, each with an exact quote.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "quote": {
                            "type": "string",
                            "description": "Exact text copied from the field update (3-20 words). No paraphrasing, no ellipses.",
                        },
                        "claim_type": _enum_schema(ClaimType, "What kind of claim this is."),
                        "row_id": {
                            "type": "string",
                            "enum": [*row_ids, "none"],
                            "description": "Verified stock row this claim is about, or 'none'.",
                        },
                        "stock_claim": _enum_schema(
                            StockClaim, "What the rep claims about stock level; not_applicable otherwise."
                        ),
                    },
                    "required": ["quote", "claim_type", "row_id", "stock_claim"],
                },
            },
            "recommended_next_action": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": _enum_schema(ActionType, "The single recommended next action."),
                    "details": {
                        "type": "string",
                        "description": "One or two sentences, max 300 characters, specific to this update.",
                    },
                },
                "required": ["type", "details"],
            },
            "missing_information": {
                "type": "array",
                "description": "Up to 5 short items a reviewer would need before acting.",
                "items": {"type": "string"},
            },
            "needs_human_review": {"type": "boolean"},
            "review_reason": {
                "type": "string",
                "description": "One sentence explaining why review is needed, or empty string.",
            },
        },
        "required": [
            "issue_category",
            "urgency",
            "claims",
            "recommended_next_action",
            "missing_information",
            "needs_human_review",
            "review_reason",
        ],
    }


# --------------------------------------------------------------------------
# What the API returns
# --------------------------------------------------------------------------


class StockRecord(BaseModel):
    outlet: str
    sku: str
    system_stock: int


class FieldUpdateEvidence(BaseModel):
    """A claim quoted verbatim from the field update.

    ``span`` gives [start, end) character offsets into ``meta.analyzed_text``
    so every quote can be traced back to the exact text that was submitted.
    ``status`` and ``note`` are computed by code, not by the model.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["field_update"] = "field_update"
    quote: str
    span: tuple[int, int]
    claim_type: ClaimType
    status: EvidenceStatus
    compared_with_row: Optional[str] = None
    note: str


class VerifiedTableEvidence(BaseModel):
    """A row from the verified stock table. Values are copied by code from the table."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["verified_table"] = "verified_table"
    row_id: str
    record: StockRecord
    status: Literal[EvidenceStatus.VERIFIED] = EvidenceStatus.VERIFIED
    note: str


Evidence = Annotated[Union[FieldUpdateEvidence, VerifiedTableEvidence], Field(discriminator="source")]


class NextAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType
    details: str = Field(min_length=1, max_length=MAX_ACTION_DETAILS_CHARS)


class Meta(BaseModel):
    """Operational metadata. Not part of the triage decision, but makes it auditable."""

    request_id: str
    provider: str
    model: Optional[str]
    degraded: bool = Field(description="True when the LLM was unavailable or its output was rejected; rules-only triage was used.")
    llm_attempts: int
    rules_triggered: list[str]
    policy_overrides: list[str]
    guardrail_notes: list[str]
    latency_ms: int
    analyzed_text: str = Field(description="The exact text that was analysed; evidence spans index into it.")
    version: str = APP_VERSION


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_category: IssueCategory
    urgency: Urgency
    evidence: list[Evidence]
    recommended_next_action: NextAction
    missing_information: list[str] = Field(max_length=MAX_MISSING_ITEMS)
    needs_human_review: bool
    review_reason: Optional[str]
    meta: Meta

    @model_validator(mode="after")
    def _review_reason_consistent(self) -> "TriageResult":
        if self.needs_human_review and not (self.review_reason and self.review_reason.strip()):
            raise ValueError("review_reason is required when needs_human_review is true")
        if not self.needs_human_review and self.review_reason is not None:
            raise ValueError("review_reason must be null when needs_human_review is false")
        return self
