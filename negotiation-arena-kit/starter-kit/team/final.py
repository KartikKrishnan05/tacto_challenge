"""Live Final runner (step 5): our agent, one worker per supplier thread, every wave by itself.

Start it before Sunday 12:00 and leave it running until the last wave has closed - no human in the
loop. It polls the final case list (allowed), never the live feed (/v1/events, /screen, /leaderboard,
/results). After a crash, just start it again: open sessions are picked up where they stand.

    uv run python -m team.final                         # the Live Final: waits for wave 1 ... 5
    uv run python -m team.final --rehearse case:D02     # dress rehearsal on the free sandbox, 8-min cap
    uv run python -m team.final --rehearse case:P01     # same on a practice case (1 practice session)
    uv run python -m team.final --check                 # pre-flight: key, data, Sunday release, LLM, sessions
"""

import argparse
import functools
import json
import logging
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from arena_kit.cli import load_env
from arena_kit.client import ArenaClient, ArenaError
from arena_kit.play import RemoteTable

from .agent import make
from .runner import play_case

log = logging.getLogger("arena")
POLL_S = 5.0
MAX_PARALLEL = 10  # the arena allows 10 open sessions per key


def setup_logging(folder):
    folder.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.FileHandler(folder / "final.log", encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def save(folder, case_id, result):
    data = result if isinstance(result, dict) else {"error": repr(result)}
    (folder / f"{case_id.replace(':', '_')}.json").write_text(
        json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


def one(client, case, deadline, folder):
    """Play a case until it has a result or the cap is near. Every try uses a fresh table: after a crash,
    the 409 'one session per case' resumes the running session where it stands."""
    cap = deadline() if callable(deadline) else deadline
    result = None
    while True:
        try:
            result = play_case(RemoteTable(client, case), case["case_id"], make(), deadline)
            if isinstance(result, dict) and (result.get("ended") or result.get("status") == "ended"
                                             or result.get("score") is not None):
                break  # only an ended session is a result: an open one is resumed by the next try
            log.warning("%s: no result yet (%s) - retrying", case["case_id"], result)
        except ArenaError as e:
            result = e
            if e.status == 429 and "open sessions" in str(e.detail):
                close_practice_sessions(client)
            elif not (e.status >= 500 or e.status in (409, 429)):
                log.exception("%s failed", case["case_id"])
                break
            log.warning("%s: %s - retrying", case["case_id"], e)
        except Exception as e:  # venue Wi-Fi and other trouble: try again while the wave runs
            result = e
            log.exception("%s failed - retrying", case["case_id"])
        cap = (deadline() if callable(deadline) else deadline) or cap
        if not cap or time.time() > cap - 20:
            break
        time.sleep(2)
    save(folder, case["case_id"], result)
    score = result.get("score") if isinstance(result, dict) else None
    log.warning("DONE %s deal=%s score=%s", case["case_id"],
                result.get("deal") if isinstance(result, dict) else None, score)
    return result


def close_practice_sessions(client):
    """Open practice or sandbox sessions count against the 10 open sessions per key - and would block
    final cases. Walk away from them (practice keeps the best score per case, so nothing is lost)."""
    for set_ in ("practice", "sandbox"):
        try:
            sessions = client.sessions(set_)
        except ArenaError as e:
            log.warning("cannot list %s sessions: %s", set_, e)
            continue
        rows = sessions.get("sessions", sessions) if isinstance(sessions, dict) else sessions
        for s in rows or []:
            if s.get("status") == "open":
                try:
                    client.walk_away(s["session_id"])
                    log.warning("closed open %s session %s (%s)", set_, s["session_id"], s.get("case_id"))
                except ArenaError as e:
                    log.warning("could not close %s: %s", s["session_id"], e)


def live_final(client, folder, waves=5):
    started, closed, caps = {}, set(), {}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        while len(closed) < waves or not all(f.done() for f in started.values()):
            try:
                cases = client.cases("final")
            except ArenaError as e:
                if e.status != 403:  # 403: no wave has started yet
                    log.warning("case list: %s", e)
                cases = []
            except Exception as e:  # venue Wi-Fi: keep polling
                log.warning("case list failed: %s", e)
                cases = []
            for case in cases:
                if not case.get("open"):
                    closed.add(case["wave"])
                    continue
                if case.get("seconds_left") is not None:  # each wave has its own cap; follow an extension
                    caps[case["wave"]] = time.time() + case["seconds_left"]
                if case["case_id"] not in started and case.get("seconds_left"):
                    log.warning("wave %s (%s): starting %s, %ss left", case["wave"], case.get("theme"),
                                case["case_id"], case["seconds_left"])
                    started[case["case_id"]] = pool.submit(one, client, case, functools.partial(caps.get, case["wave"]),
                                                           folder)
            time.sleep(POLL_S)
    results = {cid: f.result() for cid, f in started.items()}
    scores = [r.get("score") or 0 for r in results.values() if isinstance(r, dict)]
    log.warning("all waves closed: %d cases, mean score %.1f", len(results),
                100 * statistics.mean(scores) if scores else 0)
    return results


def rehearse(client, case_id, folder, cap=480):
    set_ = "sandbox" if case_id.startswith("case:D") else "practice"
    case = next(c for c in client.cases(set_) if c["case_id"] == case_id)
    t0 = time.time()
    result = one(client, case, t0 + cap, folder)
    took = time.time() - t0
    keep = ("deal", "supplier_id", "terms", "c_open", "c_deal", "c_best", "score")
    print(json.dumps({k: result.get(k) for k in keep} if isinstance(result, dict) else repr(result), indent=1))
    print(f"took {took:.0f}s of the {cap}s cap" + ("  <-- TOO SLOW" if took > cap - 30 else "  ok"))


def preflight(client):
    """Sunday 09:00: everything the final needs, without playing a case."""
    from .agent import openai_complete

    ok = True

    def check(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"{'OK ' if good else 'BAD'}  {name}  {detail}")

    me = client.me()
    check("arena key", me.get("status") == "active", f"{me.get('team_name')}, practice left {me['practice']['left']}")
    data = Path(os.environ.get("ARENA_DATA", "data/package"))
    check("data package", (data / "facts.json").is_file(), str(data))
    sunday = data.parent / "release-sunday"
    check("Sunday release (from Sun 08:00: uv run arena data)", (sunday / "facts.json").is_file(), str(sunday))
    t0 = time.time()
    try:
        reply = openai_complete()("Answer in JSON.", 'Return {"ok": true}.')
        check("LLM", "ok" in reply, f"{os.environ.get('LLM_MODEL')} in {time.time() - t0:.1f}s")
    except Exception as e:
        check("LLM", False, str(e)[:120])
    open_ = []
    for set_ in ("practice", "sandbox"):
        try:
            rows = client.sessions(set_)
            rows = rows.get("sessions", rows) if isinstance(rows, dict) else rows
            open_ += [s for s in rows or [] if s.get("status") == "open"]
        except ArenaError:
            pass
    check("no open practice sessions (the final runner closes them)", not open_, f"{len(open_)} open")
    print("READY" if ok else "NOT READY - fix the BAD lines")


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="final", description=__doc__.split("\n\n")[0])
    ap.add_argument("--rehearse", metavar="CASE", help="one sandbox/practice case through the final path")
    ap.add_argument("--cap", type=float, default=480, help="rehearsal cap in seconds")
    ap.add_argument("--waves", type=int, default=5)
    ap.add_argument("--check", action="store_true", help="pre-flight check only")
    a = ap.parse_args(argv)
    if a.check:
        return preflight(ArenaClient())
    for key in ("ARENA_URL", "ARENA_API_KEY", "LLM_API_KEY", "LLM_MODEL"):
        if not os.environ.get(key):
            raise SystemExit(f"{key} is missing in .env")
    folder = Path("runs") / f"{datetime.now():%m%d-%H%M}_{'rehearse' if a.rehearse else 'FINAL'}"
    setup_logging(folder)
    client = ArenaClient()
    log.warning("team: %s", (client.me() or {}).get("team_name"))
    if a.rehearse:
        return rehearse(client, a.rehearse, folder, a.cap)
    close_practice_sessions(client)
    log.warning("Live Final runner up - waiting for the waves. Do not close this window.")
    live_final(client, folder, a.waves)


if __name__ == "__main__":
    main()
