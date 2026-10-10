"""Our buyer agent (step 2 "negotiate" + step 3 "close").

Per move: code computes the cost C of every offer (with our estimate of the supplier's margin in
relationship cases), ranks the levers, plans 2-3 term sheets on a concession path and enforces the
close rules; the LLM reads the supplier, picks arguments from the data briefing and writes the
e-mail - and may adapt the planned term sheets. If the LLM is slow or broken, the planned move goes
out with a template e-mail, so no move is ever late.

    --agent team.agent:make           (team.evaluate, team.final)
"""

import json
import logging
import math
import os
import re
import time

import httpx

from arena_kit.agents.llm import ID_RE, parse
from arena_kit.cost import RELATIONSHIP_GOALS, complete_offer, cost_breakdown, must_haves_ok, total_cost
from arena_kit.docs import standard_terms
from arena_kit.play import latest, live_threads, moves_left, standing

from .briefing import Briefing
from .levers import _label, lever_table

log = logging.getLogger("arena")

LLM_TIMEOUT_S = float(os.environ.get("TEAM_LLM_TIMEOUT", "28"))
ROUND_BUDGET = int(os.environ.get("TEAM_ROUNDS", "6"))  # aim to close within this many of our messages
SECONDS_PER_ROUND = 75  # our move + the supplier's reply (up to about a minute)
ANCHOR = {"leverage": 0.14, "routine": 0.10, "quality_first": 0.07, "stabilise": 0.06, "secure_supply": 0.05}
RFQ_EXTRA = 0.04  # competition: open lower
MIN_LEVER_SHARE = 0.003  # ask only for levers worth at least 0.3 % of C
COVERAGE = {"contract_months", "volume_per_year"}  # covering the need always counts

SYSTEM = """You are a senior procurement negotiator for Hollerau Pumpenwerke, a German pump maker,
negotiating by e-mail with a supplier's sales team (real people with feelings, bosses and cash needs).

GOAL: the lowest total cost C (EUR) of covering our need - not the lowest unit price. C counts price x
volume, need left uncovered (bought at a worse fallback price), payment days, freight, lead time,
quality and tooling, and in strategic cases also squeezing the supplier below a healthy margin.
Code gives you C for every offer, the value of each lever and PLANNED term sheets. Trust those numbers.

HOW TO NEGOTIATE
- Read the supplier first: its notes, letters, scorecards and replies tell you what it needs (cash,
  volume, certainty, respect). Give it what is cheap for us and valuable for it; take what saves us most.
- Lead with the biggest levers from the LEVERS table, not only the price.
- Arguments: use the ready-made sentences from ARGUMENTS THAT CHECK OUT, copied with their IDs
  (max 3 per e-mail, never an ID we already used). Never invent figures or IDs.
- Offer the PLANNED term sheets. You may re-mix their terms, but code caps every sheet at the planned C
  (a sheet that costs us more gets a lower price), so concede only via the plan. Do not write our offered
  prices in the body - refer to "the attached term sheets". Always ask for something back.
- Be warm, respectful and specific; acknowledge their situation; never threaten, never reveal urgency,
  a budget or that we lack alternatives. Ask questions (cost breakdown, cheaper variants, earliest date).
- Suppliers walk away when pushed too long: move towards a deal every round. A final_offer is accepted
  automatically, so before it comes, make sure the big levers are already in the term sheets.
- Never accept an offer that breaks a must-have or costs more than our alternative.

Reply with ONE JSON object only:
{"action": "send", "subject": "...", "body": "...", "term_sheets": [{"unit_price": 0.0, "...": "..."}],
 "why": "one sentence: what you expect the supplier to do"}
or {"action": "accept"}  (take its standing term sheet)   or {"action": "walk_away"}
Body: a short business e-mail, at most 150 words, signed "Hollerau Pumpenwerke, Purchasing"."""

PLAYBOOK = {
    "price_increase": "A supplier announces a price increase. Contest it with data: cost-driver indices "
    "(the drivers usually justify far less than asked), should-cost, our order prices, a short notice "
    "period vs. the contract. Push the effective date as late as allowed - often worth more than the price. "
    "Settle on a small increase late rather than a big one soon.",
    "renewal": "A framework agreement is renewed. Volume and contract length are our currency: commit the "
    "full need for the full horizon (uncovered need is expensive) in exchange for a lower price and better "
    "payment terms.",
    "rfq": "Several suppliers quote at once (parallel threads). Create competition: tell each supplier that "
    "a competitor is cheaper, citing the rival's offer ID and price, and ask for their best terms. Keep every "
    "must-have (e.g. lead time). Close with the cheapest C once offers stop improving.",
    "bundling": "We bundle several items with one supplier. The bundle's volume and a longer contract buy a "
    "lower price; ask for the bundle discount explicitly.",
    "raw_material": "Raw-material pricing. Compare fixed price vs. index clause with the LEVERS table "
    "(expected index path and our index risk are priced in). Choose what C says, then negotiate base price "
    "and deadband.",
    "capacity": "The supplier is short of capacity. Securing volume and lead time comes first (must-haves, "
    "uncovered need is very expensive); do not squeeze its margin. Offer certainty (longer contract, firm "
    "forecast) in exchange for allocation and a fair price.",
    "quality": "The supplier has quality problems. Quality terms (ppm commitment, chargeback per complaint) "
    "are worth more than a few cents of price; cite its scorecard and complaints, ask for an 8D plan and "
    "binding ppm with chargebacks.",
}
GOAL_NOTE = {
    "stabilise": "Category goal STABILISE: a healthy strategic supplier counts - C charges for squeezing its "
    "margin, so do not push the price below the floor shown; win on dates, payment and volume instead.",
    "secure_supply": "Category goal SECURE SUPPLY: availability first; C charges for squeezing the margin.",
    "quality_first": "Category goal QUALITY FIRST: defects and lateness cost more than price.",
    "leverage": "Category goal LEVERAGE: competition and data should bring the price down - push hard but fair.",
    "routine": "Category goal ROUTINE: keep it efficient; a clean deal on good terms, no drama.",
}
CASH_WORDS = re.compile(r"cash|liquidity|pay(?:ment)? (?:of )?invoices|within \d+ days|early payment|skonto|discount",
                        re.I)


def openai_complete(timeout=LLM_TIMEOUT_S):
    """OpenAI-compatible chat call that must answer in JSON within `timeout` seconds."""
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-6-luna")
    headers = {"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}"}
    client = httpx.Client(timeout=timeout)

    def complete(system, user):
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        r = client.post(f"{base}/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    return complete


_PACKAGE = None


def make():
    """Factory for team.evaluate / team.final: one agent per case, the data package loaded once."""
    global _PACKAGE
    if _PACKAGE is None and (root := os.environ.get("ARENA_DATA")):
        from arena_kit.data import DataPackage

        try:
            _PACKAGE = DataPackage(root)
            _PACKAGE.lookup("")  # build the ID index once, before agents share it
        except FileNotFoundError:
            log.warning("no data package at %s", root)
    return TeamAgent(openai_complete(), _PACKAGE)


class TeamAgent:
    def __init__(self, complete, package=None):
        self.complete, self.package = complete, package
        self.case_id, self.brief, self.supplier_est, self.confirmed = None, None, {}, set()
        self.deadline = None  # set by the runner in the Live Final (time.time() of the wave's cap)
        self.log = []  # (sid, round, source, why) for the evaluation

    # ---- per case ------------------------------------------------------------------------------
    def prepare(self, case):
        self.case_id = case["case_id"]
        try:
            self.brief = Briefing(case, self.package)
        except Exception as e:  # data trouble must not cost the case
            log.warning("briefing failed: %s", e)
            self.brief = Briefing(case, None)
        self.supplier_est, self.confirmed = {}, set()
        for sid in case["suppliers"]:
            text = " ".join(s for s in self.brief.sections if sid in s)
            self.supplier_est[sid] = {"cash_short": bool(CASH_WORDS.search(text))}
        self.should_cost = next((c["value"] for c in self.brief.claims if c["kind"] == "should_cost"), None)

    def supplier(self, case, sid, std):
        """Our estimate of the supplier for the relationship term (strategic goals only)."""
        if case["buyer"].get("category_goal") not in RELATIONSHIP_GOALS or not self.should_cost or not std:
            return None
        rate = 0.14 if self.supplier_est.get(sid, {}).get("cash_short") else 0.08
        b, fixed = case["buyer"], case.get("fixed_terms", {})
        full_std = {  # the quote PDF may leave fields out: fill them as the buyer would assume
            "payment_days": b["reference_payment_days"],
            "incoterm": fixed.get("incoterm") or min(b["freight_eur_per_unit"], key=b["freight_eur_per_unit"].get),
            **{k: v for k, v in std.items() if v is not None},
        }
        return {"standard": full_std, "interest_rate": rate, "usual_rate": 0.08, "full_cost": self.should_cost}

    def cost(self, case, terms, std, sup):
        try:
            return total_cost(case, terms, std, sup)
        except Exception:
            return math.inf

    def c_alt(self, case):
        alt = case["buyer"].get("alternative") or {}
        return total_cost(case, alt["terms"]) if alt.get("terms", {}).get("unit_price") else math.inf

    # ---- the planner -----------------------------------------------------------------------------
    def plan(self, case, events, sid, threads=None):
        """2-3 term sheets on the concession path, the price floor and the C of each."""
        _, theirs = standing(events)
        std = standard_terms(events)
        sup = self.supplier(case, sid, std)
        if theirs.get("unit_price") is None:
            return {"sheets": [], "floor": None, "target": None, "std": std, "sup": sup}
        their_price = theirs["unit_price"]
        goal = case["buyer"].get("category_goal")
        mine = [ts["terms"] for m in events if m.get("from") == "buyer" for ts in (m.get("term_sheets") or [])]
        r = sum(m.get("from") == "buyer" for m in events)  # our messages so far
        budget = self.rounds_budget(case, events)
        # the best value of every non-price lever, from their standing offer
        best, base_c = {}, self.cost(case, theirs, std, sup)
        for row in lever_table(case, theirs, std, sup):
            # small levers are not worth asking for: the supplier prices every demand into the price
            big = -row["delta_eur"] >= MIN_LEVER_SHARE * base_c or row["field"] in COVERAGE
            if row["field"] != "unit_price" and row["must_haves_ok"] and row["delta_eur"] < 0 and big:
                if row["field"] not in best or row["delta_eur"] < best[row["field"]]["delta_eur"]:
                    best[row["field"]] = row
        levers = {f: row["option"] for f, row in best.items()}
        # price floor: below it C rises again (relationship charge), or the should-cost minus a bit
        floor = self.price_floor(case, {**theirs, **levers}, std, sup, their_price)
        # the lowest price anyone has put on the table (all threads), our reference for the anchor
        seen = [ts["terms"]["unit_price"] for ev in (threads or {sid: events}).values() for m in ev
                if m.get("from") != "buyer" for ts in (m.get("term_sheets") or []) if ts["terms"].get("unit_price")]
        reference = min(seen + [their_price])
        if case.get("type") == "price_increase" and case["buyer"].get("current_unit_price"):
            anchor = case["buyer"]["current_unit_price"]
        else:
            cut = ANCHOR.get(goal, 0.08) + (RFQ_EXTRA if len(case["suppliers"]) > 1 else 0)
            anchor = reference * (1 - cut)
            if self.should_cost and goal not in RELATIONSHIP_GOALS:  # a fair margin over its cost
                anchor = min(anchor, self.should_cost * 1.04)
        anchor = max(anchor, floor)
        frac = min(1.0, (r / max(budget, 1)) ** 1.6)  # small steps first, bigger towards the end
        target = anchor + (their_price - anchor) * frac * 0.85
        if mine:  # never step back from a price we offered
            target = max(target, max(t.get("unit_price") or 0 for t in mine))
        target = round(min(max(target, floor), their_price), 2)
        a = complete_offer(case, {**theirs, **levers, "unit_price": target})
        sheets = [a]
        c_a = self.cost(case, a, std, sup)
        # B, C: equal-cost alternatives (MESO). Each gives the supplier one term it asked for, and takes
        # the same value back in price - we do not care which it picks, its pick shows what it values.
        for v in self.menu(case, a, theirs, std, sup, floor, c_a)[:2]:
            if v not in sheets:
                sheets.append(v)
        return {"sheets": sheets[:3], "floor": floor, "target": target, "std": std, "sup": sup, "levers": best,
                "costs": [self.cost(case, s, std, sup) for s in sheets[:3]], "budget": budget, "round": r}

    def menu(self, case, a, theirs, std, sup, floor, c_a):
        """Equal-cost variants of sheet A, the cheapest gives first: [sheet]."""
        neg, options = case["negotiable"], case.get("term_options", {})
        full = complete_offer(case, {**{k: v for k, v in (std or {}).items() if k in neg}, **theirs})
        variants = []
        for f in neg:
            if f != "unit_price" and full.get(f) is not None and full.get(f) != a.get(f):
                variants.append(complete_offer(case, {**a, f: full[f]}))
        if "index_clause" in neg and full.get("index_clause") and not a.get("index_clause"):
            # it wants an index clause: offer its series with the deadband that costs us least
            series = full["index_clause"]["series"]
            same = [o for o in options.get("index_clause", []) if o and o.get("series") == series]
            if same:
                best = min(same, key=lambda o: self.cost(case, {**a, "index_clause": o}, std, sup))
                variants.append(complete_offer(case, {**a, "index_clause": best}))
        if "payment_days" in neg and options.get("payment_days"):  # paying faster: cash for it, cheap for us
            fast = min(options["payment_days"], key=lambda d: abs(d - 30))
            if fast != a.get("payment_days"):
                variants.append(complete_offer(case, {**a, "payment_days": fast}))
        out = []
        for v in variants:
            v = self.iso_price(case, v, c_a, std, sup, floor)
            # a give worth more than the price room (e.g. half the need uncovered) is no offer
            if v and self.cost(case, v, std, sup) <= c_a * 1.002 and must_haves_ok(case, v, std) and v not in out:
                out.append(v)
        return sorted(out, key=lambda v: a["unit_price"] - v["unit_price"])

    def price_floor(self, case, terms, std, sup, their_price):
        """The price below which C does not fall any more (relationship), never below 80 % of theirs."""
        lo = their_price * 0.8
        if sup is None:
            return round(lo, 2)
        best_p, best_c = their_price, math.inf
        for i in range(81):
            p = lo + (their_price - lo) * i / 80
            c = self.cost(case, {**terms, "unit_price": round(p, 2)}, std, sup)
            if c < best_c - 1:
                best_p, best_c = p, c
        return round(best_p, 2)

    def iso_price(self, case, sheet, c_target, std, sup, floor):
        """The same sheet with the price that makes its C equal `c_target` (not below the floor)."""
        p0 = sheet["unit_price"]
        c0, c1 = self.cost(case, sheet, std, sup), self.cost(case, {**sheet, "unit_price": p0 + 1}, std, sup)
        if not math.isfinite(c0) or c1 - c0 <= 0:
            return None
        p = round(max(p0 - (c0 - c_target) / (c1 - c0), floor), 2)
        return complete_offer(case, {**sheet, "unit_price": p})

    def rounds_budget(self, case, events):
        """How many of our messages we plan for: the round limit, our target, the time left."""
        left_msgs = moves_left(case, events) + sum(m.get("from") == "buyer" for m in events)
        budget = min(left_msgs, ROUND_BUDGET)
        if self.deadline:
            spent = sum(m.get("from") == "buyer" for m in events)
            by_time = spent + int(max(0, self.deadline - time.time() - 60) // SECONDS_PER_ROUND)
            budget = min(budget, max(by_time, spent + 1))
        return max(budget, 1)

    # ---- the move --------------------------------------------------------------------------------
    def decide(self, case, threads, sid=None):
        if case["case_id"] != self.case_id:
            self.prepare(case)
        if sid is not None and sid not in live_threads(threads):
            return {"supplier_id": sid, "idle": True}
        live = live_threads(threads)
        for s in live:  # a reply asking for faster payment marks a supplier short of cash
            if any(CASH_WORDS.search(str(m.get("body", ""))) for m in threads[s] if m.get("from") != "buyer"):
                self.supplier_est.setdefault(s, {})["cash_short"] = True
        if sid is None:  # the kit's sequential loop: talk to the supplier whose offer costs least
            sid = min(live, key=lambda s: self.standing_cost(case, threads, s))
        events = threads[sid]
        try:
            plan = self.plan(case, events, sid, threads)
        except Exception as e:  # never let a planning bug cost the case
            log.exception("plan failed: %s", e)
            plan = {"sheets": [], "floor": None, "target": None, "std": standard_terms(events), "sup": None}
        rule = self.close_rule(case, threads, sid, plan)
        if rule:
            self.log.append((sid, plan.get("round"), "rule", rule[1]))
            return rule[0]
        t0 = time.monotonic()
        try:
            reply = self.complete(SYSTEM, self.render(case, threads, sid, plan))
            d = parse(reply)
            move = self.guard(case, threads, sid, plan, d)
            self.log.append((sid, plan.get("round"), "llm", d.get("why", "")))
        except Exception as e:  # slow or broken LLM: the planned move, on time
            log.warning("LLM failed after %.0fs (%s) - planned move", time.monotonic() - t0, e)
            move = None
        return move or self.planned_move(case, events, sid, plan)

    def standing_cost(self, case, threads, sid):
        _, terms = standing(threads[sid])
        std = standard_terms(threads[sid])
        return self.cost(case, terms, std, self.supplier(case, sid, std)) if terms.get("unit_price") else math.inf

    def acceptable(self, case, events, sid):
        sheet_id, terms = standing(events)
        std = standard_terms(events)
        if not sheet_id or terms.get("unit_price") is None or not must_haves_ok(case, terms, std):
            return False
        return total_cost(case, terms, std) <= self.c_alt(case)

    def best_thread(self, case, threads):
        ok = [s for s in live_threads(threads) if self.acceptable(case, threads[s], s)]
        return min(ok, key=lambda s: self.standing_cost(case, threads, s)) if ok else None

    def close_rule(self, case, threads, sid, plan):
        """Hard rules before the LLM: (decision, reason) or None."""
        events = threads[sid]
        status = latest(events).get("status")
        left = moves_left(case, events)
        best = self.best_thread(case, threads)
        accept = lambda s: {"supplier_id": s, "message": {"action": {"type": "accept",  # noqa: E731
                                                                     "term_sheet_id": standing(threads[s])[0]}}}
        if status == "agreed" and self.acceptable(case, events, sid) and best == sid:
            return accept(sid), "agreed to our terms"
        if status == "final_offer" and best:  # its last word: pushing on makes it walk away
            confirm = self.confirm_final(case, events, sid, plan)
            if confirm and best == sid:
                return confirm, "accept its price, confirm our terms"
            return accept(best), "final offer"
        if self.stalled(case, threads, sid) and best == sid:
            earlier = self.better_earlier(case, events, sid, plan)
            if earlier:
                return earlier, "back to its own earlier, cheaper offer"
            return accept(sid), "no more movement"
        spent = plan.get("round") or 0
        if (left <= 0 or spent >= plan.get("budget", ROUND_BUDGET)) and best:
            if best == sid or len(live_threads(threads)) == 1:
                return accept(best), "round budget used"
        if left <= 0 or (spent >= plan.get("budget", ROUND_BUDGET) and best and best != sid):
            if len(live_threads(threads)) > 1:  # RFQ: leave this thread, the others go on
                return {"supplier_id": sid, "idle": True}, "no messages left here"
            if not best:
                return {"supplier_id": sid, "message": {"action": {"type": "walk_away"}}}, "nothing acceptable"
        return None

    def confirm_final(self, case, events, sid, plan):
        """A final offer that leaves fields out closes them on its standard terms. Once per thread:
        take its price and ask it to confirm our terms for the rest - if that saves at least 0.5 %."""
        missing = latest(events).get("missing_fields") or []
        _, theirs = standing(events)
        if sid in self.confirmed or moves_left(case, events) <= 0 or not theirs.get("unit_price"):
            return None
        # fields it left out, and the coverage of our need (an uncovered need is bought at the fallback price)
        wanted = set(missing) | {"contract_months", "volume_per_year"}
        levers = {f: row["option"] for f, row in (plan.get("levers") or {}).items() if f in wanted}
        if not levers:
            return None
        std, sup = plan.get("std"), plan.get("sup")
        sheet = complete_offer(case, {**theirs, **levers})
        sheets = [sheet]
        if "index_clause" in case["negotiable"] and not sheet.get("index_clause"):  # longer only with a clause?
            clauses = [o for o in case.get("term_options", {}).get("index_clause", []) if o]
            if clauses:
                best = min(clauses, key=lambda o: self.cost(case, {**sheet, "index_clause": o}, std, sup))
                sheets.append(complete_offer(case, {**sheet, "index_clause": best}))
        c_now = self.cost(case, theirs, std, sup)
        sheets = [s for s in sheets if self.cost(case, s, std, sup) <= 0.995 * c_now]
        if not sheets:
            return None
        self.confirmed.add(sid)
        body = (f"Thank you - we accept your price of EUR {theirs['unit_price']:.2f}. To complete the agreement and "
                "cover our full requirement, please confirm it on one of the attached term sheets; that is all we "
                "need to sign.\n\nBest regards\nHollerau Pumpenwerke, Purchasing")
        return {"supplier_id": sid, "message": {"subject": f"Re: {case['title']}"[:300], "body": body,
                                                 "term_sheets": [{"terms": s} for s in sheets]}}

    def better_earlier(self, case, events, sid, plan):
        """Once per thread: if one of its earlier offers cost us 0.3 % less than the standing one, offer
        exactly those terms back - it has said yes to them before."""
        if ("earlier", sid) in self.confirmed or moves_left(case, events) <= 0:
            return None
        std, sup = plan.get("std"), plan.get("sup")
        offers = [(ts["term_sheet_id"], ts["terms"]) for m in events if m.get("from") != "buyer"
                  for ts in (m.get("term_sheets") or []) if ts["terms"].get("unit_price") is not None]
        if len(offers) < 2:
            return None
        oid, terms = min(offers[:-1], key=lambda o: self.cost(case, o[1], std, sup))
        if self.cost(case, terms, std, sup) > 0.997 * self.cost(case, offers[-1][1], std, sup):
            return None
        sheet = complete_offer(case, terms)
        if not must_haves_ok(case, sheet, std):
            return None
        self.confirmed.add(("earlier", sid))
        body = (f"Thank you. We have reviewed everything again and are ready to sign on your own earlier offer "
                f"{oid} - the attached term sheet repeats exactly those terms. Can you confirm?\n\n"
                "Best regards\nHollerau Pumpenwerke, Purchasing")
        return {"supplier_id": sid, "message": {"subject": f"Re: {case['title']}"[:300], "body": body,
                                                 "term_sheets": [{"terms": sheet}]}}

    def stalled(self, case, threads, sid):
        """Its offer has not got cheaper for us over its last two replies, after 3+ of our messages."""
        events = threads[sid]
        replies = [m for m in events if m.get("from") != "buyer" and m.get("term_sheets")]
        if sum(m.get("from") == "buyer" for m in events) < 3 or len(replies) < 3:
            return False
        std = standard_terms(events)
        cs = [self.cost(case, m["term_sheets"][0]["terms"], std, None) for m in replies[-3:]]
        return cs[-1] >= cs[0] - max(1.0, 0.001 * cs[0])

    def planned_move(self, case, events, sid, plan):
        if not plan["sheets"]:
            body = ("Thank you for your message. Could you please send us your offer with a unit price and "
                    "your standard terms?\n\nBest regards\nHollerau Pumpenwerke, Purchasing")
        else:
            body = ("Thank you for your offer. We value working with you and have looked at the whole package. "
                    "Please find our proposals attached as term sheets - we are flexible on how we get there "
                    "and would appreciate your best terms.\n\nBest regards\nHollerau Pumpenwerke, Purchasing")
        self.log.append((sid, plan.get("round"), "planned", ""))
        return {"supplier_id": sid, "message": {"subject": f"Re: {case['title']}"[:300], "body": body,
                                                 "term_sheets": [{"terms": s} for s in plan["sheets"]]}}

    # ---- prompt ----------------------------------------------------------------------------------
    def render(self, case, threads, sid, plan):
        b = case["buyer"]
        alt = self.c_alt(case)
        head = {k: case.get(k) for k in ("case_id", "type", "title", "briefing", "negotiable", "fixed_terms",
                                         "term_options", "round_limit")}
        buyer = {k: b.get(k) for k in ("need_per_year", "horizon_years", "current_unit_price", "fallback_unit_price",
                                       "category_goal", "must_haves", "need_lead_time_days", "index_paths")}
        out = [
            "CASE " + json.dumps(head, ensure_ascii=False, default=str),
            "BUYER " + json.dumps(buyer, ensure_ascii=False, default=str),
            f"OUR ALTERNATIVE without a deal: C = {alt:,.0f}" if math.isfinite(alt) else
            "OUR ALTERNATIVE: none - without a deal the need is bought at the fallback price (very expensive).",
            "STRATEGY: " + PLAYBOOK.get(case.get("type"), "") + " " + GOAL_NOTE.get(b.get("category_goal"), ""),
            self.brief.text if self.brief else "",
        ]
        if case.get("trigger_doc") and self.package:
            try:
                out.append("THE LETTER THAT STARTED IT:\n" + self.package.document(case["trigger_doc"])[:1500])
            except (FileNotFoundError, ValueError):
                pass
        used = sorted({i for m in self.all_buyer_messages(threads) for i in ID_RE.findall(m.get("body", ""))})
        if used:
            out.append("IDS WE ALREADY CITED (do not cite again): " + ", ".join(used))
        rivals = [s for s in live_threads(threads) if s != sid]
        for s in rivals:
            rid, rterms = standing(threads[s])
            if rid:
                out.append(f"RIVAL {s}: standing {rid} unit_price {rterms.get('unit_price')} -> C = "
                           f"{self.standing_cost(case, threads, s):,.0f} (cite '{rid}' with its price to create "
                           f"competition)")
        events = threads[sid]
        sheet_id, terms = standing(events)
        c_now = self.standing_cost(case, threads, sid)
        out.append(f"\nTHREAD WITH {sid}: status {latest(events).get('status')}, our messages so far "
                   f"{plan.get('round')}, planned total {plan.get('budget')}, messages left {moves_left(case, events)}")
        out.append(f"THEIR STANDING OFFER {sheet_id}: {json.dumps(terms, default=str)} -> C = "
                   + (f"{c_now:,.0f}" if math.isfinite(c_now) else "-"))
        if terms.get("unit_price") is not None:
            try:
                gap = cost_breakdown(case, terms, plan.get("std"))["uncovered_need"]
            except Exception:
                gap = 0
            if gap > 0:
                out.append(f"WARNING: their offer leaves need uncovered, costing us {gap:,.0f} EUR at the fallback "
                           "price. Covering the full need (contract_months / volume_per_year) comes first - it is "
                           "worth more than any price cut; trade other terms for it.")
        if plan.get("std"):
            out.append("THEIR STANDARD TERMS (apply to anything not in a term sheet): " + json.dumps(plan["std"]))
        if plan.get("levers"):
            out.append("LEVERS from their offer (EUR saved if we get it; per unit in brackets):")
            for f, row in sorted(plan["levers"].items(), key=lambda kv: kv[1]["delta_eur"]):
                out.append(f"- {f}: {_label(row['current'])} -> {_label(row['option'])}: {-row['delta_eur']:,.0f} "
                           f"EUR ({-row['price_equiv']:.2f}/unit)")
        if plan["sheets"]:
            out.append(f"PRICE FLOOR {plan['floor']} (going lower does not lower C). PLANNED TERM SHEETS:")
            for s, c in zip(plan["sheets"], plan["costs"], strict=False):
                out.append(f"- {json.dumps(s, default=str)} -> C = {c:,.0f}")
        out.append("CONVERSATION (latest last):")
        for m in events[-6:]:
            meta = {k: m.get(k) for k in ("status", "term_sheets", "missing_fields", "claim_results", "signals")
                    if m.get(k)}
            out.append(f"--- {m.get('from')}: {json.dumps(meta, default=str)[:900]}\n{str(m.get('body', ''))[:1200]}")
            for att in m.get("attachments") or []:
                if not att.get("name", "").startswith("quote"):
                    out.append(f"[attachment {att.get('name')}]\n{str(att.get('text', ''))[:1500]}")
        out.append(f"\nYOUR TASK: the next move in the thread with {sid}. JSON only.")
        return "\n".join(out)

    @staticmethod
    def all_buyer_messages(threads):
        return [m for ev in threads.values() for m in ev if m.get("from") == "buyer"]

    # ---- guard -----------------------------------------------------------------------------------
    def guard(self, case, threads, sid, plan, d):
        kind = d.get("action")
        events = threads[sid]
        if kind == "accept":
            r = plan.get("round") or 0
            status = latest(events).get("status")
            best = self.best_thread(case, threads)
            # no accepting an open offer early: it can still move
            if best == sid and (status in ("final_offer", "agreed") or r >= 2):
                return {"supplier_id": sid,
                        "message": {"action": {"type": "accept", "term_sheet_id": standing(events)[0]}}}
            return None
        if kind == "walk_away":
            alt = self.c_alt(case)
            if math.isfinite(alt) and not self.best_thread(case, threads) and len(live_threads(threads)) == 1:
                return {"supplier_id": sid, "message": {"action": {"type": "walk_away"}}}
            return None
        if kind != "send" or moves_left(case, events) <= 0:
            return None
        _, theirs = standing(events)
        floor = plan.get("floor") or 0
        sheets = []
        for ts in (d.get("term_sheets") or [])[:3]:
            t = complete_offer(case, {**theirs, **ts.get("terms", ts)})
            if t.get("unit_price") is not None:
                t["unit_price"] = round(max(t["unit_price"], floor), 2)
                if theirs.get("unit_price"):
                    t["unit_price"] = round(min(t["unit_price"], theirs["unit_price"]), 2)
            if plan["sheets"] and t.get("unit_price") is not None:
                # the concession path is ours: a sheet that costs more than the planned one gets a lower price
                c_plan = plan["costs"][0]
                if self.cost(case, t, plan.get("std"), plan.get("sup")) > c_plan * 1.002:
                    t = self.iso_price(case, t, c_plan, plan.get("std"), plan.get("sup"), floor)
            if t and must_haves_ok(case, t, plan.get("std")) and t not in sheets:
                sheets.append(t)
        if plan["sheets"] and plan["sheets"][0] not in sheets and len(sheets) < 3:
            sheets.append(plan["sheets"][0])  # always offer the planned package too
        if not sheets and plan["sheets"]:
            sheets = plan["sheets"]
        offers = {standing(ev)[0] for ev in threads.values()}

        def known(m):  # an ID the data does not have would read as a lie
            ref = m.group(0)
            if ref in offers or (self.brief and ref in self.brief.facts):
                return ref
            log.warning("dropped unknown data ID %s", ref)
            return ""

        body = re.sub(r"\s*\(\s*[,;\s]*\)", "", ID_RE.sub(known, str(d.get("body", "")))).strip()[:8000]
        if not body:
            return None
        return {"supplier_id": sid, "message": {"subject": str(d.get("subject") or f"Re: {case['title']}")[:300],
                                                 "body": body, "term_sheets": [{"terms": t} for t in sheets]}}
