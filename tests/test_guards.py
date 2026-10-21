from app.guards import (
    allowed_numbers,
    find_commercial_commitments,
    find_diagnosis_language,
    find_invented_figures,
    find_system_action_claims,
)


def test_invented_figures_flagged(data):
    allowed = allowed_numbers("Owner says we are losing major sales.", data)
    assert find_invented_figures(["Outlet is losing about ₹25,000 per week."], allowed) == ["₹25,000"]
    assert find_invented_figures(["Sales are down 30%."], allowed) == ["30%"]
    assert find_invented_figures(["They lost 200 units."], allowed) == ["200 units"]


def test_figures_from_sources_and_non_impact_numbers_allowed(data):
    allowed = allowed_numbers("Owner says losses are Rs 5,000 a day.", data)
    assert find_invented_figures(["Rep reports Rs 5,000 per day in losses."], allowed) == []
    assert find_invented_figures(["System shows 48 units."], allowed) == []  # from the verified table
    assert find_invented_figures(["Follow up within 24 hours and visit 2 nearby outlets."], allowed) == []


def test_diagnosis_language():
    source = "customer felt unwell after drinking it"
    assert find_diagnosis_language(["Likely food poisoning; not serious."], source)
    assert find_diagnosis_language(["Escalate to the quality & safety team now."], source) == []
    # Restating the rep's own words is not diagnosing.
    assert find_diagnosis_language(["Rep reports an allergic reaction."], "she had an allergic reaction") == []


def test_commercial_commitments_respect_negation():
    assert find_commercial_commitments(["Approve a price match for the beat."])
    assert find_commercial_commitments(["Offer a discount to retain the outlet."])
    assert find_commercial_commitments(["Match the competitor's price immediately."])
    assert find_commercial_commitments(["Do not approve any price match until the commercial team reviews evidence."]) == []
    assert find_commercial_commitments(["Send the request to the commercial team for approval."]) == []


def test_system_action_claims():
    assert find_system_action_claims(["Marked as resolved per the update."])
    assert find_system_action_claims(["This has been escalated to QA."])
    assert find_system_action_claims(["Do not mark this as resolved."]) == []
    assert find_system_action_claims(["Escalate to the quality & safety team."]) == []
