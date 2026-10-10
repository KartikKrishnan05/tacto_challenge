"""The buyer: reads the supplier before the case opens, lets code decide the numbers, lets the model
write the e-mail, and checks the e-mail before it goes out.

  prime(case)        before the session opens (no clock yet): a dossier per supplier from the whole
                     data package (`dossier`), and one model call that turns it into a profile -
                     who sits at the desk, what presses it, which terms it values, what to avoid,
                     which data arguments will land.
  decide_all(...)    every round: code decides accept / walk / offer and builds the term sheets
                     (`strategy`); the model writes each e-mail from the profile, the supplier's
                     last e-mail and the checked arguments (`claims`); code assembles and lints it
                     (`writing`). In an RFQ every live thread gets its e-mail at once, so no thread
                     idles into the 60-second rule.

If anything breaks, the case is not lost: a failed model call gets a plain e-mail written by code,
and a failure in the planning falls back to the naive move.
"""

import json
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .. import claims as cl
from .. import dossier, runlog, strategy, writing
from ..cost import must_haves_ok, total_cost
from ..docs import standard_terms
from ..play import latest, live_threads, moves_left, offers
from .naive import NaiveAgent

log = logging.getLogger("arena")
WRITE_TIMEOUT_S = 35  # the model's share of the 60 seconds per message
PROFILE_TIMEOUT_S = 90

GOAL_WORDS = {
    "stabilise": "a strategic supplier: we want a healthy, reliable partner and stable prices, not the last cent",
    "secure_supply": "a bottleneck part: security of supply matters most; a supplier that is squeezed is a risk",
    "quality_first": "quality first: defects and late deliveries cost us more than a little price",
    "leverage": "leverage: competition and data should bring the price down",
    "routine": "a routine part: a fair market price with little effort",
}

PROFILE_SYSTEM = """You are a senior procurement analyst at Hollerau Pumpenwerke, a German pump maker.
Before a negotiation by e-mail, read everything our data says about one supplier and write a short
profile for the buyer. Read between the lines: call and meeting notes, letters, past negotiations
(what it asked vs what it settled), its finances, how we pay it, its scorecards, the cost drivers.
Some notes are just noise.

Return ONE JSON object and nothing else:
{"persona": "who is at the desk and how they think (1-2 sentences)",
 "tone": "how to write to them (e.g. personal and appreciative; crisp and factual; ...)",
 "pressures": ["what presses the supplier right now"],
 "wants": ["what it values beyond price"],
 "levers": ["negotiable field names, most valuable to the SUPPLIER first"],
 "avoid": ["arguments or wording that would backfire with this supplier"],
 "cash_short": true or false,
 "claim_ranking": ["keys from ARGUMENTS, most convincing for this supplier first; leave out what would backfire"],
 "questions": ["1-3 questions worth asking it"],
 "angle": "the line of argument for the first e-mail, in one or two sentences"}"""


def _goal(case):
    return case["buyer"].get("category_goal", "routine")


class BuyerAgent:
    def __init__(self, complete=None, package=None, prep_complete=None):
        self.complete, self.package = complete, package
        self.prep_complete = prep_complete or complete
        self.fallback = NaiveAgent()
        self.case_id = None

    # --- before the clock -------------------------------------------------------------------
    def prime(self, case):
        """Dossiers and profiles for every supplier of the case. Safe to call again."""
        if case["case_id"] == self.case_id:
            return
        self.case_id, self.case = case["case_id"], case
        self.dossiers, self.profiles, self.plans = {}, {}, {}
        self.ledger, self.sent_claims, self.estimates = cl.Ledger(), {}, {}
        rec = runlog.current()
        t0 = time.monotonic()
        for sid in case["suppliers"]:
            self.dossiers[sid] = dossier.gather(self.package, case, sid)
            self.plans[sid] = strategy.Plan()
        pool = ThreadPoolExecutor(max_workers=max(1, len(case["suppliers"])))
        futures = {sid: pool.submit(self._profile, case, sid, rec) for sid in case["suppliers"]}
        deadline = time.monotonic() + PROFILE_TIMEOUT_S
        for sid, f in futures.items():
            try:
                self.profiles[sid] = f.result(timeout=max(0.1, deadline - time.monotonic()))
            except Exception as e:  # no profile is no reason to lose the case
                log.warning("profile for %s failed (%s) - using defaults", sid, e)
                self.profiles[sid] = self._default_profile(case, sid)
        pool.shutdown(wait=False)  # a hung model call must not hold the case
        log.info("%s primed in %.1fs", case["case_id"], time.monotonic() - t0)

    def _default_profile(self, case, sid):
        d = self.dossiers[sid]
        return {
            "persona": "",
            "tone": "polite, factual, constructive",
            "pressures": [],
            "wants": [],
            "levers": list(
                dict.fromkeys(
                    (["payment_days"] if d.get("cash_signals") else []) + strategy.TYPE_LEVERS.get(case.get("type"), [])
                )
            ),
            "avoid": [],
            "cash_short": bool(d.get("cash_signals")),
            "claim_ranking": [],
            "questions": [],
            "angle": "",
        }

    def _profile(self, case, sid, rec):
        if self.prep_complete is None:
            return self._default_profile(case, sid)
        args = cl.candidates(self.package, case, sid)
        brief = {
            "title": case["title"],
            "type": case.get("type"),
            "goal": GOAL_WORDS.get(_goal(case)),
            "parts": case.get("parts"),
            "negotiable": case["negotiable"],
            "term_options": case.get("term_options"),
            "need_per_year": case["buyer"].get("need_per_year"),
        }
        user = (
            f"CASE\n{json.dumps(brief, ensure_ascii=False, default=str)}\n\n{dossier.render(self.dossiers[sid])}\n\n"
            "ARGUMENTS ([key] sentence - note)\n" + "\n".join(f"[{a.key}] {a.sentence} - {a.note}" for a in args)
        )
        t0 = time.monotonic()
        reply = parsed = error = None
        try:
            with runlog.bind(rec):
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
        fields = {f: f for f in case["negotiable"] if f != "unit_price"}  # models write "payment_days - why"
        profile["levers"] = list(
            dict.fromkeys(f for t in profile.get("levers") or [] if (f := writing.match_key(t, fields)))
        )
        keys = {a.key: a.key for a in args}
        profile["claim_ranking"] = [k for t in profile.get("claim_ranking") or [] if (k := writing.match_key(t, keys))]
        profile["cash_short"] = bool(profile.get("cash_short")) or bool(
            self.dossiers[sid].get("cash_signals") and "payment_days" in profile["levers"][:1]
        )
        return profile

    # --- every round ----------------------------------------------------------------------
    def decide(self, case, threads):
        return self.decide_all(case, threads)[0]

    def decide_all(self, case, threads):
        rec = runlog.current()
        try:
            self.prime(case)
            moves = self._plan(case, threads, rec)
        except Exception as e:
            log.exception("planning failed (%s) - falling back to the naive move", e)
            if rec:
                rec.flag = "fell back to naive"
            return [self.fallback.decide(case, threads)]
        return moves

    def _state(self, case, sid, events):
        std = standard_terms(events)
        opening = next(
            (s["terms"] for m in events if m.get("from") != "buyer" for s in m.get("term_sheets") or []), None
        )
        est = strategy.estimate(
            case, sid, self.package, events, opening, cash_short=self.profiles.get(sid, {}).get("cash_short", False)
        )
        self.estimates[sid] = est
        # A supplier may offer alternatives next to its standing sheet: take the one that is best for us.
        choices = [(i, t) for i, t in offers(events) if t.get("unit_price")]
        sheet_id, terms = min(
            choices,
            key=lambda c: (not must_haves_ok(case, c[1], std), strategy.cost(case, c[1], std, est)),
            default=(None, {}),
        )
        c_now = strategy.cost(case, terms, std, est) if sheet_id else math.inf
        return {
            "sid": sid,
            "events": events,
            "sheet_id": sheet_id,
            "terms": terms,
            "std": std,
            "est": est,
            "c_now": c_now,
            "status": latest(events).get("status"),
            "left": moves_left(case, events),
            "ok": bool(sheet_id) and strategy.acceptable(case, terms, std),
        }

    def _record_feedback(self, sid, events):
        """Which of our last arguments checked out."""
        results = latest(events).get("claim_results") or []
        for r in results:
            sent = self.sent_claims.get(sid, [])
            if r.get("index") is not None and r["index"] < len(sent):
                self.ledger.results[sent[r["index"]].key] = r.get("result")
                if r.get("result") == "contradicted":
                    log.warning("%s: argument %s was contradicted", sid, sent[r["index"]].key)

    def _plan(self, case, threads, rec):
        live = live_threads(threads)
        states = {s: self._state(case, s, threads[s]) for s in live}
        quoting = {s: st for s, st in states.items() if st["sheet_id"] and st["terms"].get("unit_price")}
        if not quoting:
            return [{"supplier_id": live[0], "message": {"action": {"type": "walk_away"}}}]
        for s in quoting:
            self._record_feedback(s, threads[s])
        rfq = len(case["suppliers"]) > 1
        horizon = 5 if rfq else strategy.ROUND_HORIZON

        # Targets: where each thread's next offer should land, in C.
        best_c = min(st["c_now"] for st in quoting.values())
        c_alt = strategy.c_alternative(case)
        for s, st in quoting.items():
            plan = self.plans.setdefault(s, strategy.Plan())
            # The price is negotiated on the best structure for both sides, not on theirs as quoted.
            price = st["terms"]["unit_price"]
            ref = strategy.mutual_combo(case, st["terms"], st["std"], st["est"])
            c_ref = min(strategy.cost(case, {**ref, "unit_price": price}, st["std"], st["est"]), st["c_now"])
            replies = sum(m.get("from") != "buyer" for m in threads[s])
            if replies > plan.replies:  # one entry per reply - a supplier that holds shows a zero step
                plan.their_costs.append(c_ref)
                plan.replies = replies
            if plan.anchor is None:
                # In an RFQ every bidder is anchored to the best quote, not to its own (D02: anchored
                # to its own 17.10, the dearer bidder agreed to 15.09 at once; about 13 was reachable).
                rivals = [o["terms"]["unit_price"] for o2, o in quoting.items() if o2 != s and rfq]
                # Priced on our reference terms: what the supplier asks beyond them is not given away free.
                combo = strategy.reference_combo(case, ref, st["std"], st["est"], price)
                p = strategy.anchor_price(case, st["terms"], st["est"], min([price, *rivals]), combo, st["std"])
                plan.anchor = min(strategy.cost(case, {**combo, "unit_price": p}, st["std"], st["est"]), c_ref)
            k = len(plan.targets)
            target = plan.anchor + strategy.concession(k, horizon) * (c_ref - plan.anchor)
            # How far they came toward us since our last e-mail, in C. Still conceding (P01: 2.25 EUR
            # in one reply) means their offer is not their last word.
            step = plan.their_costs[-2] - plan.their_costs[-1] if len(plan.their_costs) > 1 else 0.0
            st["moving"] = step > strategy.MIN_STEP * c_ref
            st["our_step"] = None
            if k:
                # Reciprocity, visibly: when they move, we move - by less than they did (D01, 10 Oct:
                # holding after their big step got "no reciprocal movement" and a final offer); when
                # they hold, we hold once, then make one visible step to break the deadlock. Never a
                # token step (D01: "a nominal adjustment of one cent").
                prev, visible = plan.targets[-1], strategy.MIN_STEP * c_ref
                planned = max(target, prev) - prev
                if step > 0.001 * c_ref:
                    ours = max(min(planned, strategy.RECIPROCITY * step), visible)
                elif plan.holds >= 1:
                    ours = visible
                else:
                    ours = 0.0
                target = prev + ours
                plan.holds = plan.holds + 1 if ours <= 0 else 0
                st["our_step"] = ours
            if st["moving"] and target >= c_ref:
                target = c_ref - 0.5 * step  # their offer meets ours, but they are still moving: ask again
            others = [o["c_now"] for o2, o in quoting.items() if o2 != s]
            if rfq and others:
                target = max(plan.anchor, min(target, min(others) * 0.99))  # beat the best rival
            st["target"] = min(target, c_alt * 0.995)  # never offer what loses to our alternative

        # Close: accept the best standing term sheet, or walk away to the alternative.
        ok = {s: st for s, st in quoting.items() if st["ok"]}
        best = min(ok.values(), key=lambda st: st["c_now"]) if ok else None
        all_final = all(st["status"] in ("final_offer", "agreed") for st in quoting.values())
        if best:
            reason = None
            if best["status"] == "agreed":
                reason = "the supplier agreed to our terms"
            elif best["status"] == "final_offer" and (not rfq or all_final or best["c_now"] <= best_c):
                reason = "final offer that beats our alternative"
            elif best["c_now"] <= best["target"] * 1.002 and not (best["moving"] and best["left"] > 3):
                reason = "their offer meets our next target"
            elif self.plans[best["sid"]].targets and best["c_now"] <= self.plans[best["sid"]].targets[-1] * 1.001:
                reason = "their offer is as good as what we last offered"
            elif best["left"] <= 1 or all(st["left"] <= 1 for st in quoting.values()):
                reason = "no rounds left"
            elif all(len(self.plans[s].targets) >= horizon + 2 for s in quoting):
                reason = "the planned rounds are over"
            if reason:
                log.info("accept %s (%s): C %.0f", best["sheet_id"], reason, best["c_now"])
                self._note(rec, f"accept: {reason}")
                return [
                    {
                        "supplier_id": best["sid"],
                        "message": {"action": {"type": "accept", "term_sheet_id": best["sheet_id"]}},
                    }
                ]
        elif all_final or all(st["left"] <= 1 for st in quoting.values()):
            self._note(rec, "walk away: nothing beats our alternative")
            return [{"supplier_id": next(iter(quoting)), "message": {"action": {"type": "walk_away"}}}]

        # Offer: every thread worth talking to (an RFQ's distant laggards are left to lapse).
        talk = [s for s, st in quoting.items() if st["left"] > 0 and st["status"] not in ("final_offer",)]
        if rfq and len(talk) > 2:
            talk = [s for s in talk if quoting[s]["c_now"] <= best_c * 1.25 or len(self.plans[s].targets) < 2]
        if not talk:
            if best:
                return [
                    {
                        "supplier_id": best["sid"],
                        "message": {"action": {"type": "accept", "term_sheet_id": best["sheet_id"]}},
                    }
                ]
            return [{"supplier_id": next(iter(quoting)), "message": {"action": {"type": "walk_away"}}}]
        moves = [self._offer(case, threads, quoting[s], quoting) for s in talk]
        return self._write_all(case, threads, moves, rec)

    def _offer(self, case, threads, st, quoting):
        profile = self.profiles.get(st["sid"], {})
        levers = list(dict.fromkeys(profile.get("levers", []) + strategy.TYPE_LEVERS.get(case.get("type"), [])))
        est, price = st["est"], st["terms"]["unit_price"]
        floor = est.full_cost * 0.97 if est.full_cost and est.source != strategy.OPENING_GUESS else price * 0.6
        asks = strategy.supplier_asks(case, st["terms"], st["std"], est)
        sheets = strategy.sheets(case, st["std"], est, st["terms"], st["target"], levers, floor, asks)
        if not sheets:  # the target is out of reach (their floor): a small step on their terms
            t = strategy.complete_offer(
                case,
                {**strategy.base_combo(case, st["terms"], st["std"]), "unit_price": round(max(floor, price * 0.99), 2)},
            )
            if must_haves_ok(case, t, st["std"]):
                sheets = [("their terms", t)]
        self.plans[st["sid"]].targets.append(st["target"])
        rivals = {o["sheet_id"]: o["terms"]["unit_price"] for s2, o in quoting.items() if s2 != st["sid"]}
        args = cl.candidates(self.package, case, st["sid"], their_price=price, rivals=rivals)
        ranking = profile.get("claim_ranking") or []
        for a in args:
            if a.key in ranking and a.kind == "news":
                a.favourable = True  # the profile judged this news helpful
        args = [a for a in cl.usable(args, self.ledger, st["sid"]) if self.ledger.results.get(a.key) != "contradicted"]
        args.sort(key=lambda a: ranking.index(a.key) if a.key in ranking else len(ranking))
        held = st.get("our_step") is not None and st["our_step"] <= 0
        return {"st": st, "sheets": sheets, "args": args, "profile": profile, "hold": held}

    # --- the e-mails ----------------------------------------------------------------------
    def _write_all(self, case, threads, moves, rec):
        pool = ThreadPoolExecutor(max_workers=len(moves))
        futures = [pool.submit(self._write, case, threads, m, rec) for m in moves]
        deadline, out = time.monotonic() + WRITE_TIMEOUT_S, []
        for m, f in zip(moves, futures, strict=True):
            try:
                out.append(f.result(timeout=max(0.1, deadline - time.monotonic())))
            except Exception as e:  # late or broken: a plain e-mail by code keeps the clock
                log.warning("writing to %s failed (%s) - plain e-mail", m["st"]["sid"], e)
                f.cancel()
                out.append(
                    self._compose(case, threads, m, writing.fallback_parts(), rec, error=f"{type(e).__name__}: {e}")
                )
        pool.shutdown(wait=False)
        return out

    def _questions(self, case, events):
        ours = " ".join(m.get("body", "") for m in events if m.get("from") == "buyer").lower()
        got = " ".join(a.get("name", "") for m in events for a in m.get("attachments") or []).lower()
        qs = []
        if "breakdown" not in ours and "breakdown" not in got:
            qs.append("Could you share a cost breakdown for this part - material, conversion, overhead?")
        by_type = {
            "quality": "Could you send us the 8D report with root cause and containment for the recent complaints?",
            "capacity": "Could you send a delivery schedule with the monthly quantities you can commit to?",
            "rfq": "What is the shortest lead time you could commit to, and would you suggest a cheaper design "
            "or material variant?",
            "price_increase": "Which cost elements of this part have actually risen since our last price agreement?",
            "renewal": "Which volume or contract length would let you improve the price further?",
            "bundling": "Which further parts or volumes would make a bundle more attractive for you?",
            "raw_material": "Which base month and deadband would you consider fair for an index clause?",
        }
        if (q := by_type.get(case.get("type"))) and q.split()[2] not in ours:
            qs.append(q)
        return qs

    @staticmethod
    def _unasked(questions, events):
        """Questions whose gist is not in one of our e-mails yet."""
        ours = " ".join(m.get("body", "") for m in events if m.get("from") == "buyer").lower()
        return [q for q in questions if " ".join(str(q).lower().split()[:6]) not in ours]

    def _material(self, case, events):
        """Text the e-mail may take figures from: the supplier's e-mails and the letter that started it."""
        parts = [m.get("body", "") for m in events if m.get("from") != "buyer"]
        if self.package and case.get("trigger_doc"):
            try:
                parts.append(self.package.document(case["trigger_doc"]))
            except (FileNotFoundError, ValueError):
                pass
        return "\n".join(parts)

    def _write(self, case, threads, move, rec):
        st, events = move["st"], threads[move["st"]["sid"]]
        if self.complete is None:
            return self._compose(case, threads, move, writing.fallback_parts(), rec)
        last = latest(events)
        ours = [m for m in events if m.get("from") == "buyer"]
        plan = self.plans[st["sid"]]
        theirs = plan.their_costs
        moved = (theirs[-2] - theirs[-1]) / theirs[-2] * 100 if len(theirs) > 1 and theirs[-2] else 0.0
        labelled = [(label, strategy.summarise_terms(case, t)) for label, t in move["sheets"]]
        prof = {k: move["profile"].get(k) for k in ("persona", "tone", "pressures", "wants", "avoid", "angle")}
        feedback = {k: v for k, v in self.ledger.results.items()}
        company = self._company(st["sid"], events)
        part = (self.dossiers[st["sid"]].get("parts") or [{}])[0].get("description") or ""
        user = "\n\n".join(
            x
            for x in [
                f"CASE: {case['title']} ({case.get('type')}). Part: {part}. Supplier: {company}. "
                f"Our stance: {GOAL_WORDS.get(_goal(case))}.",
                f"PROFILE: {json.dumps(prof, ensure_ascii=False)}",
                f"THEIR LAST E-MAIL (from {last.get('from')}, status {last.get('status')}, "
                f"subject {last.get('subject')!r}):\n"
                f"{(last.get('body') or '')[:2500]}",
                "\n".join(
                    f"[their attachment {a.get('name')}]\n{(a.get('text') or '')[:1200]}"
                    for a in last.get("attachments") or []
                    if not (a.get("name") or "").startswith("quote")
                ),
                f"HOW OUR ARGUMENTS CHECKED OUT SO FAR: {json.dumps(feedback)}" if feedback else "",
                "OUR EARLIER E-MAILS (oldest first):\n" + "\n---\n".join((m.get("body") or "")[:700] for m in ours[-2:])
                if ours
                else "This is our first reply to their quote.",
                f"THE MOVE: message {len(ours) + 1} in this thread. "
                + (f"Since our last e-mail they moved {moved:.1f}% toward us. " if ours else "")
                + self._stance(move, moved, ours)
                + "The options (code inserts them after your bridge): "
                + "; ".join(f"[{label}] {text}" for label, text in labelled),
                "ARGUMENTS ([key] sentence - note; answer with the keys only):\n"
                + (
                    "\n".join(f"[{a.key}] {a.sentence} - {a.note}" for a in move["args"][:6])
                    or "(none left - write without data arguments)"
                ),
                "QUESTIONS (pick one or none):\n"
                + "\n".join(
                    self._questions(case, events)
                    + self._unasked(list(move["profile"].get("questions") or []), events)[:2]
                ),
            ]
            if x
        )
        t0 = time.monotonic()
        reply = parsed = error = None
        try:
            with runlog.bind(rec):
                reply = self.complete(writing.SYSTEM, user)
            parsed = writing.parse(reply)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            parsed = writing.fallback_parts((self._questions(case, events) or [""])[0])
        return self._compose(
            case, threads, move, parsed, rec, prompt=user, reply=reply, error=error, seconds=time.monotonic() - t0
        )

    def _compose(self, case, threads, move, parts, rec, prompt=None, reply=None, error=None, seconds=0.0):
        st = move["st"]
        events = threads[st["sid"]]
        labelled = [(label, strategy.summarise_terms(case, t)) for label, t in move["sheets"]]
        by_key = {a.key: a for a in move["args"]}
        if parts.get("fallback") and move["args"]:  # even the plain e-mail carries the best arguments
            parts["claims"] = [a.key for a in move["args"] if a.ids][:2]
        material = self._material(case, events)
        clauses = " ".join(r.get("text", "") for r in self.dossiers[st["sid"]].get("clauses", []))
        numbers = writing.allowed(
            material, clauses, *(a.sentence for a in move["args"]), *(text for _, text in labelled)
        )
        company = self._company(st["sid"], events)
        dropped = []
        body, picked = writing.assemble(parts, by_key, labelled, company, material, numbers, dropped)
        for a in picked:
            self.ledger.use(a, st["sid"])
        self.sent_claims[st["sid"]] = [a for a in picked if a.ids]
        refs = [i for a in picked for i in a.ids][:20]
        msg = {
            "subject": writing.subject(case, events),
            "body": body[:8000],
            "term_sheets": [{"terms": t} for _, t in move["sheets"]],
        }
        if refs:
            msg["refs"] = refs
        if rec:
            rec.llm(
                kind="write",
                supplier_id=st["sid"],
                seconds=round(seconds, 2),
                prompt=prompt,
                reply=reply,
                parsed=parts,
                error=error,
                dropped=dropped,
                target_c=round(st["target"], 2),
                their_c=round(st["c_now"], 2),
                estimate=vars(st["est"]),
                sheets=[
                    {"label": label, "terms": t, "c": total_cost(case, t, st["std"])} for label, t in move["sheets"]
                ],
                sent=msg,
            )
        return {"supplier_id": st["sid"], "message": msg}

    @staticmethod
    def _stance(move, moved, ours):
        """What our move means, in words, so the e-mail says it plainly."""
        if not ours:
            return "We open: our options are ambitious; give the reasons, not apologies. "
        if move.get("hold"):
            if moved > 0.2:
                return (
                    "We HOLD our position: they are still coming down, so we do not move. Acknowledge their "
                    "step briefly, then say clearly that their price is still too high and why. "
                )
            return (
                "We HOLD our position because they did not move. Say so politely but firmly; do not thank them "
                "for flexibility they did not show; give one concrete reason or ask what would let them move. "
            )
        return "We make a modest step. Make it clear this costs us and that we expect a step back. "

    def _company(self, sid, events):
        """The supplier's name: from the data, else from its own e-mails (subject "Quote from X:" or
        a signature line "Owner, X") - never the bare ID (D02: "Dear colleagues at sup:DEMO-J1")."""
        if name := (self.dossiers.get(sid, {}).get("supplier") or [{}])[0].get("name"):
            return name
        for m in events:
            if m.get("from") == "buyer":
                continue
            if found := re.search(r"Quote from (.+?):", m.get("subject") or ""):
                return found.group(1).strip()
            for line in reversed((m.get("body") or "").splitlines()):
                if found := re.match(
                    r"^[^,]{2,40},\s*(.+\b(?:GmbH|KG|AG|SE|Ltd|Kft|s\.r\.o|Sp\. z o\.o|S\.A)\.?.*)$", line.strip()
                ):
                    return found.group(1).strip()
        return "your team"

    @staticmethod
    def _note(rec, text):
        if rec:
            rec.flag = text
