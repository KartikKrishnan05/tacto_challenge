"""Play a case with one worker per supplier thread (R12.18, R13.16), ported from TeamAgent's
`team/runner.py`.

Every thread has its own 60-second clock and a supplier reply can take up to a minute, so each
thread answers as soon as its supplier has replied (R8.1). Network and API trouble never cost a case:
calls are retried, a send that may have landed is checked against the arena before it is repeated, a
worker never dies on an error, and a case is done only when the arena has ended the session. Close to
a Live Final wave's cap the accept ladder closes the case.

R13.16: a slow POST reads the reply from the thread (`RemoteTable.send`); an accept in an RFQ waits
for replies still in flight from other bidders (R12.16) - but never past 45 s after its own reply, so
a final offer that is the best on the table is accepted inside the 60 seconds; a refused accept
(`422`, the offer moved on) reloads the threads and decides again.
"""

import copy
import logging
import threading
import time

import httpx

from ..client import ArenaError
from ..play import (
    CLOSE_BEFORE_CAP_S,
    MOVE_LIMIT_S,
    ROUND_MARGIN_S,
    closing_move,
    fetch_documents,
    live_threads,
    moves_left,
)

log = logging.getLogger("arena")
RETRY_SLEEP_S = (1, 3, 5, 6, 6)
MAX_FAILURES = 3
ACCEPT_WAIT_S = 45.0  # R13.16: an accept waits for rival replies at most this long after its own reply


def retryable(e):
    if isinstance(e, httpx.TransportError):
        return True
    if isinstance(e, ArenaError):
        detail = str(e.detail)
        return e.status >= 500 or (e.status == 429 and "practice" not in detail and "open sessions" not in detail)
    return False


def call(fn, *args):
    for i, pause in enumerate((*RETRY_SLEEP_S, None)):
        try:
            return fn(*args)
        except Exception as e:
            if pause is None or not retryable(e):
                raise
            log.warning("retry %d after %s", i + 1, e)
            time.sleep(pause)


def safe_documents(table, message):
    try:
        return fetch_documents(table, message)
    except Exception as e:
        log.warning("attachment unreadable: %s", e)
        return message


def case_file(table, case_id):
    """The case file before the session opens, so the agent prepares without the clock (R1.3)."""
    if getattr(table, "case", None):
        return table.case
    for set_ in ("practice", "sandbox"):
        try:
            found = next((c for c in table.client.cases(set_) if c["case_id"] == case_id), None)
        except Exception:
            continue
        if found:
            return found
    return None


def play_case(table, case_id, agent, deadline=None):
    """Play one case to its end with `agent` (a SpecAgent). Returns the result."""
    pre = case_file(table, case_id)
    if pre is not None and hasattr(agent, "prime"):
        try:
            agent.prime(pre)
        except Exception:
            log.exception("%s: prime failed - going on without profiles", case_id)
    case, threads = call(table.open, case_id)
    if getattr(table, "done", None):
        return table.done
    for events in threads.values():
        for m in events:
            safe_documents(table, m)
    lock, close_lock, done, closing = threading.Lock(), threading.Lock(), threading.Event(), threading.Event()
    out, slowest = {}, [0.0]
    reply_at = {sid: time.monotonic() for sid in threads}
    inflight = {sid: False for sid in threads}
    agent.reply_at, agent.inflight = reply_at, inflight
    seconds_left = (deadline() if callable(deadline) else deadline) or None
    seconds_left = seconds_left - time.time() if seconds_left else (pre or {}).get("seconds_left")
    agent.prepare(case, threads, seconds_left)

    def cap():
        return deadline() if callable(deadline) else deadline

    def refresh(sid):
        try:
            messages = call(table.client.thread, table.session_id, sid)["messages"]
        except Exception as e:
            log.warning("%s %s: refresh failed: %s", case_id, sid, e)
            return
        messages = [safe_documents(table, m) for m in messages]
        with lock:
            threads[sid] = messages

    def ended():
        try:
            state = call(table.client.session, table.session_id)
        except Exception:
            return None
        return state if state.get("status") == "ended" else None

    def ladder(view):
        try:
            return agent.accept_candidates(case, view)
        except Exception:
            log.exception("%s: ladder failed", case_id)
            d = closing_move(case, view)
            a = d["message"]["action"]
            return [(d["supplier_id"], a["term_sheet_id"])] if a.get("type") == "accept" else []

    def close(decision):
        msg = decision["message"]
        with close_lock:
            if done.is_set():
                return True
            if msg["action"].get("type") != "accept":
                try:
                    out["result"] = call(table.close, decision["supplier_id"], msg)
                except ArenaError as e:
                    log.warning("%s: walk away failed: %s", case_id, e.detail)
                if out.get("result") or ended():
                    done.set()
                return done.is_set()
            candidates = decision.get("candidates") or [(decision["supplier_id"], msg["action"]["term_sheet_id"])]
            for rnd in range(2):
                for sid, oid in candidates[:4]:
                    try:
                        log.info("%s %s accept %s (%s)", case_id, sid, oid, decision.get("reason", ""))
                        out["result"] = call(table.close, sid, {"action": {"type": "accept", "term_sheet_id": oid}})
                        done.set()
                        return True
                    except ArenaError as e:
                        log.warning("%s: accept %s refused: %s", case_id, oid, e.detail)
                        if state := ended():
                            out["result"] = state.get("result") or table.result()
                            done.set()
                            return True
                if rnd == 0:  # the offer moved on: reload, decide again
                    for sid in {s for s, _ in candidates}:
                        refresh(sid)
                    with lock:
                        view = copy.deepcopy(threads)
                    candidates = ladder(view)
                    if not candidates:
                        break
            log.warning("%s: no accept went through - the case goes on", case_id)
            return False

    def send_safe(target, msg):
        with lock:
            mine = sum(m.get("from") == "buyer" for m in threads[target])
        for attempt in range(3):
            try:
                return table.send(target, msg)
            except ArenaError as e:
                if not retryable(e):
                    raise
            except httpx.TransportError as e:
                log.warning("%s %s: send failed (%s) - checking whether it landed", case_id, target, e)
            for _ in range(30):
                try:
                    t = call(table.client.thread, table.session_id, target)
                except Exception:
                    break
                held = t.get("messages") or []
                if sum(m.get("from") == "buyer" for m in held) <= mine:
                    break
                if held and held[-1].get("from") != "buyer":
                    return held[-1]
                time.sleep(2)
            time.sleep(RETRY_SLEEP_S[attempt])
        raise ArenaError(503, "send failed three times")

    def worker(sid, rank=0):
        failures, first = 0, True
        while not done.is_set() and failures < MAX_FAILURES:
            with lock:
                if sid not in live_threads(threads) or moves_left(case, threads[sid]) < 0:
                    return
                view = copy.deepcopy(threads)
            t0 = time.monotonic()
            c = cap()
            if c and time.time() > c - max(CLOSE_BEFORE_CAP_S, slowest[0] + ROUND_MARGIN_S):
                log.info("%s: the wave's cap is near - closing on the ladder", case_id)
                cands = ladder(view)
                close({"supplier_id": cands[0][0], "candidates": cands, "reason": "wave cap",
                       "message": {"action": {"type": "accept", "term_sheet_id": cands[0][1]}}}
                      if cands else closing_move(case, view))  # fmt: skip
                return
            try:
                d = agent.decide(case, view, sid)
            except Exception:
                log.exception("%s %s: decide failed - planned move", case_id, sid)
                try:
                    d = agent.fallback(case, view, sid)
                except Exception:
                    log.exception("%s %s: fallback failed", case_id, sid)
                    failures += 1
                    refresh(sid)
                    time.sleep(2)
                    continue
            if (took := time.monotonic() - t0) > MOVE_LIMIT_S * 0.75:
                log.warning("%s %s: decision took %.1fs", case_id, sid, took)
            if not d or d.get("idle"):
                return
            target, msg = d["supplier_id"], d["message"]
            if msg.get("action"):
                if msg["action"].get("type") == "accept" and len(live_threads(view)) > 1:
                    closing.set()  # R12.16: a rival reply may be on its way - but R13.16: inside 60 s
                    while (
                        any(busy for s, busy in inflight.items() if s != target)
                        and time.monotonic() < reply_at.get(target, 0) + ACCEPT_WAIT_S
                    ):
                        time.sleep(0.5)
                    with lock:
                        view = copy.deepcopy(threads)
                    d = agent.accept(case, view) or d
                ok = close(d)
                closing.clear()
                if ok:
                    return
                failures += 1
                continue
            while closing.is_set() and not done.is_set():
                time.sleep(0.5)
            if done.is_set():
                return
            if first and rank:  # RFQ: calls on one session queue up - the most promising bidder first
                time.sleep(0.3 * rank)
            first, late = False, False
            inflight[target] = True
            agent.sent(target)
            try:
                reply = send_safe(target, msg)
            except ArenaError as e:
                if done.is_set():
                    return
                if e.status == 409:
                    late = True
                    try:
                        reply = table.missed(target, e)
                    except Exception as e2:
                        log.warning("%s %s: %s", case_id, target, e2)
                        failures += 1
                        refresh(target)
                        continue
                elif e.status == 422:
                    log.warning("%s %s: message refused (%s) - planned move", case_id, target, e.detail)
                    try:
                        reply = send_safe(target, agent.fallback(case, view, target, plain=True)["message"])
                    except Exception as e2:
                        log.warning("%s %s: planned move failed too: %s", case_id, target, e2)
                        failures += 1
                        refresh(target)
                        continue
                else:
                    log.warning("%s %s: %s", case_id, target, e)
                    failures += 1
                    refresh(target)
                    continue
            except Exception as e:
                log.warning("%s %s: send failed: %s", case_id, target, e)
                failures += 1
                refresh(target)
                continue
            finally:
                inflight[target] = False
            failures = 0
            reply_at[target] = time.monotonic()
            agent.replied(target)
            if "result" in reply:
                with close_lock:
                    out.setdefault("result", reply["result"])
                    done.set()
                return
            reply = safe_documents(table, reply)
            with lock:
                threads[target] += [{**msg, "from": "buyer", **({"undelivered": True} if late else {})}, reply]
                slowest[0] = max(slowest[0], time.monotonic() - t0)
            log.info("%s %s %s %s", case_id, target, reply.get("status"),
                     (reply.get("term_sheets") or [{}])[0].get("terms"))  # fmt: skip

    live = live_threads(threads)
    if len(live) > 1:
        live = sorted(live, key=lambda s: agent.standing_cost(case, threads, s))
    workers = [threading.Thread(target=worker, args=(sid, rank), daemon=True) for rank, sid in enumerate(live)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    if not done.is_set():  # every worker stopped without closing: the best the arena still holds
        for sid in list(threads):
            refresh(sid)
        with lock:
            view = copy.deepcopy(threads)
        if live_threads(view):
            d = agent.final_close(case, view)
            close(d)
    result = out.get("result") or table.result()
    agent.finish(result)
    return result
