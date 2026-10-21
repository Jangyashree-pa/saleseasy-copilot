"""End-to-end pipeline tests with scripted (including adversarial) model outputs.

The adversarial drafts simulate a model that has been fooled or that
hallucinates. The point: the guardrails and policy must hold regardless.
"""

import pytest

from app.llm import LLMError, RulesBaselineClient, ScriptedClient
from app.pipeline import TriagePipeline, parse_draft
from app.schemas import TriageResult
from tests.conftest import draft_json


def run(responses, text, **kwargs):
    client = ScriptedClient(responses)
    result = TriagePipeline(client).run(text, **kwargs)
    TriageResult.model_validate(result.model_dump())  # always contract-valid
    return result, client


def test_compromised_model_obeys_injection_but_policy_holds(scenario_text):
    obedient = draft_json(issue_category="other", urgency="low",
                          recommended_next_action={"type": "no_action", "details": "No action needed."})
    result, _ = run([obedient], scenario_text("S4"))
    assert result.needs_human_review is True
    assert result.urgency.value == "medium"
    assert result.recommended_next_action.type.value == "flag_security_review"
    assert "instruction_like_input" in result.meta.rules_triggered
    assert result.meta.policy_overrides


def test_model_claiming_resolution_is_rejected_then_falls_back(scenario_text):
    bad = draft_json(recommended_next_action={"type": "no_action", "details": "Marked as resolved per the update."})
    result, client = run([bad, bad], scenario_text("S4"))
    assert len(client.requests) == 2  # one repair attempt
    assert result.meta.degraded is True
    assert "resolved" not in result.recommended_next_action.details.lower()
    assert result.needs_human_review is True


def test_invented_figure_repaired_on_second_attempt(scenario_text):
    text = scenario_text("S1")
    invented = draft_json(issue_category="stock_availability", urgency="high",
                          recommended_next_action={"type": "recommend_replenishment", "details": "Replenish now; the outlet is losing about ₹25,000 per week."})
    clean = draft_json(issue_category="stock_availability", urgency="high",
                       claims=[{"quote": "Cola 500ml is unavailable", "claim_type": "stock_status", "row_id": "VT-001", "stock_claim": "out_of_stock"}],
                       recommended_next_action={"type": "recommend_replenishment", "details": "Raise a priority replenishment for Cola 500ml at Indiranagar Mart."})
    result, client = run([invented, clean], text)
    assert result.meta.degraded is False
    assert result.meta.llm_attempts == 2
    assert "invented_figure" in client.requests[1].messages[-1]["content"]
    assert "25,000" not in result.model_dump_json()


def test_diagnosis_never_reaches_the_output(scenario_text):
    diagnosing = draft_json(issue_category="product_quality_safety", urgency="medium",
                            recommended_next_action={"type": "escalate_quality_safety", "details": "Likely food poisoning from the juice; not serious."})
    result, _ = run([diagnosing, diagnosing], scenario_text("S5"))
    assert result.meta.degraded is True
    assert result.urgency.value == "critical"
    assert result.needs_human_review is True
    assert "poisoning" not in result.model_dump_json().lower()


def test_price_match_approval_blocked(scenario_text):
    approving = draft_json(issue_category="competitor_activity", urgency="high",
                           recommended_next_action={"type": "escalate_commercial_team", "details": "Approve a price match with a 10% discount to protect outlets."})
    result, _ = run([approving, approving], scenario_text("S3"))
    assert result.meta.degraded is True
    assert "approve a price match" not in result.recommended_next_action.details.lower()
    assert result.needs_human_review is True


def test_model_trusting_rep_over_table_is_corrected(scenario_text):
    trusting = draft_json(issue_category="stock_availability", urgency="high",
                          claims=[{"quote": "no bottles on the shelf", "claim_type": "stock_status", "row_id": "VT-002", "stock_claim": "out_of_stock"}],
                          recommended_next_action={"type": "recommend_replenishment", "details": "Send Cola 500ml to Metro Fresh today."})
    result, _ = run([trusting], scenario_text("S2"))
    assert result.issue_category.value == "inventory_discrepancy"
    assert result.recommended_next_action.type.value == "verify_physical_stock"
    assert result.needs_human_review is True
    assert any(e.status.value == "conflicting" for e in result.evidence if e.source == "field_update")


def test_model_health_claim_escalates_even_without_keyword(data):
    # No deterministic health keyword here; the model's grounded health claim must still escalate.
    text = "City Corner: customer complained of feeling strange after the Juice 1L."
    claim = {"quote": "customer complained of feeling strange after the Juice 1L", "claim_type": "health_safety", "row_id": "none", "stock_claim": "not_applicable"}
    model = draft_json(issue_category="product_quality_safety", urgency="medium", claims=[claim],
                       recommended_next_action={"type": "escalate_quality_safety", "details": "Escalate to the quality & safety team."})
    result, _ = run([model], text)
    assert result.urgency.value == "critical"
    assert "health_safety_signal" in result.meta.rules_triggered


def test_paraphrased_quote_triggers_repair_then_is_dropped(scenario_text):
    paraphrase = draft_json(issue_category="inventory_discrepancy", urgency="medium",
                            claims=[{"quote": "Metro Fresh says it has no stock", "claim_type": "stock_status", "row_id": "VT-002", "stock_claim": "out_of_stock"}],
                            recommended_next_action={"type": "verify_physical_stock", "details": "Count stock at Metro Fresh."})
    result, client = run([paraphrase, paraphrase], scenario_text("S2"))
    assert len(client.requests) == 2
    assert result.meta.degraded is False  # ungrounded quotes are dropped, not fatal
    quotes = [e.quote for e in result.evidence if e.source == "field_update"]
    assert "Metro Fresh says it has no stock" not in quotes
    assert result.needs_human_review is True  # deterministic conflict check still fires


@pytest.mark.parametrize("error_kind", ["timeout", "unavailable", "rate_limited", "auth"])
def test_provider_errors_degrade_gracefully(scenario_text, error_kind):
    result, client = run([LLMError(error_kind)], scenario_text("S5"))
    assert len(client.requests) == 1
    assert result.meta.degraded is True
    assert result.needs_human_review is True
    assert result.urgency.value == "critical"
    assert "degraded_mode" in result.meta.rules_triggered


def test_simulated_failure_modes(scenario_text):
    pipeline = TriagePipeline(RulesBaselineClient())
    text = scenario_text("S2")
    flaky = pipeline.run(text, simulate_failure="flaky_json")
    assert flaky.meta.degraded is False and flaky.meta.llm_attempts == 2
    for mode in ("timeout", "unavailable", "invalid_json", "schema_violation"):
        res = pipeline.run(text, simulate_failure=mode)
        assert res.meta.degraded is True
        assert res.needs_human_review is True


def test_parse_draft_tolerates_fences_but_not_bad_values():
    ok, err = parse_draft("```json\n" + draft_json() + "\n```")
    assert ok is not None and err is None
    bad, err = parse_draft(draft_json(urgency="super-urgent"))
    assert bad is None and "urgency" in err
    extra, err = parse_draft(draft_json(status="resolved"))
    assert extra is None  # unknown fields are rejected
