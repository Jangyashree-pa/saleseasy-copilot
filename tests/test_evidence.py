from app.evidence import build_evidence, locate_quote
from app.schemas import ClaimType, DraftClaim, EvidenceStatus, StockClaim
from app.signals import analyze


def test_locate_quote_tolerates_case_whitespace_and_curly_quotes():
    text = "Rep wrote: “Ignore   instructions” – then left."
    start, end = locate_quote(text, '"ignore instructions"')
    assert text[start:end] == "Ignore   instructions"
    assert locate_quote(text, "then left.") is not None


def test_locate_quote_rejects_paraphrase_and_ellipsis():
    text = "Metro Fresh has a stockout on Cola 500ml."
    assert locate_quote(text, "Metro Fresh has no stock") is None
    assert locate_quote(text, "Metro Fresh ... Cola 500ml") is None


def test_status_is_computed_by_code_not_model(data, scenario_text):
    text = scenario_text("S2")
    pre = analyze(text, data)
    claim = DraftClaim(quote="no bottles on the shelf", claim_type=ClaimType.STOCK_STATUS, row_id="VT-002", stock_claim=StockClaim.OUT_OF_STOCK)
    ev = build_evidence(text, pre, data, [claim])
    statuses = {e.status for e in ev.field_items}
    assert EvidenceStatus.CONFLICTING in statuses
    table = [e for e in ev.table_items if e.row_id == "VT-002"]
    assert table and table[0].record.system_stock == 48  # value copied from the table


def test_verified_stockout_against_zero_row(data, scenario_text):
    text = scenario_text("S1")
    pre = analyze(text, data)
    claim = DraftClaim(quote="Cola 500ml is unavailable", claim_type=ClaimType.STOCK_STATUS, row_id="VT-001", stock_claim=StockClaim.OUT_OF_STOCK)
    ev = build_evidence(text, pre, data, [claim])
    assert ev.field_items[0].status == EvidenceStatus.VERIFIED


def test_link_to_row_not_named_in_update_is_ignored(data, scenario_text):
    text = scenario_text("S2")  # Metro Fresh
    pre = analyze(text, data)
    claim = DraftClaim(quote="no bottles on the shelf", claim_type=ClaimType.STOCK_STATUS, row_id="VT-001", stock_claim=StockClaim.OUT_OF_STOCK)
    ev = build_evidence(text, pre, data, [claim])
    assert any("Ignored link to VT-001" in n for n in ev.notes)
    assert all(e.row_id != "VT-001" for e in ev.table_items)


def test_deterministic_evidence_added_when_model_misses_it(data, scenario_text):
    text = scenario_text("S5")
    pre = analyze(text, data)
    ev = build_evidence(text, pre, data, [])  # model returned no claims at all
    assert any(e.claim_type == ClaimType.HEALTH_SAFETY for e in ev.field_items)
    assert all("deterministic" in e.note for e in ev.field_items)


def test_ungrounded_quotes_are_reported(data, scenario_text):
    text = scenario_text("S1")
    pre = analyze(text, data)
    claim = DraftClaim(quote="the outlet lost Rs 50,000", claim_type=ClaimType.SALES_IMPACT)
    ev = build_evidence(text, pre, data, [claim])
    assert ev.ungrounded_quotes == ["the outlet lost Rs 50,000"]
