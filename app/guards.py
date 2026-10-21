"""Output guardrails: deterministic checks on what the model wrote.

Each check maps to a failure mode named in the brief:

* ``ungrounded_quote``      evidence must be traceable to the field update
* ``invented_figure``       "do not invent an impact figure" (scenario 1)
* ``commercial_commitment`` "avoid unsupported action" (scenario 3)
* ``claims_system_action``  "mark this resolved" must never come back as done (scenario 4)
* ``diagnosis_language``    "do not diagnose" (scenario 5)

Violations are fed back to the model once (repair). If they persist, the
pipeline discards the model output and falls back to rules-only triage with
mandatory human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .evidence import locate_quote
from .schemas import LLMDraft
from .verified_data import VerifiedData

_I = re.IGNORECASE


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    # "repairable": ask the model to fix it; if it persists, reject the draft.
    # "droppable": on the final attempt the offending item is dropped instead of rejecting the draft.
    severity: str = "repairable"


# ---------------------------------------------------------------- figures --
_NUM = r"\d[\d,]*(?:\.\d+)?"
_SCALE = r"(?:\s*(?:k|lakhs?|lacs?|crores?|cr|thousand|million|mn|bn))?"
_CURRENCY = r"(?:₹|rs\.?|inr|rupees?)"
# Quantities that would describe an impact or a stock level. Counts such as
# "visit 2 nearby outlets" are recommendations, not impact figures, so they are not listed.
_QTY_UNITS = r"(?:units?|cases?|bottles?|packs?|packets?|cartons?|pcs|pieces?|crates?|boxes?|customers?|orders?)"
_FIGURE_PATTERNS = [
    re.compile(rf"{_CURRENCY}\s*({_NUM}){_SCALE}", _I),
    re.compile(rf"({_NUM}){_SCALE}\s*(?:rupees?|inr|rs\b)", _I),
    re.compile(rf"({_NUM})\s*(?:%|percent\b|per\s*cent\b)", _I),
    re.compile(rf"({_NUM})\s*(?:{_QTY_UNITS})\b", _I),
    re.compile(rf"({_NUM})\s*(?:k|lakhs?|lacs?|crores?|thousand|million)\b", _I),
]
_ANY_NUMBER = re.compile(_NUM)


def _norm_number(raw: str) -> str:
    n = raw.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n


def allowed_numbers(text: str, data: VerifiedData) -> set[str]:
    allowed = {_norm_number(m.group(0)) for m in _ANY_NUMBER.finditer(text)}
    for row in data.rows:
        allowed.add(str(row.system_stock))
        allowed |= {_norm_number(m.group(0)) for m in _ANY_NUMBER.finditer(f"{row.row_id} {row.outlet} {row.sku}")}
    return allowed


def find_invented_figures(fields: list[str], allowed: set[str]) -> list[str]:
    """Figures (money, %, quantities) in model text that appear nowhere in the sources.

    Bare numbers and time expressions ("within 24 hours") are not treated as
    impact figures; only numbers attached to a currency, percentage or
    quantity unit are checked.
    """
    found: list[str] = []
    for value in fields:
        for pattern in _FIGURE_PATTERNS:
            for m in pattern.finditer(value):
                if _norm_number(m.group(1)) not in allowed:
                    found.append(m.group(0).strip())
    return sorted(set(found))


# -------------------------------------------------------------- diagnosis --
_DIAGNOSIS_PATTERNS = [
    r"\bfood[\s\-]*poisoning\b",
    r"\bpoison(ed|ing)?\b",
    r"\ballerg(y|ies|ic)\b",
    r"\binfect(ion|ions|ed|ious)\b",
    r"\bgastro\w*",
    r"\bdiarrh\w*",
    r"\bdehydrat\w*",
    r"\bdiagnos\w*",
    r"\bsymptoms?\s+of\b",
    r"\b(likely|probably|possibly|presumably)\s+(caused|due|because|from|related|a\s+result)\b",
    r"\b(may|might|must|could)\s+have\s+been\s+caused\b",
    r"\b(not|nothing)\s+(serious|dangerous|harmful)\b",
    r"\bharmless\b",
    r"\bsafe\s+to\s+(drink|consume|eat)\b",
    r"\bno\s+(health\s+)?risk\b",
]
_DIAGNOSIS_RE = [re.compile(p, _I) for p in _DIAGNOSIS_PATTERNS]


def find_diagnosis_language(fields: list[str], source_text: str) -> list[str]:
    """Medical cause/severity language the rep did not use themselves."""
    hits: list[str] = []
    for value in fields:
        for pattern in _DIAGNOSIS_RE:
            for m in pattern.finditer(value):
                term = m.group(0)
                if re.search(re.escape(term), source_text, _I):
                    continue  # restating the rep's own words is not diagnosing
                hits.append(term)
    return sorted(set(hits))


# ---------------------------------------------- negation-aware phrase scan --
_NEGATION_RE = re.compile(
    r"\b(not|never|no|without|cannot|can't|cant|don't|dont|before|until|unless|pending|"
    r"require|requires|required|requiring|need|needs|seek|request|requested|whether|if|avoid|refrain)\b",
    _I,
)


def _affirmative_matches(pattern: re.Pattern[str], value: str, window: int = 35) -> list[str]:
    hits: list[str] = []
    for m in pattern.finditer(value):
        before = value[max(0, m.start() - window) : m.start()]
        clause_start = max(before.rfind("."), before.rfind(";"), before.rfind("!"), before.rfind("?"))
        if clause_start >= 0:
            before = before[clause_start + 1 :]
        if _NEGATION_RE.search(before):
            continue
        hits.append(m.group(0))
    return hits


_COMMIT_VERBS = (
    r"(approv\w*|grant\w*|authori[sz]\w*|appl(y|ied|ying)|offer\w*|giv(e|en|ing)|provid\w*|extend\w*|"
    r"match\w*|implement\w*|process\w*|confirm\w*|honou?r\w*|accept\w*|agree\w*|go\s+ahead\s+with|proceed\s+with)"
)
_COMMERCIAL_OBJECTS = (
    r"(price[\s\-]?match\w*|discount\w*|price\s+(cut|reduction|drop|decrease)|lower(ed)?\s+price\w*|reduced\s+price\w*|"
    r"refund\w*|credit\s+notes?|free\s+(goods|stock|units|cases|bottles|packs)|compensation|special\s+rate|rate\s+cut|scheme|"
    r"(competitor'?s?|their|the\s+competitor'?s?|rival'?s?)\s+pric\w*)"
)
_COMMIT_RE = re.compile(rf"\b{_COMMIT_VERBS}\b[^.;:!?\n]{{0,40}}?\b{_COMMERCIAL_OBJECTS}", _I)


def find_commercial_commitments(fields: list[str]) -> list[str]:
    hits: list[str] = []
    for value in fields:
        hits += _affirmative_matches(_COMMIT_RE, value)
    return sorted(set(hits))


_SYSTEM_ACTION_RES = [
    re.compile(r"\b(has|have|had)\s+been\s+(marked|closed|resolved|approved|processed|completed|actioned|escalated|fixed)\b", _I),
    re.compile(r"\b(i|we)\s*('ve|\s+have)?\s+(marked|closed|resolved|approved|processed|completed|escalated|actioned)\b", _I),
    re.compile(r"\bmark(ed|ing)?\s+(this|it|the\s+(issue|case|ticket|update|report))?\s*(as\s+)?(resolved|closed|done|complete[d]?)\b", _I),
    re.compile(r"\b(issue|case|ticket|update|report)\s+(is\s+)?(now\s+)?(resolved|closed)\b", _I),
]


def find_system_action_claims(fields: list[str]) -> list[str]:
    hits: list[str] = []
    for value in fields:
        for pattern in _SYSTEM_ACTION_RES:
            hits += _affirmative_matches(pattern, value)
    return sorted(set(hits))


# ------------------------------------------------------------------ driver --
def free_text_fields(draft: LLMDraft) -> list[str]:
    return [draft.recommended_next_action.details, *draft.missing_information, draft.review_reason or ""]


def check_draft(draft: LLMDraft, text: str, data: VerifiedData) -> list[Violation]:
    violations: list[Violation] = []
    fields = free_text_fields(draft)

    for claim in draft.claims:
        if locate_quote(text, claim.quote) is None:
            violations.append(
                Violation(
                    "ungrounded_quote",
                    f'Quote "{claim.quote[:120]}" does not appear verbatim in the field update. Copy text exactly.',
                    severity="droppable",
                )
            )

    figures = find_invented_figures(fields, allowed_numbers(text, data))
    if figures:
        violations.append(
            Violation(
                "invented_figure",
                f"These figures are not in the field update or verified data: {', '.join(figures)}. "
                "Remove them; ask for the figure in missing_information instead.",
            )
        )

    diagnosis = find_diagnosis_language(fields, text)
    if diagnosis:
        violations.append(
            Violation(
                "diagnosis_language",
                f"Do not diagnose or speculate about health causes or severity (found: {', '.join(diagnosis)}). "
                "Recommend escalation to the quality & safety team only.",
            )
        )

    commitments = find_commercial_commitments(fields)
    if commitments:
        violations.append(
            Violation(
                "commercial_commitment",
                f"The assistant cannot approve or apply commercial actions (found: {', '.join(commitments)}). "
                "Route the request to escalate_commercial_team and ask for evidence.",
            )
        )

    claimed_actions = find_system_action_claims(fields)
    if claimed_actions:
        violations.append(
            Violation(
                "claims_system_action",
                f"The assistant cannot take actions or close cases (found: {', '.join(claimed_actions)}). "
                "Only recommend a next action.",
            )
        )
    return violations
