from app.evidence import build_evidence
from app.policy import apply_policy
from app.schemas import DraftAction, IssueCategory, LLMDraft, Urgency
from app.signals import analyze


def _draft(**kw):
    base = dict(
        issue_category=IssueCategory.OTHER,
        urgency=Urgency.LOW,
        claims=[],
        recommended_next_action=DraftAction(type="request_more_info", details="Ask the rep."),
        missing_information=[],
        needs_human_review=False,
        review_reason="",
    )
    base.update(kw)
    return LLMDraft(**base)


def _apply(text, data, draft, degraded=False):
    pre = analyze(text, data)
    ev = build_evidence(text, pre, data, draft.claims)
    return apply_policy(draft, ev, pre, data, degraded=degraded, degraded_reason="LLM timeout" if degraded else None)


def test_rules_only_escalate_never_clear(data, scenario_text):
    # The model says "no review, low urgency"; the health rule must override both.
    out = _apply(scenario_text("S5"), data, _draft())
    assert out.needs_human_review is True
    assert out.urgency == Urgency.CRITICAL
    assert out.review_reason and "health" in out.review_reason.lower()


def test_model_can_add_review_but_reason_is_recorded(data):
    out = _apply("Metro Fresh asked about next week's delivery slot.", data,
                 _draft(needs_human_review=True, review_reason="Unclear request."))
    assert out.needs_human_review is True
    assert out.rules_triggered == ["model_requested_review"]


def test_vague_report_goes_back_to_rep_not_review(data, scenario_text):
    out = _apply(scenario_text("A1"), data, _draft(issue_category=IssueCategory.INSUFFICIENT_INFORMATION))
    assert out.needs_human_review is False
    assert out.review_reason is None
    joined = " ".join(out.missing_information).lower()
    assert "outlet" in joined and "sku" in joined


def test_missing_info_capped_and_deduplicated(data, scenario_text):
    items = ["Batch code", "batch code", "Photos", "Purchase date", "Units affected", "Store name", "Customer consent"]
    out = _apply(scenario_text("S5"), data, _draft(missing_information=items))
    assert len(out.missing_information) == 5
    assert len({i.lower() for i in out.missing_information}) == 5


def test_degraded_mode_always_reviews(data):
    out = _apply("Metro Fresh asked about next week's delivery slot.", data, _draft(), degraded=True)
    assert out.needs_human_review is True
    assert "degraded_mode" in out.rules_triggered
    assert out.urgency == Urgency.MEDIUM


def test_long_details_truncated(data):
    long = "Check the shelf. " * 40
    out = _apply("Metro Fresh asked about next week's delivery slot.", data,
                 _draft(recommended_next_action=DraftAction(type="request_more_info", details=long)))
    assert len(out.action.details) <= 300
