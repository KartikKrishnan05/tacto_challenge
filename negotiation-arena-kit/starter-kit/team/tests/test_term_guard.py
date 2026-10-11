from team.agent import TeamAgent
from team.term_guard import (
    anti_capitulation_check,
    validate_acceptance_offer,
    validate_contractual_notice,
    validate_effective_date_pricing,
    validate_minimum_options,
    validate_monotonic_options,
    validate_reciprocity,
    validate_strategic_options,
    validate_term_options,
)


def base(**overrides):
    terms = {
        "unit_price": 100.0,
        "payment_days": 30,
        "contract_months": 12,
        "lead_time_days": 28,
        "index_clause": None,
    }
    terms.update(overrides)
    return terms


def test_identical_terms_different_prices_are_blocked():
    errors = validate_term_options([base(unit_price=100), base(unit_price=101)])
    assert any("Rule 1" in error and "Option A" in error and "Option B" in error for error in errors)


def test_longer_payment_must_cost_more():
    errors = validate_term_options([base(unit_price=100), base(unit_price=100, payment_days=90)])
    assert any("Rule 2" in error and "payment_days=90" in error for error in errors)


def test_longer_contract_must_cost_less():
    errors = validate_term_options([base(unit_price=100), base(unit_price=100, contract_months=24)])
    assert any("Rule 3" in error and "contract_months=24" in error for error in errors)


def test_indexed_price_must_undercut_fixed_price():
    errors = validate_term_options([base(unit_price=100), base(unit_price=100, index_clause={"series": "steel"})])
    assert any("Rule 4" in error and "index clause" in error for error in errors)


def test_longer_lead_time_may_not_cost_more():
    errors = validate_term_options([base(unit_price=100), base(unit_price=101, lead_time_days=56)])
    assert any("Rule 5" in error and "lead_time_days=56" in error for error in errors)


def test_rival_ceiling_requires_one_option_at_or_below_quote():
    errors = validate_term_options([base(unit_price=100), base(unit_price=101)], rival_price=99)
    assert any("Rule 6" in error and "EUR 99.00" in error for error in errors)


def test_multi_lever_menu_is_not_over_rejected():
    errors = validate_term_options([
        base(unit_price=100, payment_days=30, contract_months=12, lead_time_days=28),
        base(unit_price=103, payment_days=90, contract_months=12, lead_time_days=28),
        base(unit_price=98, payment_days=30, contract_months=24, lead_time_days=56),
    ])
    assert errors == []


def test_notice_and_index_ceiling_are_hard_constraints():
    errors = validate_strategic_options(
        [base(unit_price=130, effective_date="2026-11-01")],
        min_effective_date="2027-01-01",
        price_ceiling=129.48,
    )
    assert any("Rule 7" in error and "2027-01-01" in error for error in errors)
    assert any("Rule 8" in error and "129.48" in error for error in errors)


def test_single_option_is_blocked():
    errors = validate_minimum_options([base(unit_price=100)])
    assert any("Rule 10" in error and "at least 2" in error for error in errors)


def test_punitive_price_above_opening_is_blocked():
    errors = validate_acceptance_offer(
        {"unit_price": 141.90, "payment_days": 14},
        {"unit_price": 138.55, "payment_days": 30},
    )
    assert any("Rule 11" in error and "138.55" in error for error in errors)


def test_price_above_opening_can_be_paid_for_with_material_payment_extension():
    errors = validate_acceptance_offer(
        {"unit_price": 141.90, "payment_days": 90},
        {"unit_price": 138.55, "payment_days": 30},
    )
    assert errors == []


def test_effective_date_options_need_a_real_price_trade():
    errors = validate_effective_date_pricing([
        base(unit_price=129.48, effective_date="2027-04-01"),
        base(unit_price=129.48, effective_date="2027-01-01"),
    ])
    assert any("Rule 12" in error and "date value" in error for error in errors)


def test_earlier_effective_date_can_trade_for_lower_price():
    errors = validate_effective_date_pricing([
        base(unit_price=129.48, effective_date="2027-04-01"),
        base(unit_price=129.00, effective_date="2027-01-01"),
    ])
    assert errors == []


def test_equivalent_option_cannot_get_cheaper_in_a_later_round():
    errors = validate_monotonic_options(
        [base(unit_price=130.21, effective_date="2027-01-01")],
        [base(unit_price=129.48, effective_date="2027-01-01")],
    )
    assert any("Rule 13" in error and "130.21" in error for error in errors)


def test_price_rise_without_non_price_supplier_concession_is_blocked():
    events = [
        {"from": "buyer", "term_sheets": [{"terms": {"unit_price": 100, "payment_days": 90}}]},
        {"from": "sales", "term_sheets": [{"terms": {"unit_price": 110, "payment_days": 30}}]},
    ]
    errors = TeamAgent.reciprocity_errors(
        {"negotiable": ["unit_price", "payment_days"]},
        events,
        [base(unit_price=101, payment_days=90)],
    )
    assert any("Rule 9" in error and "non-price concession" in error for error in errors)


def test_option_lines_make_structured_packages_visible():
    body = TeamAgent.with_option_text(
        "Please review the attached terms.",
        [base(unit_price=100), base(unit_price=102, payment_days=90)],
    )
    assert "Option A" in body and "Option B" in body and "EUR 100.00 per unit" in body
    assert "unit_price=" not in body


def test_contractual_notice_blocks_date_before_compliant_floor():
    errors = validate_contractual_notice(
        [base(effective_date="2026-11-01")],
        90,
        current_date="2026-09-11",
    )
    assert any("Rule 14" in error and "2026-12-10" in error for error in errors)


def test_contractual_notice_allows_date_on_or_after_floor():
    errors = validate_contractual_notice(
        [base(effective_date="2026-12-10")],
        90,
        current_date="2026-09-11",
    )
    assert errors == []


def test_early_acceptance_above_ideal_target_is_blocked():
    errors = anti_capitulation_check(base(unit_price=110), current_round=1, ideal_target=100)
    assert any("Rule 15" in error and "round 1" in error for error in errors)


def test_early_acceptance_near_ideal_target_is_allowed():
    assert anti_capitulation_check(base(unit_price=100.50), current_round=2, ideal_target=100) == []


def test_higher_price_without_buyer_term_trade_is_blocked():
    errors = validate_reciprocity(
        [base(unit_price=131.90, payment_days=30)],
        [base(unit_price=132.65, payment_days=30)],
    )
    assert any("Rule 16" in error and "132.65" in error for error in errors)


def test_higher_price_with_better_payment_is_allowed():
    errors = validate_reciprocity(
        [base(unit_price=131.90, payment_days=30)],
        [base(unit_price=132.65, payment_days=90)],
    )
    assert errors == []

