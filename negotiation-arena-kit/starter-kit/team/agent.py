"""Our buyer agent (step 2 "negotiate" + step 3 "close").

Per move: code computes the cost C of every offer (with our estimate of the supplier's margin in
relationship cases), ranks the levers, plans 2-3 term sheets on a concession path and enforces the
close rules; the LLM reads the supplier, picks arguments from the data briefing and writes the
e-mail - and may adapt the planned term sheets. If the LLM is slow or broken, the planned move goes
out with a template e-mail, so no move is ever late.

    --agent team.agent:make           (team.evaluate, team.final)
"""

import base64
import json
import logging
import math
import os
import re
import time

import httpx

from arena_kit.agents.llm import ID_RE, parse
from arena_kit.cli import load_env
from arena_kit.cost import RELATIONSHIP_GOALS, complete_offer, must_haves_ok, total_cost
from arena_kit.docs import standard_terms
from arena_kit.play import latest, live_threads, moves_left, standing

from .briefing import Briefing
from .levers import _label, lever_table, revealed_slopes

log = logging.getLogger("arena")

load_env()  # .env before the settings below are read (real environment variables win)
LLM_TIMEOUT_S = float(os.environ.get("TEAM_LLM_TIMEOUT", "28"))
ROUND_BUDGET = int(os.environ.get("TEAM_ROUNDS", "6"))  # aim to close within this many of our messages
SECONDS_PER_ROUND = 75  # our move + the supplier's reply (up to about a minute)
ANCHOR = {"leverage": 0.14, "routine": 0.10, "quality_first": 0.07, "stabilise": 0.06, "secure_supply": 0.05}
RFQ_EXTRA = 0.04  # competition: open lower
MIN_LEVER_SHARE = 0.003  # ask only for levers worth at least 0.3 % of C
COVERAGE = {"contract_months", "volume_per_year"}  # covering the need always counts
STEPS = (0.0, 0.15, 0.3, 0.45, 0.6)  # share of the C gap we give by our 1st, 2nd, ... message (steps of 15 %+
MIN_STEP = 0.15  # of the gap left: zero steps drew a final offer 37 % of the time, steps >= 15 % of the gap 17 %)

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
ASK_DATA = re.compile(r"send (?:us )?the data|which figures|share the (?:detailed )?(?:forecast )?data|provide the "
                      r"(?:forecast|data|figures)|verify (?:this|it)", re.I)
# a supplier short of cash, from its own words only (never our briefing labels or echoes of our e-mails)
CASH = re.compile(r"\bcash\b|liquidit|pay(?:ment)?\s+(?:of\s+)?(?:our\s+)?invoices\s+(?:with)?in\s+\d+\s+days|"
                  r"faster payment|early payment|overdue|restructur|insolven", re.I)
FULL_COST_K = 1.02  # its full cost = should-cost / 1.02: fits the arena's relationship charge at 31 recorded deals


def openai_complete(timeout=LLM_TIMEOUT_S):
    """OpenAI-compatible chat call that must answer in JSON within `timeout` seconds."""
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-6-luna")
    headers = {"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}"}
    client = httpx.Client(timeout=timeout)

    def complete(system, user, timeout=timeout):
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        r = client.post(f"{base}/chat/completions", json=body, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    return complete


_PACKAGE = None


def numeric(v):
    """An LLM value as the arena wants it: '45' -> 45, '5.10' -> 5.1, a date with a time -> the date."""
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", s):
            return s[:10]
    return v


def same(a, b):
    """Two term sheets equal up to a cent."""
    if a.keys() != b.keys():
        return False
    return all(abs(a[k] - b[k]) <= 0.005 if isinstance(a[k], float) else a[k] == b[k] for k in a)


def distance(a, b):
    """How far an option is from a value: numbers by difference, anything else 0 if equal else 1."""
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return abs(a - b)
    return 0 if a == b else 1


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
        self.case_id, self.brief, self.supplier_est, self.confirmed, self.slopes = None, None, {}, set(), {}
        self.anchor = {}  # sid -> our fixed anchor price
        self.reply_at, self.inflight = {}, {}  # set by the runner: when each supplier last replied, who is busy
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
        self.supplier_est, self.confirmed, self.slopes, self.anchor = {}, set(), {}, {}
        for sid in case["suppliers"]:
            text = getattr(self.brief, "supplier_text", {}).get(sid, "")
            self.supplier_est[sid] = {"cash_short": bool(CASH.search(text))}
        self.should_cost = next((c["value"] for c in self.brief.claims if c["kind"] == "should_cost"), None)

    def supplier(self, case, sid, std):
        """Our estimate of the supplier for the relationship term (strategic goals only)."""
        if case["buyer"].get("category_goal") not in RELATIONSHIP_GOALS or not self.should_cost or not std:
            return None
        rate = 0.10 if self.supplier_est.get(sid, {}).get("cash_short") else 0.08
        if (slope := self.slopes.get(sid, {}).get("payment_days")) and slope > 0:  # its revealed cost of money
            rate = min(max(slope * 360, 0.03), 0.40)
        b, fixed = case["buyer"], case.get("fixed_terms", {})
        full_std = {  # the quote PDF may leave fields out: fill them as the buyer would assume
            "payment_days": b["reference_payment_days"],
            "incoterm": fixed.get("incoterm") or min(b["freight_eur_per_unit"], key=b["freight_eur_per_unit"].get),
            **{k: v for k, v in std.items() if v is not None},
        }
        return {"standard": full_std, "interest_rate": rate, "usual_rate": 0.08,
                "full_cost": self.should_cost / FULL_COST_K}

    @staticmethod
    def std(events):
        """Its standard terms from the quote PDF; a PDF we cannot parse gives {} instead of an exception."""
        try:
            return standard_terms(events)
        except Exception as e:
            log.warning("standard terms unreadable: %s", e)
            return {}

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
        std = self.std(events)
        # what it charges per term, from its own sheet pairs (e.g. main vs alternative): our lever prices
        slopes = revealed_slopes(case, [m for m in events if m.get("from") != "buyer"])
        self.slopes[sid] = slopes
        sup = self.supplier(case, sid, std)
        _, theirs = self.best_sheet(case, events, sid)  # plan from the cheapest sheet it has on the table
        if theirs.get("unit_price") is None:
            return {"sheets": [], "floor": None, "std": std, "sup": sup}
        their_price = theirs["unit_price"]
        goal = case["buyer"].get("category_goal")
        delivered = [m for m in events if m.get("from") == "buyer" and not m.get("undelivered")]
        r = len(delivered)  # our messages that reached it
        budget = self.rounds_budget(case, events)
        # the best value of every non-price lever, first at their price (for floor and anchor)
        base = self.keep_must_haves(case, complete_offer(case, theirs), std)  # levers from a valid sheet
        best = self.best_levers(case, base, std, sup, slopes)
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
        # the anchor is fixed per thread (a moving anchor froze our steps); in an RFQ it may only fall
        anchor = min(anchor, self.anchor[sid]) if sid in self.anchor and len(case["suppliers"]) > 1 \
            else self.anchor.get(sid, anchor)
        self.anchor[sid] = anchor
        anchor = round(max(anchor, floor), 2)
        # at OUR price: whether covering the whole need pays depends on the price we get for it
        best = self.best_levers(case, {**base, "unit_price": anchor}, std, sup, slopes)
        levers = {f: row["option"] for f, row in best.items()}
        struct = self.keep_must_haves(case, complete_offer(case, {**base, **levers, "unit_price": anchor}), std)
        # real steps in C along STEPS of the gap between our anchor and its cheapest sheet, and at least
        # MIN_STEP of what was left after our last message - suppliers mirror steps, not words
        c_anchor, c_their = self.cost(case, struct, std, sup), self.cost(case, theirs, std, sup)
        target_c = c_anchor + (c_their - c_anchor) * STEPS[min(r, len(STEPS) - 1)]
        if delivered and (last := (delivered[-1].get("term_sheets") or [{}])[0].get("terms")):
            c_last = self.cost(case, complete_offer(case, last), std, sup)
            if math.isfinite(c_last):
                target_c = max(target_c, c_last + MIN_STEP * (c_their - c_last))
        target_c = min(target_c, 0.997 * c_their)
        a = self.iso_price(case, struct, target_c, std, sup, floor) or struct
        a = {**a, "unit_price": round(min(max(a["unit_price"], floor), their_price), 2)}
        sheets = [a]
        c_a = self.cost(case, a, std, sup)
        # B, C: equal-cost alternatives (MESO). Each gives the supplier one term it asked for, and takes
        # the same value back in price - we do not care which it picks, its pick shows what it values.
        for v in self.menu(case, a, theirs, std, sup, floor, c_a, slopes)[:2]:
            if v not in sheets:
                sheets.append(v)
        # coverage lock: once sheet A covers the need, no sheet we send covers less
        cover = {f: a[f] for f in COVERAGE if f in case["negotiable"] and a.get(f) is not None}
        last_sheets = (delivered[-1].get("term_sheets") or []) if delivered else []
        sent = [complete_offer(case, ts["terms"]) for ts in last_sheets]
        repeat = bool(sent) and all(any(same(s, t) for t in sent) for s in sheets)
        return {"repeat": repeat, "sheets": sheets, "floor": floor, "std": std, "sup": sup,
                "levers": best, "costs": [self.cost(case, s, std, sup) for s in sheets], "budget": budget,
                "round": r, "cover": cover, "their_price": their_price}

    def best_levers(self, case, terms, std, sup, slopes=None):
        """{field: lever row} - the best option per non-price field that saves at least MIN_LEVER_SHARE of C
        (small levers are not worth asking for: the supplier prices every demand into the price). With the
        supplier's revealed prices a lever counts only if it saves more than it charges; on a tie (within
        1 EUR) the option closest to the supplier's own value wins - no stricter ask than needed."""
        rows, base_c = {}, self.cost(case, terms, std, sup)
        for row in lever_table(case, terms, std, sup, slopes):
            big = -row["delta_eur"] >= MIN_LEVER_SHARE * base_c or row["field"] in COVERAGE
            if row["field"] != "unit_price" and row["must_haves_ok"] and row["delta_eur"] < 0 and big:
                rows.setdefault(row["field"], []).append(row)
        best = {}
        for f, rs in rows.items():
            top = min(r["delta_eur"] for r in rs)
            ties = [r for r in rs if r["delta_eur"] - top < 1]
            best[f] = min(ties, key=lambda r: distance(r["option"], r["current"]))
        return best

    @staticmethod
    def keep_must_haves(case, sheet, std):
        """Move every field that breaks a must-have to the allowed option closest to its value that keeps it."""
        for m in case["buyer"].get("must_haves", []):
            if not must_haves_ok(case, sheet, std):
                ok = [o for o in case.get("term_options", {}).get(m["field"], [])
                      if must_haves_ok(case, {**sheet, m["field"]: o}, std)]
                if ok:
                    sheet = {**sheet, m["field"]: min(ok, key=lambda o: distance(o, sheet.get(m["field"])))}
        return sheet

    def menu(self, case, a, theirs, std, sup, floor, c_a, slopes=None):
        """Equal-cost variants of sheet A, the cheapest gives first: [sheet]."""
        neg, options = case["negotiable"], case.get("term_options", {})
        full = complete_offer(case, {**{k: v for k, v in (std or {}).items() if k in neg}, **theirs})
        variants = []
        for f in neg:
            if f not in COVERAGE and f != "unit_price" and full.get(f) is not None and full.get(f) != a.get(f):
                variants.append(complete_offer(case, {**a, f: full[f]}))  # never give back coverage
        if "index_clause" in neg and full.get("index_clause") and not a.get("index_clause"):
            # it wants an index clause: offer its series with the deadband that costs us least
            series = full["index_clause"]["series"]
            same = [o for o in options.get("index_clause", []) if o and o.get("series") == series]
            if same:
                best = min(same, key=lambda o: self.cost(case, {**a, "index_clause": o}, std, sup))
                variants.append(complete_offer(case, {**a, "index_clause": best}))
        if "payment_days" in neg and options.get("payment_days"):  # paying faster: cash for it, cheap for us
            pay = options["payment_days"]
            if "payment_days" in (slopes or {}):
                other = min(pay, key=lambda d: abs(d - 30))
            elif (a.get("payment_days") or 0) > case["buyer"]["reference_payment_days"]:  # probe its rate
                other = min(pay, key=lambda d: abs(d - (full.get("payment_days") or 30)))
            else:
                other = max(pay)
            if other != a.get("payment_days"):
                variants.append(complete_offer(case, {**a, "payment_days": other}))
        out = []
        for v in variants:
            # each structure has its own floor: faster payment is worth more to it, so it can go lower
            floor_v = min(floor, self.price_floor(case, v, std, sup, theirs.get("unit_price") or v["unit_price"]))
            v = self.iso_price(case, v, c_a, std, sup, floor_v)
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
        """The same sheet with the price that makes its C equal `c_target`, by bisection between the floor and
        1.25 x its price (C is not linear in the price with a relationship charge); None if there is no such price."""
        def c(p):
            return self.cost(case, {**sheet, "unit_price": round(p, 2)}, std, sup)

        lo, hi = floor, sheet["unit_price"] * 1.25
        if not math.isfinite(c(lo)) or c(lo) > c_target * 1.001 or c(hi) < c_target:
            return None
        for _ in range(40):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if c(mid) <= c_target else (lo, mid)
        return complete_offer(case, {**sheet, "unit_price": round(lo, 2)})

    def rounds_budget(self, case, events):
        """How many of our messages we plan for: the round limit, our target, the time left."""
        spent = sum(m.get("from") == "buyer" for m in events)
        left_msgs = moves_left(case, events) + spent
        budget = min(left_msgs, ROUND_BUDGET)
        if self.deadline:
            by_time = spent + int(max(0, self.deadline - time.time() - 60) // SECONDS_PER_ROUND)
            budget = min(budget, max(by_time, spent + 1))
        return max(budget, 1)

    # ---- the move --------------------------------------------------------------------------------
    def decide(self, case, threads, sid=None):
        if case["case_id"] != self.case_id:
            self.prepare(case)
        live = live_threads(threads)
        if sid is not None and sid not in live:
            return {"supplier_id": sid, "idle": True}
        for s in live:  # a reply asking for faster payment marks a supplier short of cash - unless it echoes us
            mine = ""
            for m in threads[s]:
                if m.get("from") == "buyer":
                    mine = str(m.get("body", "")).lower()
                elif (hit := CASH.search(str(m.get("body", "")))) and hit.group(0).lower() not in mine:
                    self.supplier_est.setdefault(s, {})["cash_short"] = True
        if sid is None:  # the kit's sequential loop: talk to the supplier whose offer costs least
            sid = min(live, key=lambda s: self.standing_cost(case, threads, s))
        events = threads[sid]
        try:
            plan = self.plan(case, events, sid, threads)
        except Exception as e:  # never let a planning bug cost the case
            log.exception("plan failed: %s", e)
            plan = {"sheets": [], "floor": None, "std": self.std(events), "sup": None}
        rule = self.close_rule(case, threads, sid, plan)
        if rule:
            self.log.append((sid, plan.get("round"), "rule", rule[1]))
            return rule[0]
        rfq = len(case["suppliers"]) > 1
        if rfq and not plan.get("round"):  # RFQ message 1: no LLM (three first messages would miss the clock)
            return self.planned_move(case, events, sid, plan, threads, extra=self.rival_line(case, threads, sid))
        # the LLM gets what is left of the 60 s since its reply, minus room for calls queued on the session
        since = time.monotonic() - self.reply_at.get(sid, time.monotonic())
        budget = 50 - since - 15 * sum(1 for s, busy in self.inflight.items() if busy and s != sid)
        if budget < 10:
            log.warning("%s: %.0fs left for the LLM - planned move", sid, budget)
            return self.planned_move(case, events, sid, plan, threads)
        t0 = time.monotonic()
        try:
            reply = self.complete(SYSTEM, self.render(case, threads, sid, plan), timeout=min(LLM_TIMEOUT_S, budget))
            d = parse(reply)
            move = self.guard(case, threads, sid, plan, d)
            self.log.append((sid, plan.get("round"), "llm", d.get("why", "")))
        except Exception as e:  # slow or broken LLM: the planned move, on time
            log.warning("LLM failed after %.0fs (%s) - planned move", time.monotonic() - t0, e)
            move = None
        return move or self.planned_move(case, events, sid, plan, threads)

    # ---- every sheet the supplier has offered, and the accept ladder -------------------------------
    def thread_sheets(self, case, events, sid):
        """Every sheet the supplier has put on the table in this thread - main offers and alternatives, latest
        message last: [{id, terms, latest, main, c, valid}]. valid = it keeps the must-haves and beats the
        alternative; c is our C with its standard terms (and our estimate of its margin)."""
        std = self.std(events)
        sup = self.supplier(case, sid, std)
        c_alt = self.c_alt(case)
        replies = [m for m in events if m.get("from") != "buyer"]
        out = []
        for i, m in enumerate(replies):
            for j, ts in enumerate(m.get("term_sheets") or []):
                terms = ts.get("terms") or {}
                if not ts.get("term_sheet_id") or terms.get("unit_price") is None:
                    continue
                c = self.cost(case, terms, std, sup)
                valid = must_haves_ok(case, terms, std) and self.cost(case, terms, std, None) <= c_alt
                out.append({"id": ts["term_sheet_id"], "terms": terms, "latest": i == len(replies) - 1,
                            "main": j == 0, "c": c, "valid": valid})
        return out

    def accept_candidates(self, case, threads):
        """The accept ladder over all live threads: valid sheets, cheapest first; a few earlier or alternative
        IDs (the arena may refuse those - a refused accept changes nothing), then every latest valid sheet."""
        rows = sorted((r["c"], not r["latest"], not r["main"], sid, r["id"]) for sid in live_threads(threads)
                      for r in self.thread_sheets(case, threads[sid], sid) if r["valid"])
        now = [x for x in rows if not x[1]]
        if not now:  # an earlier ID alone is no safe accept
            return []
        earlier = [x for x in rows if x[1] and x[0] < now[0][0] * 0.999][:3]
        return list(dict.fromkeys((x[3], x[4]) for x in sorted(earlier + now)))

    def accept(self, case, threads):
        """An accept with the whole ladder (the runner tries the IDs in order), or None."""
        ladder = self.accept_candidates(case, threads)
        if not ladder:
            return None
        sid, oid = ladder[0]
        return {"supplier_id": sid, "message": {"action": {"type": "accept", "term_sheet_id": oid}},
                "candidates": ladder}

    def best_sheet(self, case, events, sid=None):
        """(term_sheet_id, terms) of the cheapest valid sheet in the supplier's latest message (main offer or
        alternative), else its main offer."""
        rows = [r for r in self.thread_sheets(case, events, sid) if r["latest"]]
        if not rows:
            return standing(events)
        r = min(rows, key=lambda r: (not r["valid"], r["c"]))
        return r["id"], r["terms"]

    def standing_cost(self, case, threads, sid):
        rows = [r["c"] for r in self.thread_sheets(case, threads[sid], sid) if r["latest"]]
        return min(rows) if rows else math.inf

    def best_thread(self, case, threads):
        ladder = self.accept_candidates(case, threads)
        return ladder[0][0] if ladder else None

    def close_rule(self, case, threads, sid, plan):
        """Hard rules before the LLM: (decision, reason) or None."""
        events = threads[sid]
        status = latest(events).get("status")
        left = moves_left(case, events)
        accept = self.accept(case, threads)
        best = accept["supplier_id"] if accept else None
        if status == "agreed" and accept:
            # 'agreed' only echoes our last sheet: if it offered something cheaper before, ask for that once
            earlier = self.better_earlier(case, events, sid, plan)
            if earlier:
                return earlier, "its earlier offer was cheaper than the agreed sheet"
            return accept, "agreed to our terms"
        if plan.get("repeat") and accept and left > 0:  # we would only repeat ourselves: no more room
            return accept, "no more room"
        if status == "final_offer":  # its last word: any further message makes it walk away (8/8 recorded)
            if accept:
                return accept, "final offer"
            repair = self.confirm_final(case, events, sid, plan)  # nothing acceptable anywhere: one repair try
            if repair:
                return repair, "final offer breaks a must-have - one repair message"
        if self.stalled(case, threads, sid) and best == sid:
            earlier = self.better_earlier(case, events, sid, plan)
            if earlier:
                return earlier, "back to its own earlier, cheaper offer"
            return accept, "no more movement"
        if self.hopeless(case, threads, sid):  # RFQ with 3+: stop feeding a far dearer supplier that does not move
            return {"supplier_id": sid, "idle": True}, "far dearer than the best rival and not moving"
        spent = plan.get("round") or 0
        if (left <= 0 or spent >= plan.get("budget", ROUND_BUDGET)) and accept:
            if best == sid or len(live_threads(threads)) == 1:
                return accept, "round budget used"
        if left <= 0 or (spent >= plan.get("budget", ROUND_BUDGET) and best and best != sid):
            if len(live_threads(threads)) > 1:  # RFQ: leave this thread, the others go on
                return {"supplier_id": sid, "idle": True}, "no messages left here"
            if not accept:
                return {"supplier_id": sid, "message": {"action": {"type": "walk_away"}}}, "nothing acceptable"
        return None

    def hopeless(self, case, threads, sid):
        """3+ live threads, 2+ replies here, its best sheet > 8 % dearer than the best anywhere, last step < 1 %."""
        live = live_threads(threads)
        if len(live) < 3:
            return False
        valid = {s: [r["c"] for r in self.thread_sheets(case, threads[s], s) if r["valid"]] for s in live}
        if not valid.get(sid) or not any(valid.values()):
            return False
        best_all = min(min(v) for v in valid.values() if v)
        std = self.std(threads[sid])
        replies = [m for m in threads[sid] if m.get("from") != "buyer" and m.get("term_sheets")]
        if len(replies) < 3 or min(valid[sid]) <= 1.08 * best_all:
            return False
        cs = [min(self.cost(case, ts.get("terms") or {}, std, None) for ts in m["term_sheets"]) for m in replies[-2:]]
        return cs[0] - cs[1] < 0.01 * cs[0]

    def confirm_final(self, case, events, sid, plan):
        """Only when nothing on the table is acceptable (its final offer breaks a must-have or costs more than
        our alternative): one message at its price with the nearest must-have-keeping option and full coverage."""
        _, theirs = standing(events)
        if sid in self.confirmed or moves_left(case, events) <= 0 or not theirs.get("unit_price"):
            return None
        std = plan.get("std")
        must = {m["field"] for m in case["buyer"].get("must_haves", [])}
        levers = {f: row["option"] for f, row in (plan.get("levers") or {}).items() if f in COVERAGE | must}
        sheet = self.keep_must_haves(case, complete_offer(case, {**theirs, **levers}), std)
        if not must_haves_ok(case, sheet, std):
            return None
        self.confirmed.add(sid)
        body = (f"Thank you - we accept your price of EUR {theirs['unit_price']:.2f}. To complete the agreement and "
                "cover our full requirement, please confirm it on the attached term sheet; that is all we need to "
                "sign.\n\nBest regards\nHollerau Pumpenwerke, Purchasing")
        return {"supplier_id": sid, "message": {"subject": f"Re: {case['title']}"[:300], "body": body,
                                                 "term_sheets": [{"terms": sheet}]}}

    def better_earlier(self, case, events, sid, plan):
        """Once per thread: if one of its earlier sheets (main or alternative) costs us 0.3 % less than the best
        sheet of its latest message, offer exactly those terms back - it has offered them before."""
        if ("earlier", sid) in self.confirmed or moves_left(case, events) <= 0:
            return None
        rows = self.thread_sheets(case, events, sid)
        now = [r["c"] for r in rows if r["latest"] and r["valid"]] or [r["c"] for r in rows if r["latest"]]
        before = [r for r in rows if not r["latest"] and r["valid"]]
        if not now or not before:
            return None
        r = min(before, key=lambda r: r["c"])
        if r["c"] > 0.997 * min(now):
            return None
        sheet = complete_offer(case, r["terms"])
        if not must_haves_ok(case, sheet, plan.get("std")):
            return None
        self.confirmed.add(("earlier", sid))
        body = (f"Thank you. We have reviewed everything again and are ready to sign on your own earlier offer "
                f"{r['id']} - the attached term sheet repeats exactly those terms. Can you confirm?\n\n"
                "Best regards\nHollerau Pumpenwerke, Purchasing")
        return {"supplier_id": sid, "message": {"subject": f"Re: {case['title']}"[:300], "body": body,
                                                 "term_sheets": [{"terms": sheet}]}}

    def stalled(self, case, threads, sid):
        """Its offers have not got cheaper for us over its last two replies, after 3+ of our messages."""
        events = threads[sid]
        replies = [m for m in events if m.get("from") != "buyer" and m.get("term_sheets")]
        if sum(m.get("from") == "buyer" for m in events) < 3 or len(replies) < 3:
            return False
        std = self.std(events)
        cs = [min(self.cost(case, ts.get("terms") or {}, std, None) for ts in m["term_sheets"]) for m in replies[-3:]]
        return cs[-1] >= cs[0] - max(1.0, 0.001 * cs[0])

    def no_regress(self, case, threads, sid, plan, sheets):
        """Never offer a sheet that costs us as much as one the supplier already offered: (sheets, fallback).
        If nothing is left, fall back to its earlier offer (once) or the accept ladder."""
        valid = [r["c"] for r in self.thread_sheets(case, threads[sid], sid) if r["valid"]]
        if not valid:
            return sheets, None
        best_c = min(valid)
        keep = [s for s in sheets if self.cost(case, s, plan.get("std"), plan.get("sup")) < 0.999 * best_c]
        if keep:
            return keep, None
        return [], self.better_earlier(case, threads[sid], sid, plan) or self.accept(case, threads)

    def fallback(self, case, threads, sid):
        """A move without the LLM: the close rules, else the planned sheets, else the accept ladder."""
        events = threads[sid]
        plan = self.plan(case, events, sid, threads)
        rule = self.close_rule(case, threads, sid, plan)
        if rule:
            return rule[0]
        if not plan["sheets"] and (accept := self.accept(case, threads)):
            return accept
        return self.planned_move(case, events, sid, plan, threads)

    def rival_line(self, case, threads, sid):
        """RFQ: the cheapest valid rival sheet below this supplier's price, as a checkable claim."""
        mine = self.standing_cost(case, threads, sid)
        rivals = [(r["c"], r["terms"]["unit_price"], r["id"]) for s in live_threads(threads) if s != sid
                  for r in self.thread_sheets(case, threads[s], s) if r["latest"] and r["valid"]]
        if not rivals or min(rivals)[0] >= mine:
            return ""
        _, price, oid = min(rivals)
        return f"A competing supplier has offered EUR {price:.2f} per unit ({oid}). "

    def planned_move(self, case, events, sid, plan, threads=None, extra=""):
        sheets, instead = self.no_regress(case, threads or {sid: events}, sid, plan, plan["sheets"])
        if plan["sheets"] and not sheets and instead:
            self.log.append((sid, plan.get("round"), "planned", "nothing cheaper to offer than its own sheets"))
            return instead
        plan = {**plan, "sheets": sheets}
        if not plan["sheets"]:
            body = ("Thank you for your message. Could you please send us your offer with a unit price and "
                    "your standard terms?\n\nBest regards\nHollerau Pumpenwerke, Purchasing")
        else:
            body = ("Thank you for your offer. We value working with you and have looked at the whole package. "
                    f"{extra}Please find our proposals attached as term sheets - we are flexible on how we get there "
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
            rid, rterms = self.best_sheet(case, threads[s], s)
            if rid:
                out.append(f"RIVAL {s}: standing {rid} unit_price {rterms.get('unit_price')} -> C = "
                           f"{self.standing_cost(case, threads, s):,.0f} (cite '{rid}' with its price to create "
                           f"competition)")
        events = threads[sid]
        sheet_id, terms = self.best_sheet(case, events, sid)
        c_now = self.standing_cost(case, threads, sid)
        out.append(f"\nTHREAD WITH {sid}: status {latest(events).get('status')}, our messages so far "
                   f"{plan.get('round')}, planned total {plan.get('budget')}, messages left {moves_left(case, events)}")
        out.append(f"THEIR CHEAPEST SHEET ON THE TABLE {sheet_id}: {json.dumps(terms, default=str)} -> C = "
                   + (f"{c_now:,.0f}" if math.isfinite(c_now) else "-"))
        others = [ts for ts in (latest(events).get("term_sheets") or []) if ts.get("term_sheet_id") != sheet_id]
        for ts in others:  # its other sheets show what it can give
            c = self.cost(case, ts.get("terms") or {}, plan.get("std"), plan.get("sup"))
            terms_txt = json.dumps(ts.get("terms"), default=str)
            out.append(f"  its other sheet {ts.get('term_sheet_id')}: {terms_txt} -> C = "
                       + (f"{c:,.0f}" if math.isfinite(c) else "-"))
        cover = {f: row for f, row in (plan.get("levers") or {}).items() if f in COVERAGE}
        if cover and terms.get("unit_price") is not None:  # only when covering the need lowers C
            saving = max(-row["delta_eur"] for row in cover.values())
            out.append(f"WARNING: their offer leaves need uncovered; covering it ({', '.join(cover)}) saves us "
                       f"{saving:,.0f} EUR - get it into the deal, trade other terms for it.")
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
                return self.accept(case, threads)
            return None
        if kind == "walk_away":
            alt = self.c_alt(case)
            if math.isfinite(alt) and not self.best_thread(case, threads) and len(live_threads(threads)) == 1:
                return {"supplier_id": sid, "message": {"action": {"type": "walk_away"}}}
            return None
        if kind != "send" or moves_left(case, events) <= 0:
            return None
        _, theirs = self.best_sheet(case, events, sid)
        their_price = plan.get("their_price") or theirs.get("unit_price")
        floor = plan.get("floor") or 0
        base = plan["sheets"][0] if plan["sheets"] else theirs  # what the LLM leaves out comes from OUR plan
        sheets = []
        for ts in (d.get("term_sheets") or [])[:3]:
            raw = ts.get("terms", ts) if isinstance(ts, dict) else {}
            t = complete_offer(case, {**base, **{k: numeric(v) for k, v in raw.items() if k in case["negotiable"]}})
            for f, v in (plan.get("cover") or {}).items():  # coverage lock: never cover less than sheet A
                if (t.get(f) or 0) < v:
                    t[f] = v
            if t.get("unit_price") is not None:
                t["unit_price"] = round(max(t["unit_price"], floor), 2)
                if their_price:
                    t["unit_price"] = round(min(t["unit_price"], their_price), 2)
            if plan["sheets"] and t.get("unit_price") is not None:
                # the concession path is ours: a sheet that costs more than the planned one gets a lower price
                c_plan = plan["costs"][0]
                if self.cost(case, t, plan.get("std"), plan.get("sup")) > c_plan * 1.002:
                    t = self.iso_price(case, t, c_plan, plan.get("std"), plan.get("sup"), floor)
            if t and must_haves_ok(case, t, plan.get("std")) and t not in sheets:
                sheets.append(t)
        if plan["sheets"]:  # equal-cost menus only: the supplier picks the sheet that is dearest for us
            c_plan = plan["costs"][0]
            sheets = [t for t in sheets
                      if 0.998 * c_plan <= self.cost(case, t, plan.get("std"), plan.get("sup")) <= 1.002 * c_plan]
            if plan["sheets"][0] not in sheets and len(sheets) < 3:
                sheets.insert(0, plan["sheets"][0])  # the planned package always goes out
        sheets, instead = self.no_regress(case, threads, sid, plan, sheets)
        if not sheets:
            return instead
        offers = {ts.get("term_sheet_id") for ev in threads.values() for m in ev if m.get("from") != "buyer"
                  for ts in (m.get("term_sheets") or [])}  # every offer ID in this session is citable

        def known(m):  # an ID the data does not have would read as a lie
            ref = m.group(0)
            if ref in offers or (self.brief and ref in self.brief.facts):
                return ref
            log.warning("dropped unknown data ID %s", ref)
            return ""

        body = re.sub(r"\s*\(\s*[,;\s]*\)", "", ID_RE.sub(known, str(d.get("body", "")))).strip()[:8000]
        body = self.drop_repeated_claims(threads, sid, body)
        if not body:
            return None
        message = {"subject": str(d.get("subject") or f"Re: {case['title']}")[:300], "body": body,
                   "term_sheets": [{"terms": t} for t in sheets]}
        if att := self.data_attachment(threads, body):
            message["attachments"] = [att]
        return {"supplier_id": sid, "message": message}

    def drop_repeated_claims(self, threads, sid, body):
        """Each data ID counts once per case (a repeat comes back 'duplicate', 18 % of our claims did): drop
        sentences whose IDs were all cited before. A rival's offer or quote may be cited once per supplier."""
        used = {i for m in threads[sid] if m.get("from") == "buyer" for i in ID_RE.findall(str(m.get("body", "")))}
        used |= {i for ev in threads.values() for m in ev if m.get("from") == "buyer"
                 for i in ID_RE.findall(str(m.get("body", ""))) if not i.startswith(("offer:", "quote:"))}
        parts = re.split(r"(\n+|(?<=[.!?]) +)", body)  # sentences and lines, with what follows them (keeps paragraphs)
        keep = []
        for sentence, sep in zip(parts[::2], [*parts[1::2], ""], strict=False):
            ids = ID_RE.findall(sentence)
            if not ids or not all(i in used for i in ids):
                keep.append(sentence + sep)
        return "".join(keep).strip()

    def data_attachment(self, threads, body):
        """When a supplier asks for our figures, attach them: every fact we cite, with its ID and the
        values from the data package (md attachments are read by the supplier)."""
        asked = any(ASK_DATA.search(str(m.get("body", ""))) for ev in threads.values() for m in ev[-1:]
                    if m.get("from") != "buyer")
        if not asked or not self.brief:
            return None
        ids = list(dict.fromkeys(ID_RE.findall(body) + [i for m in self.all_buyer_messages(threads)
                                                         for i in ID_RE.findall(m.get("body", ""))]))
        rows = [(i, self.brief.facts[i]) for i in ids if i in self.brief.facts]
        if not rows:
            return None
        lines = ["# Data behind our figures (Hollerau Pumpenwerke, Purchasing)", "", "| ID | data |", "|---|---|"]
        lines += [f"| {i} | {json.dumps(f, ensure_ascii=False, default=str)[:300]} |" for i, f in rows[:20]]
        content = base64.b64encode("\n".join(lines).encode("utf-8")).decode("ascii")
        return {"name": "our-figures.md", "type": "md", "content_b64": content}
