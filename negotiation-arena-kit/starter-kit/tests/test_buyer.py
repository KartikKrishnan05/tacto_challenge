import json
from pathlib import Path

import pytest

from arena_kit import ArenaClient, RemoteTable, play, strategy, writing
from arena_kit import claims as cl
from arena_kit.agents import BuyerAgent
from arena_kit.cost import must_haves_ok, total_cost
from arena_kit.sim import load_cases

PACKAGE = Path(__file__).parents[1] / "data" / "package"
CASES = load_cases()


def test_index_claims_are_the_arenas_arithmetic():
    case = CASES["case:S01"]  # its facts carry PPI-C2431 for 2025-08 and 2026-08
    got = cl.index_change(None, case, "PPI-C2431")
    assert got == (round((129.1 / 115.0 - 1) * 100, 1), "idx:PPI-C2431@2025-08", "idx:PPI-C2431@2026-08")
    claims = cl.candidates(None, case, "sup:70270", their_price=17.5)
    index = next(c for c in claims if c.key == "index:PPI-C2431")
    assert "+12.3%" in index.sentence and not index.favourable  # a rise argues the supplier's side


def test_a_claim_counts_once_per_case_a_rival_quote_once_per_supplier():
    ledger = cl.Ledger()
    idx = cl.Claim("index:X", "index", ["idx:X@2025-08", "idx:X@2026-08"], "")
    rival = cl.Claim("rival:offer:A", "rival", ["offer:A-1"], "")
    ledger.use(idx, "sup:A")
    ledger.use(rival, "sup:B")
    assert ledger.spent(idx, "sup:C") and ledger.spent(rival, "sup:B") and not ledger.spent(rival, "sup:C")


@pytest.mark.skipif(not (PACKAGE / "facts.json").is_file(), reason="needs the data package (arena data)")
def test_claims_from_the_package_match_facts_json():
    from arena_kit.data import DataPackage

    pkg = DataPackage(PACKAGE)
    facts = json.loads((PACKAGE / "facts.json").read_text())
    case = {
        "case_id": "case:X", "type": "price_increase", "trigger_doc": "doc:20260911-70110-P01",
        "parts": ["part:310010"], "suppliers": ["sup:70110"], "term_options": {},
        "refs": ["doc:20260911-70110-P01", "ctr:4600070110", "sc:70110@2026Q3"], "buyer": {},
    }  # fmt: skip
    out = {c.key: c for c in cl.candidates(pkg, case, "sup:70110", their_price=138.55)}
    assert out["notice"].figure == facts["doc:20260911-70110-P01"]["days"] == 51
    a, b = out["index:PPI-D351"].ids
    assert out["index:PPI-D351"].figure == round((facts[b]["value"] / facts[a]["value"] - 1) * 100, 1) == -3.7
    assert out["scorecard"].figure == facts["sc:70110@2026Q3"]["otd_pct"]
    assert not out["should_cost"].favourable  # 132.64 leaves no room under 138.55 - citing it would backfire
    assert max(out.values(), key=lambda c: c.strength).key == "notice"


def test_sheets_are_small_medium_large_around_the_target():
    """Lean / recommended / other: the recommended option costs the target; the lean one is it a bit
    cheaper with a term worse for the supplier, so it is even better for us if taken."""
    case = CASES["case:S02"]
    std = {"payment_days": 30, "lead_time_days": 35, "incoterm": "DAP"}
    their = {"unit_price": 20.0, "payment_days": 30, "lead_time_days": 35}
    est = strategy.Estimate(None)
    target = total_cost(case, {**their, "unit_price": 18.5}, std)
    out = strategy.sheets(case, std, est, their, target, ["payment_days"], floor=10)
    assert [label for label, _ in out][:2] == ["lean", "recommended"] and len(out) == 3
    lean, middle = out[0][1], out[1][1]
    assert abs(total_cost(case, middle, std) - target) / target < 0.001
    assert lean["unit_price"] == round(middle["unit_price"] * (1 - strategy.DECOY_GAP), 2)
    assert lean["payment_days"] > middle["payment_days"]  # the term the supplier values, moved our way
    assert total_cost(case, lean, std) < total_cost(case, middle, std)
    assert all(must_haves_ok(case, t, std) and t["unit_price"] < 20.0 for _, t in out)


def test_concessions_shrink():
    steps = [strategy.concession(k + 1) - strategy.concession(k) for k in range(6)]
    assert strategy.concession(0) == 0 and strategy.concession(6) == 1
    assert all(a > b for a, b in zip(steps, steps[1:], strict=False))


def test_breakdown_cost_skips_the_margin():
    text = (
        "[Cost breakdown]\nElement | EUR per unit | Share of price\nMaterial | 50.0 | =B2/B6\n"
        "Conversion | 30.0 | =B3/B6\nOverhead | 15.0 | =B4/B6\nMargin | 5.0 | =B5/B6\nUnit price | =SUM(B2:B5) | 1"
    )
    assert strategy.breakdown_cost(text) == 95.0


def test_lint_drops_leaks_ids_and_invented_figures():
    numbers = writing.allowed("Your letter asks for 9.5 %.")
    dropped = []
    text = (
        "We understand your position on the 9.5% increase. We urgently need the parts. "
        "Our budget is tight. Energy fell 12.4% last year. See idx:PPI-D351@2026-08 for that. "
        "We value our partnership."
    )
    out = writing.lint(text, numbers, dropped)
    assert out == "We understand your position on the 9.5% increase. We value our partnership."
    assert [d["why"] for d in dropped] == ["leak", "leak", "figure 12.4", "data ID"]


def test_the_body_states_exactly_the_sheets_and_never_invents_a_name():
    claim = cl.Claim(
        "notice",
        "notice",
        ["doc:L", "ctr:C"],
        "Your letter gives us only 51 days of notice (doc:L, ctr:C).",
        extra=["Our contract requires 90 days."],
    )
    parts = {
        "salutation": "Dear Mr Huber,",
        "opening": "Thank you for your letter.",
        "claims": ["notice", "nope"],
        "bridge": "Both options keep the price fair for you.",
        "question": "",
        "closing": "Best wishes.",
    }
    labelled = [("their terms", "EUR 10.00 per unit, payment 30 days net")]
    body, picked = writing.assemble(parts, {"notice": claim}, labelled, "Huber GmbH", "no names here", set())
    assert body.startswith("Dear colleagues at Huber GmbH,")
    assert "51 days of notice (doc:L, ctr:C). Our contract requires 90 days." in body
    assert "We propose: EUR 10.00 per unit, payment 30 days net." in body and picked == [claim]


def scripted_writer(calls):
    def complete(system, user):
        calls.append((system, user))
        if "senior procurement analyst" in system:
            return json.dumps(
                {
                    "persona": "a sales rep",
                    "tone": "factual",
                    "levers": ["payment_days", "unit_price"],
                    "cash_short": False,
                    "claim_ranking": [],
                    "questions": ["Any variants?"],
                }
            )
        keys = [
            ln.split(":")[0] + ":" + ln.split(":")[1]
            for ln in user.split("ARGUMENTS")[1].splitlines()[1:3]
            if ln.startswith(("index", "scorecard"))
        ]
        return json.dumps(
            {
                "salutation": "Dear colleagues,",
                "opening": "Thank you for your offer.",
                "claims": keys,
                "bridge": "We think both options are fair.",
                "question": "Any variants?",
                "closing": "Kind regards in advance, and we urgently need it.",
            }
        )

    return complete


@pytest.mark.parametrize("case_id", ["case:S01", "case:S02", "case:S03"])
def test_the_buyer_closes_every_sim_case(sim, case_id):
    url, state, _ = sim
    key = ArenaClient(url).register("Buyer", "sim")["api_key"]
    calls = []
    agent = BuyerAgent(scripted_writer(calls), None)
    table = RemoteTable(ArenaClient(url, key))
    result = play(table, case_id, agent)
    assert result["deal"] and result["score"] > 0.5
    profiles = [u for s, u in calls if "senior procurement analyst" in s]
    assert len(profiles) == len(CASES[case_id]["suppliers"])  # primed before the session opened
    sent = [
        m for th in state.sessions[table.session_id]["threads"].values() for m in th["messages"] if m["from"] == "buyer"
    ]
    assert sent and all("urgently" not in (m.get("body") or "") for m in sent)
    assert all(
        not ts.get("terms") or set(ts["terms"]) == set(CASES[case_id]["negotiable"])
        for m in sent
        for ts in m.get("term_sheets") or []
    )


def test_an_rfq_writes_to_every_thread_at_once(sim):
    url, state, clock = sim
    key = ArenaClient(url).register("Buyer", "sim")["api_key"]
    agent = BuyerAgent(None, None)  # no model: code writes plain e-mails
    table = RemoteTable(ArenaClient(url, key))
    case, threads = table.open("case:S02")
    agent.prime(case)
    decisions = agent.decide_all(case, threads)
    assert {d["supplier_id"] for d in decisions} == set(case["suppliers"])
    assert all(d["message"]["term_sheets"] and "Option" in d["message"]["body"] for d in decisions)


@pytest.mark.skipif(not (PACKAGE / "facts.json").is_file(), reason="needs the data package (arena data)")
def test_p01_the_floor_errs_low_and_a_conceding_supplier_is_not_accepted():
    """Arena run of 10 Oct: Brandlhuber came down 138.55 -> 136.30 in one reply and we accepted; the
    arena charged nothing for its health at 136.30, and 133.70 at 60 days scored 99.7."""
    from arena_kit.data import DataPackage

    pkg = DataPackage(PACKAGE)
    case = {
        "case_id": "case:P01", "type": "price_increase", "title": "Brandlhuber +9.5 %",
        "trigger_doc": "doc:20260911-70110-P01", "parts": ["part:310010"], "suppliers": ["sup:70110"],
        "negotiable": ["unit_price", "effective_date", "payment_days"],
        "fixed_terms": {"lead_time_days": 42, "volume_per_year": 8150, "contract_months": 12,
                        "index_clause": None, "incoterm": "FCA", "tooling_eur": 0},
        "term_options": {"effective_date": ["2026-11-01", "2027-01-01", "2027-04-01"],
                         "payment_days": [14, 30, 45, 60, 90]},
        "buyer": {"need_per_year": 8150, "horizon_years": 1.0, "current_unit_price": 126.53,
                  "fallback_unit_price": 136.65, "interest_rate": 0.08, "reference_payment_days": 60,
                  "need_lead_time_days": 45, "category_goal": "stabilise", "fallback_risk_pct": 10,
                  "index_risk_pct": 4, "late_cost_per_day": 420, "cost_per_defect": 216,
                  "sustainable_margin_pct": 3, "relationship_eur_per_point": 15470,
                  "freight_eur_per_unit": {"EXW": 2.19, "FCA": 1.9, "DAP": 0.0}, "index_paths": {},
                  "must_haves": [], "alternative": {"kind": "quote", "ref": "quote:7200001", "terms": {
                      "unit_price": 143.22, "payment_days": 60, "lead_time_days": 70, "volume_per_year": 8150,
                      "contract_months": 12, "effective_date": "2027-01-01", "index_clause": None,
                      "incoterm": "FCA", "tooling_eur": 1500, "ppm_commitment": 2376}}},
        "round_limit": 10, "refs": ["doc:20260911-70110-P01", "ctr:4600070110", "sc:70110@2026Q3", "quote:7200001"],
    }  # fmt: skip
    quote = {"from": "sales@70110", "status": "open", "subject": "Quote", "body": "Our Angebot.", "attachments": [],
             "term_sheets": [{"term_sheet_id": "offer:70110-0",
                              "terms": {"unit_price": 138.55, "effective_date": "2026-11-01"}}]}  # fmt: skip
    agent = BuyerAgent(None, pkg)
    first = agent.decide_all(case, {"sup:70110": [quote]})[0]["message"]
    assert agent.estimates["sup:70110"].full_cost < 126  # the low end, not the 132.64 point estimate
    assert max(t["terms"]["unit_price"] for t in first["term_sheets"]) < 131
    reply = {**quote, "term_sheets": [{"term_sheet_id": "offer:70110-1",
                                       "terms": {"unit_price": 136.30, "payment_days": 30,
                                                 "effective_date": "2027-01-01"}}]}  # fmt: skip
    second = agent.decide_all(case, {"sup:70110": [quote, {**first, "from": "buyer"}, reply]})[0]["message"]
    assert "action" not in second and second["term_sheets"]  # still moving: we counter, not accept


def _quote(sid, price, sheet_id, status="open", extra=()):
    sheets = [{"term_sheet_id": sheet_id, "terms": {"unit_price": price, "payment_days": 30}}, *extra]
    return {"from": f"sales@{sid[4:]}", "status": status, "subject": "Quote", "body": "Our offer.",
            "attachments": [], "term_sheets": sheets}  # fmt: skip


def _prices(decision):
    return sorted(t["terms"]["unit_price"] for t in decision["message"].get("term_sheets") or [])


def test_a_final_offer_is_read_package_by_package():
    """D01: the final offer held two packages; only the first was looked at."""
    case = CASES["case:S01"]
    cheaper = {"term_sheet_id": "offer:70270-5-a", "terms": {"unit_price": 16.40, "payment_days": 30}}
    final = _quote("sup:70270", 16.90, "offer:70270-5", "final_offer", [cheaper])
    out = BuyerAgent(None, None).decide_all(case, {"sup:70270": [final]})[0]
    assert out["message"]["action"] == {"type": "accept", "term_sheet_id": "offer:70270-5-a"}


def test_no_token_steps_hold_then_a_visible_step():
    """D01: "a nominal adjustment of one cent does not enable us to make further concessions"."""
    case, sid, agent = CASES["case:S01"], "sup:70270", BuyerAgent(None, None)
    events = [_quote(sid, 17.50, "offer:70270-0")]
    sent = []
    for n in range(1, 4):  # the supplier never moves
        d = agent.decide_all(case, {sid: events})[0]
        sent.append(min(_prices(d)))
        events += [{**d["message"], "from": "buyer"}, _quote(sid, 17.50, f"offer:70270-{n}")]
    steps = [b - a for a, b in zip(sent, sent[1:], strict=False)]
    assert steps[0] == 0  # they did not move: we hold
    assert steps[1] >= 0.003 * 17.5  # both held: one visible step, never a cent


def test_rfq_bidders_are_anchored_to_the_best_quote():
    """D02: the dearer bidder (17.10) was anchored to its own quote and agreed to 15.09 at once."""
    case = CASES["case:S02"]
    a, b = case["suppliers"]
    threads = {a: [_quote(a, 17.10, f"offer:{a[4:]}-0")], b: [_quote(b, 16.00, f"offer:{b[4:]}-0")]}
    moves = {d["supplier_id"]: d for d in BuyerAgent(None, None).decide_all(case, threads)}
    assert max(_prices(moves[a])) < 16.00  # asked to beat the best quote, not just its own


def test_a_concession_is_answered_visibly_with_a_smaller_step():
    """D01, 10 Oct: holding after the supplier came down 52.90 -> 51.30 got "no reciprocal movement"."""
    case, sid, agent = CASES["case:S01"], "sup:70270", BuyerAgent(None, None)
    events = [_quote(sid, 17.50, "offer:70270-0")]
    first = agent.decide_all(case, {sid: events})[0]
    events += [{**first["message"], "from": "buyer"}, _quote(sid, 17.00, "offer:70270-1")]  # they give 0.50
    second = agent.decide_all(case, {sid: events})[0]
    ours = min(_prices(second)) - min(_prices(first))
    assert 0.003 * 17.0 <= ours < 0.50  # a visible step, smaller than theirs


def test_an_ask_is_granted_for_a_lower_price_not_for_free():
    """P01, 10 Oct: the supplier quoted payment in 14 days and our 14-day sheet carried the same price
    as a 30-day one - the target was priced on its terms. Now: yes to the ask at a skonto, or our
    reference terms at the target, and no decoy that takes the ask back."""
    from arena_kit.agents import DualAgent

    case, sid = CASES["case:S01"], "sup:70270"
    quote = {**_quote(sid, 17.50, "offer:70270-0"), "term_sheets": [
        {"term_sheet_id": "offer:70270-0", "terms": {"unit_price": 17.50, "payment_days": 14}}]}  # fmt: skip
    agent = DualAgent(None, None)
    agent.prime(case)
    sheets = [t["terms"] for t in agent.decide_all(case, {sid: [quote]})[0]["message"]["term_sheets"]]
    assert [t["payment_days"] for t in sheets] == [14, 60]
    ask, ref = sheets
    assert ask["unit_price"] <= round(ref["unit_price"] * (1 - strategy.SKONTO_PER_30_DAYS * 46 / 30), 2) + 0.01
    assert total_cost(case, ask) < total_cost(case, ref)


def test_an_ask_in_prose_is_read_and_snapped_to_the_options():
    case = CASES["case:S01"]
    their = {"unit_price": 17.50, "payment_days": 60}
    hints = [{"field": "payment_days", "hint": "they need payment within 15 days for liquidity"}]
    assert strategy.supplier_asks(case, their, {}, strategy.Estimate(None), hints) == {"payment_days": 14}
    assert strategy.supplier_asks(case, their, {}, strategy.Estimate(None)) == {}  # 60 days is our reference


def test_the_anchor_comes_from_the_should_cost_and_stays_credible():
    case = {**CASES["case:S01"], "type": "renewal"}
    goal = case["buyer"]["category_goal"]
    est = strategy.Estimate(22.36, source="should-cost, low end", point=24.31)
    fair = round(24.31 / (1 - strategy.FAIR_MARGIN[goal]), 2)
    assert strategy.anchor_price(case, {}, est, 32.20) == fair  # deeper than the goal's fixed cut
    low = strategy.Estimate(9.5, source="should-cost, low end", point=10.0)
    assert strategy.anchor_price(case, {}, low, 32.20) == round(32.20 * (1 - strategy.MAX_ANCHOR_CUT), 2)
    near = strategy.Estimate(30.0, source="should-cost, low end", point=31.5)
    assert strategy.anchor_price(case, {}, near, 32.20) == round(30.0 * 1.01, 2)  # never under its cost
