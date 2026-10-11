"""Shared guardrails as applied by the code-driven SpecAgent."""

from types import SimpleNamespace

from arena_kit.spec.closing import Row, View, guarded_ladder
from arena_kit.spec.sheets import validate_menu
from team.term_guard import anti_capitulation_check, validate_reciprocity


def option(**overrides):
    out = {
        "unit_price": 100.0,
        "payment_days": 30,
        "contract_months": 12,
        "lead_time_days": 28,
    }
    out.update(overrides)
    return out


def test_spec_menu_blocks_a_date_before_contractual_notice_floor():
    ctx = SimpleNamespace(case={"type": "price_increase"})
    errors = validate_menu(
        ctx,
        [
            ("A", option(unit_price=100, effective_date="2026-11-01")),
            ("B", option(unit_price=101, effective_date="2026-12-10")),
        ],
        minimum_notice_days=90,
        current_date="2026-09-11",
    )
    assert any("Rule 14" in error and "2026-12-10" in error for error in errors)


def test_reciprocity_blocks_a_higher_price_without_a_buyer_trade():
    errors = validate_reciprocity(
        [option(unit_price=131.90)],
        [option(unit_price=132.65)],
    )
    assert any("Rule 16" in error and "132.65" in error for error in errors)


def test_reciprocity_allows_a_higher_price_for_longer_payment():
    assert validate_reciprocity(
        [option(unit_price=131.90)],
        [option(unit_price=132.65, payment_days=90)],
    ) == []


def test_spec_close_ladder_blocks_a_price_above_the_early_target():
    view = View(
        "sup:1",
        "open",
        6,
        1,
        rows=[Row("offer:1-1", option(unit_price=110), 100_000, True, True)],
        current_round=1,
        ideal_price=100,
    )
    assert guarded_ladder({"sup:1": view}) == []
    assert anti_capitulation_check(option(unit_price=110), current_round=1, ideal_target=100)


def test_spec_close_ladder_allows_the_same_price_after_round_three():
    view = View(
        "sup:1",
        "open",
        6,
        3,
        rows=[Row("offer:1-1", option(unit_price=110), 100_000, True, True)],
        current_round=3,
        ideal_price=100,
    )
    assert guarded_ladder({"sup:1": view}) == [("sup:1", "offer:1-1")]
