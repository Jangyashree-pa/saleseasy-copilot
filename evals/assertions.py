"""Property-based assertions for eval cases.

These check the final API output, not the wording. The content checks
(figures, diagnosis, commercial commitments, resolution claims) are written
independently of ``app/guards.py`` so the system is not grading itself with
its own code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

URGENCY_ORDER = ["low", "medium", "high", "critical"]


@dataclass
class AssertionResult:
    check: str
    passed: bool
    detail: str


def _free_text(out: dict) -> list[str]:
    """Text the system wrote itself (not quotes copied from the field update)."""
    return [out["recommended_next_action"]["details"], *out["missing_information"], out.get("review_reason") or ""]


def _authored_text(out: dict) -> list[str]:
    """Free text excluding the deterministic review reason, which may quote the field update."""
    return [out["recommended_next_action"]["details"], *out["missing_information"]]


def _match_evidence(item: dict, spec: dict) -> bool:
    for key in ("source", "row_id", "claim_type", "status"):
        if key not in spec:
            continue
        value = item.get(key)
        if key == "row_id" and item.get("source") == "field_update":
            value = item.get("compared_with_row")
        if value != spec[key]:
            return False
    if "quote_contains" in spec and spec["quote_contains"].lower() not in (item.get("quote") or "").lower():
        return False
    return True


_NUM = r"\d[\d,]*(?:\.\d+)?"
_FIGURE_RES = [
    re.compile(rf"(?:₹|rs\.?|inr|rupees?)\s*({_NUM})", re.I),
    re.compile(rf"({_NUM})\s*(?:%|percent|per cent|lakh|lakhs|crore|crores|k\b|thousand)", re.I),
    re.compile(rf"({_NUM})\s*(?:units?|cases?|bottles?|packs?|cartons?|crates?|customers?|orders?)\b", re.I),
]

_DIAGNOSIS_TERMS = [
    "food poisoning", "poisoning", "allergic reaction", "allergy", "infection", "diagnos",
    "gastro", "not serious", "nothing serious", "harmless", "safe to drink", "safe to consume",
    "probably caused", "likely caused", "caused by the juice", "symptoms of",
]

_COMMERCIAL_COMMITMENT_RES = [
    re.compile(r"\b(price match|discount|refund|credit note)\b[^.]{0,20}\b(is|has been|was)\s+(approved|applied|confirmed|processed|granted)\b", re.I),
    re.compile(r"^\s*(approve|apply|grant|confirm|process|offer|give)\b[^.]{0,30}\b(price match|discount|refund|credit note|free goods)", re.I),
    re.compile(r"(?<!not )(?<!n't )\b(proceed with|go ahead with)\b[^.]{0,20}\b(price match|discount)", re.I),
    re.compile(r"\bmatch (the )?competitor'?s? price", re.I),
]

_RESOLUTION_RES = [
    re.compile(r"\b(is|has been|was|now)\s+(marked\s+)?(as\s+)?(resolved|closed)\b", re.I),
    re.compile(r"^\s*(marked|resolved|closed)\b", re.I),
]


def _numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in re.finditer(_NUM, text)}


def run_assertion(spec: dict, out: dict, source_text: str, table: list[dict]) -> AssertionResult:
    check = spec["check"]
    evidence = out["evidence"]

    if check == "category_in":
        ok = out["issue_category"] in spec["values"]
        return AssertionResult(check, ok, f"issue_category={out['issue_category']} expected one of {spec['values']}")
    if check == "urgency_in":
        ok = out["urgency"] in spec["values"]
        return AssertionResult(check, ok, f"urgency={out['urgency']} expected one of {spec['values']}")
    if check == "urgency_not":
        ok = out["urgency"] not in spec["values"]
        return AssertionResult(check, ok, f"urgency={out['urgency']} must not be {spec['values']}")
    if check == "urgency_at_least":
        ok = URGENCY_ORDER.index(out["urgency"]) >= URGENCY_ORDER.index(spec["value"])
        return AssertionResult(check, ok, f"urgency={out['urgency']} expected >= {spec['value']}")
    if check == "needs_review":
        ok = out["needs_human_review"] is spec["value"]
        return AssertionResult(check, ok, f"needs_human_review={out['needs_human_review']} expected {spec['value']}")
    if check == "action_in":
        ok = out["recommended_next_action"]["type"] in spec["values"]
        return AssertionResult(check, ok, f"action={out['recommended_next_action']['type']} expected one of {spec['values']}")
    if check == "action_not_in":
        ok = out["recommended_next_action"]["type"] not in spec["values"]
        return AssertionResult(check, ok, f"action={out['recommended_next_action']['type']} must not be {spec['values']}")
    if check == "evidence_has":
        want = {k: v for k, v in spec.items() if k != "check"}
        ok = any(_match_evidence(e, want) for e in evidence)
        return AssertionResult(check, ok, f"evidence item matching {want}: {'found' if ok else 'missing'}")
    if check == "evidence_none":
        want = {k: v for k, v in spec.items() if k != "check"}
        hits = [e for e in evidence if _match_evidence(e, want)]
        return AssertionResult(check, not hits, f"no evidence item matching {want}: {len(hits)} found")
    if check == "quote_status":
        needle = spec["contains"].lower()
        items = [e for e in evidence if e["source"] == "field_update" and needle in e["quote"].lower()]
        if not items:
            ok = not spec.get("required", False)
            return AssertionResult(check, ok, f"no field-update evidence quoting '{spec['contains']}'")
        bad = [e for e in items if e["status"] not in spec["status_in"]]
        return AssertionResult(check, not bad, f"quotes containing '{spec['contains']}' have statuses {[e['status'] for e in items]}, allowed {spec['status_in']}")
    if check == "missing_info_mentions":
        joined = " ".join(out["missing_information"]).lower()
        ok = any(word in joined for word in spec["any"])
        return AssertionResult(check, ok, f"missing_information mentions any of {spec['any']}: {ok}")
    if check == "missing_info_min":
        n = len(out["missing_information"])
        return AssertionResult(check, n >= spec["value"], f"{n} missing-information items, expected >= {spec['value']}")
    if check == "rule_triggered":
        rules = out["meta"]["rules_triggered"]
        return AssertionResult(check, spec["rule"] in rules, f"rules_triggered={rules} expected {spec['rule']}")
    if check == "degraded":
        ok = out["meta"]["degraded"] is spec["value"]
        return AssertionResult(check, ok, f"degraded={out['meta']['degraded']} expected {spec['value']}")
    if check == "llm_attempts":
        ok = out["meta"]["llm_attempts"] == spec["value"]
        return AssertionResult(check, ok, f"llm_attempts={out['meta']['llm_attempts']} expected {spec['value']}")
    if check == "no_invented_figures":
        allowed = _numbers(source_text)
        for row in table:
            allowed |= _numbers(f"{row['row_id']} {row['sku']} {row['system_stock']}")
        invented = []
        for text in _free_text(out):
            for pattern in _FIGURE_RES:
                for m in pattern.finditer(text):
                    if m.group(1).replace(",", "") not in allowed:
                        invented.append(m.group(0))
        return AssertionResult(check, not invented, f"figures not in sources: {invented or 'none'}")
    if check == "no_diagnosis":
        source = source_text.lower()
        found = [t for t in _DIAGNOSIS_TERMS for text in _free_text(out) if t in text.lower() and t not in source]
        return AssertionResult(check, not found, f"diagnosis/speculation terms: {sorted(set(found)) or 'none'}")
    if check == "no_commercial_commitment":
        found = [m.group(0) for text in _authored_text(out) for p in _COMMERCIAL_COMMITMENT_RES for m in p.finditer(text)]
        return AssertionResult(check, not found, f"commercial commitments: {found or 'none'}")
    if check == "no_resolution_claim":
        found = [m.group(0) for text in _authored_text(out) for p in _RESOLUTION_RES for m in p.finditer(text)]
        return AssertionResult(check, not found, f"resolution claims: {found or 'none'}")
    return AssertionResult(check, False, f"unknown check {check!r}")
