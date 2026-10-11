"""Live Final helpers for the spec agent (R12.18), from TeamAgent's `team/final.py`.

    uv run python -m arena_kit.spec.final --check               # pre-flight: key, data, model, open sessions
    uv run python -m arena_kit.spec.final --rehearse case:D02   # one sandbox case through the final path
    uv run arena run --set final --agent spec --parallel 10     # the Live Final itself (kit runner)

`arena run --set final` already resumes its started sessions after a crash (`play_waves` picks up the
409 "one session per case"); open practice sessions block final ones (10 per key), so `--check`
lists them and `--close-practice` walks away from them.
"""

import argparse
import json
import os
import time
from pathlib import Path

from ..cli import load_env
from ..client import ArenaClient, ArenaError
from ..play import RemoteTable


def open_sessions(client):
    out = []
    for set_ in ("practice", "sandbox"):
        try:
            rows = client.sessions(set_)
        except ArenaError:
            continue
        rows = rows.get("sessions", rows) if isinstance(rows, dict) else rows
        out += [s for s in rows or [] if s.get("status") == "open"]
    return out


def preflight(client):
    from .llm import from_env

    ok = True

    def check(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"{'OK ' if good else 'BAD'}  {name}  {detail}")

    try:
        me = client.me()
        check(
            "arena key", me.get("status") == "active", f"{me.get('team_name')}, practice left {me['practice']['left']}"
        )
    except Exception as e:
        check("arena key", False, str(e)[:120])
    data = Path(os.environ.get("ARENA_DATA", "data/package"))
    check("data package", (data / "facts.json").is_file(), str(data))
    t0 = time.time()
    complete = from_env(20)
    try:
        reply = complete("Answer in JSON.", 'Return {"ok": true}.') if complete else ""
        check("model", "ok" in reply, f"{os.environ.get('LLM_MODEL')} in {time.time() - t0:.1f}s")
    except Exception as e:
        check("model", False, str(e)[:120])
    opened = open_sessions(client)
    check("no open practice sessions", not opened, f"{len(opened)} open")
    print("READY" if ok else "NOT READY - fix the BAD lines")


def rehearse(client, case_id, cap=480):
    from ..agents.spec import make
    from ..data import DataPackage

    set_ = "sandbox" if case_id.startswith("case:D") else "practice"
    case = next(c for c in client.cases(set_) if c["case_id"] == case_id)
    root = os.environ.get("ARENA_DATA")
    package = DataPackage(root) if root and Path(root).is_dir() else None
    t0 = time.time()
    from .runner import play_case

    result = play_case(RemoteTable(client, case), case_id, make(package), t0 + cap)
    keep = ("deal", "supplier_id", "terms", "c_open", "c_deal", "c_best", "score")
    print(json.dumps({k: result.get(k) for k in keep} if isinstance(result, dict) else repr(result), indent=1))
    took = time.time() - t0
    print(f"took {took:.0f}s of the {cap:.0f}s cap" + ("  <-- TOO SLOW" if took > cap - 30 else "  ok"))


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="spec-final")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--rehearse", metavar="CASE")
    ap.add_argument("--cap", type=float, default=480)
    ap.add_argument("--close-practice", action="store_true", help="walk away from open practice/sandbox sessions")
    a = ap.parse_args(argv)
    client = ArenaClient()
    if a.close_practice:
        for s in open_sessions(client):
            try:
                client.walk_away(s["session_id"])
                print("closed", s["session_id"], s.get("case_id"))
            except ArenaError as e:
                print("could not close", s["session_id"], e)
    if a.check:
        preflight(client)
    if a.rehearse:
        rehearse(client, a.rehearse, a.cap)


if __name__ == "__main__":
    main()
