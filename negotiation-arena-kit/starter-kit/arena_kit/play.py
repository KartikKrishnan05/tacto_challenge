"""Play cases with an agent against the arena over HTTP - the hosted arena (practice, sandbox,
final) or `arena-sim`, the local contract simulator. Both speak the same API.

An agent is any object with `decide(case, threads) -> {"supplier_id": ..., "message": {...}}`:
  threads: {supplier_id: [message, ...]} - every e-mail so far, both directions, in order;
           supplier attachments carry their text under "text" (see `docs.read_document`).
  message: subject, body, up to 3 term sheets, refs - or an "action" that closes the case:
           {"type": "accept", "term_sheet_id": ...} or {"type": "walk_away"}.
"""

import functools
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import runlog
from .client import ArenaError
from .cost import must_haves_ok, total_cost
from .docs import read_document, standard_terms

log = logging.getLogger("arena")

ENDED = {"walked_away", "no_bid"}
# A message later than this after the supplier's last one counts as a timeout.
MOVE_LIMIT_S = 60
# In the Live Final, close this long before the wave's cap - or the slowest round of the case so far
# (your decision plus a supplier reply of up to about a minute) plus a margin, if that is longer.
CLOSE_BEFORE_CAP_S, ROUND_MARGIN_S = 90, 15
RUNNING = re.compile(r"one session per case in the final: (\S+)")
# Practice: the case list shows every practice case file; only those marked hosted can be played
# (a session on another is a 403). Without the marker every listed case counts as hosted.
HOSTED_FIELD = "hosted"


class RemoteTable:
    """The arena over HTTP: the practice arena, the final, or a hosted sandbox."""

    def __init__(self, client, case=None):
        self.client, self.session_id, self.case, self.done = client, None, case, None

    def open(self, case_id):
        try:
            r = self.client.open_session(case_id)
        except ArenaError as e:
            running = RUNNING.search(str(e.detail)) if e.status == 409 else None
            if not (running and self.case):
                raise
            return self.resume(running.group(1))
        self.session_id = r["session_id"]
        log.info("session %s", self.session_id)
        return r["case"], r["threads"]

    def resume(self, session_id):
        """Pick up a final session this team already started (e.g. after a crash): every thread
        so far, from the arena."""
        self.session_id = session_id
        log.info("resuming session %s", session_id)
        state = self.client.session(session_id)
        if state["status"] == "ended":
            self.done = self.result()
        return self.case, {s: self.client.thread(session_id, s)["messages"] for s in state["threads"]}

    def case_file(self, case_id):
        """The case file before a session is opened (to prepare without the clock), or None."""
        if self.case:
            return self.case
        for case_set in ("practice", "sandbox"):
            try:
                found = next((c for c in self.client.cases(case_set) if c["case_id"] == case_id), None)
            except ArenaError:
                continue
            if found:
                return found
        return None

    def send(self, supplier_id, message):
        return self.client.send_message(self.session_id, supplier_id, message)

    def close(self, supplier_id, message):
        try:
            reply = self.client.send_message(self.session_id, supplier_id, message)
        except ArenaError as e:
            # An accept of a sheet the arena does not hold as standing (P01: a final offer's `-a`
            # alternative) leaves the case open until it times out at no deal. Accept the thread's
            # standing offer instead - the supplier's own sheet from the same e-mail.
            action = message.get("action") or {}
            if e.status != 422 or action.get("type") != "accept":
                raise
            t = self.client.thread(self.session_id, supplier_id)
            if t.get("closed") or not t.get("offer_id") or t["offer_id"] == action.get("term_sheet_id"):
                raise
            log.warning("%s: accept %s refused (%s) - accepting %s", supplier_id, action.get("term_sheet_id"),
                        e.detail, t["offer_id"])  # fmt: skip
            if rec := runlog.current():
                rec.agent_note(
                    f"accept of {action.get('term_sheet_id')} refused ({e.detail}); accepted {t['offer_id']}"
                )
            message = {**message, "action": {"type": "accept", "term_sheet_id": t["offer_id"]}}
            reply = self.client.send_message(self.session_id, supplier_id, message)
        if (rec := runlog.current()) and reply.get("body"):
            rec.received(supplier_id, reply)
        return reply.get("result", reply)  # the supplier's last e-mail carries the result

    def attachment(self, attachment_id):
        return self.client.attachment(self.session_id, attachment_id)

    def missed(self, supplier_id, error):
        """After a 409: the result if the case has ended (e.g. at a wave's cap), else what the
        thread now says (a timeout answer, or the reason it ended)."""
        state = self.client.session(self.session_id)
        if state.get("status") == "ended":
            return {"result": state.get("result") or {"deal": False, "end_reason": state.get("end_reason")}}
        t = self.client.thread(self.session_id, supplier_id)
        last = (t.get("timeouts") or [{}])[-1]
        sheet = [{"term_sheet_id": t["offer_id"], "terms": last.get("terms", {})}]
        return {
            "from": "arena",
            "status": "walked_away" if t["closed"] else t["status"],
            "term_sheets": [] if t["closed"] else sheet,
            "body": f"({error.detail})",
            "missing_fields": last.get("missing_fields", []),
            "attachments": [],
        }

    def result(self):
        state = self.client.session(self.session_id)
        return state.get("result") or {"deal": False, "end_reason": state.get("end_reason"), **state}


def latest(events):
    """The supplier's latest message in a thread."""
    return next(m for m in reversed(events) if m.get("from") != "buyer")


def standing(events):
    """(term_sheet_id, terms) of the supplier's standing term sheet, or (None, {})."""
    sheets = latest(events).get("term_sheets") or []
    return (sheets[0]["term_sheet_id"], sheets[0]["terms"]) if sheets else (None, {})


def offers(events):
    """Every term sheet of the supplier's latest message: [(term_sheet_id, terms)]. A supplier may
    send alternatives next to its standing offer (e.g. `offer:X-5` and `offer:X-5-a`); each can be
    accepted."""
    return [(s["term_sheet_id"], s["terms"]) for s in latest(events).get("term_sheets") or [] if s.get("terms")]


def live_threads(threads):
    """Suppliers whose thread has not ended."""
    return [sid for sid, ev in threads.items() if latest(ev).get("status") not in ENDED]


def moves_left(case, events):
    """Messages you may still send in this thread (the round limit counts each of them)."""
    return case.get("round_limit", 10) - sum(m.get("from") == "buyer" for m in events)


def fetch_documents(table, message):
    """Download every attachment of a supplier message and add its text."""
    for att in message.get("attachments") or []:
        if att.get("attachment_id") and "text" not in att:
            data = table.attachment(att["attachment_id"])
            if rec := runlog.current():
                rec.attachment(att, data)
            att["text"] = read_document(att["name"], data)
    return message


def closing_move(case, threads):
    """Accept the cheapest standing term sheet that keeps the must-haves and beats the alternative
    (C with each supplier's standard terms), else walk away - what to do when time runs out."""
    alt = case["buyer"].get("alternative") or {}
    c_alt = total_cost(case, alt["terms"]) if alt.get("terms", {}).get("unit_price") else float("inf")
    best = None
    for sid in live_threads(threads):
        std = standard_terms(threads[sid])
        for sheet_id, terms in offers(threads[sid]):  # alternatives count too
            if must_haves_ok(case, terms, std) and (c := total_cost(case, terms, std)) < c_alt:
                best = min(best or (c, sid, sheet_id), (c, sid, sheet_id))
    if best is None:
        return {"supplier_id": live_threads(threads)[0], "message": {"action": {"type": "walk_away"}}}
    return {"supplier_id": best[1], "message": {"action": {"type": "accept", "term_sheet_id": best[2]}}}


def play(table, case_id, agent, deadline=None):
    """Play one case to its end. `deadline` (time.time(), or a function returning it): a Live Final
    wave's cap - shortly before it, `closing_move` closes the case so it does not end without a deal.
    Every e-mail, decision and API call goes into the run log (`runlog`)."""
    with runlog.case(case_id, agent, table) as rec:
        result = _play(table, case_id, agent, deadline, rec)
        if rec:
            rec.finish(result, table)
        return result


def _prime(table, case_id, agent):
    """An agent with `prime(case)` prepares before the session opens - before any clock runs."""
    if not hasattr(agent, "prime") or not hasattr(table, "case_file"):
        return
    try:
        case = table.case_file(case_id)
        if case:
            agent.prime(case)
    except Exception:  # preparing is a bonus; the agent primes itself again once the case is open
        log.exception("%s: preparing before the session failed", case_id)


def _send_all(table, decisions, rec):
    """Several e-mails at once, one per thread: [(decision, reply or ArenaError)], in order."""

    def one(d):
        with runlog.bind(rec):
            try:
                return table.send(d["supplier_id"], d["message"])
            except ArenaError as e:
                return e

    with ThreadPoolExecutor(max_workers=len(decisions)) as pool:
        return list(zip(decisions, pool.map(one, decisions), strict=True))


def _play(table, case_id, agent, deadline, rec):
    _prime(table, case_id, agent)
    case, threads = table.open(case_id)
    for events in threads.values():
        for m in events:
            fetch_documents(table, m)
    if rec:
        rec.opened(getattr(table, "session_id", None), case, threads, resumed=bool(getattr(table, "done", None)))
    if getattr(table, "done", None):  # resumed a case that had already ended
        return table.done
    slowest = 0.0  # the longest round so far: decision + the supplier's reply
    while live_threads(threads):
        t0 = time.monotonic()
        cap = deadline() if callable(deadline) else deadline
        if cap and time.time() > cap - max(CLOSE_BEFORE_CAP_S, slowest + ROUND_MARGIN_S):
            log.info("%s: the wave's cap is near - closing", case_id)
            decision, source = closing_move(case, threads), "closing_move"
        else:
            source = type(agent).__name__
            # An agent with `decide_all` may write to several threads at once (an RFQ: no thread
            # waits into the 60-second rule while another is answered).
            if hasattr(agent, "decide_all"):
                decisions = agent.decide_all(case, threads)
            else:
                decisions = [agent.decide(case, threads)]
            decision = next((d for d in decisions if d["message"].get("action")), decisions[0])
            if len(decisions) > 1 and not decision["message"].get("action"):
                took = time.monotonic() - t0
                for d in decisions:
                    if rec:
                        rec.decision(d, took, source)
                        rec.sent(d["supplier_id"], d["message"])
                for d, reply in _send_all(table, decisions, rec):
                    sid, msg = d["supplier_id"], d["message"]
                    if isinstance(reply, ArenaError):
                        if reply.status != 409:
                            raise reply
                        log.info("%s: %s", sid, reply.detail)
                        reply = table.missed(sid, reply)
                    if "result" in reply:  # the case has ended (e.g. at the cap)
                        if rec and reply.get("body"):
                            rec.received(sid, reply)
                        return reply["result"]
                    threads[sid] += [{**msg, "from": "buyer"}, fetch_documents(table, reply)]
                    if rec:
                        rec.received(sid, reply)
                    log.info("%s %s %s", sid, reply.get("status"), standing(threads[sid])[1])
                slowest = max(slowest, time.monotonic() - t0)
                continue
        if (took := time.monotonic() - t0) > MOVE_LIMIT_S * 0.75:
            log.warning("decision took %.1fs - the limit is %ss per message", took, MOVE_LIMIT_S)
        sid, msg = decision["supplier_id"], decision["message"]
        if rec:
            rec.decision(decision, took, source)
            rec.sent(sid, msg)
        try:
            if msg.get("action"):
                log.info("%s %s", sid, msg["action"])
                return table.close(sid, msg)
            reply = table.send(sid, msg)
        except ArenaError as e:
            if e.status != 409:
                raise
            log.info("%s: %s", sid, e.detail)
            reply = table.missed(sid, e)  # e.g. late, or the thread or the case has ended
            if msg.get("action") and "result" not in reply:  # the case goes on: decide again
                threads[sid].append(fetch_documents(table, reply))
                if rec:
                    rec.received(sid, reply)
                continue
        if "result" in reply:  # the reply ended the case, e.g. the last supplier walked away
            if rec and reply.get("body"):
                rec.received(sid, reply)
            return reply["result"]
        threads[sid] += [{**msg, "from": "buyer"}, fetch_documents(table, reply)]
        if rec:
            rec.received(sid, reply)
        slowest = max(slowest, time.monotonic() - t0)
        log.info("%s %s %s", sid, reply.get("status"), standing(threads[sid])[1])
    return table.result()


def hosted(cases):
    """The cases you can play: those marked hosted, or all of them if the list has no marker."""
    if any(HOSTED_FIELD in c for c in cases):
        return [c for c in cases if c.get(HOSTED_FIELD)]
    return cases


def quota_used_up(error):
    """The practice quota's 429 ("practice limit: …"): retrying does not help before it resets."""
    return isinstance(error, ArenaError) and error.status == 429 and str(error.detail).startswith("practice limit")


def play_all(new_table, case_ids, new_agent, parallel=8):
    """Play many cases at once (e.g. every practice case). `new_table()` and `new_agent()` give
    each case its own table and agent. Once the practice quota is used up, no further case starts.
    Returns {case_id: result or the exception that stopped it}."""
    used_up = threading.Event()

    def one(case_id):
        if used_up.is_set():
            return case_id, ArenaError(429, "not started: the practice quota is used up")
        try:
            return case_id, play(new_table(), case_id, new_agent())
        except ArenaError as e:
            if quota_used_up(e):
                used_up.set()
                log.warning("practice quota used up: %s", e.detail)
            elif e.status == 403:
                log.warning("%s: %s", case_id, e.detail)  # e.g. not hosted for practice
            else:
                log.exception("%s failed", case_id)
            return case_id, e
        except Exception as e:  # one broken case must not stop the others
            log.exception("%s failed", case_id)
            return case_id, e

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        return dict(pool.map(one, case_ids))


def play_waves(client, new_agent, waves=5, parallel=10, poll=5.0):
    """The Live Final: wait for each wave, play its cases in parallel within the wave's cap, wait
    for the next. Keep it running from before the first wave until the last has closed; it returns
    after `waves` waves have closed and every case has finished: {case_id: result or exception}."""
    started, closed, caps = {}, set(), {}  # caps: wave -> its cap as time.time()

    def one(case, deadline):
        try:
            return play(RemoteTable(client, case), case["case_id"], new_agent(), deadline)
        except Exception as e:  # one broken case must not stop the others
            log.exception("%s failed", case["case_id"])
            return e

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        while len(closed) < waves or not all(f.done() for f in started.values()):
            try:
                cases = client.cases("final")
            except ArenaError as e:
                if e.status != 403:  # 403: no wave has started yet
                    raise
                cases = []
            for case in cases:
                if not case.get("open"):
                    closed.add(case["wave"])
                    continue
                if case.get("seconds_left") is not None:  # each wave has its own cap; follow an extension
                    caps[case["wave"]] = time.time() + case["seconds_left"]
                if case["case_id"] not in started and case.get("seconds_left"):
                    if case["wave"] not in {c["wave"] for c in cases if c["case_id"] in started}:
                        log.warning("wave %s started: %s", case["wave"], case.get("theme"))
                    started[case["case_id"]] = pool.submit(one, case, functools.partial(caps.get, case["wave"]))
            time.sleep(poll)
    return {case_id: f.result() for case_id, f in started.items()}
