"""The dual agent: a negotiator on the critical path, an analyst per supplier thread beside it.

  Negotiator  answers every supplier e-mail well inside the 60 seconds: code decides close / counter
              (`strategy`, `cost`), a concession guard caps every step at RECIPROCITY × the
              supplier's last step, and the e-mail is written in the wording style of one tactic
              (`tactics`) picked from the analyst's latest view - the tactic never touches a number.
  Analyst     one per supplier thread (`intel.Analyst`), in worker threads: structured signals from
              the API at once, a model extraction of the body in the background, cost arithmetic
              and walk risk in code, dossier mining between e-mails. The negotiator reads whatever
              snapshot is ready after a short grace period.

Clock (from case open): no new counter-offer after NO_NEW_COUNTER_AT, closed by FORCE_CLOSE_AT.
Closing accepts the standing term sheet with the lowest C that beats the alternative and keeps the
must-haves (standard terms from the quote PDF fill what it leaves out), else walks away.

Every model call has a timeout and a code fallback: without a model the agent still negotiates,
with code-written wording per tactic.
"""

import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .. import claims as cl
from .. import dossier, runlog, strategy, tactics, writing
from ..cost import closing_terms, complete_offer, must_haves_ok, total_cost
from ..docs import standard_terms
from ..intel import Analyst
from ..play import closing_move, latest, live_threads, moves_left, offers
from .buyer import GOAL_WORDS, PROFILE_SYSTEM
from .naive import NaiveAgent

log = logging.getLogger("arena")

NO_NEW_COUNTER_AT = 360.0  # s after case open: no new counter-offers
FORCE_CLOSE_AT = 420.0  # s after case open: closed at the latest
WALK_RISK_CLOSE = 0.7
ANALYST_GRACE_S = 12.0  # how long the negotiator waits for the analyst's model layer
WRITE_TIMEOUT_S = 25.0  # the writer model's share of the 60 seconds
DEFAULT_REPLY_S = 45.0  # a supplier round trip before we have measured one
TOKEN_STEP = 0.001  # a supplier move under 0.1 % of C is no move: we do not answer it
MAX_REPLY_S = 90.0  # a reply takes up to about a minute; longer waits are not the supplier's pace
ROUND_MARGIN_S = 10.0
PROFILE_TIMEOUT_S = 90.0
NEGOTIATOR_MODEL = ANALYST_MODEL = "claude-sonnet-5-5"
PART_RE = re.compile(r"\bpart:\d+\b")
QUESTIONS = {
    "quality": "Could you send us the 8D report with root cause and containment for the recent complaints?",
    "capacity": "Could you send a delivery schedule with the monthly quantities you can commit to?",
    "rfq": "What is the shortest lead time you could commit to, and is there a cheaper design or material variant?",
    "renewal": "Which volume or contract length would let you improve the price further?",
    "bundling": "Which further parts or volumes would make a bundle more attractive for you?",
    "raw_material": "Which base month and deadband would you consider fair for an index clause?",
}
BREAKDOWN_Q = "Could you share a cost breakdown for this part - material, conversion, overhead?"


def llms_from_env():
    """(negotiator, analyst, prep) completions on LLM_PROVIDER (as `llm_from_env`), or None each
    when no model or credentials are set (the agent then writes code-only).
      LLM_MODEL           the negotiator's writer (anthropic default: claude-sonnet-5-5)
      LLM_ANALYST_MODEL   the analyst (default: LLM_MODEL; anthropic: claude-sonnet-5-5)
      LLM_PREP_MODEL      optional profiles before the clock runs
    ARENA_DUAL_LLM=0 switches the models off."""
    from .llm import llm_from_env

    if os.environ.get("ARENA_DUAL_LLM", "1").lower() in ("0", "false", "no", "off"):
        return None, None, None
    provider = os.environ.get("LLM_PROVIDER", "openai")
    model, analyst = os.environ.get("LLM_MODEL") or "", os.environ.get("LLM_ANALYST_MODEL")
    if provider == "anthropic":
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            log.warning("dual agent: no Anthropic credentials - negotiating with code-written e-mails")
            return None, None, None
        model = model if model.startswith("claude") else NEGOTIATOR_MODEL
        analyst = analyst or ANALYST_MODEL
    elif not (model and os.environ.get("LLM_API_KEY")):
        log.warning("dual agent: no LLM_MODEL / LLM_API_KEY for %s - code-written e-mails", provider)
        return None, None, None
    prep = os.environ.get("LLM_PREP_MODEL")
    return (
        llm_from_env(model, timeout=WRITE_TIMEOUT_S, provider=provider, retries=0),
        llm_from_env(analyst or model, timeout=ANALYST_GRACE_S + 15, provider=provider, retries=0),
        llm_from_env(prep, timeout=PROFILE_TIMEOUT_S, provider=provider) if prep else None,
    )


def concession_guard(prev, proposed, their_step, scale):
    """(target, our step) in C: never further toward them than RECIPROCITY × their last step, never
    back from what we offered; a real step of theirs (TOKEN_STEP of `scale`) is answered visibly."""
    allowed, visible = strategy.RECIPROCITY * max(their_step, 0.0), strategy.MIN_STEP * scale
    if their_step < TOKEN_STEP * scale:
        return prev, 0.0  # no real move from them: we hold
    # A real move gets an answer: at least a visible step, never more than the guard allows (P01,
    # 10 Oct: holding after their steps of 12.8k and 8.2k in C drew a final offer both times).
    ours = min(max(proposed - prev, visible), allowed)
    return prev + ours, ours


def weighted_sentence(intel):
    """The cost-share arithmetic by the supplier's own figures, or None."""
    shared = [d for d in intel.cost_drivers if d.get("share_pct") is not None]
    if not shared or intel.cost_impact_pct is None or intel.asked_pct is None or (intel.cost_gap_pct or 0) <= 1.0:
        return None
    items = ", ".join(
        f"{d['series'] or d['name']} {d['change_pct']:+.1f}% at {d['share_pct']:g}% of your cost" for d in shared
    )
    return (
        f"By the figures you gave us - {items} - the weighted effect on the price is about "
        f"{intel.cost_impact_pct:+.1f}%, not the {intel.asked_pct:g}% you ask for."
    )


class DualAgent:
    def __init__(self, complete=None, package=None, analyst_complete=None, prep_complete=None, clock=time.monotonic):
        self.complete, self.package = complete, package
        self.analyst_complete, self.prep_complete = analyst_complete, prep_complete
        self.clock = clock
        self.fallback = NaiveAgent()
        self.case_id = None

    # --- before the clock -------------------------------------------------------------------------
    def prime(self, case):
        """Dossiers, our own context and (optionally) a profile per supplier. Safe to call again."""
        if case["case_id"] == self.case_id:
            return
        self.case_id, self.case = case["case_id"], case
        self.dossiers, self.profiles, self.plans, self.analysts = {}, {}, {}, {}
        self.ledger, self.sent_claims, self.said = cl.Ledger(), {}, {}
        self.last_sheets, self.tactic_log, self.latencies = {}, {}, []
        self.t_open = self.last_sent_at = None
        self.pool = ThreadPoolExecutor(max_workers=4 * len(case["suppliers"]) + 2, thread_name_prefix="analyst")
        self.company = self._company_context()
        self.context = self._case_context(case)
        for sid in case["suppliers"]:
            self._gather(case, sid)
        if self.prep_complete is not None:
            rec = runlog.current()
            futures = {sid: self.pool.submit(self._profile, case, sid, rec) for sid in case["suppliers"]}
            deadline = time.monotonic() + PROFILE_TIMEOUT_S
            for sid, f in futures.items():
                try:
                    self.profiles[sid] = f.result(timeout=max(0.1, deadline - time.monotonic()))
                except Exception as e:
                    log.warning("profile for %s failed (%s) - using defaults", sid, e)

    def _gather(self, case, sid):
        if sid not in self.dossiers:
            self.dossiers[sid] = dossier.gather(self.package, case, sid)
            self.plans[sid] = strategy.Plan()
            self.profiles.setdefault(sid, self._default_profile(case, sid))

    def _company_context(self):
        root = getattr(self.package, "root", None)
        for path in [root / "company.md"] if root else []:
            if path.is_file():
                return path.read_text(encoding="utf-8")[:3000]
        return ""

    @staticmethod
    def _case_context(case):
        b = case["buyer"]
        keep = (
            "need_per_year",
            "horizon_years",
            "current_unit_price",
            "category_goal",
            "reference_payment_days",
            "need_lead_time_days",
            "must_haves",
            "alternative",
        )
        return {
            "title": case.get("title"),
            "type": case.get("type"),
            "briefing": case.get("briefing"),
            **{k: b.get(k) for k in keep},
        }

    def _default_profile(self, case, sid):
        d = self.dossiers.get(sid, {})
        levers = (["payment_days"] if d.get("cash_signals") else []) + strategy.TYPE_LEVERS.get(case.get("type"), [])
        return {"levers": list(dict.fromkeys(levers)), "cash_short": bool(d.get("cash_signals")), "claim_ranking": []}

    def _profile(self, case, sid, rec):
        args = cl.candidates(self.package, case, sid)
        user = (
            f"CASE\n{json.dumps(self.context, ensure_ascii=False, default=str)}\n\nOUR COMPANY\n{self.company}\n\n"
            f"{dossier.render(self.dossiers[sid])}\n\nARGUMENTS ([key] sentence - note)\n"
            + "\n".join(f"[{a.key}] {a.sentence} - {a.note}" for a in args)
        )
        t0, reply, parsed, error = time.monotonic(), None, None, None
        try:
            reply = self.prep_complete(PROFILE_SYSTEM, user)
            parsed = writing.parse(reply)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            if rec:
                rec.llm(
                    kind="profile",
                    supplier_id=sid,
                    seconds=round(time.monotonic() - t0, 2),
                    prompt=user,
                    reply=reply,
                    parsed=parsed,
                    error=error,
                )
        profile = {**self._default_profile(case, sid), **{k: v for k, v in parsed.items() if v not in (None, "")}}
        fields = {f: f for f in case["negotiable"] if f != "unit_price"}
        levers = (writing.match_key(t, fields) for t in profile.get("levers") or [])
        profile["levers"] = list(dict.fromkeys(f for f in levers if f))
        keys = {a.key: a.key for a in args}
        profile["claim_ranking"] = [k for t in profile.get("claim_ranking") or [] if (k := writing.match_key(t, keys))]
        profile["cash_short"] = bool(profile.get("cash_short")) or bool(self.dossiers[sid].get("cash_signals"))
        return profile

    # --- every round ------------------------------------------------------------------------------
    def decide(self, case, threads):
        return self.decide_all(case, threads)[0]

    def decide_all(self, case, threads):
        rec = runlog.current()
        t0 = self.clock()
        try:
            self.prime(case)
            if self.t_open is None:
                self.t_open = t0
            elif self.last_sent_at is not None:
                self.latencies.append(t0 - self.last_sent_at)
            moves = self._round(case, threads, rec, t0)
        except Exception as e:
            log.exception("dual agent failed (%s) - falling back", e)
            if rec:
                rec.flag = "fell back"
            late = self.t_open is not None and t0 - self.t_open >= NO_NEW_COUNTER_AT
            moves = [closing_move(case, threads) if late else self.fallback.decide(case, threads)]
        if not any(m["message"].get("action") for m in moves):
            self.last_sent_at = self.clock()
        return moves

    def _analyst(self, case, sid, rec):
        if sid not in self.analysts:
            self.analysts[sid] = Analyst(
                case, sid, self.package, self.dossiers.get(sid), self.analyst_complete, self.pool, rec
            )
        return self.analysts[sid]

    def _open_case(self, case, threads):
        """From the first mails: every supplier and part named, each with its dossier."""
        parts = set(case.get("parts", []))
        for events in threads.values():
            for m in events:
                parts |= set(PART_RE.findall(m.get("body") or ""))
        if parts - set(case.get("parts", [])):
            log.info("parts named in the mails beyond the case file: %s", sorted(parts - set(case["parts"])))
        for sid in threads:
            self._gather(case, sid)

    def _state(self, case, sid, events, intel):
        std = standard_terms(events)
        sheets = (s["terms"] for m in events if m.get("from") != "buyer" for s in m.get("term_sheets") or [])
        opening = next(sheets, None)
        est = strategy.estimate(
            case, sid, self.package, events, opening, cash_short=self.profiles.get(sid, {}).get("cash_short", False)
        )
        choices = [(i, t) for i, t in offers(events) if t.get("unit_price")]
        sheet_id, terms = min(
            choices,
            key=lambda c: (not must_haves_ok(case, c[1], std), strategy.cost(case, c[1], std, est)),
            default=(None, {}),
        )
        return {
            "sid": sid,
            "events": events,
            "sheet_id": sheet_id,
            "terms": terms,
            "std": std,
            "est": est,
            "c_now": strategy.cost(case, terms, std, est) if sheet_id else math.inf,
            "status": latest(events).get("status"),
            "left": moves_left(case, events),
            "intel": intel,
        }

    def _round(self, case, threads, rec, t0):
        elapsed = t0 - self.t_open
        live = live_threads(threads)
        if not self.analysts:
            self._open_case(case, threads)
        for sid in live:
            self._analyst(case, sid, rec).observe(threads[sid])
        if self.analyst_complete is not None:  # the model layer gets a short grace, then we go with what is ready
            deadline = time.monotonic() + ANALYST_GRACE_S
            for sid in live:
                self.analysts[sid].wait(deadline - time.monotonic())
        intel = {sid: self.analysts[sid].snapshot() for sid in live}
        states = {s: self._state(case, s, threads[s], intel[s]) for s in live}
        quoting = {s: st for s, st in states.items() if st["sheet_id"] and st["terms"].get("unit_price")}
        record = {
            "elapsed_s": round(elapsed, 1),
            "intel": {s: i.to_log() for s, i in intel.items()},
            "received": {
                s: [{**o, "c_est": self._c(case, o["terms"], states[s])} for o in i.offers] for s, i in intel.items()
            },
        }
        if not quoting:
            return self._finish(rec, record, [self._walk(live[0])], "no supplier quotes a price")
        for s in quoting:
            self._feedback(s, threads[s])
        rfq = len(case["suppliers"]) > 1
        for st in quoting.values():
            self._target(case, st, quoting, rfq)
        record["targets"] = {s: round(st["target"], 2) for s, st in quoting.items()}

        reason = self._close_reason(case, quoting, rfq, elapsed)
        if reason:
            return self._finish(rec, record, [self._close(case, threads, quoting)], reason)
        if elapsed >= NO_NEW_COUNTER_AT:
            return self._late(case, threads, quoting, rec, record, elapsed)

        talk = [s for s, st in quoting.items() if st["left"] > 0 and st["status"] != "final_offer"]
        best_c = min(st["c_now"] for st in quoting.values())
        if rfq and len(talk) > 2:
            talk = [s for s in talk if quoting[s]["c_now"] <= best_c * 1.25 or len(self.plans[s].targets) < 2]
        if not talk:
            return self._finish(rec, record, [self._close(case, threads, quoting)], "no thread left to talk to")
        moves = [self._offer(case, quoting[s], quoting, rfq, elapsed) for s in talk]
        return self._finish(rec, record, self._write_all(case, threads, moves, rec), None, moves)

    # --- the numbers --------------------------------------------------------------------------------
    def _c(self, case, terms, st):
        try:
            return round(strategy.cost(case, terms, st["std"], st["est"]), 2)
        except Exception:
            return None

    def _feedback(self, sid, events):
        for r in latest(events).get("claim_results") or []:
            sent = self.sent_claims.get(sid, [])
            if r.get("index") is not None and r["index"] < len(sent):
                self.ledger.results[sent[r["index"]].key] = r.get("result")
                if r.get("result") == "contradicted":
                    log.warning("%s: argument %s was contradicted", sid, sent[r["index"]].key)

    def _target(self, case, st, quoting, rfq):
        """Where our next offer lands, in C - the concession curve, capped by the guard."""
        sid, plan = st["sid"], self.plans[st["sid"]]
        price = st["terms"]["unit_price"]
        ref = strategy.mutual_combo(case, st["terms"], st["std"], st["est"])
        c_ref = min(strategy.cost(case, {**ref, "unit_price": price}, st["std"], st["est"]), st["c_now"])
        replies = sum(m.get("from") != "buyer" for m in st["events"])
        if replies > plan.replies:
            plan.their_costs.append(c_ref)
            plan.replies = replies
        if plan.anchor is None:
            rivals = [o["terms"]["unit_price"] for s2, o in quoting.items() if s2 != sid and rfq]
            p = strategy.anchor_price(case, st["terms"], st["est"], min([price, *rivals]))
            plan.anchor = min(strategy.cost(case, {**ref, "unit_price": p}, st["std"], st["est"]), c_ref)
        k = len(plan.targets)
        horizon = 5 if rfq else strategy.ROUND_HORIZON
        proposed = plan.anchor + strategy.concession(k, horizon) * (c_ref - plan.anchor)
        step = plan.their_costs[-2] - plan.their_costs[-1] if len(plan.their_costs) > 1 else 0.0
        st["their_step"], st["moving"] = step, step > strategy.MIN_STEP * c_ref
        target, st["our_step"] = concession_guard(plan.targets[-1], proposed, step, c_ref) if k else (proposed, None)
        others = [o["c_now"] for s2, o in quoting.items() if s2 != sid]
        if rfq and others:
            target = max(plan.anchor, min(target, min(others) * 0.99))  # beat the best rival
        target = min(target, strategy.c_alternative(case) * 0.995)
        st["meets"] = st["c_now"] <= target * 1.002 or (k and st["c_now"] <= plan.targets[-1] * 1.001)
        if st["meets"] and st["moving"] and st["left"] > 3:
            st["meets"] = False  # still conceding: their offer is not their last word - ask once more
            target = min(target, st["c_now"] - 0.5 * step)
        st["target"] = min(target, st["c_now"] * (1 - 1e-4))  # never their current standing offer
        st["gap_pct"] = (st["c_now"] - st["target"]) / st["target"] * 100 if st["target"] > 0 else None

    def _acceptable(self, case, terms, std, est):
        c_alt = strategy.c_alternative(case)
        return (
            must_haves_ok(case, terms, std)
            and total_cost(case, terms, std) < c_alt
            and strategy.cost(case, terms, std, est) < c_alt
        )

    def _best_offer(self, case, threads, quoting):
        """(rank C, sid, sheet_id) of the best acceptable sheet across all live threads, or None."""
        best = None
        for sid in live_threads(threads):
            std = standard_terms(threads[sid])
            est = quoting.get(sid, {}).get("est")
            for sheet_id, terms in offers(threads[sid]):
                if terms.get("unit_price") and self._acceptable(case, terms, std, est):
                    c = strategy.cost(case, terms, std, est)
                    best = min(best or (c, sid, sheet_id), (c, sid, sheet_id))
        return best

    def _close_reason(self, case, quoting, rfq, elapsed):
        if elapsed >= FORCE_CLOSE_AT:
            return f"force close: {elapsed:.0f}s since case open"
        best_sid = min(quoting, key=lambda s: quoting[s]["c_now"])

        def fires(pred):  # single thread: that thread; an RFQ: the best thread, or every thread
            return pred(quoting[best_sid]) or (rfq and all(pred(st) for st in quoting.values()))

        if fires(lambda st: st["status"] == "agreed"):
            return "the supplier agreed to our terms"
        if fires(lambda st: st["status"] == "final_offer"):
            return "final offer"
        if fires(lambda st: st["intel"].walk_risk >= WALK_RISK_CLOSE):
            i = quoting[best_sid]["intel"]
            return f"walk risk {i.walk_risk:.2f} ({', '.join(i.walk_signals)})"
        if fires(lambda st: st["left"] <= 1):
            return "rounds left <= 1"
        if fires(lambda st: st["meets"]):
            return "their offer meets our target"
        if fires(lambda st: st["intel"].unchanged_rounds >= 2 and st.get("our_step") == 0):
            return "deadlock: neither side moved for two rounds"
        if elapsed + self._round_time() > FORCE_CLOSE_AT:
            return f"no time for another round ({elapsed:.0f}s + {self._round_time():.0f}s > {FORCE_CLOSE_AT:.0f}s)"
        return None

    def _round_time(self):
        seen = [min(x, MAX_REPLY_S) for x in self.latencies[-3:]]
        return (max(seen) if seen else DEFAULT_REPLY_S) + ROUND_MARGIN_S

    def _close(self, case, threads, quoting):
        best = self._best_offer(case, threads, quoting)
        if best is None:
            return self._walk(live_threads(threads)[0])
        c, sid, sheet_id = best
        std = standard_terms(threads[sid])
        terms = dict(next(t for i, t in offers(threads[sid]) if i == sheet_id))
        self._closing = {
            "supplier_id": sid,
            "sheet_id": sheet_id,
            "c_est": round(c, 2),
            "c": total_cost(case, terms, std),
            "closing_terms": closing_terms(case, terms, std),
            "missing_fields": latest(threads[sid]).get("missing_fields") or [],
            "c_alternative": strategy.c_alternative(case),
        }
        return {"supplier_id": sid, "message": {"action": {"type": "accept", "term_sheet_id": sheet_id}}}

    def _walk(self, sid):
        self._closing = {"walk_away": True, "c_alternative": strategy.c_alternative(self.case)}
        return {"supplier_id": sid, "message": {"action": {"type": "walk_away"}}}

    def _late(self, case, threads, quoting, rec, record, elapsed):
        """After NO_NEW_COUNTER_AT: no new counter. Lock in a better prose offer if a round still fits,
        repeat our last offer only when nothing on the table is acceptable (nothing to lose), else close."""
        fits = elapsed + self._round_time() <= FORCE_CLOSE_AT
        best = self._best_offer(case, threads, quoting)
        locks = [(lk["c_est"], s, lk) for s, st in quoting.items() if (lk := self._lock_in(case, st))]
        lock = min(locks, key=lambda x: x[0]) if locks else None
        if fits and lock and (best is None or lock[0] < best[0] - 0.01):
            _, sid, lk = lock
            move = self._move(
                case,
                quoting[sid],
                [("their prose offer", lk["terms"])],
                quoting,
                "appreciation_close",
                "lock in the offer they stated in prose",
                hold=False,
                lock=lk,
            )
            return self._finish(rec, record, self._write_all(case, threads, [move], rec), None, [move])
        if fits and best is None:
            moves = [
                self._move(
                    case,
                    quoting[s],
                    self.last_sheets[s],
                    quoting,
                    "firm_disappointment",
                    "nothing acceptable yet: repeat our last offer",
                    hold=True,
                )
                for s in quoting
                if self.last_sheets.get(s) and quoting[s]["left"] > 1
            ]
            if moves:
                return self._finish(rec, record, self._write_all(case, threads, moves, rec), None, moves)
        return self._finish(rec, record, [self._close(case, threads, quoting)], f"no new counters after {elapsed:.0f}s")

    def _lock_in(self, case, st):
        """The supplier's best offer stated only in prose, if it beats its standing sheet: {terms, c_est}."""
        best = None
        for po in st["intel"].prose_offers:
            if po.get("in_sheet") or not po.get("must_haves_ok"):
                continue
            terms = complete_offer(case, po["terms"])
            c = self._c(case, terms, st)
            if c is not None and c < st["c_now"] - 0.01 and self._acceptable(case, terms, st["std"], st["est"]):
                if best is None or c < best["c_est"]:
                    best = {"terms": terms, "c_est": c, "quote": po.get("quote")}
        return best

    def _offer(self, case, st, quoting, rfq, elapsed):
        sid, plan, profile = st["sid"], self.plans[st["sid"]], self.profiles.get(st["sid"], {})
        levers = list(dict.fromkeys(profile.get("levers", []) + strategy.TYPE_LEVERS.get(case.get("type"), [])))
        est, price = st["est"], st["terms"]["unit_price"]
        floor = est.full_cost * 0.97 if est.full_cost and est.source != strategy.OPENING_GUESS else price * 0.6
        held = st.get("our_step") is not None and st["our_step"] <= 0
        sheets = []
        if held and self.last_sheets.get(sid):
            sheets = list(self.last_sheets[sid])  # we hold: the same offer again, word for word
        else:
            sheets = strategy.sheets(case, st["std"], est, st["terms"], st["target"], levers, floor)
        sheets = self._guard_sheets(case, st, sheets)
        if not sheets:
            combo = strategy.base_combo(case, st["terms"], st["std"])
            p = strategy.price_at(case, combo, st["std"], est, st["target"], max(0.01, floor), price)
            t = complete_offer(case, {**combo, "unit_price": p}) if p else None
            sheets = self._guard_sheets(case, st, [("their terms", t)] if t else [])
        if not sheets and self.last_sheets.get(sid):
            sheets = list(self.last_sheets[sid])
        lock = self._lock_in(case, st)
        late = elapsed + 2 * self._round_time() > NO_NEW_COUNTER_AT
        pressure = st["left"] <= 2 or st["intel"].walk_risk >= 0.5 or late
        if lock and (pressure or lock["c_est"] <= st["target"] * 1.0005):
            sheets = [("their prose offer", lock["terms"]), *[s for s in sheets if s[1] != lock["terms"]]][:3]
        plan.targets.append(st["target"])
        name, reason = self._pick(case, st, quoting, rfq, sheets)
        return self._move(case, st, sheets, quoting, name, reason, hold=held, lock=lock)

    def _guard_sheets(self, case, st, sheets):
        """Code's last word on the numbers: every sheet costs us at most the guarded target, is better
        for us than their standing offer, is not one of their sheets, and keeps the must-haves."""
        theirs = [complete_offer(case, {**st["std"], **t}) for _, t in offers(st["events"])]
        out = []
        for label, t in sheets:
            c = self._c(case, t, st)
            if c is None or c > st["target"] + max(0.01, 1e-5 * st["target"]) or c >= st["c_now"]:
                continue
            if t in theirs or not must_haves_ok(case, t, st["std"]):
                continue
            out.append((label, t))
        return out

    def _pick(self, case, st, quoting, rfq, sheets):
        sid, i = st["sid"], st["intel"]
        rivals = {o["sheet_id"]: o["terms"]["unit_price"] for s2, o in quoting.items() if s2 != sid}
        args = self._args(case, st, rivals)
        ctx = {
            "first": not self.plans[sid].targets[:-1],
            "rfq": rfq,
            "rival_cheaper": any(o["c_now"] < st["c_now"] for s2, o in quoting.items() if s2 != sid)
            or any(a.kind == "rival" for a in args),
            "cash_short": self.profiles.get(sid, {}).get("cash_short", False),
            "gap_pct": st.get("gap_pct"),
            "cost_gap_pct": i.cost_gap_pct,
            "data_claims": sum(1 for a in args if a.ids and a.kind != "news"),
            "relationship": i.relationship_style == "relationship",
            "asked_for_terms": bool(i.lever_hints),
        }
        return tactics.pick(i, ctx)

    def _args(self, case, st, rivals):
        sid, profile = st["sid"], self.profiles.get(st["sid"], {})
        args = cl.candidates(self.package, case, sid, their_price=st["terms"]["unit_price"], rivals=rivals)
        keys = {a.key for a in args}
        args += [c for c in st["intel"].new_claims if c.key not in keys]
        ranking = profile.get("claim_ranking") or []
        for a in args:
            if a.key in ranking and a.kind == "news":
                a.favourable = True
        args = [a for a in cl.usable(args, self.ledger, sid) if self.ledger.results.get(a.key) != "contradicted"]
        args.sort(key=lambda a: (ranking.index(a.key) if a.key in ranking else len(ranking), -a.strength))
        return args

    def _move(self, case, st, sheets, quoting, tactic, reason, hold, lock=None):
        sid = st["sid"]
        rivals = {o["sheet_id"]: o["terms"]["unit_price"] for s2, o in quoting.items() if s2 != sid}
        self.last_sheets[sid] = list(sheets)
        self.tactic_log.setdefault(sid, []).append(tactic)
        return {
            "st": st,
            "sheets": sheets,
            "args": self._args(case, st, rivals),
            "tactic": tactic,
            "reason": reason,
            "hold": hold,
            "lock": lock,
        }

    # --- the e-mails --------------------------------------------------------------------------------
    def _write_all(self, case, threads, moves, rec):
        futures = [self.pool.submit(self._write, case, threads, m, rec) for m in moves]
        deadline, out = time.monotonic() + WRITE_TIMEOUT_S + 2, []
        for m, f in zip(moves, futures, strict=True):
            try:
                out.append(f.result(timeout=max(0.1, deadline - time.monotonic())))
            except Exception as e:  # late or broken: code writes the e-mail and keeps the clock
                log.warning("writing to %s failed (%s) - code e-mail", m["st"]["sid"], e)
                error = f"{type(e).__name__}: {e}"
                out.append(self._compose(case, threads, m, self._code_parts(case, m), rec, error=error))
        return out

    def _code_parts(self, case, move):
        t = tactics.TACTICS[move["tactic"]]
        return {
            "salutation": "",
            "opening": t.opening,
            "claims": None,
            "bridge": t.bridge,
            "question": (self._questions(case, move["st"]["events"]) or [""])[0],
            "closing": t.closing,
            "fallback": True,
        }

    def _questions(self, case, events):
        ours = " ".join(m.get("body", "") for m in events if m.get("from") == "buyer").lower()
        got = " ".join(a.get("name", "") for m in events for a in m.get("attachments") or []).lower()
        qs = [BREAKDOWN_Q] if "breakdown" not in ours and "breakdown" not in got else []
        if (q := QUESTIONS.get(case.get("type"))) and q[:40].lower() not in ours:
            qs.append(q)
        return qs

    def _write(self, case, threads, move, rec):
        if self.complete is None:
            return self._compose(case, threads, move, self._code_parts(case, move), rec)
        st, events = move["st"], threads[move["st"]["sid"]]
        last, ours, i = latest(events), [m for m in events if m.get("from") == "buyer"], st["intel"]
        t = tactics.TACTICS[move["tactic"]]
        labelled = [(label, strategy.summarise_terms(case, s)) for label, s in move["sheets"]]
        view = {
            "status": i.status,
            "tone": i.tone,
            "relationship_style": i.relationship_style,
            "constraints": i.constraints,
            "lever_hints": i.lever_hints,
            "their_questions": i.questions,
            "we_still_miss": i.missing_info,
            "clues_from_our_files": [c["text"] for c in i.clues[:4]],
        }
        stance = (
            "This is our first reply to their quote: we open ambitiously; give reasons, not apologies."
            if not ours
            else "We HOLD our last offer: say so plainly and give one reason."
            if move["hold"]
            else "We make a step: make clear it costs us and that we expect a step back."
        )
        if move.get("lock") and any(label == "their prose offer" for label, _ in move["sheets"]):
            stance += " One option takes up the offer they stated in their own e-mail; name it as theirs."
        user = "\n\n".join(
            x
            for x in [
                f"CASE: {case['title']} ({case.get('type')}). Supplier: {self._company(st['sid'], events)}. "
                f"Our stance: {GOAL_WORDS.get(case['buyer'].get('category_goal', 'routine'))}.",
                f"TACTIC: {t.name} - {t.style}",
                f"WHAT OUR ANALYST SEES: {json.dumps(view, ensure_ascii=False, default=str)}",
                f"THEIR LAST E-MAIL (from {last.get('from')}, status {last.get('status')}):\n"
                f"{(last.get('body') or '')[:2500]}",
                "OUR EARLIER E-MAILS:\n" + "\n---\n".join((m.get("body") or "")[:600] for m in ours[-2:])
                if ours
                else "",
                f"THE MOVE: {stance} The options (code inserts them after your bridge): "
                + "; ".join(f"[{label}] {text}" for label, text in labelled),
                "ARGUMENTS ([key] sentence - note; answer with the keys only):\n"
                + ("\n".join(f"[{a.key}] {a.sentence} - {a.note}" for a in move["args"][:6]) or "(none)"),
                "QUESTIONS (pick one or none):\n" + "\n".join(self._questions(case, events)),
            ]
            if x
        )
        t0, reply, parsed, error = time.monotonic(), None, None, None
        try:
            reply = self.complete(writing.SYSTEM + f"\n\nWording style for this e-mail ({t.name}): {t.style}", user)
            parsed = writing.parse(reply)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            parsed = self._code_parts(case, move)
        return self._compose(
            case, threads, move, parsed, rec, prompt=user, reply=reply, error=error, seconds=time.monotonic() - t0
        )

    def _code_sentences(self, case, move):
        """Sentences code writes in full: the cost-share arithmetic and our payment-term position."""
        st, sid = move["st"], move["st"]["sid"]
        said = self.said.setdefault(sid, set())
        first = not any(m.get("from") == "buyer" for m in st["events"])
        analysis, position = [], []
        again = move["tactic"] == "data_argument" and "gap" not in said
        if (s := weighted_sentence(st["intel"])) and (first or again):
            analysis.append(s)
            said.add("gap")
        ref = case["buyer"].get("reference_payment_days")
        trade = first or move["tactic"] == "lever_trade"
        if "payment_days" in case["negotiable"] and ref and trade and "pay" not in said:
            position.append(
                f"Our standard payment term is {ref} days net: paying earlier ties up our working "
                "capital, so an option with faster payment carries a lower price."
            )
            days = [(n, t.get("payment_days", ref)) for n, (_, t) in enumerate(move["sheets"])]
            fast = [(n, d) for n, d in days if d < ref]
            if fast and len(move["sheets"]) > 1:
                n, d = min(fast, key=lambda x: x[1])
                position.append(f"If quick payment matters to you, Option {'ABC'[n]} pays in {d} days.")
            said.add("pay")
        if move["hold"]:
            position.append("We keep our offer as it stands.")
        elif move.get("lock") and any(label == "their prose offer" for label, _ in move["sheets"]):
            position.append(
                ("Option A takes up" if len(move["sheets"]) > 1 else "This takes up")
                + " the offer you described in your e-mail."
            )
        return analysis, position

    def _pick_claims(self, parts, move):
        by_key = {a.key: a for a in move["args"]}
        if not parts.get("fallback"):
            keys = parts.get("claims") or []
        else:  # code picks: the data tactics carry more arguments
            n = 2 if move["tactic"] in ("data_argument", "competitive_pressure") else 1
            kinds = ("rival",) if move["tactic"] == "competitive_pressure" else None
            keys = [a.key for a in move["args"] if a.ids and a.kind != "news" and (not kinds or a.kind in kinds)][:n]
        picked = []
        for k in keys:
            if (c := writing.match_key(k, by_key)) and c not in picked and len(picked) < 3:
                picked.append(c)
        return picked

    def _compose(self, case, threads, move, parts, rec, prompt=None, reply=None, error=None, seconds=0.0):
        st = move["st"]
        sid, events = st["sid"], threads[st["sid"]]
        labelled = [(label, strategy.summarise_terms(case, t)) for label, t in move["sheets"]]
        company = self._company(sid, events)
        material = self._material(case, events)
        clauses = " ".join(r.get("text", "") for r in self.dossiers.get(sid, {}).get("clauses", []))
        picked = self._pick_claims(parts, move)
        analysis, position = self._code_sentences(case, move)
        numbers = writing.allowed(
            material, clauses, *(a.sentence for a in move["args"]), *(t for _, t in labelled), *analysis, *position
        )
        dropped = []
        clean = {k: writing.lint(parts.get(k), numbers, dropped) for k in ("opening", "bridge", "question", "closing")}
        args = " ".join(c.sentence + "".join(" " + e for e in c.extra) for c in picked)
        hello = writing.salutation(parts.get("salutation"), company, material) if company else "Dear colleagues,"
        body = "\n\n".join(
            p
            for p in [
                hello,
                clean["opening"],
                " ".join(x for x in (*analysis, args) if x),
                " ".join(x for x in (clean["bridge"], *position) if x),
                writing.options_paragraph(labelled),
                clean["question"],
                clean["closing"],
                writing.SIGNATURE,
            ]
            if p
        )
        for a in picked:
            self.ledger.use(a, sid)
        self.sent_claims[sid] = [a for a in picked if a.ids]
        refs = [i for a in picked for i in a.ids][:20]
        msg = {
            "subject": writing.subject(case, events),
            "body": body[:8000],
            "term_sheets": [{"terms": t} for _, t in move["sheets"]],
        }
        if refs:
            msg["refs"] = refs
        move["sent_sheets"] = [
            {"label": label, "terms": t, "c_est": self._c(case, t, st), "c": total_cost(case, t, st["std"])}
            for label, t in move["sheets"]
        ]
        if rec:
            rec.llm(
                kind="write",
                supplier_id=sid,
                tactic=move["tactic"],
                seconds=round(seconds, 2),
                prompt=prompt,
                reply=reply,
                parsed=parts,
                error=error,
                dropped=dropped,
                sent=msg,
            )
        return {"supplier_id": sid, "message": msg}

    def _material(self, case, events):
        parts = [m.get("body", "") for m in events if m.get("from") != "buyer"]
        if self.package and case.get("trigger_doc"):
            try:
                parts.append(self.package.document(case["trigger_doc"]))
            except (FileNotFoundError, ValueError, KeyError):
                pass
        return "\n".join(parts)

    def _company(self, sid, events):
        if name := (self.dossiers.get(sid, {}).get("supplier") or [{}])[0].get("name"):
            return name
        for m in events:
            if m.get("from") != "buyer" and (found := re.search(r"Quote from (.+?):", m.get("subject") or "")):
                return found.group(1).strip()
        return None

    # --- the log ------------------------------------------------------------------------------------
    def _finish(self, rec, record, decisions, close_reason, moves=()):
        record["close_reason"] = close_reason
        record["seconds"] = round(self.clock() - self.t_open - record["elapsed_s"], 2)
        if close_reason:
            record["closing"] = getattr(self, "_closing", None)
            self.closed = {"reason": close_reason, "elapsed_s": record["elapsed_s"], **(record["closing"] or {})}
            log.info("%s: close (%s) -> %s", self.case_id, close_reason, decisions[0]["message"]["action"])
        record["moves"] = [
            {
                "supplier_id": m["st"]["sid"],
                "tactic": m["tactic"],
                "reason": m["reason"],
                "hold": m["hold"],
                "target_c": round(m["st"]["target"], 2),
                "c_now": round(m["st"]["c_now"], 2),
                "their_step": round(m["st"].get("their_step") or 0, 2),
                "our_step": m["st"].get("our_step"),
                "lock_in": m.get("lock"),
                "sheets": m.get("sent_sheets"),
            }
            for m in moves
        ]
        if rec:
            rec.agent_round(**record)
            notes = [f"{m['supplier_id']}: tactic {m['tactic']} ({m['reason']})" for m in record["moves"]]
            if close_reason:
                notes.append(f"close: {close_reason}")
            rec.agent_note(f"DualAgent at {record['elapsed_s']:.0f}s - " + "; ".join(notes))
        return decisions

    def summary(self):
        """Tactic sequence per thread and how the case closed, for reports."""
        return {"tactics": dict(self.tactic_log), "closed": getattr(self, "closed", None)}
