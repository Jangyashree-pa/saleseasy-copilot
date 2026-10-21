"""Deterministic pre-analysis of a field update (no LLM involved).

These signals are the backbone of the safety story: the policy engine uses
them to force human review and urgency floors, so escalation of safety,
manipulation and commercial cases does not depend on the model behaving.

Keyword lists are deliberately biased towards recall for the high-cost
categories (health, manipulation). A false positive costs a reviewer a
minute; a false negative on a health incident costs far more.
Known limitation: patterns cover English plus common Hinglish phrasings only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .verified_data import Mention, RowMatch, VerifiedData

_I = re.IGNORECASE

SIGNAL_PATTERNS: dict[str, list[str]] = {
    "stockout": [
        r"\bout[\s\-]+of[\s\-]+stock\b",
        r"\bstock[\s\-]?outs?\b",
        r"\bsold[\s\-]+out\b",
        r"\bunavailable\b",
        r"\bnot\s+available\b",
        r"\b(no|zero|nil)\s+stock\b",
        r"\bempty\s+shel(f|ves)\b",
        r"\bshel(f|ves)\s+(is\s+|are\s+)?(completely\s+|totally\s+)?empty\b",
        r"\bno\s+(bottles|units|packs|cases|pieces|cartons)\b",
        r"\b(stock|maal)\s+(khatam|khtm|nahi|nahin|illa)\b",
        r"\bkhatam\b",
        r"\b(0|zero|nil|none)\s+(left|remaining|available|in\s+stock|on\s+(the\s+)?shel(f|ves))\b",
        r"\bran\s+out\b",
        r"खत्म|ख़त्म|स्टॉक\s*नहीं",
    ],
    "low_stock": [
        r"\blow\s+stock\b",
        r"\brunning\s+(low|out)\b",
        r"\bonly\s+(a\s+)?few\s+(left|units|bottles|packs)\b",
        r"\bhalf[\s\-]+empty\b",
        r"\balmost\s+(out|empty|finished)\b",
    ],
    "health": [
        r"\b((felt|feel|feels|feeling|fell|is|was|were|are|became)\s+)?unwell\b",
        r"\b(felt|feel|feels|feeling|fell|falls|became|become|got|gets|getting|is|was|were|are)\s+(sick|ill)\b",
        r"\bvomit\w*",
        r"\bnause\w*",
        r"\bstomach\s*(ache|pain|upset|cramps?)\b",
        r"\bdiarrh\w*",
        r"\bhospital\w*",
        r"\b(doctor|clinic|medical\s+attention|emergency\s+room)\b",
        r"\bfaint\w*",
        r"\bdizz\w*",
        r"\ballergic\b",
        r"\brash(es)?\b",
        r"\binjur\w*",
        r"\bchok(e|ed|ing)\b",
        r"\bpoison\w*",
        r"\bfever\b",
        r"\bbreathing\b",
        r"\bmade\s+(\w+\s+){0,2}(sick|ill|unwell)\b",
        r"\b(threw|throw|throws|throwing|thrown)\s+up\b",
        r"\b(had|has|having|developed|got|get|gets)\s+(an?\s+)?(allergic\s+|skin\s+|bad\s+|severe\s+)?reaction\b",
        r"\bloose\s+motions?\b",
        r"\bupset\s+stomach\b",
        r"\b(passed\s+out|collapsed|ambulance|admitted)\b",
        r"\btabiyat\s+(kharab|theek\s+nahi|thik\s+nahi)\b",
        r"\bbimaar\b",
        r"\bulti\b",
        r"बीमार|उल्टी|तबीयत\s*खराब",
    ],
    "contamination": [
        r"\bcontaminat\w*",
        r"\bforeign\s+(object|body|particle|matter)s?\b",
        r"\b(insect|worm|cockroach|fly|flies|hair|glass|plastic)\s+(in|inside|found)\b",
        r"\bfound\s+(an?\s+)?(insect|worm|cockroach|hair|glass)\b",
        r"\bmou?ldy?\b",
        r"\bfung(us|al)\b",
        r"\btamper\w*",
        r"\bexpired\b",
        r"\bpast\s+(its\s+|the\s+)?expiry\b",
        r"\bswollen\b",
        r"\bbloated\b",
        r"\b(smells?|smelled|smelling|tastes?|tasted|tasting)\s+(bad|off|strange|weird|sour|funny|rotten)\b",
        r"\bfoul\s+(smell|odou?r|taste)\b",
        r"\bspoil(ed|t)\b",
        r"\brotten\b",
    ],
    "packaging_damage": [
        r"\bdamaged\b",
        r"\bleak\w*",
        r"\bbroken\s+(seal|cap|pack|bottle|packet)\b",
        r"\bseal\s+(is\s+|was\s+)?broken\b",
        r"\b(torn|dented|crushed|punctured)\b",
    ],
    "injection": [
        r"\bignore\b[^.\n]{0,40}\b(instructions?|rules|prompts?|previous|above|guidelines|polic(y|ies)|system)\b",
        r"\b(instructions?|rules)\b[^.\n]{0,30}\bignore\b",
        r"\b(disregard|forget|override|bypass|skip)\b[^.\n]{0,40}\b(instructions?|rules|prompts?|polic(y|ies)|guardrails?|checks?|review|validation)\b",
        r"\b(system\s+prompt|developer\s+(message|mode)|admin\s+mode|god\s+mode|jailbreak)\b",
        r"\byou\s+are\s+now\b",
        r"\bpretend\s+(you\s+are|you're|to\s+be)\b",
        r"\byou\s+are\s+(the\s+|an?\s+)?(admin|administrator|area\s+manager|manager|asm|supervisor|system)\b",
        r"\b(close|resolve)\s+(this|the|my)\s+(ticket|case|issue|report)\b",
        r"\bnew\s+instructions?\b",
        r"\bact\s+as\s+(an?\s+)?(admin|system|developer|administrator)\b",
        r"\bmark\s+(this|it|the\s+(issue|case|ticket|update|report))?\s*(as\s+)?(resolved|closed|done|completed?)\b",
        r"\b(set|change|make|keep|put|mark|use)\b[^.\n]{0,30}\b(urgency|priority|severity|needs_human_review|review)\b[^.\n]{0,30}\b(low|false|none|no|zero|minimal)\b",
        r"\b(with|at)\s+low\s+(urgency|priority)\b",
        r"\b(do\s+not|don'?t|dont|no\s+need\s+to|never)\s+(escalate|flag|report|review|notify)\b",
        r"\b(needs_human_review|review_reason|issue_category|recommended_next_action|missing_information)\b",
        r"<\s*/?\s*(system|assistant|instructions?|prompt)\s*>",
        r"\b(BEGIN|END)_FIELD_UPDATE",
    ],
    "commercial": [
        r"\bprice[\s\-]?match\w*",
        r"\bmatch\w*\b[^.\n]{0,20}\b(price|prices|pricing|rate|rates)\b",
        r"\bdiscount\w*",
        r"\bprice\s+(cut|reduction|drop|war)\b",
        r"\b(cut|reduce|lower|drop|slash)\w*\b[^.\n]{0,15}\b(price|prices|rate|rates|mrp)\b",
        r"\brefund\w*",
        r"\bcredit\s+notes?\b",
        r"\bfree\s+(goods|stock|units|cases|bottles|packs|samples)\b",
        r"\bcompensat\w*",
        r"\bspecial\s+(rate|price|pricing|margin)\b",
        r"\bscheme\b",
    ],
    "competitor": [
        r"\bcompetitors?\b",
        r"\bcompetition\b",
        r"\brivals?\b",
        r"\b(other|another|competing)\s+brands?\b",
    ],
    "sales_impact": [
        r"\b(los(e|es|ing|t)|loss(es)?\s+of)\b[^.\n]{0,30}\b(sales|business|customers|revenue|orders|outlets?|market\s+share)\b",
        r"\b(major|huge|big|heavy|massive|significant)\s+(sales\s+)?loss(es)?\b",
        r"\bcustomers?\s+(are\s+)?(walking\s+away|leaving|going\s+back|turning\s+away)\b",
        r"\bwapas\s+ja\s+rahe\b",
        r"\bsales\s+(are\s+)?(down|dropping|falling|hit)\b",
    ],
    "urgency_language": [
        r"\burgent\w*",
        r"\basap\b",
        r"\bimmediately\b",
        r"\bright\s+now\b",
        r"\bemergency\b",
        r"\bjaldi\b",
    ],
}

COMPILED: dict[str, list[re.Pattern[str]]] = {
    kind: [re.compile(p, _I) for p in patterns] for kind, patterns in SIGNAL_PATTERNS.items()
}

# Clause boundaries: sentence punctuation, colon, semicolon, newline, spaced dashes,
# and commas that are not digit separators (so "1,000" stays intact).
_BOUNDARY_RE = re.compile(r"(?<!\d)[.](?!\d)|[!?;:\n]|,(?!\d)|\s[-–—]+\s")
_MAX_CLAUSE = 160


@dataclass(frozen=True)
class Signal:
    kind: str
    match: str
    start: int
    end: int
    clause_start: int
    clause_end: int

    def clause(self, text: str) -> str:
        return text[self.clause_start : self.clause_end]


def clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Return [start, end) of the clause containing text[start:end], trimmed of whitespace."""
    left = 0
    for m in _BOUNDARY_RE.finditer(text, 0, start):
        left = m.end()
    right = len(text)
    m = _BOUNDARY_RE.search(text, end)
    if m:
        right = m.start()
    if right - left > _MAX_CLAUSE:
        # Long run-on clause: take a window around the match, snapped to word boundaries.
        left = max(left, start - 60)
        right = min(right, end + 60)
        while left > 0 and not text[left - 1].isspace() and left < start:
            left += 1
        while right < len(text) and not text[right].isspace() and right > end:
            right -= 1
    while left < right and text[left].isspace():
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    # Never return less than the match itself.
    return min(left, start), max(right, end)


@dataclass
class PreAnalysis:
    text: str
    outlets: list[Mention]
    skus: list[Mention]
    rows: list[RowMatch]
    signals: list[Signal] = field(default_factory=list)

    def kinds(self) -> set[str]:
        return {s.kind for s in self.signals}

    def has(self, kind: str) -> bool:
        return any(s.kind == kind for s in self.signals)

    def of(self, kind: str) -> list[Signal]:
        return [s for s in self.signals if s.kind == kind]

    @property
    def row_ids(self) -> set[str]:
        return {m.row.row_id for m in self.rows}

    def summary(self) -> dict:
        return {
            "outlets": sorted({m.canonical for m in self.outlets}),
            "skus": sorted({m.canonical for m in self.skus}),
            "rows": sorted(self.row_ids),
            "signals": sorted(self.kinds()),
        }


def detect_signals(text: str) -> list[Signal]:
    signals: list[Signal] = []
    for kind, patterns in COMPILED.items():
        spans: list[tuple[int, int]] = []
        for pattern in patterns:
            for m in pattern.finditer(text):
                if any(s <= m.start() < e or s < m.end() <= e for s, e in spans):
                    continue  # overlapping match of the same kind
                spans.append((m.start(), m.end()))
                cs, ce = clause_bounds(text, m.start(), m.end())
                signals.append(Signal(kind, text[m.start() : m.end()], m.start(), m.end(), cs, ce))
    signals.sort(key=lambda s: (s.start, s.kind))
    return signals


# Stockout phrasing anchored on a known product mention:
# "No Cola 500ml at Metro Fresh", "out of Cola 500ml", "Cola 500ml has been out since Monday".
_BEFORE_SKU_RE = re.compile(r"\b(no|no\s+more|out\s+of|without)\s+$", _I)
_AFTER_SKU_RE = re.compile(
    r"^\s*(is|are|was|were|has\s+been|have\s+been|went|got)?\s*(completely\s+|totally\s+)?(out|over|finished|gone|nil|zero|0)(?![\w\-])",
    _I,
)


def _sku_anchored_stockouts(text: str, skus: list[Mention], existing: list[Signal]) -> list[Signal]:
    found: list[Signal] = []
    taken = [(s.start, s.end) for s in existing if s.kind == "stockout"]
    for mention in skus:
        start, end = mention.start, mention.end
        before = _BEFORE_SKU_RE.search(text[max(0, start - 14) : start])
        after = _AFTER_SKU_RE.match(text[end : end + 40])
        if before:
            start = start - (len(text[max(0, start - 14) : start]) - before.start())
        elif after:
            end = end + after.end()
        else:
            continue
        if any(s < end and start < e for s, e in taken):
            continue
        cs, ce = clause_bounds(text, start, end)
        found.append(Signal("stockout", text[start:end], start, end, cs, ce))
        taken.append((start, end))
    return found


def analyze(text: str, data: VerifiedData) -> PreAnalysis:
    outlets, skus, rows = data.match_rows(text)
    signals = detect_signals(text)
    signals += _sku_anchored_stockouts(text, skus, signals)
    signals.sort(key=lambda s: (s.start, s.kind))
    return PreAnalysis(text=text, outlets=outlets, skus=skus, rows=rows, signals=signals)
