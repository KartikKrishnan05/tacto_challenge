"""Play a case with one worker per supplier thread.

Every thread has its own 60-second clock, and a supplier's reply can take up to a minute. The kit's
loop talks to one supplier at a time, so in an RFQ the others wait - and run into timeouts. Here
each thread answers as soon as its supplier has replied; the first accept (or walk away) ends the
case. Close to a Live Final wave's cap, the cheapest acceptable offer is accepted.
"""

import copy
import logging
import threading
import time

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


def play_case(table, case_id, agent, deadline=None):
    """Like `arena_kit.play.play`, but threads run in parallel. Returns the result."""
    case, threads = table.open(case_id)
    if getattr(table, "done", None):
        return table.done
    for events in threads.values():
        for m in events:
            fetch_documents(table, m)
    lock, close_lock, done = threading.Lock(), threading.Lock(), threading.Event()
    out, slowest = {}, [0.0]

    def cap():
        return deadline() if callable(deadline) else deadline

    def close(sid, msg):
        with close_lock:
            if done.is_set():
                return
            try:
                log.info("%s %s %s", case_id, sid, msg["action"])
                out["result"] = table.close(sid, msg)
            except ArenaError as e:
                log.warning("%s: close failed: %s", case_id, e.detail)
                out["result"] = table.result()
            done.set()

    def worker(sid):
        while not done.is_set():
            with lock:
                if sid not in live_threads(threads) or moves_left(case, threads[sid]) < 0:
                    return
                view = copy.deepcopy(threads)
            t0 = time.monotonic()
            c = cap()
            if c and time.time() > c - max(CLOSE_BEFORE_CAP_S, slowest[0] + ROUND_MARGIN_S):
                log.info("%s: the wave's cap is near - closing", case_id)
                d = closing_move(case, view)
                return close(d["supplier_id"], d["message"])
            if hasattr(agent, "deadline"):
                agent.deadline = c
            d = agent.decide(case, view, sid)  # works on its own copy of the threads
            if (took := time.monotonic() - t0) > MOVE_LIMIT_S * 0.75:
                log.warning("%s: decision took %.1fs", case_id, took)
            if d.get("idle"):
                return
            target, msg = d["supplier_id"], d["message"]
            if msg.get("action"):
                return close(target, msg)
            if done.is_set():
                return
            try:
                reply = table.send(target, msg)
            except ArenaError as e:
                if done.is_set():
                    return
                if e.status != 409:
                    log.warning("%s %s: %s", case_id, target, e)
                    return
                reply = table.missed(target, e)
            if "result" in reply:
                with close_lock:
                    out.setdefault("result", reply["result"])
                    done.set()
                return
            with lock:
                threads[target] += [{**msg, "from": "buyer"}, fetch_documents(table, reply)]
                slowest[0] = max(slowest[0], time.monotonic() - t0)

    if hasattr(agent, "prepare"):  # the briefing once, before the threads share the agent
        agent.prepare(case)
    workers = [threading.Thread(target=worker, args=(sid,), daemon=True) for sid in live_threads(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    if not done.is_set():  # every worker stopped without closing: take the best offer still standing
        with lock:
            live = live_threads(threads)
        if live:
            d = closing_move(case, threads)
            close(d["supplier_id"], d["message"])
    return out.get("result") or table.result()
