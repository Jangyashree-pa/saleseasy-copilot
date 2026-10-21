"""Evidence: grounding quotes in the field update and verifying claims against the table.

Principle: the model extracts claims; code decides their status.

* A quote is accepted only if it can be found in the submitted text (after
  normalising case, whitespace and typographic quotes/dashes). The returned
  quote is the original substring, with character offsets.
* ``verified`` / ``conflicting`` is computed by comparing the claimed stock
  level with the verified row. Anything the table cannot check is an
  ``unverified_claim``.
* Deterministic checks add evidence the model missed: a stockout phrase about
  a verified row, and any safety / manipulation / commercial signal that
  triggers a review rule, so every escalation is backed by a traceable quote.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schemas import (
    ClaimType,
    DraftClaim,
    EvidenceStatus,
    FieldUpdateEvidence,
    StockClaim,
    StockRecord,
    VerifiedTableEvidence,
)
from .signals import PreAnalysis, Signal
from .verified_data import StockRow, VerifiedData

_CHAR_MAP = {
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'", "′": "'", "`": "'",
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
    " ": " ", " ": " ", " ": " ",
}
_STRIP_CHARS = " \t\n\"'“”‘’«»"

# Signal kind -> claim type used when code adds deterministic evidence.
_SIGNAL_CLAIM_TYPE = {
    "health": ClaimType.HEALTH_SAFETY,
    "contamination": ClaimType.PRODUCT_QUALITY,
    "injection": ClaimType.INSTRUCTION_TO_SYSTEM,
    "commercial": ClaimType.PRICING_REQUEST,
}


def _normalize_with_map(s: str) -> tuple[str, list[int]]:
    out: list[str] = []
    index: list[int] = []
    prev_space = False
    for i, ch in enumerate(s):
        ch = _CHAR_MAP.get(ch, ch)
        if ch.isspace():
            if prev_space:
                continue
            prev_space = True
            out.append(" ")
            index.append(i)
            continue
        prev_space = False
        for lowered in ch.lower():
            out.append(lowered)
            index.append(i)
    return "".join(out), index


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Find ``quote`` in ``text`` tolerating case, whitespace and typographic quote differences.

    Returns [start, end) offsets into ``text`` or None if the quote is not grounded.
    Ellipses are rejected: a quote with "..." is a paraphrase, not a quote.
    """
    q = quote.strip(_STRIP_CHARS).rstrip(".,;:!?").strip(_STRIP_CHARS)
    if len(q) < 2 or "..." in q or "…" in q:
        return None
    norm_text, index = _normalize_with_map(text)
    norm_q, _ = _normalize_with_map(q)
    norm_q = norm_q.strip()
    if not norm_q:
        return None
    pos = norm_text.find(norm_q)
    if pos < 0:
        return None
    start = index[pos]
    end = index[pos + len(norm_q) - 1] + 1
    return start, end


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _row_label(row: StockRow) -> str:
    return f"{row.outlet} / {row.sku}"


def classify_claim(claim_type: ClaimType, stock_claim: StockClaim, row: StockRow | None) -> tuple[EvidenceStatus, str]:
    if claim_type == ClaimType.STOCK_STATUS and row is not None:
        n = row.system_stock
        label = _row_label(row)
        if stock_claim == StockClaim.OUT_OF_STOCK:
            if n == 0:
                return EvidenceStatus.VERIFIED, f"Matches verified data: system shows 0 units for {label} ({row.row_id})."
            return EvidenceStatus.CONFLICTING, f"Conflicts with verified data: system shows {n} units for {label} ({row.row_id})."
        if stock_claim == StockClaim.IN_STOCK:
            if n > 0:
                return EvidenceStatus.VERIFIED, f"Matches verified data: system shows {n} units for {label} ({row.row_id})."
            return EvidenceStatus.CONFLICTING, f"Conflicts with verified data: system shows 0 units for {label} ({row.row_id})."
        if stock_claim == StockClaim.LOW_STOCK:
            if n == 0:
                return EvidenceStatus.CONFLICTING, f"Rep implies some stock remains; system shows 0 units for {label} ({row.row_id})."
            return EvidenceStatus.UNVERIFIED_CLAIM, f"'Low' is not checkable against the table; system shows {n} units for {label} ({row.row_id})."
        return EvidenceStatus.UNVERIFIED_CLAIM, f"Field claim about {label}; no stock level stated."
    if claim_type == ClaimType.STOCK_STATUS:
        return EvidenceStatus.UNVERIFIED_CLAIM, "Field claim; no verified stock record matches this outlet and SKU."
    if claim_type == ClaimType.INSTRUCTION_TO_SYSTEM:
        return EvidenceStatus.UNVERIFIED_CLAIM, "Instruction-like text inside the field update; treated as data and not followed."
    if claim_type == ClaimType.HEALTH_SAFETY:
        return EvidenceStatus.UNVERIFIED_CLAIM, "Reported possible health incident; not verifiable here and not assessed medically."
    if claim_type == ClaimType.SALES_IMPACT:
        return EvidenceStatus.UNVERIFIED_CLAIM, "Field claim about sales impact; no figure is verified."
    if claim_type == ClaimType.PRICING_REQUEST:
        return EvidenceStatus.UNVERIFIED_CLAIM, "Commercial request from the field; requires commercial team approval."
    return EvidenceStatus.UNVERIFIED_CLAIM, "Field claim; not checkable against verified data."


@dataclass
class EvidenceBuild:
    field_items: list[FieldUpdateEvidence] = field(default_factory=list)
    table_items: list[VerifiedTableEvidence] = field(default_factory=list)
    ungrounded_quotes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def all(self) -> list:
        return [*self.field_items, *self.table_items]

    def has_status(self, status: EvidenceStatus) -> bool:
        return any(e.status == status for e in self.field_items)

    def conflicting_rows(self) -> list[str]:
        return sorted({e.compared_with_row for e in self.field_items if e.status == EvidenceStatus.CONFLICTING and e.compared_with_row})


def _signal_item(text: str, sig: Signal, claim_type: ClaimType, status: EvidenceStatus, note: str, row: StockRow | None = None) -> FieldUpdateEvidence:
    return FieldUpdateEvidence(
        quote=text[sig.clause_start : sig.clause_end],
        span=(sig.clause_start, sig.clause_end),
        claim_type=claim_type,
        status=status,
        compared_with_row=row.row_id if row else None,
        note=note + " [added by deterministic check]",
    )


def build_evidence(text: str, pre: PreAnalysis, data: VerifiedData, claims: list[DraftClaim]) -> EvidenceBuild:
    build = EvidenceBuild()
    matched_ids = pre.row_ids

    # 1. Model (or baseline) claims: ground the quote, validate the row link, classify.
    for claim in claims:
        loc = locate_quote(text, claim.quote)
        if loc is None:
            build.ungrounded_quotes.append(claim.quote)
            continue
        row: StockRow | None = None
        if claim.row_id and claim.row_id != "none":
            if claim.row_id in matched_ids:
                row = data.get(claim.row_id)
            else:
                build.notes.append(
                    f"Ignored link to {claim.row_id}: its outlet and SKU are not both named in the update."
                )
        status, note = classify_claim(claim.claim_type, claim.stock_claim, row)
        span = (loc[0], loc[1])
        if any(e.span == span and e.claim_type == claim.claim_type for e in build.field_items):
            continue  # duplicate claim
        build.field_items.append(
            FieldUpdateEvidence(
                quote=text[span[0] : span[1]],
                span=span,
                claim_type=claim.claim_type,
                status=status,
                compared_with_row=row.row_id if row else None,
                note=note,
            )
        )

    # 2. Deterministic stock check, independent of the model.
    stockouts = pre.of("stockout")
    if stockouts and len(pre.rows) == 1:
        row = pre.rows[0].row
        expected = EvidenceStatus.VERIFIED if row.system_stock == 0 else EvidenceStatus.CONFLICTING
        already = any(
            e.compared_with_row == row.row_id and e.claim_type == ClaimType.STOCK_STATUS and e.status == expected
            for e in build.field_items
        )
        if not already:
            sig = stockouts[0]
            # A model stock reading of the same words that disagrees with the deterministic one is replaced.
            contradicting = [
                e for e in build.field_items
                if e.claim_type == ClaimType.STOCK_STATUS and _overlaps(e.span, (sig.start, sig.end))
            ]
            for e in contradicting:
                build.field_items.remove(e)
                build.notes.append(f"Replaced the model's reading of \"{e.quote}\" with the deterministic stock check.")
            status, note = classify_claim(ClaimType.STOCK_STATUS, StockClaim.OUT_OF_STOCK, row)
            build.field_items.append(_signal_item(text, sig, ClaimType.STOCK_STATUS, status, note, row))
    elif stockouts and len(pre.rows) > 1:
        build.notes.append("Several outlet/SKU rows are named; the deterministic stock check was skipped to avoid guessing which row the stockout refers to.")

    # 3. Every policy-relevant signal must be backed by a quote in the evidence.
    for kind, claim_type in _SIGNAL_CLAIM_TYPE.items():
        for sig in pre.of(kind):
            sig_span = (sig.start, sig.end)
            if any(_overlaps(e.span, sig_span) for e in build.field_items):
                continue
            status, note = classify_claim(claim_type, StockClaim.NOT_APPLICABLE, None)
            build.field_items.append(_signal_item(text, sig, claim_type, status, note))

    build.field_items.sort(key=lambda e: (e.span[0], e.span[1]))

    # 4. Verified table rows referenced by claims or named in the update. Values come from the table.
    referenced = {e.compared_with_row for e in build.field_items if e.compared_with_row}
    for row_id in sorted(referenced | matched_ids):
        row = data.get(row_id)
        if row is None:
            continue
        match = next((m for m in pre.rows if m.row.row_id == row_id), None)
        how = "outlet and SKU named in the update"
        if match is not None and match.sku_match == "generic":
            how = f"outlet named; generic product mention mapped to {row.sku}"
        build.table_items.append(
            VerifiedTableEvidence(
                row_id=row.row_id,
                record=StockRecord(outlet=row.outlet, sku=row.sku, system_stock=row.system_stock),
                note=f"System of record (fictional verified data); linked because {how}.",
            )
        )
    return build
