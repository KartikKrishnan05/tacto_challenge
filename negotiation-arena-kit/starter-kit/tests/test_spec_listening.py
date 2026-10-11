"""Listening to the supplier and keeping words and numbers together: no step back behind our own last
offer (P02), every cost driver a sentence names (P18), refused terms out of our sheets and answered (P08),
no argument that concedes more than we offer (P08, P18), and fresh lines and arguments every round."""

import json

import pytest
from spec_support import CASES, KIT, PKG

from arena_kit.spec import email, sheets, tactics
from arena_kit.spec.analyst import Intel, clean_extraction, drivers_by_code, refused_by_code
from arena_kit.spec.closing import Row, View, decide
from arena_kit.spec.concession import CEILING, Plan

LAB = KIT / "runs" / "lab" / "1011-085641_learned-2"


# --- no step back (P02) ----------------------------------------------------------------------------
def test_the_ceiling_never_pushes_us_behind_our_last_offer():
    """P02: their C came within 0.3 % of our last target; 0.997 x their C fell under it and the next offer
    went from 370.85 to 370.52 - "a step backwards" - and the supplier sent its final offer."""
    p = Plan(anchor=2_235_519, targets=[2_235_519, 2_244_600, 2_253_658],
             their_costs=[2_282_867, 2_270_368, 2_264_405, 2_258_878], replies=4)  # fmt: skip
    target, info = p.next_target(2_258_878, horizon=6)
    assert CEILING * 2_258_878 < 2_253_658
    assert target >= 2_253_658 and info["at_ceiling"]
    assert p.breaker(2_258_878) >= 2_253_658


def test_at_the_ceiling_our_offer_stands_once_then_its_offer_is_taken():
    r = Row("offer:70510-3", {"unit_price": 371.88}, 2_258_878, True, True, True, 3)
    v = View("sup:70510", "open", 6, 3, [r], target=2_253_658, last_target=2_253_658, at_ceiling=True)
    assert decide({v.sid: v}, v.sid).kind == "repeat"
    v.held = True
    c = decide({v.sid: v}, v.sid)
    assert c.kind == "accept" and c.sheet_id == "offer:70510-3"


# --- every driver (P18) -----------------------------------------------------------------------------
P18_DRIVERS = (
    "To give you some background on our current cost structure, AS-14404 rose 5.9 % in twelve months and is 45 % "
    "of our cost. Furthermore, LCI-C rose 2.7 % in twelve months and is 5 % of our cost, while PPI-C2431 rose "
    "12.3 % in twelve months and is 38 % of our cost."
)


def test_two_drivers_in_one_sentence_are_both_read():
    found = {d["name"]: (d["change_pct"], d["share_pct"]) for d in drivers_by_code(P18_DRIVERS)}
    assert found == {"AS-14404": (5.9, 45.0), "LCI-C": (2.7, 5.0), "PPI-C2431": (12.3, 38.0)}


# --- refused terms (P08) ----------------------------------------------------------------------------
NEG = ["unit_price", "index_clause", "payment_days", "contract_months"]


@pytest.mark.parametrize("body", [
    "As we cannot offer a fixed price over a 24-month period, we must include an AS-14404 index clause.",
    "We can accept the 24-month term, but we must include an AS-14404 index clause.",
])  # fmt: skip
def test_a_refused_fixed_price_is_read(body):
    assert {"field": "index_clause", "value": None} in refused_by_code(body, NEG)


def test_lead_time_days_are_not_payment_days():
    assert refused_by_code("We cannot offer 7 days lead time.", NEG) == []
    assert refused_by_code("We cannot accept 90 days payment.", NEG) == [{"field": "payment_days", "value": 90}]


def test_the_model_may_report_refusals_only_with_figures_from_the_mail():
    body = "We cannot accept 90-day payment terms."
    parsed = {"refused_terms": [{"field": "payment_days", "value": 90}, {"field": "payment_days", "value": 60},
                                {"field": "contract_months", "value": None}],
              "at_limit": True, "wants_close": "yes"}  # fmt: skip
    out = clean_extraction(parsed, body, NEG)
    assert out["refused_terms"] == [{"field": "payment_days", "value": 90}]
    assert out["at_limit"] is True and out["wants_close"] is False


def test_refused_sheets_are_dropped_unless_nothing_is_left():
    idx = {"series": "AS-14404", "material_share": 0.45, "deadband_pct": 0, "base_month": "2026-08"}
    menu = [("A", {"unit_price": 4.68, "index_clause": idx}), ("B", {"unit_price": 4.82, "index_clause": None})]
    no_fixed = [{"field": "index_clause", "value": None}]
    assert sheets.drop_refused(menu, no_fixed) == menu[:1]
    assert sheets.drop_refused(menu[1:], no_fixed) == menu[1:]


# --- words match numbers ----------------------------------------------------------------------------
def test_an_argument_conceding_more_than_we_offer_is_not_cited():
    """P08: "the weighted effect is about +7.5 %" on 5.45 argues for 5.86; we offered 4.63."""
    c = CASES["case:P08"]
    assert not email.consistent(c, 7.5, 4.63)
    assert email.consistent(c, 7.5, 5.90)
    assert email.consistent(c, None, 4.63)


def test_position_lines_argue_only_with_what_the_supplier_can_check():
    lines = dict(email.position_lines(CASES["case:P08"], Intel("sup:70710"), 5.47, 1.2, held=True))
    assert lines["ask_left"] == "Your latest offer is still 0.4% above today's price of EUR 5.45."
    assert "moved 1.2% toward you" in lines["our_steps"]
    assert set(lines) == {"ask_left", "our_steps"}
    assert email.position_lines(CASES["case:P08"], Intel("sup:70710"), 5.40, 1.2) == []


def test_the_weighted_argument_is_an_upper_bound_when_we_offer_less():
    i = Intel("sup:70710", cost_drivers=[{"name": "AS-14404", "series": "AS-14404", "change_pct": 5.9,
                                          "share_pct": 45.0}], cost_impact_pct=2.66, asked_pct=11.01, cost_gap_pct=8.35)
    assert email.weighted_sentence(i).startswith("By the figures you gave us")
    assert "at most about +2.7% on the price, not the 11.01%" in email.weighted_sentence(i, face_value=True)


def test_a_volume_question_is_answered_with_the_case_volume():
    assert email.commitment({"contract_months": 24}, 23_700) == (
        "We can commit to 23,700 units per year over 24 months, as set out in the term sheets.")


def test_one_sheet_gets_no_line_about_choosing():
    for t in tactics.TACTICS.values():
        assert not any(tactics.CHOICE.search(x) for x in tactics.fresh(t, single=True))


def test_code_lines_are_not_repeated_in_a_thread():
    t = tactics.TACTICS["data_argument"]
    first = tactics.fresh(t)
    second = tactics.fresh(t, "\n".join(first))
    assert all(a != b for a, b in zip(first, second, strict=True))


# --- the recorded games, through the new code -------------------------------------------------------
def _record(case):
    path = next(LAB.glob(f"{case}__*.json"), None)
    if path is None:
        pytest.skip(f"no recorded {case} game")
    rec = json.loads(path.read_text(encoding="utf-8"))
    sid, thread = next(iter(rec["threads"].items()))
    return rec, CASES[rec["case_id"]], sid, thread["messages"]


def _play(case, replies):
    """Our decisions after each of the supplier's first `replies` recorded replies (code only)."""
    from arena_kit.agents.spec import SpecAgent
    from arena_kit.spec import strategy

    rec, case_file, sid, msgs = _record(case)
    a = SpecAgent(None, PKG, strategy=strategy.with_knobs(**rec["knobs"]), log_dir=KIT / "runs" / "spec-logs" / "tests")
    a.prime(case_file, timeout=0)
    out, thread = [], []
    for m in msgs:
        if m.get("from") == "buyer":
            continue
        thread.append(m)  # its recorded reply, after our own e-mail (not the recorded one) as the thread has it
        d = a.decide(case_file, {sid: list(thread)}, sid, use_model=False)
        out.append(d)
        if len(out) >= replies or "term_sheets" not in d.get("message", {}):
            break
        thread.append({"from": "buyer", "body": d["message"]["body"], "term_sheets": d["message"]["term_sheets"]})
    return msgs, out


@pytest.mark.skipif(PKG is None, reason="no data package")
def test_p08_answers_the_refused_fixed_price_and_the_call():
    _, out = _play("P08", 2)
    msg = out[1]["message"]
    assert all(ts["terms"].get("index_clause") for ts in msg["term_sheets"])
    assert "every option below includes an index clause" in msg["body"]
    assert email.CALL_LINE in msg["body"]
    assert "making good pumps together" not in msg["body"]  # a leverage case is not argued with a joke


@pytest.mark.skipif(PKG is None, reason="no data package")
def test_p18_cites_no_driver_arithmetic_that_argues_for_more_than_we_offer():
    _, out = _play("P18", 3)
    bodies = [d["message"]["body"] for d in out]
    assert not any("+2.8%" in b for b in bodies)  # its own drivers explain +7.5 %, more than it asks: no argument
    later = [b.split("\n\n")[1:-1] for b in bodies[1:]]
    assert not set(later[0]) & set(later[1])  # no paragraph repeated word for word


@pytest.mark.skipif(PKG is None, reason="no data package")
def test_p02_never_offers_less_than_before():
    msgs, out = _play("P02", 4)
    prices = [d["message"]["term_sheets"][0]["terms"]["unit_price"] for d in out if "term_sheets" in d["message"]]
    assert prices == sorted(prices)
    last = out[-1]["message"]
    assert last.get("action", {}).get("type") == "accept" or prices[-1] >= 370.85
