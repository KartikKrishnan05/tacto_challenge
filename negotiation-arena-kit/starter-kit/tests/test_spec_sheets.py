"""Term sheets: surplus (R6.2), ask sheets (R6.3), decoy (R6.4), floor (R6.5), repair (R11.9), rebuild
on hold (R11.10), the guard (R11.11), coverage lock (R12.2), payment probe (R12.7), never regress
(R12.10) and exact prices (R13.8)."""

import pytest
from spec_support import CASES, PKG, case

from arena_kit.cost import must_haves_ok, total_cost
from arena_kit.spec import estimate as est_mod
from arena_kit.spec import playbook, pricing, sheets
from arena_kit.spec.steps import Steps
from arena_kit.spec.terms import complete


def ctx_for(c, their, std=None, est=None):
    their = complete(c, their, std or {})
    est = est or est_mod.estimate(c, PKG, [], their)
    return sheets.Ctx(c, std or {}, est, their, playbook.row(c))


def leverage_ctx():
    c = case("case:P01", category_goal="leverage")  # no margin charge: C is linear in the price
    return c, ctx_for(c, {"unit_price": 138.55, "effective_date": "2026-11-01", "payment_days": 30})


def test_r12_3_no_unnecessary_expedited_delivery_when_normal_lead_is_cost_neutral():
    c = case("case:P05")
    ctx = ctx_for(c, {"unit_price": 36, "payment_days": 60, "lead_time_days": 28})
    base = sheets.structure(ctx, 30)
    assert base["lead_time_days"] == 35
    assert ctx.c({**base, "unit_price": 30}) == ctx.c({**base, "lead_time_days": 28, "unit_price": 30})
    c["buyer"]["must_haves"] = [{"field": "lead_time_days", "op": "<=", "value": 28}]
    assert sheets.structure(ctx, 30)["lead_time_days"] == 28


# --- R13.8 ------------------------------------------------------------------------------------------
def test_price_solves_the_target_exactly_without_margin_charge():
    c, ctx = leverage_ctx()
    combo = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    target = ctx.c(ctx.their) * 0.97
    p = pricing.price_for(c, combo, {}, target)
    assert abs(total_cost(c, {**combo, "unit_price": p}) - target) <= pricing.tolerance(c, combo)


def test_step_packages_cost_the_same_within_the_cent_tolerance():
    """R13.8: every sheet of a step has the same C (EUR 1, or half a cent per committed unit)."""
    from arena_kit.spec.steps import Steps

    c, ctx = leverage_ctx()
    base = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    target = ctx.c(ctx.their) * 0.97
    pk = Steps().packages(ctx, "payment_days", base, target, 0.0)
    assert len(pk) >= 2
    tol = pricing.tolerance(c, base)
    assert tol < 50  # 8,150 units: whole cents land within EUR 41
    for _, t in pk:
        assert abs(ctx.c(t) - target) <= tol


def test_with_margin_charge_the_price_sits_above_the_cheapest_price():
    """R13.8: C is convex in the price with the margin charge; the solved price is on the side above
    the C-minimising price."""
    c = CASES["case:P01"]  # stabilise
    ctx = ctx_for(c, {"unit_price": 138.55, "effective_date": "2026-11-01"})
    sup = ctx.supplier
    assert sup is not None
    combo = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    low = pricing.cheapest_price(c, combo, {}, sup, 138.55)
    c_low = total_cost(c, {**combo, "unit_price": low}, {}, sup)
    assert total_cost(c, {**combo, "unit_price": low - 3}, {}, sup) > c_low  # below it C rises again
    target = c_low + 20_000
    p = pricing.price_for(c, combo, {}, target, sup, 138.55)
    assert p > low
    assert abs(total_cost(c, {**combo, "unit_price": p}, {}, sup) - target) <= pricing.tolerance(c, combo)
    assert pricing.price_for(c, combo, {}, c_low - 50_000, sup, 138.55) is None  # out of reach


# --- R6.2 -------------------------------------------------------------------------------------------
def test_give_price_shares_the_surplus():
    assert sheets.give_price(100.0, 96.0) == 98.0  # halfway to the supplier's indifference price
    assert sheets.give_price(100.0, 101.0) == 100.0  # worth less to it than to us: equal cost
    assert sheets.give_price(100.0, None) == 100.0
    assert sheets.give_price(None, 96.0) is None


def test_faster_payment_to_a_cash_short_supplier_is_a_shared_gain():
    """R6.2: at a 14 % cost of money paying 30 days earlier is worth more to it than to us (8 %)."""
    c, ctx = leverage_ctx()
    ctx.est.rate = 0.14
    ref = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    ref["payment_days"] = 60
    give = {**ref, "payment_days": 30}
    p_ind = sheets.indifference_price(ctx, give, ref, 130.0)
    p_eq = pricing.price_for(c, give, {}, ctx.c({**ref, "unit_price": 130.0}))
    assert p_ind < p_eq
    p = sheets.give_price(p_eq, p_ind)
    assert p_ind < p < p_eq


# --- R6.3 -------------------------------------------------------------------------------------------
def test_the_supplier_asked_two_sheets_its_ask_cheaper_and_our_reference():
    c, ctx = leverage_ctx()
    base = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    target = ctx.c(ctx.their) * 0.97
    out = sheets.price_sheets(ctx, base, target, ["payment_days"], 100.0, asks={"payment_days": 30})
    assert [label for label, _ in out] == ["their ask", "reference"]
    ask, ref = out[0][1], out[1][1]
    assert ask["payment_days"] == 30 and ref["payment_days"] == 60
    assert ask["unit_price"] < ref["unit_price"]


# --- R6.4 -------------------------------------------------------------------------------------------
def test_decoy_lean_recommended_other():
    c, ctx = leverage_ctx()
    base = {**{k: v for k, v in ctx.their.items() if k != "unit_price"}, "payment_days": 60}
    ctx.their["payment_days"] = 60
    target = ctx.c(ctx.their) * 0.97
    out = sheets.price_sheets(ctx, base, target, ["payment_days", "effective_date"], 100.0)
    labels = [label for label, _ in out]
    assert labels[:2] == ["lean", "recommended"]
    lean, mid = out[0][1], out[1][1]
    assert lean["unit_price"] == pytest.approx(round(mid["unit_price"] * 0.98, 2))
    assert sum(lean[f] != mid[f] for f in lean if f != "unit_price") == 1  # one term moved our way
    assert ctx.c(lean) <= ctx.c(mid)  # safe: if it takes the lean one, we gain


# --- R6.5 / R6.6 ------------------------------------------------------------------------------------
def test_sheet_floor_and_out_of_reach():
    c, ctx = leverage_ctx()
    assert sheets.floor(ctx) == pytest.approx(max(ctx.est.full_cost * 0.97, 0.75 * 138.55))
    base = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    far = ctx.c({**base, "unit_price": 50.0})
    assert sheets.priced(ctx, base, far, sheets.floor(ctx)) is None  # under the floor: not credible
    t = sheets.out_of_reach(ctx, base, sheets.floor(ctx))[0][1]
    assert t["unit_price"] == pytest.approx(max(sheets.floor(ctx), 0.99 * 138.55), abs=0.01)


# --- R11.9 ------------------------------------------------------------------------------------------
def test_repair_moves_a_broken_must_have_to_the_nearest_allowed_value():
    """P05: a 52-day lead time against <= 42 left no sheet to send."""
    c = CASES["case:P05"]
    fixed = sheets.repair(c, {"unit_price": 31.5, "lead_time_days": 52, "payment_days": 60})
    assert fixed["lead_time_days"] == 42 and must_haves_ok(c, fixed)


# --- R11.10 -----------------------------------------------------------------------------------------
def test_hold_rebuilds_the_sheets_on_their_new_terms_at_the_same_target():
    c, ctx = leverage_ctx()
    target = ctx.c(ctx.their) * 0.97
    first = sheets.price_sheets(ctx, {k: v for k, v in ctx.their.items() if k != "unit_price"}, target, [], 100.0)
    ctx2 = ctx_for(c, {"unit_price": 137.0, "effective_date": "2027-01-01", "payment_days": 45})
    again = sheets.price_sheets(ctx2, {k: v for k, v in ctx2.their.items() if k != "unit_price"}, target, [], 100.0)
    assert [t for _, t in again] != [t for _, t in first]  # new sheets, not "no adjustments"
    for _, t in again:  # every sheet at the same target C at most (the lean decoy may cost us less)
        assert ctx2.c(t) <= target + pricing.tolerance(c, t)


# --- R11.11 -----------------------------------------------------------------------------------------
def test_guard_is_codes_last_word():
    c, ctx = leverage_ctx()
    target = ctx.c(ctx.their) * 0.97
    base = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    ok = sheets.priced(ctx, base, target, 0)
    above = complete(c, {**base, "unit_price": ok["unit_price"] + 2})
    theirs = dict(ctx.their)
    out = sheets.guard(ctx, [("ok", ok), ("above target", above), ("theirs", theirs)], target, [theirs])
    assert [label for label, _ in out] == ["ok"]
    last = [("last", ok)]
    assert sheets.guard(ctx, [("above", above)], target, [theirs], last) == last  # nothing survives: our last


def test_guard_drops_sheets_breaking_a_must_have():
    c = CASES["case:P05"]
    ctx = ctx_for(c, {"unit_price": 31.5, "lead_time_days": 35, "payment_days": 60})
    t = complete(c, {"unit_price": 25.0, "lead_time_days": 56, "payment_days": 60})
    assert sheets.guard(ctx, [("bad", t)], ctx.c(ctx.their), []) == []


# --- R12.2 ------------------------------------------------------------------------------------------
def test_coverage_lock_no_sheet_covers_less_than_sheet_a():
    c = CASES["case:P08"]
    ctx = ctx_for(c, {"unit_price": 6.05, "payment_days": 30, "contract_months": 12})
    sheets_ = [("A", complete(c, {"unit_price": 5.2, "contract_months": 24})),
               ("B", complete(c, {"unit_price": 5.1, "contract_months": 12}))]  # fmt: skip
    out = sheets.lock_coverage(ctx, sheets_, {"contract_months": 24})
    assert all(t["contract_months"] >= 24 for _, t in out)


# --- R12.7 ------------------------------------------------------------------------------------------
def test_payment_probe_varies_payment_at_equal_cost():
    c, ctx = leverage_ctx()
    target = ctx.c(ctx.their) * 0.97
    base = {**{k: v for k, v in ctx.their.items() if k != "unit_price"}, "payment_days": 90}
    a = sheets.priced(ctx, base, target, 0)
    probe = sheets.payment_probe(ctx, a, target, 0)
    assert probe["payment_days"] == 30  # the supplier's own value
    assert abs(ctx.c(probe) - ctx.c(a)) <= 2 * pricing.tolerance(c, a)


# --- R12.10 -----------------------------------------------------------------------------------------
def test_never_regress_behind_an_offer_the_supplier_already_made():
    c, ctx = leverage_ctx()
    base = {k: v for k, v in ctx.their.items() if k != "unit_price"}
    cheap = complete(c, {**base, "unit_price": 130.0})
    dear = complete(c, {**base, "unit_price": 134.0})
    supplier_offered = ctx.c(complete(c, {**base, "unit_price": 133.0}))
    out = sheets.no_regress(ctx, [("cheap", cheap), ("dear", dear)], [supplier_offered])
    assert [label for label, _ in out] == ["cheap"]


def test_later_effective_date_can_trade_for_higher_price_at_lower_total_cost():
    c, ctx = leverage_ctx()
    combo = {**ctx.their, "effective_date": "2027-04-01"}
    target = ctx.c(ctx.their) * .99
    t = sheets.priced(ctx, combo, target, 100)
    assert t and t["unit_price"] > ctx.their_price
    assert ctx.c(t) < ctx.c_theirs


def test_price_round_cannot_undo_settled_payment_or_quality_terms():
    c, ctx = leverage_ctx()
    ctx.locked = {"payment_days": 30.0, "effective_date": "2026-11-01"}
    out = sheets.price_sheets(ctx, ctx.their, ctx.c(ctx.their) * .97, [], 100)
    assert out
    assert all(t["payment_days"] == 30 and t["effective_date"] == "2026-11-01" for _, t in out)


def test_supplier_insisted_index_menu_offers_its_series_not_no_clause():
    c = CASES["case:P08"]
    zero = next(o for o in c["term_options"]["index_clause"] if o and o["deadband_pct"] == 0)
    ctx = ctx_for(c, {"unit_price": 5.57, "index_clause": zero, "payment_days": 90, "contract_months": 24})
    offered = ctx.allowed("index_clause")
    assert None not in offered
    assert sorted(o["deadband_pct"] for o in offered) == [0, 3, 5]
    # the step menu leads with the deadband that costs us least
    assert Steps.option_order(ctx, "index_clause", ctx.their)[0]["deadband_pct"] == 5
