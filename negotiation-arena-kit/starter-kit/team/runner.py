"""Play a case with one worker per supplier thread.

Every thread has its own 60-second clock, and a supplier's reply can take up to a minute. The kit's
loop talks to one supplier at a time, so in an RFQ the others wait - and run into timeouts. Here
each thread answers as soon as its supplier has replied; the first accept (or walk away) ends the
case. Close to a Live Final wave's cap, the cheapest acceptable offer is accepted.

Network and API trouble must never cost a case: calls are retried, a send that may have landed is
checked against the arena before it is repeated, and a worker never dies on an error.
"""

import copy
import logging
import threading
import time

import httpx

from arena_kit.client import ArenaError
from arena_kit.play import (
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
MAX_FAILURES = 3  # consecutive failures before a worker gives its thread up


def retryable(e):
    """Venue Wi-Fi and server hiccups, not our mistakes (and not the practice quota or the session limit)."""
    if isinstance(e, httpx.TransportError):
        return True
    if isinstance(e, ArenaError):
        detail = str(e.detail)
        return e.status >= 500 or (e.status == 429 and "practice" not in detail and "open sessions" not in detail)
    return False


def call(fn, *args):
    """fn(*args), retried on transient errors."""
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
    except Exception as e:  # a document we cannot download or parse must not stop the thread
        log.warning("attachment unreadable: %s", e)
        return message


def play_case(table, case_id, agent, deadline=None):
    """Like `arena_kit.play.play`, but threads run in parallel. Returns the result."""
    case, threads = call(table.open, case_id)
    if getattr(table, "done", None):
        return table.done
    for events in threads.values():
        for m in events:
            safe_documents(table, m)
    lock, close_lock, done = threading.Lock(), threading.Lock(), threading.Event()
    out, slowest = {}, [0.0]

    def cap():
        return deadline() if callable(deadline) else deadline

    def refresh(sid):
        """The thread as the arena holds it now (after a timeout, a lost reply or a failed call)."""
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
        if hasattr(agent, "accept_candidates"):
            return agent.accept_candidates(case, view)
        d = closing_move(case, view)
        action = d["message"]["action"]
        return [(d["supplier_id"], action["term_sheet_id"])] if action.get("type") == "accept" else []

    def close(decision):
        """Accept along the ladder (cheapest first; a refused ID changes nothing), or walk away. Sets `done`
        only when the arena has ended the session."""
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
                        log.info("%s %s accept %s", case_id, sid, oid)
                        out["result"] = call(table.close, sid, {"action": {"type": "accept", "term_sheet_id": oid}})
                        done.set()
                        return True
                    except ArenaError as e:
                        log.warning("%s: accept %s refused: %s", case_id, oid, e.detail)
                        if state := ended():
                            out["result"] = state.get("result") or table.result()
                            done.set()
                            return True
                if rnd == 0:  # IDs may have moved (a timeout while our call waited): re-read, re-validate
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
        """Send once; after a transport error check whether it landed before sending again."""
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
            for _ in range(30):  # did the arena take it? then wait for the supplier's reply instead
                try:
                    t = call(table.client.thread, table.session_id, target)
                except Exception:
                    break
                held = t.get("messages") or []
                if sum(m.get("from") == "buyer" for m in held) <= mine:
                    break  # it did not land: send again
                if held and held[-1].get("from") != "buyer":
                    return held[-1]
                time.sleep(2)
            time.sleep(RETRY_SLEEP_S[attempt])
        raise ArenaError(503, "send failed three times")

    def worker(sid):
        failures = 0
        while not done.is_set() and failures < MAX_FAILURES:
            with lock:
                if sid not in live_threads(threads) or moves_left(case, threads[sid]) < 0:
                    return
                view = copy.deepcopy(threads)
            t0 = time.monotonic()
            c = cap()
            if c and time.time() > c - max(CLOSE_BEFORE_CAP_S, slowest[0] + ROUND_MARGIN_S):
                log.info("%s: the wave's cap is near - closing", case_id)
                cands = ladder(view)
                if cands:
                    sid0, oid = cands[0]
                    decision = {"supplier_id": sid0, "candidates": cands,
                                "message": {"action": {"type": "accept", "term_sheet_id": oid}}}
                else:
                    decision = closing_move(case, view)
                close(decision)
                return
            if hasattr(agent, "deadline"):
                agent.deadline = c
            try:
                d = agent.decide(case, view, sid)  # works on its own copy of the threads
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
                log.warning("%s: decision took %.1fs", case_id, took)
            if not d or d.get("idle"):
                return
            target, msg = d["supplier_id"], d["message"]
            if msg.get("action"):
                if close(d):
                    return
                failures += 1
                continue
            if done.is_set():
                return
            try:
                reply = send_safe(target, msg)
            except ArenaError as e:
                if done.is_set():
                    return
                if e.status == 409:  # late, round limit or the thread ended: the arena says what happened
                    try:
                        reply = table.missed(target, e)
                    except Exception as e2:
                        log.warning("%s %s: %s", case_id, target, e2)
                        failures += 1
                        refresh(target)
                        continue
                elif e.status == 422 and hasattr(agent, "fallback"):  # invalid message, nothing happened
                    log.warning("%s %s: message refused (%s) - planned move", case_id, target, e.detail)
                    try:
                        reply = send_safe(target, agent.fallback(case, view, target)["message"])
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
            failures = 0
            if "result" in reply:
                with close_lock:
                    out.setdefault("result", reply["result"])
                    done.set()
                return
            reply = safe_documents(table, reply)
            with lock:
                threads[target] += [{**msg, "from": "buyer"}, reply]
                slowest[0] = max(slowest[0], time.monotonic() - t0)

    if hasattr(agent, "prepare"):  # the briefing once, before the threads share the agent
        agent.prepare(case)
    workers = [threading.Thread(target=worker, args=(sid,), daemon=True) for sid in live_threads(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    if not done.is_set():  # every worker stopped without closing: take the best offer the arena still holds
        for sid in list(threads):
            refresh(sid)
        with lock:
            view = copy.deepcopy(threads)
        if live_threads(view):
            cands = ladder(view)
            if cands:
                close({"supplier_id": cands[0][0], "candidates": cands,
                       "message": {"action": {"type": "accept", "term_sheet_id": cands[0][1]}}})
            elif hasattr(agent, "c_alt") and agent.c_alt(case) < float("inf"):
                close({"supplier_id": live_threads(view)[0], "message": {"action": {"type": "walk_away"}}})
    return out.get("result") or table.result()
