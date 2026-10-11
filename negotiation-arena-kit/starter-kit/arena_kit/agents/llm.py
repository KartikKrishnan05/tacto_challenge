"""An LLM buyer that negotiates by e-mail with a sequential MESO strategy: code decides, the model
writes.

Per supplier thread, code ranks the non-price terms by how much they move the cost C and settles
them one at a time: each step sends up to three term sheets that differ only in that term and in
price, priced so that each costs us the same C (multiple equivalent simultaneous offers). The
value the supplier answers with is locked, and the next step varies the next term. Once every term
is locked, only price is left. A playbook per category goal (`PLAYBOOK`) sets the anchor, the
concession pace, which terms go first, the stance on index clauses, whether the supplier's margin
penalty is priced in (stabilise, secure_supply) and whether to walk.

Around that engine:
  - terms both sides gain from (a contract or volume that covers our need) are set at once, not
    spent a step on; a term the supplier reveals itself ("Alternatively, with 30 days: EUR ...") is
    locked from its own two prices; a term the supplier asks for (faster payment) is the next step,
    with one package that gives it at a lower price
  - every new offer gives at least MIN_STEP of the gap left - holding draws a final offer; no
    package goes below the playbook's anchor price, whatever term it improves
  - a term the supplier answered far against us is stepped once more before it is locked
  - an offer the supplier made earlier that costs us less than its standing one is offered back
    once before we close; a final offer is never answered (one more message makes it walk)
  - index clauses on a volatile series are never offered
Code decides when to accept (agreed, final offer, a small gap, a stalled supplier, or the round
limit - only if it keeps the must-haves and beats the alternative) and when to walk away.

The e-mail (`writing`): the model writes the words, code inserts the data arguments (`claims`,
computed from the facts so they check out), its own sentences and the options paragraph built from
the term sheets, and lints what the model wrote. Any provider works: pass
`complete(system, user) -> str`, or use `llm_from_env()`.
"""

import base64
import json
import logging
import math
import os
import re
import threading
from collections import Counter

import httpx

from .. import claims, writing
from ..cost import closing_terms, complete_offer, must_haves_ok, total_cost
from ..docs import standard_terms
from ..play import latest, live_threads, moves_left, standing

log = logging.getLogger("arena")
ID_RE = re.compile(r"\b(?:idx|quote|offer|sc|qn|po|cost|fc|news|doc|ctr):[\w.:@#/-]*\w")
SYSTEM = writing.SYSTEM

# The sequential MESO strategy: per thread, the non-price terms are settled one at a time. Each
# step sends up to three term sheets that differ only in that term and in price, priced so that
# each costs us the same C; the term the supplier picks is locked, and the next step varies the
# next term. When every term is locked, price alone is left.
MAX_STEPS = 3  # terms settled with alternatives; the rest stay at the supplier's value
MIN_WEIGHT = 0.002  # a term that moves C by less than this share is not worth a round
FLOOR_ROOM = 0.07  # a package priced this far below our anchor share of the supplier's price is not credible
RFQ_PRICE_ROUNDS = 3  # RFQ: price rounds per thread after the steps
RISE_PENALTY = 2  # the supplier raised its price after our opening: concede as if this many rounds later
MIN_STEP = 0.15  # every new offer gives at least this share of the C gap left to the supplier's offer
STALL_SHARE = 0.001  # a supplier whose C moved less than this over its last three replies has stalled
REVISIT_SHARE = 0.003  # an earlier offer this much cheaper than the standing one is worth asking for
REOPEN_SHARE = 0.01  # a locked term costing us this share of C over its best option gets one more step
FULL_COST_K = 1.02  # the supplier's full cost = should-cost / 1.02 (fits the arena's relationship charge)
VOLATILE_PCT = 15.0  # an index that moved more than this in 12 months is not offered as a clause
COVERAGE = ("contract_months", "volume_per_year")  # set where both sides gain, never a step
# Which way each term is worth more to the supplier: +1 higher, -1 lower (index_clause: having one).
SUPPLIER_LIKES = {
    "payment_days": -1, "effective_date": -1, "contract_months": +1, "volume_per_year": +1,
    "lead_time_days": +1, "ppm_commitment": +1, "chargeback_eur_per_complaint": -1, "tooling_eur": +1,
    "incoterm": -1, "index_clause": +1,
}  # fmt: skip
INCOTERM_RANK = {"EXW": 0, "FCA": 1, "DAP": 2}  # the supplier carries more freight up the list
# Terms a supplier asks for in its e-mail: the next step offers a package that gives it.
ASKS = (
    (re.compile(r"faster payment|quick(?:er)? payment|early payment|shorter payment|cash flow", re.I), "payment_days"),
)
VOLUME_ASK = re.compile(r"which volume|binding (?:higher )?volume|volume commitment|volume can you commit", re.I)
ASK_DATA = re.compile(
    r"send (?:us )?the data|which figures|share the (?:detailed )?(?:forecast )?data|provide the "
    r"(?:specific )?(?:forecast|data|figures|volume data)|data behind",
    re.I,
)

# One playbook per category goal - the same engine, other settings:
#   anchor     first target as a share of the opening price (None: the healthy-supplier price)
#   concede    our target closes (1 - concede**k) of the gap to the supplier's price by message k
#   first      the terms settled first, in this order (the rest follow by EUR weight)
#   index      index clauses we offer: any, deadband (>= 3 % deadband only) or avoid
#   floor      price the supplier's margin penalty into C (stabilise, secure_supply)
#   walk       walk away when no offer beats the best valid opening; False: take the best one
#   close_gap  accept once the supplier's C is within this share of our target
#   tone       what the e-mails should convey
PLAYBOOK = {
    "leverage": {
        "anchor": 0.85, "concede": 0.85, "first": ["payment_days", "index_clause"], "index": "any",
        "floor": False, "walk": True, "close_gap": 0.005,
        "tone": "A competitive category: argue firmly on price with market data; we have options.",
    },
    "routine": {
        "anchor": 0.90, "concede": 0.8, "first": ["contract_months", "payment_days"], "index": "avoid",
        "floor": False, "walk": True, "close_gap": 0.01,
        "tone": "A routine category: keep it short and simple; we value a lean multi-year deal.",
    },
    "stabilise": {
        "anchor": None, "concede": 0.8, "first": ["volume_per_year", "contract_months", "payment_days"],
        "index": "deadband", "floor": True, "walk": False, "close_gap": 0.005,
        "tone": "A strategic supplier: acknowledge real cost pressure and aim for a fair, stable long-term"
        " deal; do not fight a justified increase, ask for transparency instead.",
    },
    "secure_supply": {
        "anchor": 0.95, "concede": 0.7, "first": ["volume_per_year", "lead_time_days", "contract_months"],
        "index": "avoid", "floor": True, "walk": False, "close_gap": 0.015,
        "tone": "A bottleneck part: securing the full volume and lead time matters most; be cooperative"
        " and a reliable partner.",
    },
    "quality_first": {
        "anchor": 0.92, "concede": 0.8, "first": ["ppm_commitment", "chargeback_eur_per_complaint"],
        "index": "avoid", "floor": False, "walk": True, "close_gap": 0.005,
        "tone": "A quality escalation: a binding defect commitment and chargebacks come before price;"
        " refer to the quality record.",
    },
}  # fmt: skip
DEFAULT_PLAY = {
    "anchor": 0.92, "concede": 0.8, "first": [], "index": "any", "floor": False, "walk": True,
    "close_gap": 0.005, "tone": "",
}  # fmt: skip

# The supplier's "Alternatively, with <terms>: €<price> per unit." lines (written from its state).
ALT_RE = re.compile(r"Alternatively, with (.+?):\s*(?:€|EUR)\s?(\d[\d.,]*)\s*per unit", re.I)
PAYMENT_RE = re.compile(r"(?:zahlungsziel|payment(?: terms?)?(?: of| in)?)\s+(\d{1,3})\s+(?:days|tage)", re.I)
LEAD_RE = re.compile(r"(?:lieferzeit|lead time)(?: of)?\s+(\d{1,3})\s+days", re.I)
MONTHS_RE = re.compile(r"\b(\d{1,2})[- ]months?\b", re.I)
VOLUME_RE = re.compile(r"\b(\d{1,3}(?:[.,]\d{3})+|\d{3,7})\s+(?:pcs|pieces|units|kg)\s+(?:per|a)\s+year", re.I)
PPM_RE = re.compile(r"(\d{1,5})\s*ppm", re.I)
CHARGEBACK_RE = re.compile(r"chargeback of (?:€|EUR)\s?(\d[\d.,]*)", re.I)
INCOTERM_RE = re.compile(r"\b(EXW|FCA|DAP)\b")
ISO_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
LONG_DATE_RE = re.compile(r"\b(\d{1,2})\.?\s+([A-Za-zä]+)\s+(20\d\d)\b")
MONTH_NAMES = {
    name: i + 1
    for i, names in enumerate(
        [
            ("january", "januar"), ("february", "februar"), ("march", "märz", "maerz"), ("april",),
            ("may", "mai"), ("june", "juni"), ("july", "juli"), ("august",), ("september",),
            ("october", "oktober"), ("november",), ("december", "dezember"),
        ]
    )
    for name in names
}  # fmt: skip


def llm_from_env():
    """LLM_PROVIDER=anthropic (Anthropic SDK) or openai (any OpenAI-compatible endpoint)."""
    provider, model = os.environ.get("LLM_PROVIDER", "openai"), os.environ["LLM_MODEL"]
    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic(timeout=45.0)
        extra = {"output_config": {"effort": e}} if (e := os.environ.get("LLM_EFFORT")) else {}

        def complete(system, user):
            r = client.messages.create(
                model=model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                **extra,
            )
            return "".join(b.text for b in r.content if b.type == "text")

        return complete
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}"}

    def complete(system, user):
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        r = httpx.post(f"{base}/chat/completions", json=body, headers=headers, timeout=45.0)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    return complete


class LLMAgent:
    def __init__(self, complete, package=None):
        self.complete, self.package = complete, package
        self.case_id, self.brief, self.dossier = None, "", {}
        self.order = {}  # supplier -> the terms worth a step, in this order
        self.steps = {}  # supplier -> {index of our message: the term it varied, None (price) or a one-off move}
        self.special = set()  # (supplier, "revisit"): one-off moves already made
        self.ledger = claims.Ledger()  # the data IDs cited in this case
        self.volatility = {}  # index series -> 12-month change in %
        self.lock = threading.Lock()  # an RFQ runs its threads in parallel

    def decide(self, case, threads):
        """The next move, one thread at a time; once every thread is done, close the case."""
        sid = self.next_thread(case, threads)
        if sid is None:
            return self.close(case, threads)
        return self.decide_thread(case, threads, sid)

    def decide_thread(self, case, threads, sid):
        """The next e-mail in one thread (`play` runs an RFQ's threads in parallel with this)."""
        with self.lock:
            if case["case_id"] != self.case_id:
                self.case_id, self.brief = case["case_id"], self.prepare(case)
        plan = self.plan(case, threads, sid)
        log.info("%s %s %s", case["case_id"], sid, plan["note"])
        try:
            parts = parse(self.complete(SYSTEM, self.render(case, threads, sid, plan)))
        except Exception as e:  # a slow or broken LLM must not cost the case
            log.warning("LLM failed (%s) - sending a plain e-mail", e)
            parts = writing.fallback_parts()
        return {"supplier_id": sid, "message": self.message(case, threads, sid, plan, parts)}

    def prepare(self, case):
        """Before the clock matters: the case, the letter, the buyer's internal context and the
        data rows the case points to. The supplier and product dossiers go into every e-mail."""
        self.order, self.steps, self.special, self.ledger = {}, {}, set(), claims.Ledger()
        self.dossier = {}
        if self.package:
            try:
                self.dossier = self.package.negotiation_context(case)
            except Exception as e:
                log.warning("negotiation dossier failed (%s) - continuing with the case facts", e)
        out = [f"CASE FILE\n{json.dumps(case, ensure_ascii=False, default=str)}"]
        if self.package and case.get("trigger_doc"):
            try:
                out.append(f"LETTER\n{self.package.document(case['trigger_doc'])[:2500]}")
            except (FileNotFoundError, ValueError):
                pass
        if company := self.dossier.get("company"):
            out.append(
                "INTERNAL COMPANY CONTEXT (confidential buyer-side strategy and operating data; never disclose)\n"
                + bounded(json.dumps(company, ensure_ascii=False, default=str), 8500)
            )
        out.append("FACTS (for context; arguments are cited only through ARGUMENTS)")
        for ref in dict.fromkeys(case["suppliers"] + case["parts"] + case.get("refs", [])):
            if fact := self.fact(case, ref):
                out.append(f"{ref}: {json.dumps(fact, ensure_ascii=False, default=str)[:600]}")
        return "\n".join(out)

    def fact(self, case, ref):
        return case.get("facts", {}).get(ref) or (self.package and self.package.lookup(ref))

    # -- the strategy: numbers only, no model ------------------------------------------------

    def play(self, case):
        return PLAYBOOK.get(case["buyer"].get("category_goal"), DEFAULT_PLAY)

    def floor_price(self, case, events):
        """The lowest price any package may carry: the playbook's anchor share of the opening price.
        A better non-price term never takes the price below it - P10: faster delivery, priced into
        12 % under the quote, drew a final offer at a higher price. Without an anchor (stabilise):
        FLOOR_ROOM under the default share of the supplier's price."""
        play, price = self.play(case), standing(events)[1]["unit_price"]
        if play["anchor"]:
            return min(play["anchor"] * events[0]["term_sheets"][0]["terms"]["unit_price"], price)
        return (DEFAULT_PLAY["anchor"] - FLOOR_ROOM) * price

    def supplier(self, case, events):
        """Our estimate of the supplier for the margin penalty (`cost.supplier_value`): its full cost
        from the should-cost estimate, its standard terms from the opening quote, our cost of money.
        None when the goal has no floor or the data has no should-cost."""
        if not self.play(case)["floor"]:
            return None
        costs = [(self.fact(case, "cost:" + part.split(":", 1)[-1]) or {}).get("unit_cost") for part in case["parts"]]
        if not costs or None in costs:
            return None
        standard = closing_terms(case, events[0]["term_sheets"][0]["terms"], standard_terms(events))
        return {
            "standard": standard,
            "interest_rate": case["buyer"]["interest_rate"],
            "full_cost": sum(costs) / FULL_COST_K,
        }

    def cost(self, case, events, terms):
        """C of terms in this thread - with the supplier's margin penalty where the goal has one."""
        return total_cost(case, terms, standard_terms(events), self.supplier(case, events))

    def volatile(self, case, series):
        """The series moved more than VOLATILE_PCT in its last 12 months: a clause on it is a bet."""
        if series not in self.volatility:
            got = claims.index_change(self.package, case, series) if self.package else None
            self.volatility[series] = got[0] if got else 0.0
        return abs(self.volatility[series]) > VOLATILE_PCT

    def options(self, case, field, terms, std):
        """The values of a term we offer: valid ones; index clauses as the playbook allows and never
        on a volatile series."""
        opts = valid_options(case, field, terms, std)
        if field != "index_clause":
            return opts
        stance = self.play(case)["index"]
        keep = [o for o in opts if o is None or not self.volatile(case, o["series"])]
        if stance == "avoid":
            keep = [o for o in keep if o is None]
        elif stance == "deadband":
            keep = [o for o in keep if o is None or o.get("deadband_pct", 0) >= 3]
        return keep or opts

    def next_thread(self, case, threads):
        """The live thread that still has work, the least advanced first; None once all are done."""
        open_ = [s for s in live_threads(threads) if not self.done(case, threads, s)]
        if not open_:
            return None
        return min(open_, key=lambda s: (sent(threads[s]), self.cost_now(case, threads[s])))

    def cost_now(self, case, events):
        return self.cost(case, events, standing(events)[1])

    def reference(self, case, threads):
        """The best opening quote that keeps the must-haves - what the arena scores against."""
        costs = [
            self.cost(case, ev, ev[0]["term_sheets"][0]["terms"])
            for ev in threads.values()
            if ev[0].get("term_sheets") and must_haves_ok(case, ev[0]["term_sheets"][0]["terms"], standard_terms(ev))
        ]
        return min(costs, default=math.inf)

    def c_alt(self, case):
        alt = case["buyer"].get("alternative") or {}
        return total_cost(case, alt["terms"]) if alt.get("terms", {}).get("unit_price") else math.inf

    def acceptable(self, case, threads, sid, strict=True):
        """The standing sheet keeps the must-haves and beats the alternative - and, if strict, costs
        no more than the best valid opening quote."""
        events = threads[sid]
        sheet_id, terms = standing(events)
        if not sheet_id or not must_haves_ok(case, terms, standard_terms(events)):
            return False
        c = self.cost_now(case, events)
        return c <= self.c_alt(case) and (not strict or c <= self.reference(case, threads))

    def best_offer(self, case, threads, strict=True):
        """Accept the cheapest acceptable standing sheet, or None."""
        ok = [s for s in live_threads(threads) if self.acceptable(case, threads, s, strict)]
        if not ok:
            return None
        sid = min(ok, key=lambda s: self.cost_now(case, threads[s]))
        return {
            "supplier_id": sid,
            "message": {"action": {"type": "accept", "term_sheet_id": standing(threads[sid])[0]}},
        }

    def close(self, case, threads):
        """Every thread is done: accept the best acceptable sheet; walk away only if the playbook
        walks (stabilise and secure_supply take the best sheet that beats the alternative)."""
        best = self.best_offer(case, threads) or (not self.play(case)["walk"] and self.best_offer(case, threads, False))
        if best:
            return best
        live = live_threads(threads)
        return {"supplier_id": live[0], "message": {"action": {"type": "walk_away"}}} if live else None

    def accept_now(self, case, threads, sid):
        """RFQ: a final offer or agreement that is the best acceptable offer on the table is taken
        at once - left waiting while other threads talk, it times out and the supplier walks."""
        if latest(threads[sid]).get("status") not in {"agreed", "final_offer"}:
            return None
        if not self.done(case, threads, sid):  # a revisit is still due here
            return None
        best = self.best_offer(case, threads)
        return best if best and best["supplier_id"] == sid else None

    def done(self, case, threads, sid):
        """Nothing more to win in this thread - unless an earlier, cheaper offer of the supplier's is
        still to be taken up before we close."""
        events = threads[sid]
        if not standing(events)[0] or moves_left(case, events) <= 0:
            return True
        return self.over(case, threads, sid) and self.revisit_terms(case, sid, events) is None

    def finished(self, case, threads, sid):
        """The supplier agreed or made its final offer (pushing on makes it walk), it has stalled,
        an RFQ thread has had its price rounds, or - once only price is left - its price meets our
        target."""
        events = threads[sid]
        if latest(events).get("status") in {"agreed", "final_offer"}:
            return True
        if self.stalled(case, events):
            return True
        price_rounds = sum(1 for k, f in self.steps.get(sid, {}).items() if f is None and k < sent(events))
        if len(case["suppliers"]) > 1 and price_rounds >= RFQ_PRICE_ROUNDS:
            return True
        ours = our_sheets(events)
        if price_rounds > 0 and ours:
            target = self.cost(case, events, ours[0])
            return self.cost_now(case, events) <= target * (1 + self.play(case)["close_gap"])
        return False

    def stalled(self, case, events):
        """After 3+ of our messages, its offers have not got cheaper for us over its last three replies."""
        replies = [m for m in events if m.get("from") != "buyer" and m.get("term_sheets")]
        if sent(events) < 3 or len(replies) < 3:
            return False
        cs = [self.cost(case, events, m["term_sheets"][0]["terms"]) for m in replies[-3:]]
        return cs[-1] >= cs[0] * (1 - STALL_SHARE)

    def revisit_terms(self, case, sid, events):
        """Once per thread, before we close: the cheapest offer the supplier made in this thread -
        an earlier standing sheet or an "Alternatively" line - if it keeps the must-haves and costs
        us REVISIT_SHARE less than its standing sheet. The arena only accepts the standing sheet, so
        it goes back as our term sheet. Never after a final offer: one more message makes it walk."""
        if (sid, "revisit") in self.special or moves_left(case, events) <= 0:
            return None
        if latest(events).get("status") == "final_offer":
            return None
        std, now = standard_terms(events), self.cost_now(case, events)
        best = None
        for t in self.offered(case, events):
            if must_haves_ok(case, t, std) and (c := self.cost(case, events, t)) < now * (1 - REVISIT_SHARE):
                best = min(best or (c, t), (c, t), key=lambda x: x[0])
        return complete_offer(case, best[1]) if best else None

    def offered(self, case, events):
        """Every package the supplier has put forward in this thread: each standing sheet and each
        "Alternatively" line of its e-mails, completed with its standard terms."""
        std, out = standard_terms(events), []
        for m in events:
            if m.get("from") == "buyer" or not m.get("term_sheets"):
                continue
            main = m["term_sheets"][0]["terms"]
            out.append(closing_terms(case, main, std))
            out += [closing_terms(case, {**main, **a}, std) for a in prose_alternatives(case, m)]
        return [t for t in out if t.get("unit_price")]

    def revealed(self, case, events):
        """Terms the supplier priced itself: its latest e-mail offers two packages that differ in one
        term only (its sheet and an "Alternatively" line) - that term is locked at the value that
        costs us less at the supplier's own prices, no step needed."""
        msg = latest(events)
        if not msg.get("term_sheets"):
            return {}
        std = standard_terms(events)
        main = msg["term_sheets"][0]["terms"]
        s = closing_terms(case, main, std)
        best = {}
        for a in prose_alternatives(case, msg):
            full = closing_terms(case, {**main, **a}, std)
            diff = [f for f in case["negotiable"] if f != "unit_price" and full.get(f) != s.get(f)]
            if len(diff) != 1 or diff[0] in COVERAGE or not must_haves_ok(case, full, std):
                continue
            f = diff[0]
            for c, value in ((self.cost(case, events, s), s[f]), (self.cost(case, events, full), full[f])):
                if f not in best or c < best[f][0]:
                    best[f] = (c, value)
        return {f: v for f, (_, v) in best.items()}

    def asks(self, case, events):
        """Negotiable terms the supplier asked for in its latest e-mail."""
        body = str(latest(events).get("body") or "")
        return [f for rx, f in ASKS if f in case["negotiable"] and rx.search(body)]

    def terms_order(self, case, sid, events):
        """The non-price terms worth a step - the playbook's first, then by EUR weight; fixed at the
        first move. Coverage terms are set where both sides gain (`mutual`), never a step."""
        if sid not in self.order:
            std = standard_terms(events)
            base = self.base(case, events, standing(events)[1], {})
            c_base = self.cost(case, events, base)
            first = self.play(case)["first"]
            weights = []
            for field in case["negotiable"]:
                if field == "unit_price" or field in COVERAGE:
                    continue
                opts = self.options(case, field, base, std)
                if len(opts) > 1:
                    costs = [self.cost(case, events, {**base, field: o}) for o in opts]
                    if (w := max(costs) - min(costs)) >= MIN_WEIGHT * c_base:
                        weights.append((w, field))
            weights.sort(key=lambda x: (first.index(x[1]) if x[1] in first else len(first), -x[0]))
            self.order[sid] = [f for _, f in weights[:MAX_STEPS]]
            log.info(
                "%s %s %s, steps: %s",
                case["case_id"],
                sid,
                case["buyer"].get("category_goal"),
                ", ".join(f"{f} ({w:,.0f} EUR)" for w, f in weights[:MAX_STEPS]) or "price only",
            )
        return self.order[sid]

    def locked(self, case, sid, events):
        """After each step, the value of its term the supplier answered with (our first package's
        value if it answered without one or with a value that breaks a must-have) - plus the terms
        the supplier priced itself (`revealed`). A term stepped only once whose answer costs us more
        than REOPEN_SHARE against its best option stays open for one more step (P01: the supplier
        picked the earliest effective date, worth 3.5 % of C)."""
        out, std = {}, standard_terms(events)
        mine = [i for i, m in enumerate(events) if m.get("from") == "buyer"]
        for k, field in sorted(self.steps.get(sid, {}).items()):
            if field not in case["negotiable"] or k >= len(mine):
                continue
            i = mine[k]
            ours = (events[i].get("term_sheets") or [{}])[0].get("terms", {}).get(field)
            reply = next((m for m in events[i + 1 :] if m.get("from") != "buyer"), None)
            if reply is None:
                continue
            sheets = reply.get("term_sheets") or []
            theirs = sheets[0]["terms"].get(field, ours) if sheets else ours
            out[field] = theirs if must_haves_ok(case, {**out, field: theirs}, std) else ours
        counts = Counter(self.steps.get(sid, {}).values())
        t = complete_offer(case, {**standing(events)[1], **out})
        for field in [f for f in out if counts[f] == 1]:
            costs = [self.cost(case, events, {**t, field: o}) for o in self.options(case, field, t, std)]
            if costs and self.cost(case, events, t) - min(costs) > REOPEN_SHARE * self.cost(case, events, t):
                del out[field]
        for field, value in self.revealed(case, events).items():
            out.setdefault(field, value)
        return out

    def next_field(self, case, sid, events, base, locked, asks):
        """The term this message varies: a term the supplier asked for, else the next term of the
        order - one not locked and stepped at most once before; None once only price is left."""
        std = standard_terms(events)
        counts = Counter(self.steps.get(sid, {}).values())
        for f in [*asks, *self.terms_order(case, sid, events)]:
            if f in locked or counts[f] >= 2 or f in COVERAGE or f not in case["negotiable"] or f == "unit_price":
                continue
            if len(self.options(case, f, base, std)) > 1:
                return f
        return None

    def mutual(self, case, events, terms, locked):
        """Coverage terms moved to where both sides gain: the option the supplier likes at least as
        much as its own (a longer contract, more volume) that costs us least - if it saves us money."""
        std, t = standard_terms(events), dict(terms)
        for f in COVERAGE:
            if f not in case["negotiable"] or f in locked or t.get(f) is None:
                continue
            better = [o for o in self.options(case, f, t, std) if o is not None and o >= t[f]]
            if better:
                pick = min(better, key=lambda o: self.cost(case, events, {**t, f: o}))
                if self.cost(case, events, {**t, f: pick}) < self.cost(case, events, t):
                    t[f] = pick
        return t

    def base(self, case, events, terms, locked):
        """The supplier's terms with our locked values and the coverage both sides gain from; a value
        that breaks a must-have is replaced by the cheapest valid option - before the coverage move,
        which only sees options that keep the must-haves (P10: the supplier's short volume hid the
        24-month contract that saved EUR 340k)."""
        std = standard_terms(events)

        def keep_must_haves(t):
            for field in case["negotiable"]:
                if field != "unit_price" and not must_haves_ok(case, t, std):
                    if opts := self.options(case, field, t, std):
                        t[field] = min(opts, key=lambda o: self.cost(case, events, {**t, field: o}))
            return t

        t = keep_must_haves(complete_offer(case, {**terms, **locked}))
        return keep_must_haves(self.mutual(case, events, t, locked))

    def target(self, case, events, best):
        """The C we ask for with this message, as a cost: `best` - the step's term at its best
        option for us, the rest as they stand - at a price that closes (1 - concede**k) of the gap
        from the anchor to the supplier's price. The anchor is the playbook's share of the opening price
        (a price increase: the current price) - or, where the goal prices the supplier's margin,
        the price that costs us least once that penalty counts. A supplier that raised its price
        after our opening gets faster concessions. Every new offer gives at least MIN_STEP of the gap
        left since our last one (a repeat draws a final offer), and stays below what the supplier asks."""
        play, std = self.play(case), standard_terms(events)
        price = standing(events)[1]["unit_price"]
        opening = events[0]["term_sheets"][0]["terms"]["unit_price"]
        supplier = self.supplier(case, events)
        current = case["buyer"].get("current_unit_price")
        if supplier:
            anchor = cheapest_price(case, best, std, supplier, price)
        elif case.get("type") == "price_increase" and current and current < opening:
            anchor = current
        else:
            anchor = (play["anchor"] or DEFAULT_PLAY["anchor"]) * opening
        k = sent(events) + (RISE_PENALTY if price > opening else 0)
        p = min(price, anchor + (price - anchor) * (1 - play["concede"] ** k))
        target = self.cost(case, events, {**best, "unit_price": p})
        if ours := our_sheets(events):
            c_ours = self.cost(case, events, ours[0])
            target = max(target, c_ours + MIN_STEP * max(0.0, self.cost_now(case, events) - c_ours))
        return min(target, self.cost_now(case, events) * (1 - play["close_gap"]))

    def plan(self, case, threads, sid):
        """This message's term sheets: a revisit if one is due, else a step on the next
        unlocked term, or price alone - with the data arguments still unused and code's sentences."""
        events = threads[sid]
        k = sent(events)
        extras = []
        stated = any("We can commit to" in str(m.get("body") or "") for m in events if m.get("from") == "buyer")
        if VOLUME_ASK.search(str(latest(events).get("body") or "")) and not stated:
            extras.append(None)  # our volume commitment, filled once the sheets are known
        if self.over(case, threads, sid) and (t := self.revisit_terms(case, sid, events)) is not None:
            self.special.add((sid, "revisit"))
            extras = [
                "You have offered us exactly the terms on the attached term sheet; we are ready to sign on"
                " them - please confirm."
            ]
            return self.one_off(case, sid, k, "revisit", t, extras)
        field, locked, asks, sheets, target = self.draft(case, threads, sid)
        self.steps.setdefault(sid, {})[k] = field
        step = f"step {field}{' (asked for)' if field in asks else ''}" if field else "price"
        note = f"{step}, locked {json.dumps(locked)}, target C {target:,.0f}: " + " | ".join(
            json.dumps({key: s[key] for key in ("unit_price", field) if key}) for s in sheets
        )
        return {
            "kind": "step" if field else "price",
            "field": field,
            "locked": locked,
            "asks": asks,
            "sheets": sheets,
            "claims": self.arguments(case, threads, sid),
            "extras": [x if x is not None else commitment(case, sheets[0]) for x in extras],
            "note": note,
        }

    def draft(self, case, threads, sid):
        """The term sheets of our next step or price message, without recording anything:
        (field, locked, asks, sheets, target C)."""
        events = threads[sid]
        std, price = standard_terms(events), standing(events)[1]["unit_price"]
        supplier = self.supplier(case, events)
        asks = self.asks(case, events)
        locked = self.locked(case, sid, events)
        base = self.base(case, events, standing(events)[1], locked)
        field = self.next_field(case, sid, events, base, locked, asks)
        if field is None:
            picks, ranked = [None], [None]
        else:
            opts = self.options(case, field, base, std)
            ranked = sorted(opts, key=lambda o: self.cost(case, events, {**base, field: o, "unit_price": price}))
            # an asked-for term: one package gives the supplier what it asked for
            wished = [liked(field, opts)] if field in asks else []
            picks = []
            for o in [ranked[0], *wished, base[field], ranked[len(ranked) // 2], *ranked]:
                if o in opts and o not in picks and len(picks) < 3:
                    picks.append(o)
        target = self.target(case, events, base if field is None else {**base, field: ranked[0]})
        floor = self.floor_price(case, events)
        if field is not None and supplier is None:  # every package at or above the floor, all costing
            # us the same (only where C rises with the price: with the margin penalty a low price
            # costs more, and `price_for` already stays at or above the cheapest price)
            at_floor = [self.cost(case, events, {**base, field: o, "unit_price": floor}) for o in picks]
            target = min(max(target, *at_floor), self.cost_now(case, events) * (1 - self.play(case)["close_gap"]))
        sheets = []
        for o in picks:
            t = base if field is None else {**base, field: o}
            p = price_for(case, t, std, target, supplier, price)
            if p is None or p < floor - 0.01 or p >= price:  # a cent of rounding is no breach
                continue  # not credible, or no better for us than what the supplier asks
            sheet = complete_offer(case, {**t, "unit_price": p})
            if sheet not in sheets:
                sheets.append(sheet)
        if not sheets:  # nothing credible: our best option for this term, at the floor at least
            t = base if field is None else {**base, field: ranked[0]}
            p = price_for(case, t, std, target, supplier, price)
            if p is None:
                p = cheapest_price(case, t, std, supplier, price) if supplier else floor
            p = min(max(p, floor), price)
            sheets = [complete_offer(case, {**t, "unit_price": round(p, 2)})]
        return field, locked, asks, sheets, target

    def repeats(self, case, threads, sid):
        """Our next message would only repeat our last term sheets: a repeat draws a final offer, so
        there is nothing more to win here (P01, P02 on 11 Oct: the same sheet twice at the price
        where the supplier's margin penalty starts to cost us)."""
        ours = our_sheets(threads[sid])
        if not ours:
            return False
        sheets = self.draft(case, threads, sid)[3]
        return [complete_offer(case, s) for s in ours] == sheets

    def over(self, case, threads, sid):
        """Finished, or nothing new left to offer."""
        return self.finished(case, threads, sid) or self.repeats(case, threads, sid)

    def one_off(self, case, sid, k, kind, terms, extras):
        self.steps.setdefault(sid, {})[k] = kind
        return {
            "kind": kind,
            "field": None,
            "locked": {},
            "asks": [],
            "sheets": [terms],
            "claims": [],
            "extras": extras,
            "note": f"{kind}: {json.dumps(terms, default=str)}",
        }

    def arguments(self, case, threads, sid):
        """The data arguments for this supplier that argue our side and have not been cited yet."""
        price = standing(threads[sid])[1].get("unit_price")
        rivals = {}
        for s in live_threads(threads):
            sheet_id, terms = standing(threads[s])
            if s != sid and sheet_id and terms.get("unit_price"):
                rivals[sheet_id] = terms["unit_price"]
        try:
            found = claims.candidates(self.package, case, sid, price, rivals)
        except Exception as e:  # the arguments are a bonus; the move must go out
            log.warning("arguments failed (%s)", e)
            return []
        with self.lock:
            return claims.usable(found, self.ledger, sid)

    # -- the e-mail: the model writes it around the term sheets --------------------------------

    def render(self, case, threads, sid, plan):
        out = [self.brief]
        others = [s for s in live_threads(threads) if s != sid]
        if others:
            out.append(f"\nOther suppliers quoting in this RFQ: {', '.join(others)} (their prices only via ARGUMENTS)")
        if product := self.dossier.get("product"):
            product = bounded(json.dumps(product, ensure_ascii=False, default=str), 5500)
            out.append(f"\nPRODUCT DOSSIER (market and history data)\n{product}")
        if supplier := self.dossier.get("suppliers", {}).get(sid):
            supplier = bounded(json.dumps(supplier, ensure_ascii=False, default=str), 6000)
            out.append(f"\nSUPPLIER DOSSIER {sid} (profile, history, terms and notes)\n{supplier}")
        events = threads[sid]
        out.append(f"\nTHREAD {sid}: status {latest(events).get('status')}, {moves_left(case, events)} messages left")
        for m in events[-6:]:
            head = {k: m.get(k) for k in ("status", "term_sheets", "missing_fields", "claim_results") if m.get(k)}
            out.append(f"--- {m.get('from')}: {json.dumps(head)}\n{m.get('body', '')[:800]}")
            for att in m.get("attachments") or []:
                out.append(f"[attachment {att.get('name')}]\n{att.get('text', '')[:1500]}")
        args = [
            f"{c.key}: {c.sentence}{''.join(' ' + e for e in c.extra)}" + (f" [{c.note}]" if c.note else "")
            for c in plan["claims"]
        ]
        out.append("\nARGUMENTS (pick by key; inserted word for word)\n" + ("\n".join(args) or "(none left)"))
        if plan["kind"] == "revisit":
            what = (
                "The supplier offered exactly the attached terms earlier in this thread. This e-mail says we are"
                " ready to sign on them and asks for confirmation. Keep it short; no arguments, no question."
            )
        elif plan["field"]:
            what = (
                f"We settle one term at a time. Agreed so far: {json.dumps(plan['locked'])}. This e-mail"
                f" offers {len(plan['sheets'])} alternative package(s) that differ in {plan['field']} and"
                " price; ask which one works best for them."
            )
            if plan["field"] in plan["asks"]:
                what += f" The supplier asked for a better {plan['field']}: one package gives it, at a lower price."
        else:
            what = f"Every other term is settled ({json.dumps(plan['locked'])}); this e-mail is about price only."
        if plan["extras"]:
            what += f"\nCODE SENTENCES (already in the e-mail, do not repeat): {' '.join(plan['extras'])}"
        goal = case["buyer"].get("category_goal")
        out.append(f"\nCATEGORY GOAL {goal}: {self.play(case)['tone']}")
        out.append(f"\nPLAN: {what}\nTERM SHEETS OF THIS E-MAIL: {json.dumps(plan['sheets'])}")
        return "\n".join(out)

    def message(self, case, threads, sid, plan, parts):
        events = threads[sid]
        offers = {s.get("term_sheet_id") for ev in threads.values() for m in ev for s in m.get("term_sheets") or []}

        def known(m):  # an ID the data does not have would read as a lie
            if m.group(0) in offers or self.fact(case, m.group(0)):
                return m.group(0)
            log.warning("dropped unknown data ID %s", m.group(0))
            return ""

        material = "\n".join([self.brief, *(str(m.get("body") or "") for m in events)])
        numbers = writing.allowed(material, json.dumps(plan["sheets"]))
        if not parts.get("claims") and sent(events) == 0 and plan["claims"]:
            parts = {**parts, "claims": [c.key for c in plan["claims"][:2]]}  # the first e-mail argues
        fields = shown_fields(case, plan["sheets"], standing(events)[1], plan["field"])
        labelled = [describe(s, ["unit_price", *fields]) for s in plan["sheets"]]
        company = (self.fact(case, sid) or {}).get("name") or ""
        dropped = []
        body, picked = writing.assemble(
            parts, {c.key: c for c in plan["claims"]}, labelled, company, material, numbers, plan["extras"], dropped
        )
        for d in dropped:
            log.info("dropped sentence (%s): %s", d["why"], d["sentence"])
        with self.lock:
            for c in picked:
                self.ledger.use(c, sid)
        body = re.sub(r"[ \t]*\(\s*[,;\s]*\)", "", ID_RE.sub(known, body)).strip()[:8000]
        out = {
            "subject": writing.subject(case, events)[:300],
            "body": body,
            "term_sheets": [{"terms": t} for t in plan["sheets"]],
        }
        if attachment := self.data_attachment(case, threads, sid, body):
            out["attachments"] = [attachment]
        return out

    def data_attachment(self, case, threads, sid, body):
        """When the supplier asks for our figures, attach them: every fact we cite in this thread,
        with its ID and the values from the data package (the supplier reads md attachments)."""
        if not ASK_DATA.search(str(latest(threads[sid]).get("body") or "")):
            return None
        mine = [str(m.get("body") or "") for m in threads[sid] if m.get("from") == "buyer"]
        ids = list(dict.fromkeys(i for text in [body, *mine] for i in ID_RE.findall(text)))
        rows = [(i, f) for i in ids if (f := self.fact(case, i))]
        if not rows:
            return None
        lines = ["# Data behind our figures (Hollerau Pumpenwerke, Purchasing)", "", "| ID | data |", "|---|---|"]
        lines += [f"| {i} | {json.dumps(f, ensure_ascii=False, default=str)[:300]} |" for i, f in rows[:20]]
        content = base64.b64encode("\n".join(lines).encode("utf-8")).decode("ascii")
        return {"name": "our-figures.md", "type": "md", "content_b64": content}


def bounded(text, limit):
    return text if len(text) <= limit else text[: max(0, limit - 25)] + " …[context truncated]"


def sent(events):
    """Messages we sent in this thread."""
    return sum(m.get("from") == "buyer" for m in events)


def our_sheets(events):
    """The term sheets of our last message."""
    last = next((m for m in reversed(events) if m.get("from") == "buyer"), {})
    return [s["terms"] for s in last.get("term_sheets") or []]


def valid_options(case, field, terms, standard):
    """The values of a term the case allows that keep the must-haves."""
    opts = case.get("term_options", {}).get(field) or []
    return [o for o in opts if must_haves_ok(case, {**terms, field: o}, standard)]


def liked(field, opts):
    """The option the supplier likes best (`SUPPLIER_LIKES`)."""
    key = {
        "incoterm": lambda o: INCOTERM_RANK.get(o, 1),
        "index_clause": lambda o: o is not None,
        "effective_date": lambda o: o or "",
    }.get(field, lambda o: o if o is not None else 0)
    return max(opts, key=key) if SUPPLIER_LIKES.get(field, 1) > 0 else min(opts, key=key)


def commitment(case, sheet):
    """Our answer to "which volume can you commit to?": the volume and term of our package."""
    t = closing_terms(case, sheet)
    return (
        f"We can commit to {t['volume_per_year']:,} units per year over {t['contract_months']} months,"
        " as set out in the term sheets."
    )


def shown_fields(case, sheets, theirs, field):
    """The terms the options paragraph spells out: the step's term, the terms that differ between
    our packages, then those that differ from the supplier's sheet - at most four."""

    def key(v):
        return json.dumps(v, sort_keys=True, default=str)

    fields = [f for f in case["negotiable"] if f != "unit_price"]
    varying = [f for f in fields if len({key(s.get(f)) for s in sheets}) > 1]
    differ = [f for f in fields if key(sheets[0].get(f)) != key(theirs.get(f))]
    return list(dict.fromkeys(([field] if field else []) + varying + differ))[:4]


def describe(terms, fields):
    """A term sheet in words, for the options paragraph (code writes it, so it matches the sheet)."""
    out = []
    for f in fields:
        v = terms.get(f)
        if v is None and f != "index_clause":
            continue
        if f == "unit_price":
            out.append(f"EUR {v:,.2f} per unit")
        elif f == "payment_days":
            out.append(f"payment {v} days net")
        elif f == "effective_date":
            out.append(f"new price from {v}")
        elif f == "contract_months":
            out.append(f"{v}-month agreement")
        elif f == "volume_per_year":
            out.append(f"{v:,} units per year")
        elif f == "lead_time_days":
            out.append(f"lead time {v} days")
        elif f == "index_clause":
            out.append(
                "fixed price, no index clause"
                if not v
                else f"index clause on {v['series']} ({v['material_share']:.0%} material share,"
                f" {v['deadband_pct']:g}% deadband, base {v['base_month']})"
            )
        elif f == "ppm_commitment":
            out.append(f"quality commitment {v} ppm")
        elif f == "chargeback_eur_per_complaint":
            out.append(f"EUR {v:g} chargeback per complaint")
        elif f == "incoterm":
            out.append(f"Incoterm {v}")
        elif f == "tooling_eur":
            out.append(f"tooling EUR {v:,.0f}")
        elif v is not None:
            out.append(f"{f.replace('_', ' ')} {v}")
    return ", ".join(out)


def _number(text):
    text = text.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text):
        return float(re.sub(r"[.,]", "", text))
    return float(text.replace(",", "."))


def _date(text):
    if m := ISO_RE.search(text):
        return m.group(1)
    for d, mon, y in LONG_DATE_RE.findall(text):
        if (month := MONTH_NAMES.get(mon.lower())) and 1 <= int(d) <= 31:
            return f"{y}-{month:02d}-{int(d):02d}"
    return None


def prose_alternatives(case, message):
    """The supplier's "Alternatively, with <terms>: €<price> per unit." lines as term dicts. A line
    with an index clause we cannot pin to an option is skipped."""
    out, negotiable = [], case["negotiable"]
    for cond, price in ALT_RE.findall(re.sub(r"\s*\n\s*", " ", str(message.get("body") or ""))):
        t = {"unit_price": _number(price)}
        if re.search(r"preisgleitklausel|index clause", cond, re.I):
            if not re.search(r"\bno (?:preisgleitklausel|index clause)", cond, re.I):
                continue
            t["index_clause"] = None
        if "payment_days" in negotiable and (m := PAYMENT_RE.search(cond)):
            t["payment_days"] = int(m.group(1))
        if "effective_date" in negotiable and (d := _date(cond)):
            t["effective_date"] = d
        if "lead_time_days" in negotiable and (m := LEAD_RE.search(cond)):
            t["lead_time_days"] = int(m.group(1))
        if "volume_per_year" in negotiable and (m := VOLUME_RE.search(cond)):
            t["volume_per_year"] = int(_number(m.group(1)))
        elif "contract_months" in negotiable and (m := MONTHS_RE.search(cond)):
            t["contract_months"] = int(m.group(1))
        if "ppm_commitment" in negotiable and (m := PPM_RE.search(cond)):
            t["ppm_commitment"] = int(m.group(1))
        if "chargeback_eur_per_complaint" in negotiable and (m := CHARGEBACK_RE.search(cond)):
            t["chargeback_eur_per_complaint"] = _number(m.group(1))
        if "incoterm" in negotiable and (m := INCOTERM_RE.search(cond)):
            t["incoterm"] = m.group(1)
        if len(t) > 1:  # a price alone says nothing about which terms it is for
            out.append(t)
    return out


def price_for(case, terms, standard, target, supplier=None, hint=None):
    """The unit price at which these terms cost `target`, or None if no price does. Without a
    supplier estimate C is linear in the price. With one, the margin penalty makes C fall with the
    price only down to the cheapest price, and rise below it: take the price at or above the
    cheapest one, which leaves the supplier healthier."""
    if supplier is None:
        c0 = total_cost(case, {**terms, "unit_price": 0.0}, standard)
        c1 = total_cost(case, {**terms, "unit_price": 1.0}, standard)
        if c1 <= c0:
            return None
        return round((target - c0) / (c1 - c0), 2)
    low = cheapest_price(case, terms, standard, supplier, hint)
    high = 1.5 * hint

    def c(p):
        return total_cost(case, {**terms, "unit_price": p}, standard, supplier)

    if c(low) > target + 1.0:  # out of reach: even the cheapest price costs more
        return None
    if c(high) <= target:
        return round(high, 2)
    for _ in range(50):
        mid = (low + high) / 2
        low, high = (mid, high) if c(mid) < target else (low, mid)
    return round(low, 2)


def cheapest_price(case, terms, standard, supplier, hint):
    """The unit price at which these terms cost us least once the supplier's margin penalty
    counts (C is convex in the price): a golden-section search around the supplier's price."""
    lo, hi, g = 0.5 * hint, 1.5 * hint, (5**0.5 - 1) / 2

    def c(p):
        return total_cost(case, {**terms, "unit_price": p}, standard, supplier)

    for _ in range(60):
        a, b = hi - g * (hi - lo), lo + g * (hi - lo)
        lo, hi = (lo, b) if c(a) <= c(b) else (a, hi)
    return (lo + hi) / 2


def parse(text):
    """The first JSON object in the model's reply (tolerates code fences and chatter)."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"no JSON in reply: {text[:200]}")
    return json.loads(match.group(0))
