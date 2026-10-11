"""R12.4 and R13.1–R13.7: single-term menus, observed locks and prose alternatives."""

import pytest
from spec_support import agent, buyer, case, no_model, opening
from test_spec_sheets import ctx_for, leverage_ctx

from arena_kit.spec import levers
from arena_kit.spec.prose import prose_alternatives
from arena_kit.spec.steps import Steps, repeats


def test_r13_1_one_heavy_term_per_equal_cost_step_and_next_after_reply():
    c, ctx = leverage_ctx()
    base = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    steps = Steps()
    field = steps.next_field(ctx, base, {})
    assert field == "payment_days"
    packages = steps.packages(ctx, field, base, ctx.c(ctx.their) * 0.97, 0)
    assert 2 <= len(packages) <= 3
    assert len({t[field] for _, t in packages}) >= 2
    assert all(t["effective_date"] == base["effective_date"] for _, t in packages)
    sid = c["suppliers"][0]
    answer = packages[0][1]
    events = [opening(sid, ctx.their), buyer([t for _, t in packages]), opening(sid, answer)]
    steps.record(0, field)
    locked = steps.locked(ctx, events)
    assert locked == {field: answer[field]}
    assert steps.next_field(ctx, base, locked) == "effective_date"


def test_r13_1_skips_small_terms_and_limits_plan_to_three():
    c = case("case:P11", cost_per_defect=0)
    ctx = ctx_for(c, {"unit_price": 46, "ppm_commitment": 1500, "chargeback_eur_per_complaint": 0})
    base = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    assert Steps().rank(ctx, base) == []  # quality terms have no value when defects cost nothing
    c, ctx = leverage_ctx()
    assert len(Steps().rank(ctx, ctx.their)) <= 3


def test_r13_2_option_order_best_asked_current_middle_unique():
    _, ctx = leverage_ctx()
    picks = Steps.option_order(ctx, "payment_days", ctx.their, asked=14)
    assert picks == [90, 14, 30]
    assert Steps.option_order(ctx, "payment_days", ctx.their, asked=90) == [90, 30, 45]


def test_r13_3_reopens_costly_lock_once_only():
    c, ctx = leverage_ctx()
    sid = c["suppliers"][0]
    events = [opening(sid, ctx.their)]
    steps = Steps()
    for k in range(2):
        events += [buyer([ctx.their, {**ctx.their, "effective_date": "2027-04-01"}]), opening(sid, ctx.their)]
        steps.record(k, "effective_date")
        if k == 0:
            assert "effective_date" not in steps.locked(ctx, events)
        else:
            assert steps.locked(ctx, events)["effective_date"] == "2026-11-01"
            assert steps.counts()["effective_date"] == 2


def test_r13_4_no_single_package_step_or_spurious_lock():
    _, ctx = leverage_ctx()
    steps = Steps()
    target = ctx.c(ctx.their) * 0.97
    assert steps.packages(ctx, "payment_days", ctx.their, target, 1000) == []
    assert not steps.history
    steps.skipped.add("payment_days")
    assert steps.next_field(ctx, ctx.their, {}) == "effective_date"


def test_r13_5_revealed_term_chooses_cheapest_at_actual_supplier_prices():
    c, ctx = leverage_ctx()
    sid = c["suppliers"][0]
    main = {**ctx.their, "payment_days": 60, "unit_price": 138}
    alt = {"payment_days": 30, "unit_price": 134}
    msg = opening(sid, main)
    assert Steps().revealed(ctx, msg, [alt]) == {"payment_days": 30}
    assert Steps().revealed(ctx, msg, [{**alt, "unit_price": 140}]) == {"payment_days": 60}
    assert Steps().revealed(ctx, msg, [{**alt, "effective_date": "2027-04-01"}]) == {}


def test_r13_5_revealed_lock_survives_next_reply_without_alternatives():
    c, ctx = leverage_ctx()
    steps = Steps()
    msg = opening(c["suppliers"][0], {**ctx.their, "payment_days": 60})
    assert steps.revealed(ctx, msg, [{"payment_days": 30, "unit_price": 130}]) == {"payment_days": 30}
    assert steps.revealed(ctx, msg, []) == {"payment_days": 30}


def test_r13_5_revealed_cheaper_terms_override_previous_menu_lock():
    c = case("case:P01", category_goal="leverage")
    sid = c["suppliers"][0]
    terms = {"unit_price": 138.55, "payment_days": 30, "effective_date": "2027-04-01"}
    threads = {sid: [opening(sid, terms), buyer([terms]), opening(sid, terms), buyer([terms]),
                     opening(sid, terms, alternatives=[{**terms, "payment_days": 60, "unit_price": 134}])]}
    a = agent(c, threads)
    a.t[sid].steps.record(0, "payment_days")
    a.t[sid].steps.record(1, "payment_days")
    state = a.state(threads, sid)
    assert state["locked"]["payment_days"] == 60


@pytest.mark.parametrize("body,expected", [
    ("Alternatively, with 30 days net: EUR 137.00 per unit.", {"payment_days": 30, "unit_price": 137}),
    ("Alternativ, mit Zahlungsziel 30 Tage: EUR 137,00 pro Stück.", {"payment_days": 30, "unit_price": 137}),
    ("Alternativ, mit Lieferzeit 28 Tagen: EUR 31,50 je Stück.", {"lead_time_days": 28, "unit_price": 31.5}),
    ("Alternatively, with 24-month agreement: EUR 5.40 per kg.", {"contract_months": 24, "unit_price": 5.4}),
    ("Alternatively, with 23,400 units per year: EUR 137.00 per unit.",
     {"volume_per_year": 23400, "unit_price": 137}),
    ("Alternativ, mit Wirkung ab 1. April 2027: EUR 137,00 pro Stück.",
     {"effective_date": "2027-04-01", "unit_price": 137}),
    ("Alternatively, with 800 ppm and chargeback EUR 300: EUR 46.00 per unit.",
     {"ppm_commitment": 800, "chargeback_eur_per_complaint": 300, "unit_price": 46}),
    ("Alternatively, with DAP: EUR 137.00 per unit.", {"incoterm": "DAP", "unit_price": 137}),
])
def test_r13_6_english_and_german_prose(body, expected):
    c = {"negotiable": list(expected), "term_options": {}}
    assert prose_alternatives(c, {"body": body}) == [expected]


def test_r13_6_ambiguous_index_clause_is_skipped_and_explicit_band_pins_option():
    c = case("case:P08")
    opts = [o for o in c["term_options"]["index_clause"] if o]
    assert prose_alternatives(c, {"body": "Alternatively, with an index clause: EUR 5.40 per unit."}) == []
    clause = max(opts, key=lambda o: o["deadband_pct"])
    body = f"Alternativ, mit Preisgleitklausel {clause['series']}, Totband {clause['deadband_pct']}%: EUR 5,40 pro kg."
    assert prose_alternatives(c, {"body": body}) == [{"index_clause": clause, "unit_price": 5.4}]


def test_r12_4_revealed_slope_inherits_unchanged_prose_terms_and_skips_buyer():
    c, ctx = leverage_ctx()
    main = {**ctx.their, "payment_days": 60, "unit_price": 138}
    msg = opening(c["suppliers"][0], main)
    slopes = levers.revealed_slopes(c, [msg, buyer([main, {**main, "payment_days": 14, "unit_price": 90}])],
                                     [[{"payment_days": 30, "unit_price": 134}], []])
    expected = (134 / 138 - 1) / (30 - 60)
    assert slopes == {"payment_days": pytest.approx(expected)}
    assert levers.revealed_rate(slopes) == pytest.approx(expected * 360)


def test_r12_4_expensive_payment_trade_is_not_a_buyer_gain():
    c, ctx = leverage_ctx()
    terms = {**ctx.their, "payment_days": 14}
    assert "payment_days" in levers.best_levers(c, terms)
    assert "payment_days" not in levers.best_levers(c, terms, slopes={"payment_days": 0.01})


def test_r13_7_sheet_order_does_not_hide_identical_resend():
    a, b = {"unit_price": 10, "payment_days": 30}, {"unit_price": 11, "payment_days": 60}
    assert repeats([a, b], [b, a])
    assert not repeats([{**a, "unit_price": 10.01}, b], [b, a])
    assert not repeats([a], [])


def test_r12_14_first_rfq_message_by_code_cites_rival_without_model():
    c = case("case:P05")
    prices = [36, 31.5, 34]
    threads = {sid: [opening(sid, {"unit_price": p, "payment_days": 60, "lead_time_days": 42})]
               for sid, p in zip(c["suppliers"], prices, strict=True)}
    a = agent(c, threads, complete=no_model)
    def no_wait(*args, **kwargs):
        raise AssertionError("the first RFQ message must not wait for the analyst model")

    for th in a.t.values():
        th.analyst.wait = no_wait
    for sid in (c["suppliers"][0], c["suppliers"][2]):
        decision = a.decide(c, threads, sid)
        body = decision["message"]["body"]
        assert "A competing supplier has offered EUR 31.50" in body
        assert f"offer:{c['suppliers'][1].split(':')[1]}-0" in body
        assert decision["message"].get("term_sheets")


def test_r13_7_rfq_stops_instead_of_resending_identical_packages(monkeypatch):
    c = case("case:P05")
    threads = {sid: [opening(sid, {"unit_price": 36, "payment_days": 60, "lead_time_days": 42})]
               for sid in c["suppliers"]}
    a = agent(c, threads)
    sid = c["suppliers"][0]
    state = a.state(threads, sid)
    package = {**state["base"], "unit_price": 25}
    a.t[sid].last_sheets = [("same", package)]
    monkeypatch.setattr(a, "build", lambda *args: ([("same", package)], "price", None))
    decision = a.decide(c, threads, sid)
    assert decision.get("idle") and "message" not in decision


def test_r13_4_guard_removing_choice_does_not_record_single_sheet_step(monkeypatch):
    from arena_kit.spec import sheets

    c = case("case:P01", category_goal="leverage")
    sid = c["suppliers"][0]
    threads = {sid: [opening(sid, {"unit_price": 138.55, "payment_days": 30,
                                 "effective_date": "2026-11-01"})]}
    a = agent(c, threads)
    original_guard = sheets.guard

    def one_choice(*args):
        return original_guard(*args)[:1]

    monkeypatch.setattr(sheets, "guard", one_choice)
    decision = a.decide(c, threads, sid)
    assert decision["message"].get("term_sheets")
    assert a.t[sid].steps.history[0] == "price"
