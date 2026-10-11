"""R11.2, R11.8 and R11.12: extraction is evidence, never invented numbers."""

import pytest
from spec_support import agent, case, opening

from arena_kit.spec.analyst import Intel, appears, clean_extraction
from arena_kit.spec.tactics import pick


def test_r11_2_drops_unstated_prices_days_dates_and_driver_shares():
    body = "We can offer EUR 137.00 with payment 30 days, from 1 April 2027. Energy rose 2.7%."
    parsed = {
        "prose_offers": [
            {"unit_price": 99, "payment_days": 30},
            {"unit_price": 137, "payment_days": 60, "effective_date": "2027-01-01",
             "other_terms": {"lead_time_days": 42, "incoterm": "DAP"}},
            {"unit_price": 137, "payment_days": 30, "effective_date": "2027-04-01"},
        ],
        "cost_drivers": [
            {"name": "energy", "change_pct": 2.7, "cost_share_pct": 40},
            {"name": "labour", "change_pct": 5, "cost_share_pct": 30},
        ],
    }
    result = clean_extraction(parsed, body, ["unit_price", "payment_days", "effective_date",
                                             "lead_time_days", "incoterm"])
    assert [o["terms"] for o in result["prose_offers"]] == [
        {"unit_price": 137.0},
        {"unit_price": 137.0, "payment_days": 30, "effective_date": "2027-04-01"},
    ]
    assert len(result["cost_drivers"]) == 1
    assert result["cost_drivers"][0]["share_pct"] is None


@pytest.mark.parametrize("number", [37, 13, float("nan"), float("inf")])
def test_r11_2_no_substring_or_nonfinite_figures(number):
    assert not appears(number, "EUR 137.00, payment 30 days")


def test_r11_2_cannot_truncate_price_or_invent_hint_figures():
    assert not appears(137, "EUR 137.90")
    assert not appears(3, "The index moved -3%.")
    assert appears(137, "EUR 137.00, payment 30 days")
    result = clean_extraction({"constraints": ["Only payment 30 days", "Only payment 14 days"],
                               "lever_hints": [{"field": "payment_days", "hint": "14 days"}]},
                              "Payment 30 days", ["payment_days"])
    assert result["constraints"] == ["Only payment 30 days"]
    assert result["lever_hints"] == []


def test_r11_8_locks_better_prose_offer_with_inherited_terms():
    c = case("case:P01", category_goal="leverage")
    sid = c["suppliers"][0]
    terms = {"unit_price": 138.55, "payment_days": 60, "effective_date": "2027-04-01"}
    threads = {sid: [opening(sid, terms, body="We can offer EUR 135.00 with payment 60 days.")]}
    a = agent(c, threads)
    state = a.state(threads, sid)
    got = a.lock_in(state)
    assert got and got[0] == {**terms, "unit_price": 135}
    states, views = a.views(threads)
    a.clock.t_open -= 361  # late, and the default 55-second round still fits
    move = a.late(states, views, sid)
    assert move.kind == "lock_in"
    decision = a.realise(move, states, threads, sid)
    assert decision["message"]["term_sheets"][0]["terms"] == got[0]
    assert "lock" in a.t[sid].flags


def test_r11_8_does_not_lock_expensive_or_must_have_breaking_prose():
    c = case("case:P05")
    sid = c["suppliers"][0]
    threads = {sid: [opening(sid, {"unit_price": 36, "payment_days": 60, "lead_time_days": 42})]}
    a = agent(c, threads)
    state = a.state(threads, sid)
    for terms in ({"unit_price": 40}, {"unit_price": 20, "lead_time_days": 52}):
        state["intel"].prose_offers = [{"terms": terms}]
        assert a.lock_in(state) is None


def test_r11_12_competitive_pressure_requires_real_competition():
    for first in (True, False):
        name, _ = pick(Intel("s"), {"first": first, "rfq": False, "rival_cheaper": False})
        assert name != "competitive_pressure"
    assert pick(Intel("s"), {"rival_cheaper": True})[0] == "competitive_pressure"


def test_tactic_respects_risk_and_annoyed_supplier():
    intel = Intel("s", tone=-2, walk_risk=0.5, relationship_style="relationship")
    assert pick(intel, {"supplier_held": True, "hold": True, "relationship": True})[0] == "appreciation_close"


def test_r11_15_agent_closes_before_starting_round_that_would_not_fit():
    c = case("case:P01", category_goal="leverage")
    sid = c["suppliers"][0]
    threads = {sid: [opening(sid, {"unit_price": 138.55, "payment_days": 60,
                                 "effective_date": "2027-04-01"})]}
    a = agent(c, threads)
    a.clock.t_open -= 330
    a.clock.rounds = [90]
    decision = a.decide(c, threads, sid)
    assert decision["message"]["action"]["type"] == "accept"


def test_r12_17_writer_skipped_when_reply_budget_is_under_ten_seconds():
    import time

    from spec_support import no_model

    c = case("case:P01", category_goal="leverage")
    sid = c["suppliers"][0]
    threads = {sid: [opening(sid, {"unit_price": 138.55, "payment_days": 60,
                                 "effective_date": "2027-04-01"})]}
    a = agent(c, threads)
    a.complete = no_model
    a.reply_at[sid] = time.monotonic() - 45
    decision = a.decide(c, threads, sid)
    assert decision["message"].get("term_sheets")
