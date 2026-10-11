"""SpecAgent: the buyer agent of NEW_AGENT_REQUIREMENTS.md. Code decides every number, the model
writes words only, and every model call has a code-written fallback.

  before the clock   dossiers and one profile call per supplier (section 1, `spec.dossier`)
  case open          section 0.7 (`spec.prep`): the case checked, C of every opening and C_alt,
                     coverage, levers, anchor, floor, must-have filter, the case clock
  every move         per thread: what its e-mail says (`spec.analyst`), our estimate of it (section 2),
                     the close rules (section 7, `spec.closing`), else the next offer - the target C
                     from section 5 (`spec.concession`), the sheets from MESO's steps while heavy terms
                     are open (`spec.steps`) and section 6's sheets after (`spec.sheets`), guarded -
                     and an e-mail around them (`spec.email`) in one tactic's words (`spec.tactics`)
  runner             one worker per supplier thread (`spec.runner`)
  strategy           the knobs (anchor depth, concession pace, close gap) this case is played with: given
                     explicitly (the lab's experiments) or chosen from the learned playbook that earlier
                     experiments built (`spec.strategy`, `knowledge/`)

    uv run arena play case:P01 --agent spec -v
"""

import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import claims as cl
from .. import writing
from ..cost import cost_breakdown, must_haves_ok
from ..docs import standard_terms
from ..play import latest, live_threads, moves_left
from ..spec import clock as clock_mod
from ..spec import closing, dossier, email, levers, prep, sheets, tactics
from ..spec import estimate as est_mod
from ..spec import strategy as strat
from ..spec.analyst import GRACE_S, Analyst
from ..spec.concession import Plan
from ..spec.prose import prose_alternatives
from ..spec.steps import STEP_SLACK, Steps, implied_price, repeats
from ..spec.terms import COVERAGE, complete, key

log = logging.getLogger("arena")
WRITE_TIMEOUT_S = 35.0  # R10.1
SEND_MARGIN_S = 4.0  # compose and send after the model answers
# R13.11 off by default: the cost function already prices an index clause (expected path + index risk);
# the 15 % ban kept P02 from a clause its supplier paid for (0.507 -> 0.735, the other 9 cases unchanged)
VOLATILE_PCT = float(os.environ.get("SPEC_VOLATILE_PCT", "1000"))
PRESSURE_LEFT = 2  # R11.8
LOCK_RISK = 0.5  # R11.8
LOCK_TOL = 0.0005


@dataclass
class Thread:
    """What we keep per supplier thread."""

    plan: Plan = field(default_factory=Plan)
    steps: Steps = field(default_factory=Steps)
    analyst: Analyst | None = None
    last_sheets: list = field(default_factory=list)  # [(label, terms)] of our last message
    flags: set = field(default_factory=set)  # earlier, repair, restore, rival, weighted, volume
    sent_claims: list = field(default_factory=list)  # the arguments of our last message, in body order
    seen_replies: int = 0
    anchor_price: float | None = None


class SpecAgent:
    def __init__(self, complete=None, package=None, analyst_complete=None, prep_complete=None, log_dir=None,
                 strategy=None, knowledge=None):  # fmt: skip
        """`strategy`: play every case with these knobs (spec.strategy.Strategy). Else `knowledge`, the
        learned playbook (spec.strategy.load()), picks them per case; with neither, the defaults."""
        self.complete, self.package = complete, package
        self.fixed_strategy, self.knowledge = strategy, knowledge
        self.strategy, self.strategy_name, self.strategy_why = strat.BASE, "base", "defaults"
        self.analyst_complete = analyst_complete if analyst_complete is not None else complete
        self.prep_complete = prep_complete if prep_complete is not None else complete
        self.log_dir = Path(log_dir or os.environ.get("SPEC_LOG_DIR", "runs/spec-logs"))
        self.case_id, self.primed = None, None
        self.reply_at, self.inflight = {}, {}  # set by the runner
        self.lock = threading.RLock()
        self.dossiers, self.profiles, self.t = {}, {}, {}
        self.records, self._volatile = [], {}
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="spec")

    # --- the runner's entry -----------------------------------------------------------------------
    def run_case(self, table, case_id, deadline=None):
        from ..spec.runner import play_case

        return play_case(table, case_id, self, deadline)

    # --- before the clock (section 1) ---------------------------------------------------------------
    def prime(self, case, timeout=None):
        """Dossiers and profiles before the session opens. In the final the profile budget leaves room
        for the 420 s case clock inside the wave's cap."""
        c, _ = prep.normalise(case)
        t0 = time.monotonic()
        self.dossiers = {sid: dossier.gather(self.package, c, sid) for sid in c["suppliers"]}
        if timeout is None:
            timeout = dossier.PROFILE_TIMEOUT_S
            if isinstance(c.get("seconds_left"), int | float):
                timeout = min(timeout, max(0.0, c["seconds_left"] - clock_mod.FORCE_CLOSE_AT - 30))
        self.profiles = dossier.profiles(c, self.package, self.dossiers, self.prep_complete, timeout)
        self.primed = c["case_id"]
        log.info("%s primed in %.1fs (profiles: %s)", c["case_id"], time.monotonic() - t0,
                 {s: p.get("source") for s, p in self.profiles.items()})  # fmt: skip

    # --- case open (section 0.7) ---------------------------------------------------------------------
    def prepare(self, case, threads=None, seconds_left=None):
        if self.primed != case.get("case_id"):  # resumed, or no case file before the session: no model
            self.prime(case, timeout=0)
        cash = {sid: p.get("cash_short", False) for sid, p in self.profiles.items()}
        self.case = case  # hidden-case facts must be available to volatile() during preparation
        self._volatile.clear()
        self.choose_strategy(case)
        self.prep = prep.prepare(case, threads, self.package, self.volatile, seconds_left, cash, self.strategy)
        self.case, self.case_id = self.prep.case, case.get("case_id")
        self.clock = self.prep.clock.open()
        self.ledger, self.results = cl.Ledger(), {}
        self.t = {}
        for sid in self.case["suppliers"]:
            self.t[sid] = Thread(
                plan=Plan(**self.strategy.plan_kwargs()),
                analyst=Analyst(
                    self.case,
                    sid,
                    self.package,
                    self.analyst_complete,
                    self.pool,
                    cost_fn=lambda terms, std: est_mod.plain_cost(self.case, terms, std),
                )
            )
        log.info("%s", prep.summary(self.prep))
        self.record("prepare", summary=prep.summary(self.prep), warnings=self.prep.warnings)

    def choose_strategy(self, case):
        """The knobs for this case: the fixed strategy, else the learned playbook's pick, else defaults."""
        if self.fixed_strategy is not None:
            name = next((n for n, v in strat.VARIANTS.items() if v == self.fixed_strategy), "custom")
            self.strategy, self.strategy_name, self.strategy_why = self.fixed_strategy, name, "fixed for this run"
        else:
            self.strategy, self.strategy_name, self.strategy_why = strat.choose(case, self.knowledge)
        log.info("%s strategy %s (%s): %s", case.get("case_id"), self.strategy_name,
                 strat.describe(self.strategy), self.strategy_why)  # fmt: skip
        brief = strat.briefing(case, self.knowledge)
        for line in brief:
            log.info("%s lesson: %s", case.get("case_id"), line)
        self.record("strategy", name=self.strategy_name, why=self.strategy_why, knobs=self.strategy.knobs(),
                    lessons=brief)  # fmt: skip

    def volatile(self, series):
        """R13.11: the series moved more than VOLATILE_PCT in its last 12 months."""
        if series not in self._volatile:
            got = cl.index_change(self.package, getattr(self, "case", {}) or {}, series)
            if got:
                a, b = self.fact(got[1]), self.fact(got[2])
                change = (b["value"] / a["value"] - 1) * 100
                # Claim sentences round to a tenth; a risk threshold must not round 15.04% away.
                self._volatile[series] = abs(change) > VOLATILE_PCT + 1e-9
            else:
                self._volatile[series] = False
        return self._volatile[series]

    # --- clock hooks for the runner -----------------------------------------------------------------
    def sent(self, sid):
        if hasattr(self, "clock"):
            self.clock.sent(sid)

    def replied(self, sid):
        if hasattr(self, "clock"):
            self.clock.replied(sid)

    # --- the state of a thread ----------------------------------------------------------------------
    def _std(self, events):
        try:
            return standard_terms(events)
        except Exception as e:
            log.warning("standard terms unreadable: %s", e)
            return {}

    def state(self, threads, sid):
        """Everything code needs about one thread now: rows, estimate, context, target, view."""
        case, events, th = self.case, threads[sid], self.t[sid]
        std = self._std(events)
        th.analyst.observe(events)
        intel = th.analyst.snapshot()
        replies = [m for m in events if m.get("from") != "buyer"]
        self._feedback(sid, events, len(replies))
        mine = " ".join(str(m.get("body") or "") for m in events if m.get("from") == "buyer").lower()
        echo = bool(re.search(r"faster payment|cash ?flow|liquidit", mine))
        cash = self.profiles.get(sid, {}).get("cash_short", False) or (intel.cash_ask and not echo)  # R11.7, R12.19
        extra = [prose_alternatives(case, m) if m.get("from") != "buyer" else [] for m in events]
        slopes = levers.revealed_slopes(case, events, extra)
        seen = [r.terms["unit_price"] for s in threads for r in self._price_rows(threads, s)]
        # the market is judged on the opening quotes only: a later concession under the should-cost must
        # not drop the margin charge mid-case (P02: 373 against a should-cost of 374.95)
        market = [p for s in threads if (p := prep.opening_terms(threads[s]).get("unit_price"))]
        est = est_mod.estimate(case, self.package, events, prep.opening_terms(events), cash,
                               levers.revealed_rate(slopes), min(market, default=None))  # fmt: skip
        c_alt = self.prep.c_alt
        rows = []
        for n, m in enumerate(replies):
            for j, ts in enumerate(m.get("term_sheets") or []):
                terms = ts.get("terms") or {}
                if not ts.get("term_sheet_id") or terms.get("unit_price") is None:
                    continue
                c = est_mod.cost(case, terms, std, est)
                plain = est_mod.plain_cost(case, terms, std)
                valid = must_haves_ok(case, terms, std) and plain < c_alt and c < c_alt
                try:
                    bd = cost_breakdown(case, terms, std)
                    unc = bd["uncovered_need"] + bd["fallback_risk"]
                except Exception:
                    unc = 0.0
                rows.append(closing.Row(ts["term_sheet_id"], terms, c, valid, n == len(replies) - 1, j == 0, n, unc))
        latest_rows = [r for r in rows if r.latest]
        best = min(latest_rows, key=lambda r: (not r.valid, r.c)) if latest_rows else None
        their = complete(case, best.terms if best else {"unit_price": None}, std)
        ctx = sheets.Ctx(case, std, est, their, self.prep.row, self.volatile)
        out = {"sid": sid, "events": events, "std": std, "intel": intel, "est": est, "rows": rows, "best": best,
               "ctx": ctx, "slopes": slopes, "alternatives": extra[-1] if extra else [], "cash_short": cash,
               "status": latest(events).get("status") if replies else None, "left": moves_left(case, events),
               "sent": sum(m.get("from") == "buyer" and not m.get("undelivered") for m in events)}  # fmt: skip
        if best is None or not their.get("unit_price"):
            out.update(target=None, info={}, base=None, locked={}, c_ref=math.inf, only_price=False)
            return out
        locked = th.steps.locked(ctx, events)
        # R13.5: actual supplier-priced alternatives supersede an earlier blind menu pick.
        locked.update(th.steps.revealed(ctx, latest(events), out["alternatives"]))
        out["locked"] = locked
        # the anchor: fixed per thread (R4.4); in an RFQ every bidder is anchored to the lowest price on the
        # table and the anchor may only fall (R4.3, R12.6)
        rfq = len(self.case["suppliers"]) > 1
        seen = seen or [ctx.their_price]
        anchor_c = None
        if th.anchor_price is None or rfq:
            base0 = {k: v for k, v in sheets.repair(case, their, std).items() if k != "unit_price"}
            p0 = prep.anchor_price(case, ctx, ctx.their_price, min(seen + [ctx.their_price]), base0)
            combo = sheets.reference_combo(ctx, sheets.cover(ctx, base0, p0), ctx.their_price)
            p = prep.anchor_price(case, ctx, ctx.their_price, min(seen + [ctx.their_price]), combo)
            th.anchor_price = p if th.anchor_price is None else min(th.anchor_price, p)
            anchor_c = ctx.c({**combo, "unit_price": th.anchor_price})
        base = sheets.structure(ctx, th.anchor_price)
        base.update({f: v for f, v in locked.items() if f in base})
        ctx.locked = dict(locked)
        out["base"] = base
        # the supplier's offer re-timed to our structure at its own payment slope: 90 days it has not
        # offered yet is not 90 days at its 45-day price
        rate = levers.revealed_rate(slopes)
        slope = min(max(rate, est_mod.RATE_BOUNDS[0]), est_mod.RATE_BOUNDS[1]) / 360 if rate else None
        c_ref = min(ctx.c({**base, "unit_price": implied_price(ctx.their, base, slope)}), best.c)
        th.plan.observe(c_ref, len(replies))
        if anchor_c is not None and (th.plan.anchor is None or rfq):
            th.plan.set_anchor(min(anchor_c, c_ref), rfq)
        rival = [o.c for s, o in ((s, self._best_valid(threads, s)) for s in live_threads(threads) if s != sid) if o]
        target, info = th.plan.next_target(c_ref, self.prep.horizon, out["left"], c_alt, min(rival, default=None), rfq)
        out.update(target=target, info=info, c_ref=c_ref, rival_best=min(rival, default=None),
                   only_price=th.steps.next_field(ctx, base, locked) is None)  # fmt: skip
        return out

    def _price_rows(self, threads, sid):
        out = []
        for m in threads[sid]:
            if m.get("from") == "buyer":
                continue
            for ts in m.get("term_sheets") or []:
                if (ts.get("terms") or {}).get("unit_price"):
                    out.append(closing.Row(ts.get("term_sheet_id") or "", ts["terms"], 0.0, True, False))
        return out

    def _best_valid(self, threads, sid):
        std = self._std(threads[sid])
        best = None
        msg = latest(threads[sid]) if any(m.get("from") != "buyer" for m in threads[sid]) else {}
        for ts in msg.get("term_sheets") or []:
            terms = ts.get("terms") or {}
            if not terms.get("unit_price") or not must_haves_ok(self.case, terms, std):
                continue
            c = est_mod.plain_cost(self.case, terms, std)
            if c < self.prep.c_alt and (best is None or c < best.c):
                best = closing.Row(ts.get("term_sheet_id"), terms, c, True, True)
        return best

    def rival(self, threads, sid, st):
        """The rival offer to cite (R12.14): the cheapest valid rival sheet that costs us less than this
        bidder's; else the lowest rival price on the table under this bidder's price - the arena checks
        only the price (P05's 0.781 run cited offer:70240-0, whose lead time broke our must-have)."""
        mine = st["best"]
        if mine is None:
            return None
        valid = [b for s in live_threads(threads) if s != sid and (b := self._best_valid(threads, s)) and b.id]
        valid = [b for b in valid if b.c < mine.c]
        if valid:
            return min(valid, key=lambda b: b.c)
        prices = []
        for s in live_threads(threads):
            if s == sid or not any(m.get("from") != "buyer" for m in threads[s]):
                continue
            for ts in latest(threads[s]).get("term_sheets") or []:
                p = (ts.get("terms") or {}).get("unit_price")
                if ts.get("term_sheet_id") and p and p < mine.terms.get("unit_price", 0):
                    prices.append(closing.Row(ts["term_sheet_id"], ts["terms"], p, False, True))
        return min(prices, key=lambda r: r.c) if prices else None

    def _feedback(self, sid, events, n_replies):
        """R10.3: every argument's claim result, fed into the next round."""
        th = self.t[sid]
        if n_replies <= th.seen_replies:
            return
        th.seen_replies = n_replies
        for r in latest(events).get("claim_results") or []:
            i = r.get("index")
            if i is not None and i < len(th.sent_claims):
                self.results[th.sent_claims[i].key] = r.get("result")
                if r.get("result") == "contradicted":
                    log.warning("%s: argument %s was contradicted", sid, th.sent_claims[i].key)

    def view(self, st):
        th = self.t[st["sid"]]
        cs = []
        for n in sorted({r.reply for r in st["rows"]}):
            rs = [r.c for r in st["rows"] if r.reply == n]
            cs.append(min(rs))
        step_share = (cs[-2] - cs[-1]) / cs[-2] if len(cs) > 1 and cs[-2] else 0.0
        return closing.View(
            sid=st["sid"], status=st["status"], left=st["left"], sent=st["sent"], rows=st["rows"],
            target=st.get("target"), last_target=th.plan.targets[-1] if th.plan.targets else None,
            moving=bool((st.get("info") or {}).get("still_moving")), only_price=bool(st.get("only_price")),
            planned_over=len(th.plan.targets) >= self.prep.horizon + 2, close_gap=self.prep.row["close_gap"],
            replies_c=cs, earlier_used="earlier" in th.flags, repair_used="repair" in th.flags,
            restore_used="restore" in th.flags, last_step_share=step_share,
            at_limit=st["intel"].at_limit, walk_risk=st["intel"].walk_risk, wants_close=st["intel"].wants_close,
            at_ceiling=bool((st.get("info") or {}).get("at_ceiling")), held="repeat" in th.flags,
        )  # fmt: skip

    def views(self, threads):
        states = {s: self.state(threads, s) for s in live_threads(threads)}
        return states, {s: self.view(st) for s, st in states.items()}

    # --- the move ------------------------------------------------------------------------------------
    def decide(self, case, threads, sid=None, use_model=True):
        if case.get("case_id") != self.case_id:
            self.prepare(case, threads)
        live = live_threads(threads)
        if sid is None:  # the kit's sequential loop: the thread whose offer costs least
            sid = min(live, key=lambda s: self.standing_cost(case, threads, s))
        if sid not in live:
            return {"supplier_id": sid, "idle": True}
        th = self.t[sid]
        th.analyst.observe(threads[sid])
        first_rfq = len(self.case["suppliers"]) > 1 and not any(m.get("from") == "buyer" for m in threads[sid])
        terminal = latest(threads[sid]).get("status") in ("agreed", "final_offer")
        if use_model and self.analyst_complete is not None and not first_rfq and not terminal:
            # R11.3: a short grace; R12.14 first RFQ / R13.16 terminal offers go straight to code.
            since = time.monotonic() - self.reply_at.get(sid, time.monotonic())
            th.analyst.wait(min(GRACE_S, max(0.0, clock_mod.MODEL_WINDOW_S - since - 25)))
        with self.lock:
            states, views = self.views(threads)
            st = states[sid]
            close = closing.decide(views, sid, must_close=self.clock.must_close())
            if close is None and not self.clock.can_counter():
                close = self.late(states, views, sid)
            if close is not None:
                d = self.realise(close, states, threads, sid)
                if d is not None:
                    self.record("close", sid=sid, kind=close.kind, reason=close.reason, sheet=close.sheet_id,
                                elapsed=round(self.clock.elapsed(), 1))  # fmt: skip
                    return d
            if st.get("target") is None:  # no price on the table: ask for one
                return self.plain_message(threads, sid, [], "Could you please send us your offer with a unit price?")
            move = self.plan_move(states, views, threads, sid)
            if move is None or "sheets" not in move:  # a close, an offer-back or nothing left to send
                return move or {"supplier_id": sid, "idle": True}
        return self.write(states, threads, sid, move, use_model)

    def late(self, states, views, sid):
        """R11.16, after NO_NEW_COUNTER_AT: lock in a better prose offer if a round still fits; repeat our
        last offer only when nothing on the table is acceptable yet; otherwise close by R7."""
        fits = self.clock.round_fits()
        st, th = states[sid], self.t[sid]
        lock = self.lock_in(st)
        best_rows = [r for v in views.values() for r in v.rows if r.valid]
        if fits and lock and (not best_rows or lock[1] < min(r.c for r in best_rows) - 0.01) and "lock" not in th.flags:
            return closing.Close("lock_in", "late: lock in its prose offer", sid, terms=lock[0])
        if fits and not closing.ladder(views) and th.last_sheets and st["left"] > 1:
            return closing.Close("repeat", "late: nothing acceptable yet - our last offer again", sid)
        return closing.accept(views, "late: no new counter-offers") or closing.Close("idle", "late, nothing yet", sid)

    def lock_in(self, st):
        """R11.8: the supplier's best offer stated only in prose, if it keeps the must-haves, beats the
        alternative and is cheaper than its standing sheet: (terms, C) or None."""
        if st.get("best") is None:
            return None
        ctx, best = st["ctx"], None
        for po in st["intel"].prose_offers:
            if po.get("in_sheet"):
                continue
            t = complete(self.case, po["terms"], st["std"])
            if not t.get("unit_price") or not ctx.ok(t):
                continue
            c = ctx.c(t)
            if c < st["best"].c - 0.01 and est_mod.plain_cost(self.case, t, st["std"]) < self.prep.c_alt:
                if best is None or c < best[1]:
                    best = (t, c)
        return best

    def realise(self, close, states, threads, sid):
        """A close rule as a decision: accept, walk, idle, or a one-off message written by code."""
        th = self.t.get(close.sid or sid)
        if close.kind == "accept":
            return {"supplier_id": close.sid, "reason": close.reason, "candidates": close.candidates,
                    "message": {"action": {"type": "accept", "term_sheet_id": close.sheet_id}}}  # fmt: skip
        if close.kind == "walk":
            return {"supplier_id": close.sid or sid, "message": {"action": {"type": "walk_away"}}}
        if close.kind == "idle":
            return {"supplier_id": sid, "idle": True}
        st = states[close.sid or sid]
        if close.kind == "offer_back":  # R12.13
            th.flags.add("earlier")
            t = complete(self.case, close.terms, st["std"])
            text = (
                f"Thank you. We have reviewed everything again and are ready to sign on your own earlier offer "
                f"{close.sheet_id} - the attached term sheet repeats exactly those terms. Can you confirm?"
            )
            return self.plain_message(threads, close.sid, [("their earlier offer", t)], text)
        if close.kind in ("repair", "restore"):  # R12.12 / R11.17: its price, must-haves kept, full need covered
            th.flags.add(close.kind)
            terms = close.terms or (st["best"].terms if st.get("best") else latest(st["events"]).get("term_sheets",
                                    [{}])[0].get("terms", {}))  # fmt: skip
            t = complete(self.case, sheets.repair(self.case, terms, st["std"]), st["std"])
            t = {**t, **self.full_cover(st, t)}
            if not st["ctx"].ok(t) or t.get("unit_price") is None:
                return None
            text = (f"Thank you - we accept your price of EUR {t['unit_price']:.2f}. To complete the agreement and "
                    "cover our full requirement, please confirm it on the attached term sheet; that is all we need "
                    "to sign.")  # fmt: skip
            return self.plain_message(threads, close.sid, [("confirm", t)], text)
        if close.kind == "lock_in":
            th.flags.add("lock")
            return self.plain_message(threads, close.sid, [("their prose offer", close.terms)],
                                      "Thank you - we take up the offer you described in your e-mail, as set out on "
                                      "the attached term sheet. Please confirm.")  # fmt: skip
        if close.kind == "repeat":
            th.flags.add("repeat")
            return self.plain_message(
                threads, close.sid, th.last_sheets, "Thank you for your reply. Our offer stands as set out below."
            )
        return None

    def full_cover(self, st, terms):
        """The coverage options that cover the whole need over the horizon (R12.12, R11.17)."""
        b = self.case["buyer"]
        out = {}
        for f in COVERAGE:
            opts = (self.case.get("term_options") or {}).get(f) or []
            if f not in self.case["negotiable"] or not opts:
                continue
            need = b["need_per_year"] if f == "volume_per_year" else 12 * b["horizon_years"]
            enough = [o for o in opts if isinstance(o, int | float) and o >= need - 1e-9]
            out[f] = min(enough) if enough else max(opts)
        return out

    def plan_move(self, states, views, threads, sid):
        """The sheets of our next offer: a step on the next heavy term, else a price round; guarded."""
        st, th = states[sid], self.t[sid]
        ctx, base, target, info = st["ctx"], st["base"], st["target"], dict(st["info"])
        floor_p = sheets.floor(ctx)
        asks = self.asks(st)
        hold = info.get("kind") == "hold"
        cover_values = {f: base[f] for f in COVERAGE if f in base and f in self.case["negotiable"]}
        supplier_terms = [r.terms for r in st["rows"]]

        def guarded_build(c_target):
            out, kind, fld = self.build(st, base, c_target, floor_p, asks)

            def guard_menu(menu):
                menu = sheets.drop_refused(menu, st["intel"].refused)
                menu = sheets.lock_coverage(ctx, menu, cover_values)
                slack = STEP_SLACK if kind == "step" else 0.0
                menu = sheets.guard(ctx, menu, c_target, supplier_terms, th.last_sheets, slack)
                return sheets.no_regress(ctx, menu, [r.c for r in st["rows"] if r.valid])

            out = guard_menu(out)
            while kind == "step" and len({key(t.get(fld)) for _, t in out}) < 2:
                # The final guards can remove a choice too: record only the menu actually sent.
                th.steps.skipped.add(fld)
                out, kind, fld = self.build(st, base, c_target, floor_p, asks)
                out = guard_menu(out)
            return out, kind, fld

        built = guarded_build(target)
        if hold and repeats([t for _, t in built[0]], [t for _, t in th.last_sheets]):
            # R5.3 + R13.7: a hold that would only repeat our sheets becomes the deadlock-breaking step
            target = th.plan.breaker(st["c_ref"])
            info["kind"], hold = "deadlock breaker (no repeat)", False
            built = guarded_build(target)
        out, kind, fld = built
        if (not out or repeats([t for _, t in out], [t for _, t in th.last_sheets])) and not hold:
            # nothing new at this target: one real step instead (R5.3) before the thread counts as done
            target = max(target, th.plan.breaker(st["c_ref"]))
            info["kind"] = "deadlock breaker (nothing new)"
            out, kind, fld = guarded_build(target)
        if not out or repeats([t for _, t in out], [t for _, t in th.last_sheets]):  # R12.10 / R13.7
            v = views[sid]
            if (r := closing.earlier_cheaper(v)) is not None:
                return self.realise(closing.Close("offer_back", "nothing new to offer: its earlier offer", sid, r.id,
                                                  terms=r.terms), states, threads, sid)  # fmt: skip
            if len(views) == 1 and (acc := closing.accept(views, "nothing new to offer")):
                return self.realise(acc, states, threads, sid)  # R13.7: the thread is done - close
            if len(views) > 1:
                return {"supplier_id": sid, "idle": True}  # RFQ: leave this thread, the others go on
            if not out:
                out = th.last_sheets or sheets.out_of_reach(ctx, base, floor_p) or sheets.just_under(ctx, base)
        lock = self.lock_in(st)
        pressure = st["left"] <= PRESSURE_LEFT or st["intel"].walk_risk >= LOCK_RISK or self.clock.near_end()
        if lock and (pressure or lock[1] <= target * (1 + LOCK_TOL)):
            if all((lock[0].get(f) or 0) >= v for f, v in cover_values.items() if isinstance(v, int | float)):
                out = [("their prose offer", lock[0]), *[(lb, t) for lb, t in out if t != lock[0]]]
                th.flags.add("lock")
        out = out[:3]
        return {"sheets": out, "kind": kind, "field": fld, "target": target, "info": info, "hold": hold,
                "lock": bool(lock and out and out[0][0] == "their prose offer"), "asks": asks}  # fmt: skip

    def build(self, st, base, target, floor_p, asks):
        th, ctx = self.t[st["sid"]], st["ctx"]
        locked = st["locked"]
        fld = th.steps.next_field(ctx, base, locked, list(asks))
        while fld is not None:
            pk = th.steps.packages(ctx, fld, base, target, floor_p, asked=asks.get(fld))
            if pk:
                return pk, "step", fld
            th.steps.skipped.add(fld)  # R13.4: no choice, no step - the term goes to the price round
            fld = th.steps.next_field(ctx, base, locked, list(asks))
        open_fields = [f for f in (th.steps.order or []) if f not in locked]
        profile = self.profiles.get(st["sid"], {})
        lv = [f for f in dict.fromkeys([*profile.get("levers", []), *self.prep.type_levers]) if f not in locked]
        price_asks = {f: v for f, v in asks.items() if f not in locked}
        out = sheets.price_sheets(ctx, base, target, lv, floor_p, open_fields, price_asks)
        if not th.plan.targets and out and len(out) < 3:  # R12.7: the first menu probes payment days
            if not any(t.get("payment_days") != out[0][1].get("payment_days") for _, t in out):
                if probe := sheets.payment_probe(ctx, out[0][1], target, floor_p, st["slopes"]):
                    out.append(("payment probe", probe))
        if len(out) < 3 and (iv := sheets.index_variant(ctx, out[0][1] if out else base, target, floor_p)):
            out.append(("their index clause", iv))  # R12.8
        if not out:
            out = sheets.out_of_reach(ctx, base, floor_p)  # R6.6
        return out, "price", None

    def asks(self, st):
        """R11.7: what the supplier asked for beyond our reference terms - in its sheet, or in its e-mail
        (the analyst's lever hints, snapped to the case's options) - when it costs us something."""
        ctx, base = st["ctx"], st["base"]
        price = ctx.their_price
        theirs = sheets.mutual_combo(ctx, ctx.their, price)
        ref = sheets.reference_combo(ctx, theirs, price)
        out = {f: theirs[f] for f in sheets.REFERENCE_FIELDS if f in theirs and key(theirs[f]) != key(ref.get(f))
               and f not in COVERAGE and f not in st["locked"]}  # fmt: skip
        opts = self.case.get("term_options") or {}
        for h in st["intel"].lever_hints:
            f = h.get("field")
            if (
                f in out
                or f not in self.case["negotiable"]
                or f == "unit_price"
                or not opts.get(f)
                or f in st["locked"]
            ):
                continue
            if not all(isinstance(o, int | float) for o in opts[f]):
                continue
            nums = [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", h.get("hint") or "")]
            nums = [n for n in nums if min(opts[f]) * 0.5 <= n <= max(opts[f]) * 1.5]
            if nums:
                v = min(opts[f], key=lambda o: abs(o - nums[0]))
                worse = ctx.c({**base, f: v, "unit_price": price}) > ctx.c({**base, "unit_price": price})
                if worse and ctx.ok({**base, f: v}):
                    out[f] = v
        return out

    # --- the e-mail -----------------------------------------------------------------------------------
    def arguments(self, states, threads, sid):
        """R9.5: checked arguments not cited yet, never one that came back contradicted, ranked by the
        profile's claim ranking, then strength."""
        st = states[sid]
        rivals = {}
        for s in live_threads(threads):
            if s != sid and (b := self._best_valid(threads, s)) and b.id:
                rivals[b.id] = b.terms["unit_price"]
        try:
            found = cl.candidates(self.package, self.case, sid, st["ctx"].their_price, rivals)
        except Exception as e:
            log.warning("arguments failed: %s", e)
            found = []
        keys = {a.key for a in found}
        found += [c for c in st["intel"].new_claims if c.key not in keys]
        ranking = self.profiles.get(sid, {}).get("claim_ranking") or []
        args = [a for a in cl.usable(found, self.ledger, sid) if self.results.get(a.key) != "contradicted"]
        args.sort(key=lambda a: (ranking.index(a.key) if a.key in ranking else len(ranking), -a.strength))
        return args

    def used_ids(self, threads, sid):
        """IDs cited before: every ID in this thread; in other threads all but rival offers and quotes,
        which count once per supplier (R12.20, R8.3)."""
        used = {
            i for m in threads[sid] if m.get("from") == "buyer" for i in email.ID_RE.findall(str(m.get("body") or ""))
        }
        used |= {
            i
            for s, ev in threads.items()
            if s != sid
            for m in ev
            if m.get("from") == "buyer"
            for i in email.ID_RE.findall(str(m.get("body") or ""))
            if not i.startswith(("offer:", "quote:"))
        }
        return used

    def company(self, sid, events):
        name = dossier.company_name(self.dossiers.get(sid)) or ((self.package.lookup(sid) or {}).get("name")
                                                               if self.package else None)  # fmt: skip
        return email.company(name, events)

    def material(self, sid, events):
        """R9.7: the supplier's e-mails, the trigger letter, the contract clauses."""
        parts = [str(m.get("body") or "") for m in events if m.get("from") != "buyer"]
        if self.package and self.case.get("trigger_doc"):
            try:
                parts.append(self.package.document(self.case["trigger_doc"]))
            except Exception:
                pass
        parts += [str(r.get("text", "")) for r in (self.dossiers.get(sid) or {}).get("clauses", [])]
        return "\n".join(parts)

    def write(self, states, threads, sid, move, use_model=True):
        st, th, events = states[sid], self.t[sid], threads[sid]
        rfq = len(self.case["suppliers"]) > 1
        first = st["sent"] == 0
        intel = st["intel"]
        args = self.arguments(states, threads, sid)
        extras, analysis, extra_claims = [], [], []
        if rfq and "rival" not in th.flags and (r := self.rival(threads, sid, st)):  # R12.14, R8.3: once per bidder
            extras.append(email.rival_line(r.terms["unit_price"], r.id))
            extra_claims.append(cl.Claim(key=f"rival:{r.id}", kind="rival", ids=[r.id], sentence=extras[-1]))
            args = [a for a in args if r.id not in a.ids]
            th.flags.add("rival")
        gap_pct = None
        if st["best"] and move["target"]:
            gap_pct = (st["best"].c - move["target"]) / move["target"] * 100
        # words match numbers: an argument conceding "+X % is justified" is not cited next to an offer below it
        offer_price = min((t.get("unit_price") for _, t in move["sheets"] if t.get("unit_price")), default=None)
        drivers_fit = email.consistent(self.case, intel.cost_impact_pct, offer_price)
        args = [a for a in args if a.key != "summary:indices" or email.consistent(self.case, a.figure, offer_price)]
        tactic_name, why = tactics.pick(intel, {
            "first": first, "rfq": rfq, "rival_cheaper": bool(extra_claims) or any(a.kind == "rival" for a in args),
            "cash_short": st["cash_short"], "cost_gap_pct": intel.cost_gap_pct,
            "gap_pct": gap_pct,
            "data_claims": sum(1 for a in args if a.ids), "relationship": intel.relationship_style == "relationship",
            "asked_for_terms": bool(move["asks"]), "hold": move["hold"], "supplier_held": intel.unchanged_rounds >= 1,
            "goal": self.prep.goal,
        })  # fmt: skip
        tactic = tactics.TACTICS[tactic_name]
        if (
            (s := email.weighted_sentence(intel, face_value=not drivers_fit))
            and "weighted" not in th.flags
            and (first or tactic_name == "data_argument")
        ):
            analysis.append(s)
            th.flags.add("weighted")
        if not first:  # keep arguing after round 1: one fresh computed argument per e-mail (never one twice)
            t0 = th.plan.targets[0] if th.plan.targets else None
            moved_pct = (move["target"] - t0) / t0 * 100 if t0 else None
            lines = email.position_lines(self.case, intel, st["ctx"].their_price, moved_pct,
                                         intel.unchanged_rounds >= 1)  # fmt: skip
            if pos := next(((k, x) for k, x in lines if f"pos:{k}" not in th.flags), None):
                analysis.append(pos[1])
                th.flags.add(f"pos:{pos[0]}")
        # answer the supplier: refused terms, a request for our figures, an offer of a call
        sent_terms = [t for _, t in move["sheets"]]
        for r in intel.refused:
            if f"refused:{r}" not in th.flags and not any(sheets.refused(t, [r]) for t in sent_terms):
                if line := email.refusal_line(r):
                    extras.append(line)
                th.flags.add(f"refused:{r}")
        if email.asks_for_data(events) and email.data_attachment(events, "", self.fact):
            extras.append(email.DATA_LINE)
        last_body = str(latest(events).get("body") or "") if not first else ""
        if email.CALL_RE.search(last_body) and "call" not in th.flags:
            extras.append(email.CALL_LINE)
            th.flags.add("call")
        if (
            email.VOLUME_ASK.search(str(latest(events).get("body") or ""))
            and "volume" not in th.flags
            and move["sheets"]
        ):
            fixed = (self.case.get("fixed_terms") or {}).get("volume_per_year")
            volume = fixed or self.case["buyer"].get("need_per_year")
            if line := email.commitment(move["sheets"][0][1], volume):  # R13.15: answered by code from our sheet
                extras.append(line)
                th.flags.add("volume")
        if move["lock"]:
            extras.append(("Option A takes up" if len(move["sheets"]) > 1 else "This takes up")
                          + " the offer you described in your e-mail.")  # fmt: skip
        if move["hold"]:
            extras.append("We keep our offer as it stands.")
        allowed_q = email.questions(self.case, events, self.profiles.get(sid, {}).get("questions"))[:3]
        sheets_terms = [t for _, t in move["sheets"]]
        options = email.options_paragraph(self.case, sheets_terms, st["ctx"].their)
        moved = 0.0
        if len(th.plan.their_costs) > 1 and th.plan.their_costs[-2]:
            moved = (th.plan.their_costs[-2] - th.plan.their_costs[-1]) / th.plan.their_costs[-2] * 100
        note = ""
        if move["kind"] == "step":
            note = f"This e-mail settles one term ({move['field']}): the options differ only in it and in price."
        elif move["asks"] and move["sheets"] and move["sheets"][0][0] == "their ask":
            note = "Option A grants what they asked for, at a lower price; Option B keeps our standard terms."
        plan_text = {"stance": email.stance(first, move["hold"], moved), "moved_pct": moved, "options": options,
                     "note": note}  # fmt: skip
        company = self.company(sid, events)
        material = self.material(sid, events)
        parts, prompt, reply, error, took = None, None, None, None, 0.0
        budget = None
        if use_model and self.complete is not None and not (rfq and first):  # R12.14: RFQ message 1 by code
            since = time.monotonic() - self.reply_at.get(sid, time.monotonic())
            others = sum(1 for s, busy in self.inflight.items() if busy and s != sid)
            budget = clock_mod.model_budget(since, others)  # R12.17
            if budget is not None:
                budget = min(budget, self.clock.no_counter_at - self.clock.elapsed(),
                             self.clock.force_close_at - self.clock.elapsed() - self.clock.round_time())
                if budget < clock_mod.MIN_MODEL_S:
                    budget = None
        if budget is not None:
            prompt = email.render(self.case, sid, events, plan_text, self.profiles.get(sid, {}), intel,
                                  {k: v for k, v in self.results.items()}, args, allowed_q, tactic,
                                  [*analysis, *extras], company)  # fmt: skip
            t0 = time.monotonic()
            try:
                system = email.SYSTEM + f"\n\nWording style for this e-mail ({tactic.name}): {tactic.style}"
                reply = self.complete(system,
                                      prompt, timeout=min(WRITE_TIMEOUT_S, budget - SEND_MARGIN_S))  # fmt: skip
                parts = _parse(reply)
            except Exception as e:  # R10.1: late or broken - code writes the e-mail
                error = f"{type(e).__name__}: {e}"
                log.warning("%s: writer failed (%s) - code e-mail", sid, error)
            took = time.monotonic() - t0
        if not isinstance(parts, dict):
            previous = "\n".join(str(m.get("body") or "") for m in events if m.get("from") == "buyer")
            parts = email.code_parts(tactic, allowed_q[0] if allowed_q else "", previous, len(move["sheets"]) == 1)
        used = self.used_ids(threads, sid)

        def known(ref):
            offers = {
                ts.get("term_sheet_id") for ev in threads.values() for m in ev for ts in m.get("term_sheets") or []
            }
            return ref in offers or bool(self.fact(ref))

        body, picked, dropped = email.compose(self.case, events, sheets_terms, st["ctx"].their, parts, args, material,
                                              company, extras, analysis, used, known, allowed_q, first)  # fmt: skip
        with self.lock:
            for c in [*picked, *extra_claims]:
                self.ledger.use(c, sid)
            th.sent_claims = [c for c in picked if c.ids] + extra_claims
            th.plan.commit(move["target"])
            th.steps.record(len(th.plan.targets) - 1, move["field"] or "price")
            th.last_sheets = list(move["sheets"])
        msg = {"subject": writing.subject(self.case, events)[:300], "body": body,
               "term_sheets": [{"terms": t} for t in sheets_terms]}  # fmt: skip
        refs = [i for c in [*picked, *extra_claims] for i in c.ids][:20]
        if refs:
            msg["refs"] = refs
        if att := email.data_attachment(events, body, self.fact):
            msg["attachments"] = [att]
        ctx = st["ctx"]
        self.record("move", sid=sid, elapsed=round(self.clock.elapsed(), 1), kind=move["kind"], field=move["field"],
                    step=move["info"].get("kind"), target_c=round(move["target"], 2), their_c=round(st["c_ref"], 2),
                    estimate={"source": st["est"].source, "full_cost": st["est"].full_cost, "rate": st["est"].rate},
                    sheets=[{"label": lb, "terms": t, "c": round(ctx.c(t), 2)} for lb, t in move["sheets"]],
                    tactic=tactic_name, why=why, model_s=round(took, 1), error=error, prompt=prompt, reply=reply,
                    dropped=dropped, claims=[c.key for c in picked])  # fmt: skip
        log.info("%s %s %s %s target C %.0f (theirs %.0f): %s", self.case_id, sid, move["kind"], move["field"] or "",
                 move["target"], st["c_ref"], " | ".join(json.dumps({k: v for k, v in t.items()}, default=str)
                                                        for _, t in move["sheets"]))  # fmt: skip
        return {"supplier_id": sid, "message": msg}

    def plain_message(self, threads, sid, labelled, text):
        """A short code-written e-mail (offer-back, repair, lock-in, repeat)."""
        events = threads[sid]
        sheets_terms = [t for _, t in labelled]
        options = email.options_paragraph(self.case, sheets_terms, {}) if sheets_terms else ""
        company = self.company(sid, events)
        body = "\n\n".join(p for p in [f"Dear colleagues at {company},", text, options, email.SIGNATURE] if p)
        th = self.t[sid]
        if sheets_terms:
            st_target = None
            try:
                std = self._std(events)
                est = est_mod.estimate(self.case, self.package, events, prep.opening_terms(events))
                st_target = est_mod.cost(self.case, sheets_terms[0], std, est)
            except Exception:
                pass
            with self.lock:
                if st_target is not None and math.isfinite(st_target):
                    th.plan.commit(st_target)
                    th.steps.record(len(th.plan.targets) - 1, "special")
                th.last_sheets = list(labelled)
                th.sent_claims = []
        self.record("plain", sid=sid, text=text, sheets=sheets_terms, elapsed=round(self.clock.elapsed(), 1))
        msg = {"subject": writing.subject(self.case, events)[:300], "body": body[:8000]}
        if sheets_terms:
            msg["term_sheets"] = [{"terms": t} for t in sheets_terms]
        return {"supplier_id": sid, "message": msg}

    def fact(self, ref):
        return (self.case.get("facts") or {}).get(ref) or (self.package.lookup(ref) if self.package else None)

    # --- the runner's other hooks ---------------------------------------------------------------------
    def fallback(self, case, threads, sid, plain=False):
        """A move without the model (after a planning error or a refused message)."""
        try:
            return self.decide(case, threads, sid, use_model=False)
        except Exception:
            log.exception("%s: fallback planning failed - naive move", sid)
            from .naive import NaiveAgent

            return NaiveAgent().decide(case, {sid: threads[sid]})

    def accept_candidates(self, case, threads):
        if case.get("case_id") != self.case_id:
            self.prepare(case, threads)
        with self.lock:
            _, views = self.views(threads)
        return closing.ladder(views)

    def accept(self, case, threads):
        ladder = self.accept_candidates(case, threads)
        if not ladder:
            return None
        sid, oid = ladder[0]
        return {
            "supplier_id": sid,
            "candidates": ladder,
            "message": {"action": {"type": "accept", "term_sheet_id": oid}},
        }

    def final_close(self, case, threads):
        """Every worker stopped without closing: accept the ladder, else walk away."""
        if d := self.accept(case, threads):
            return d
        return {"supplier_id": live_threads(threads)[0], "message": {"action": {"type": "walk_away"}}}

    def standing_cost(self, case, threads, sid):
        if case.get("case_id") != self.case_id:
            self.prepare(case, threads)
        b = self._best_valid(threads, sid)
        return b.c if b else math.inf

    def c_alt(self, case):
        return self.prep.c_alt if getattr(self, "prep", None) else prep.c_alternative(case)

    # --- the log (R10.4) -----------------------------------------------------------------------------
    def record(self, event, **data):
        self.records.append({"t": datetime.now().isoformat(timespec="seconds"), "event": event, **data})

    def finish(self, result):
        """Write the case's run log: every move, its numbers, its prompt and reply, and the result."""
        keep = ("deal", "supplier_id", "terms", "c_open", "c_deal", "c_alt", "c_best", "score", "end_reason")
        self.record("result", **{k: result.get(k) for k in keep if isinstance(result, dict) and k in result},
                    elapsed=round(self.clock.elapsed(), 1) if hasattr(self, "clock") else None)  # fmt: skip
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            name = f"{datetime.now():%m%d-%H%M%S}_{str(self.case_id).replace(':', '_')}.jsonl"
            with open(self.log_dir / name, "w", encoding="utf-8") as f:
                for r in self.records:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            log.info("run log: %s", self.log_dir / name)
        except OSError as e:
            log.warning("run log not written: %s", e)


def _parse(text):
    from ..spec.llm import parse_json

    return parse_json(text)


def make(package=None, strategy=None, learned=True):
    """The agent with the model from the environment (`spec.llm.from_env`). It plays `strategy` if given,
    else the learned playbook's pick per case (`learned=False`: the defaults)."""
    from ..spec import llm

    writer = llm.from_env(WRITE_TIMEOUT_S)
    return SpecAgent(writer, package, analyst_complete=llm.from_env(GRACE_S + 15) if writer else None,
                     prep_complete=llm.from_env(dossier.PROFILE_TIMEOUT_S) if writer else None, strategy=strategy,
                     knowledge=strat.load() if learned and strategy is None else None)  # fmt: skip
