import json
import random

import pytest

from arena_kit import ArenaClient, RemoteTable, play, strategy, tactics
from arena_kit.agents import DualAgent, dual
from arena_kit.agents.dual import FORCE_CLOSE_AT, NO_NEW_COUNTER_AT, concession_guard, weighted_sentence
from arena_kit.cost import total_cost
from arena_kit.intel import Analyst, Mail, walk_risk, weighted_impact
from arena_kit.sim import load_cases

CASES = load_cases()
SID = "sup:70270"  # case:S01's supplier


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _mail(price, sheet_id, status="open", body="Our offer.", payment=30, delay=1, validity=30, sender="sales"):
    return {
        "from": f"{sender}@70270",
        "status": status,
        "subject": "Quote",
        "body": body,
        "attachments": [],
        "signals": {"reply_delay_days": delay, "validity_days": validity},
        "term_sheets": [{"term_sheet_id": sheet_id, "terms": {"unit_price": price, "payment_days": payment}}],
    }


def _thread(*prices, status="open"):
    """A thread where the supplier quoted `prices` and we answered in between."""
    events = []
    for n, p in enumerate(prices):
        if n:
            events.append({"from": "buyer", "body": "Our offer.", "term_sheets": []})
        events.append(_mail(p, f"offer:70270-{n}", status if n == len(prices) - 1 else "open"))
    return events


def _prices(decision):
    return sorted(t["terms"]["unit_price"] for t in decision["message"].get("term_sheets") or [])


# --- walk risk ---------------------------------------------------------------------------------------
def test_walk_risk_is_low_on_a_calm_thread():
    mails = [Mail("open", c=100.0, delay=2, validity=30, role="sales", tone=1),
             Mail("open", c=98.0, delay=2, validity=30, role="sales", tone=1),
             Mail("open", c=96.5, delay=2, validity=30, role="sales", tone=1)]  # fmt: skip
    risk, signals = walk_risk(mails)
    assert risk < 0.2 and signals == {}


def test_walk_risk_is_high_on_a_final_offer_with_a_stale_price():
    mails = [Mail("open", c=100.0), Mail("open", c=97.0), Mail("open", c=97.0), Mail("final_offer", c=97.0)]
    risk, signals = walk_risk(mails)
    assert risk >= 0.7
    assert {"final_offer", "price_unchanged_2_rounds"} <= set(signals)


def test_walk_risk_reads_the_whole_thread():
    """Delay rising, validity shrinking, the boss writing and a tone drop count against earlier mails,
    not just the last one."""
    mails = [Mail("open", c=100.0, delay=1, validity=30, role="sales", tone=1),
             Mail("open", c=99.0, delay=1, validity=30, role="sales", tone=1),
             Mail("open", c=98.0, delay=4, validity=10, role="owner", tone=-1, withdraw=True)]  # fmt: skip
    risk, signals = walk_risk(mails)
    assert set(signals) == {"reply_delay_rising", "validity_shrinking", "escalated_to_boss", "tone_drop",
                            "withdraw_language"}  # fmt: skip
    assert risk >= 0.7


def test_the_analyst_sees_a_stale_final_offer():
    events = _thread(17.50, 17.00, 17.00, 17.00, status="final_offer")
    a = Analyst(CASES["case:S01"], SID).observe(events)
    i = a.snapshot()
    assert i.status == "final_offer" and i.unchanged_rounds == 2 and i.walk_risk >= 0.7


# --- prose offers ------------------------------------------------------------------------------------
def test_a_prose_offer_becomes_a_candidate_sheet_with_its_c():
    """The model reads "Alternatively, with 30 days net: EUR 16.20" - code checks the figure is in the
    e-mail, completes the sheet and prices it."""
    case = CASES["case:S01"]
    body = "Our price stays at EUR 17.00 per unit, 60 days. Alternatively, with 30 days net: EUR 16.20 per unit."
    events = [_mail(17.00, "offer:70270-1", body=body, payment=60)]
    calls = []

    def model(system, user):
        calls.append(user)
        return json.dumps({"prose_offers": [{"unit_price": 16.2, "payment_days": 30, "quote": "Alternatively..."},
                                            {"unit_price": 15.0, "payment_days": 30, "quote": "made up"}],
                           "tone": 1})  # fmt: skip

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        a = Analyst(case, SID, complete=model, pool=pool).observe(events)
        assert a.wait(5)
    i = a.snapshot()
    assert calls and i.llm == "done"
    found = {o["terms"]["unit_price"]: o for o in i.prose_offers}
    assert 15.0 not in found  # a figure the e-mail does not have is dropped
    alt = found[16.2]
    assert alt["terms"] == {"unit_price": 16.2, "payment_days": 30} and alt["in_sheet"] is None
    assert alt["c"] == total_cost(case, {"unit_price": 16.2, "payment_days": 30})
    assert alt["beats_standing"]


def test_the_negotiator_locks_in_a_better_prose_offer_late():
    case, clock = CASES["case:S01"], FakeClock()
    agent = DualAgent(None, None, clock=clock)
    body = "We hold EUR 17.00 at 60 days. Alternatively, with 30 days net: EUR 16.20 per unit."
    agent.decide_all(case, {SID: [_mail(17.50, "offer:70270-0")]})
    clock.now += NO_NEW_COUNTER_AT + 1
    agent.last_sent_at = clock.now - 30  # our last e-mail went out 30 s ago: one more round fits before 420 s
    events = [_mail(17.50, "offer:70270-0"), {"from": "buyer", "body": "x"}, _mail(17.00, "offer:70270-1", body=body,
              payment=60)]  # fmt: skip
    d = agent.decide_all(case, {SID: events})[0]
    assert d["message"]["term_sheets"] == [{"terms": {"unit_price": 16.2, "payment_days": 30}}]
    assert "offer you described" in d["message"]["body"]


# --- cost arithmetic ---------------------------------------------------------------------------------
def test_weighted_cost_impact():
    drivers = [{"change_pct": 2.7, "share_pct": 30}, {"change_pct": 6.5, "share_pct": 24}, {"change_pct": 5.0}]
    assert weighted_impact(drivers) == pytest.approx(2.37) and round(weighted_impact(drivers), 1) == 2.4


def test_the_analyst_reads_the_p01_cost_drivers_and_the_gap():
    case = {**CASES["case:S01"], "type": "price_increase", "buyer": {**CASES["case:S01"]["buyer"],
            "current_unit_price": 126.53}}  # fmt: skip
    body = ("Over the past twelve months, LCI-C rose 2.7 % in twelve months and is 30 % of our cost. "
            "Furthermore, PPI-C241 rose 6.5 % in twelve months and is 24 % of our cost.")  # fmt: skip
    i = Analyst(case, SID).observe([_mail(138.55, "offer:70270-0", body=body)]).snapshot()
    assert [(d["name"], d["change_pct"], d["share_pct"]) for d in i.cost_drivers] == [
        ("LCI-C", 2.7, 30.0), ("PPI-C241", 6.5, 24.0)]  # fmt: skip
    assert i.asked_pct == 9.5 and i.cost_impact_pct == pytest.approx(2.37) and i.cost_gap_pct == pytest.approx(7.13)
    assert "about +2.4%, not the 9.5%" in weighted_sentence(i)


# --- the concession guard ----------------------------------------------------------------------------
def test_the_concession_guard_never_exceeds_reciprocity():
    rng = random.Random(7)
    for _ in range(2000):
        prev, scale = rng.uniform(1e4, 1e6), 1e6
        proposed, their = prev + rng.uniform(-5e4, 5e4), rng.uniform(-2e4, 5e4)
        target, ours = concession_guard(prev, proposed, their, scale)
        assert ours >= 0 and target == prev + ours
        assert ours <= strategy.RECIPROCITY * max(their, 0) + 1e-9
        if their < dual.TOKEN_STEP * scale:
            assert ours == 0  # no real move from them: we hold
        else:  # a real move is answered - visibly, unless half their step is less
            assert ours >= min(strategy.MIN_STEP * scale, strategy.RECIPROCITY * their) - 1e-9


def test_every_counter_moves_at_most_half_their_step():
    """Over a whole thread, in C: our recommended offer never moves more than RECIPROCITY × their step,
    and never to their standing price."""
    case, agent = CASES["case:S01"], DualAgent(None, None)
    prices = [17.50, 17.20, 16.60, 16.55, 16.10]
    events, ours, theirs = [_mail(prices[0], "offer:70270-0")], [], []
    for n, p in enumerate(prices[1:], 1):
        d = agent.decide_all(case, {SID: events})[0]
        assert "action" not in d["message"]
        ours.append(agent.plans[SID].targets[-1])
        theirs.append(agent.plans[SID].their_costs[-1])
        assert max(_prices(d)) < events[-1]["term_sheets"][0]["terms"]["unit_price"]
        events += [{**d["message"], "from": "buyer"}, _mail(p, f"offer:70270-{n}")]
    for k in range(1, len(ours)):
        their_step = theirs[k - 1] - theirs[k]
        assert ours[k] - ours[k - 1] <= strategy.RECIPROCITY * max(their_step, 0) + 0.01


# --- closing with a clock -----------------------------------------------------------------------------
def test_no_counter_after_360s_and_closed_by_420s():
    case, clock = CASES["case:S01"], FakeClock()
    agent = DualAgent(None, None, clock=clock)
    first = agent.decide_all(case, {SID: [_mail(17.50, "offer:70270-0")]})[0]
    assert first["message"]["term_sheets"]
    events = [_mail(17.50, "offer:70270-0"), {**first["message"], "from": "buyer"}, _mail(17.20, "offer:70270-1")]
    clock.now += NO_NEW_COUNTER_AT - 30  # still time: a counter
    agent.last_sent_at = clock.now - 30  # the supplier took 30 s to reply
    d = agent.decide_all(case, {SID: events})[0]
    assert "action" not in d["message"]
    events += [{**d["message"], "from": "buyer"}, _mail(17.00, "offer:70270-2")]
    clock.now += 31  # past 360 s, an acceptable offer on the table: accept, no new counter
    agent.last_sent_at = clock.now - 30
    d = agent.decide_all(case, {SID: events})[0]
    assert d["message"]["action"] == {"type": "accept", "term_sheet_id": "offer:70270-2"}


def test_force_close_at_420s():
    case, clock = CASES["case:S01"], FakeClock()
    agent = DualAgent(None, None, clock=clock)
    agent.decide_all(case, {SID: [_mail(17.50, "offer:70270-0")]})
    clock.now += FORCE_CLOSE_AT + 1
    d = agent.decide_all(case, {SID: _thread(17.50, 17.30)})[0]
    assert d["message"]["action"]["type"] == "accept"


def test_after_360s_a_repeat_only_when_nothing_is_acceptable():
    """No acceptable offer: nothing to lose by asking once more - but with our last numbers, not new ones."""
    case = {**CASES["case:S03"]}
    sid = case["suppliers"][0]
    alt_c = strategy.c_alternative(case)
    assert alt_c < float("inf")
    clock = FakeClock()
    agent = DualAgent(None, None, clock=clock)

    def quote(price, n):
        terms = {f: case["term_options"].get(f, [None])[0] for f in case["negotiable"]} | {"unit_price": price}
        return {"from": f"sales@{sid[4:]}", "status": "open", "body": "Offer.", "attachments": [],
                "term_sheets": [{"term_sheet_id": f"offer:{sid[4:]}-{n}", "terms": terms}]}  # fmt: skip

    # loses to the alternative, yet within MAX_ANCHOR_CUT of it (deeper, no sheet could beat the alternative)
    dear = case["buyer"]["alternative"]["terms"]["unit_price"] * 1.2
    first = agent.decide_all(case, {sid: [quote(dear, 0)]})[0]
    events = [quote(dear, 0), {**first["message"], "from": "buyer"}, quote(dear * 0.98, 1)]
    clock.now += NO_NEW_COUNTER_AT + 1
    agent.last_sent_at = clock.now - 30
    d = agent.decide_all(case, {sid: events})[0]
    assert d["message"].get("term_sheets") == first["message"]["term_sheets"]  # the same offer, not a new one


# --- never a bad deal ----------------------------------------------------------------------------------
def test_never_accepts_what_loses_to_the_alternative_walks_instead():
    case = CASES["case:S03"]
    sid = case["suppliers"][0]
    dear = case["buyer"]["alternative"]["terms"]["unit_price"] * 1.5
    terms = {f: case["term_options"].get(f, [None])[0] for f in case["negotiable"]} | {"unit_price": dear}
    final = {"from": "sales@x", "status": "final_offer", "body": "Final.", "attachments": [],
             "term_sheets": [{"term_sheet_id": "offer:x-5", "terms": terms}]}  # fmt: skip
    assert total_cost(case, terms) >= strategy.c_alternative(case)
    d = DualAgent(None, None).decide_all(case, {sid: [final]})[0]
    assert d["message"]["action"] == {"type": "walk_away"}


def test_never_accepts_a_broken_must_have():
    case = {**CASES["case:S01"], "buyer": {**CASES["case:S01"]["buyer"],
            "must_haves": [{"field": "payment_days", "op": ">=", "value": 60}]}}  # fmt: skip
    final = _mail(15.00, "offer:70270-5", status="final_offer", payment=30)
    d = DualAgent(None, None).decide_all(case, {SID: [final]})[0]
    assert d["message"]["action"] == {"type": "walk_away"}


# --- tactics ---------------------------------------------------------------------------------------------
def test_tactics_change_words_only_and_never_invent_a_rival():
    i = Analyst(CASES["case:S01"], SID).observe(_thread(17.50, 17.50)).snapshot()
    name, _ = tactics.pick(i, {"first": False, "rfq": False, "rival_cheaper": False})
    assert name == "firm_disappointment"
    name, _ = tactics.pick(i, {"first": False, "rfq": True, "rival_cheaper": True})
    assert name in tactics.TACTICS
    assert tactics.pick(None, {"first": True})[0] == "measured_lowball"
    calm = Analyst(CASES["case:S01"], SID).observe(_thread(17.50, 16.90)).snapshot()
    assert tactics.pick(calm, {"first": False, "gap_pct": 0.8})[0] == "appreciation_close"
    for n in range(20):  # without a rival, never competitive pressure
        assert tactics.pick(calm, {"first": n % 2 == 0, "gap_pct": n})[0] != "competitive_pressure"


def test_a_slow_writer_falls_back_to_code_within_the_clock(monkeypatch):
    import time as _time

    monkeypatch.setattr(dual, "WRITE_TIMEOUT_S", 0.2)

    def slow(system, user):
        _time.sleep(10)
        return "{}"

    t0 = _time.monotonic()
    d = DualAgent(slow, None).decide_all(CASES["case:S01"], {SID: [_mail(17.50, "offer:70270-0")]})[0]
    assert _time.monotonic() - t0 < 5 and d["message"]["term_sheets"] and "Option" in d["message"]["body"]


# --- whole cases on the simulator -------------------------------------------------------------------------
@pytest.mark.parametrize("case_id", ["case:S01", "case:S02", "case:S03"])
def test_the_dual_agent_closes_every_sim_case(sim, case_id):
    url, state, _ = sim
    key = ArenaClient(url).register("Dual", "sim")["api_key"]
    table = RemoteTable(ArenaClient(url, key))
    result = play(table, case_id, DualAgent(None, None))
    assert result["deal"] and result["score"] > 0.5
    sent = [m for th in state.sessions[table.session_id]["threads"].values() for m in th["messages"]
            if m["from"] == "buyer"]  # fmt: skip
    assert all(
        not ts.get("terms") or set(ts["terms"]) == set(CASES[case_id]["negotiable"])
        for m in sent
        for ts in m.get("term_sheets") or []
    )


# --- regressions from the practice replays, 10 Oct ------------------------------------------------
def test_the_first_offer_is_never_taken():
    """P05: the floor estimate left no room under one quote, so it 'met our target' before we had
    countered once - and the case closed on an opening quote."""
    d = DualAgent(None, None).decide_all(CASES["case:S01"], {SID: [_mail(9.00, "offer:70270-0")]})[0]
    assert "action" not in d["message"] and d["message"]["term_sheets"]


def test_a_quote_that_breaks_a_must_have_neither_closes_the_case_nor_goes_unanswered():
    """P05: the cheapest quote broke the lead-time must-have; it triggered the close and the accept
    went to a dearer opening quote. Now it gets a counter that keeps the must-have."""
    base = CASES["case:S02"]
    case = {**base, "buyer": {**base["buyer"], "must_haves": [{"field": "lead_time_days", "op": "<=", "value": 35}]}}
    a, b = case["suppliers"]
    slow = {"from": f"sales@{a[4:]}", "status": "open", "body": "Offer.", "attachments": [],
            "term_sheets": [{"term_sheet_id": f"offer:{a[4:]}-0",
                             "terms": {"unit_price": 12.0, "payment_days": 30, "lead_time_days": 56}}]}  # fmt: skip
    fast = {"from": f"sales@{b[4:]}", "status": "open", "body": "Offer.", "attachments": [],
            "term_sheets": [{"term_sheet_id": f"offer:{b[4:]}-0",
                             "terms": {"unit_price": 20.0, "payment_days": 30, "lead_time_days": 35}}]}  # fmt: skip
    moves = {d["supplier_id"]: d["message"] for d in DualAgent(None, None).decide_all(case, {a: [slow], b: [fast]})}
    assert not any(m.get("action") for m in moves.values())
    assert moves[a]["term_sheets"] and all(t["terms"]["lead_time_days"] <= 35 for t in moves[a]["term_sheets"])


def test_a_refused_accept_of_an_alternative_accepts_the_standing_offer():
    from arena_kit.client import ArenaError

    class Client:
        sent = []

        def send_message(self, session_id, supplier_id, message):
            self.sent.append(message["action"]["term_sheet_id"])
            if message["action"]["term_sheet_id"].endswith("-a"):
                raise ArenaError(422, "not a standing offer in this session")
            return {"result": {"deal": True, "offer_id": message["action"]["term_sheet_id"]}}

        def thread(self, session_id, supplier_id):
            return {"offer_id": "offer:1-5", "closed": None}

    table = RemoteTable(Client())
    table.session_id = "s"
    out = table.close("sup:1", {"action": {"type": "accept", "term_sheet_id": "offer:1-5-a"}})
    assert out == {"deal": True, "offer_id": "offer:1-5"} and Client.sent == ["offer:1-5-a", "offer:1-5"]
