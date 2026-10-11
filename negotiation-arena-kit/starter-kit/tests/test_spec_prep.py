"""Section 0.7: preparation from the case JSON, the merged anchor table, coverage (R12.2)."""

import copy
import math

import pytest
from spec_support import CASES, PKG, case

from arena_kit.cost import total_cost
from arena_kit.spec import playbook, prep
from arena_kit.spec.sheets import Ctx, cover
from arena_kit.spec.terms import complete, uncovered_units


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_preparation_runs_on_every_practice_case(case_id):
    """All 19 practice case files: section 0.7 runs without errors and prices every opening."""
    p = prep.prepare(CASES[case_id], package=PKG)
    assert not any("preparation failed" in w for w in p.warnings), p.warnings
    assert p.openings and set(p.openings) == set(CASES[case_id]["suppliers"])
    for o in p.openings.values():
        assert math.isfinite(o.c) and o.c > 0
        assert o.anchor is not None and 0 < o.anchor <= o.terms["unit_price"]
        assert o.floor is not None and o.floor > 0
        assert set(o.terms) == set(CASES[case_id]["negotiable"])
    assert p.goal in playbook.PLAYBOOK
    assert p.clock.force_close_at == 420.0


def test_preparation_with_opening_quotes_prices_standard_terms():
    """C of every opening with the supplier's standard terms filled in, and C_alt (R0.7.2)."""
    c = CASES["case:P01"]
    threads = {
        "sup:70110": [
            {
                "from": "sales@70110",
                "status": "open",
                "body": "",
                "term_sheets": [
                    {"term_sheet_id": "offer:70110-0", "terms": {"unit_price": 138.55, "effective_date": "2026-11-01"}}
                ],
                "attachments": [],
            }
        ]
    }
    p = prep.prepare(c, threads, PKG)
    o = p.openings["sup:70110"]
    assert o.terms["unit_price"] == 138.55 and o.terms["payment_days"] == 60  # gap filled, not left out
    assert p.c_alt == pytest.approx(total_cost(c, c["buyer"]["alternative"]["terms"]))
    assert p.c_open == pytest.approx(o.c)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["buyer"].update(horizon_years=7),  # API allows <= 3
        lambda c: c["buyer"].update(interest_rate="eight"),
        lambda c: c["buyer"].update(category_goal="world_peace"),
        lambda c: c.update(type="mystery"),
        lambda c: c.update(round_limit=None),
        lambda c: c["buyer"].update(must_haves=[{"field": "colour", "op": "~", "value": "red"}]),
        lambda c: c["buyer"].update(alternative={"kind": "barter"}),
        lambda c: c["buyer"].update(freight_eur_per_unit={"FCA": 1.9}),
        lambda c: c["buyer"].pop("index_risk_pct"),
        lambda c: c["fixed_terms"].update(payment_days=30),  # both fixed and negotiable
        lambda c: c["term_options"].pop("payment_days"),  # negotiable without options
        lambda c: c["buyer"].update(need_per_year=1_000_000),  # outside the practice range: kept, logged
    ],
)
def test_out_of_range_fields_are_logged_and_never_crash(mutate):
    c = copy.deepcopy(CASES["case:P01"])
    mutate(c)
    p = prep.prepare(c, package=PKG)
    assert p.warnings  # logged
    assert p.openings  # and prepared anyway
    assert p.row in playbook.PLAYBOOK.values()


def test_values_outside_the_practice_range_are_kept_for_the_cost():
    c = case("case:P01", interest_rate=0.12)
    norm, warnings = prep.normalise(c)
    assert norm["buyer"]["interest_rate"] == 0.12  # the arena prices C with it: never replaced
    assert any("interest_rate" in w for w in warnings)


def test_unknown_goal_and_type_take_the_routine_row_and_negotiable_levers():
    c = copy.deepcopy(CASES["case:P04"])
    c["buyer"]["category_goal"], c["type"] = "unknown", "unknown"
    assert playbook.row(c) is playbook.PLAYBOOK["routine"]
    assert playbook.type_levers(c) == [f for f in c["negotiable"] if f != "unit_price"]


def test_merged_anchor_table_takes_the_deeper_cut():
    """R4.1 vs R13.10 reconciled: the deeper of BuyerAgent's cut and MESO's share."""
    assert playbook.PLAYBOOK["leverage"]["cut"] == pytest.approx(max(0.12, 1 - 0.85))
    assert playbook.PLAYBOOK["routine"]["cut"] == pytest.approx(max(0.08, 1 - 0.90))
    assert playbook.PLAYBOOK["quality_first"]["cut"] == pytest.approx(max(0.06, 1 - 0.92))
    assert playbook.PLAYBOOK["secure_supply"]["cut"] == pytest.approx(max(0.05, 1 - 0.95))
    assert playbook.anchor_cut(CASES["case:P05"]) == pytest.approx(0.15 + playbook.RFQ_EXTRA)


def test_price_increase_anchor_is_the_current_price():
    """R13.12 / R4.1: no increase at all when the current price is below the quote (leverage: no margin)."""
    c = case("case:P01", category_goal="leverage")
    p = prep.prepare(
        c,
        {
            "sup:70110": [
                {
                    "from": "sales@70110",
                    "status": "open",
                    "body": "",
                    "attachments": [],
                    "term_sheets": [
                        {
                            "term_sheet_id": "offer:70110-0",
                            "terms": {"unit_price": 138.55, "effective_date": "2026-11-01"},
                        }
                    ],
                }
            ]
        },
        PKG,
    )
    o = p.openings["sup:70110"]
    # the lowest candidate is the current price, then lifted to the credibility limits (R4.2)
    assert o.anchor >= c["buyer"]["current_unit_price"] - 0.01
    assert o.anchor >= 0.75 * 138.55
    assert o.anchor <= max(c["buyer"]["current_unit_price"], o.estimate.full_cost * 1.01) + 0.01


def test_anchor_never_more_than_25_percent_under():
    c = CASES["case:P13"]
    p = prep.prepare(c, package=PKG)
    for o in p.openings.values():
        assert o.anchor >= 0.75 * o.terms["unit_price"] - 0.01


def test_coverage_lock_covers_the_two_year_need_when_it_pays():
    """R12.2: P08 - a 12-month quote on a 2-year need: at our price, 24 months lowers C."""
    c = CASES["case:P08"]
    their = complete(c, {"unit_price": 6.05, "payment_days": 30, "contract_months": 12})
    ctx = Ctx(c, {}, None, their, playbook.row(c))
    base = {k: v for k, v in their.items() if k != "unit_price"}
    assert uncovered_units(c, their) > 0
    covered = cover(ctx, base, 5.20)
    assert covered["contract_months"] == 24 and uncovered_units(c, {**covered, "unit_price": 5.2}) == 0


def test_coverage_is_not_forced_when_the_fallback_is_cheaper():
    """R12.2: trust the table - at a price above the fallback, covering more need only costs."""
    c = CASES["case:P08"]
    their = complete(c, {"unit_price": 6.05, "payment_days": 30, "contract_months": 12})
    ctx = Ctx(c, {}, None, their, playbook.row(c))
    base = {k: v for k, v in their.items() if k != "unit_price"}
    assert cover(ctx, base, 6.05)["contract_months"] == 12  # fallback 5.72 < 6.05
