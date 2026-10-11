"""Evaluation run: play a set of cases with an agent, then save score, cost breakdown, the levers we
left on the table and the supplier's reasons - one folder per run, compared with the previous run.

    uv run python -m team.evaluate --sim                          # local simulator: free, no quota
    uv run python -m team.evaluate --label v2-levers              # every hosted practice case (asks first)
    uv run python -m team.evaluate --cases case:P01 case:P05 --agent llm --yes

--agent: naive, llm (kit example) or "module:factory" (a function returning a fresh agent).
Output: runs/<time>_<label>/ with summary.md (read this), results.csv and one JSON per case.
"""

import argparse
import csv
import importlib
import inspect
import json
import logging
import socket
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from arena_kit.cli import agent_factory, load_env
from arena_kit.client import ArenaClient, ArenaError
from arena_kit.play import RemoteTable, hosted, play

from .levers import _label, best_per_field, lever_table
from .runner import play_case

RUNS = Path("runs")
PARTS = (
    "price",
    "uncovered_need",
    "fallback_risk",
    "payment_terms",
    "freight",
    "late_delivery",
    "quality",
    "tooling",
    "index_clause",
    "index_risk",
    "relationship",
)
log = logging.getLogger("arena")


def make_agent_factory(name, data):
    if ":" in name:
        module, attr = name.split(":", 1)
        return getattr(importlib.import_module(module), attr)
    return agent_factory(name, data)


def start_sim(data=None):
    """arena-sim in this process on a free port; returns a client with a fresh simulator key."""
    import uvicorn

    from arena_kit.sim import Sim, create_app, load_cases

    facts = None
    if data:
        from arena_kit.data import DataPackage

        facts = DataPackage(data).facts
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(Sim(load_cases(), facts)), port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    url = f"http://127.0.0.1:{port}"
    key = ArenaClient(url).register("eval", "sim")["api_key"]
    return ArenaClient(url, key), url


def play_one(client, case, new_agent, cap=None):
    table = RemoteTable(client, case)
    t0 = time.monotonic()
    agent = new_agent()
    deadline = time.time() + cap if cap else None
    try:
        for _ in range(40):  # someone else's run holds the 10 open sessions per key: wait, nothing has started
            try:
                if "sid" in inspect.signature(agent.decide).parameters:  # our agent: one worker per thread
                    result = play_case(table, case["case_id"], agent, deadline)
                else:
                    result = play(table, case["case_id"], agent, deadline)
                break
            except ArenaError as e:
                if e.status != 429 or "open sessions" not in str(e.detail) or table.session_id:
                    raise
                log.warning("%s: 10 sessions open on our key - waiting", case["case_id"])
                time.sleep(15)
                deadline = time.time() + cap if cap else None
    except Exception as e:  # one broken case must not stop the run
        log.exception("%s failed", case["case_id"])
        return {"case": case, "error": f"{type(e).__name__}: {e}", "session_id": table.session_id}
    session = {}
    if table.session_id:
        try:
            session = client.session(table.session_id)
        except ArenaError as e:
            log.warning("no session state for %s: %s", case["case_id"], e)
    full = session.get("result") or {}
    return {
        "case": case,
        "session_id": table.session_id,
        "seconds": round(time.monotonic() - t0, 1),
        "result": {**result, **{k: v for k, v in full.items() if k not in result}},
        "replay_url": session.get("replay_url"),
        "moves": [list(m) for m in getattr(agent, "log", [])],
    }


def missed_levers(case, result):
    """For a deal: the single best move per field we could still have made, by the cost function."""
    if not result.get("deal") or not result.get("terms"):
        return []
    try:
        rows = lever_table(case, result["terms"])
    except Exception as e:  # odd terms must not stop the report
        log.warning("levers failed for %s: %s", case["case_id"], e)
        return []
    return [r for r in best_per_field(rows) if r["delta_eur"] < -1]


def reasons(result, limit=12):
    """The supplier's reasons in words, from the trace (after the end)."""
    out = []
    for step in result.get("trace") or []:
        if step.get("actor") == "buyer":
            continue
        status = (step.get("move") or {}).get("status", "")
        for r in step.get("reasons") or []:
            out.append(f"r{step.get('round')} {status}: {r}")
    return out[-limit:]


def rounds_used(result):
    return max((step.get("round") or 0 for step in result.get("trace") or []), default=0)


def previous_run(current):
    runs = sorted(p for p in RUNS.glob("*/results.csv") if p.parent != current)
    if not runs:
        return None, {}
    with runs[-1].open(encoding="utf-8") as f:
        return runs[-1].parent.name, {row["case_id"]: row for row in csv.DictReader(f)}


def write_outputs(out_dir, rows, args, base_url):
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = ["case_id", "title", "goal", "deal", "score", "supplier_id", "c_open", "c_deal", "c_best", "c_alt",
              "rounds", "seconds", "end_reason", "error", "replay", *PARTS]
    table = []
    for row in rows:
        case, res = row["case"], row.get("result") or {}
        (out_dir / f"{case['case_id'].replace(':', '_')}.json").write_text(
            json.dumps(row, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
        )
        parts = res.get("cost_breakdown") or {}
        table.append({
            "case_id": case["case_id"],
            "title": case.get("title", ""),
            "goal": case["buyer"].get("category_goal"),
            "deal": res.get("deal"),
            "score": res.get("score"),
            "supplier_id": res.get("supplier_id"),
            "c_open": res.get("c_open"),
            "c_deal": res.get("c_deal"),
            "c_best": res.get("c_best"),
            "c_alt": res.get("c_alt"),
            "rounds": rounds_used(res),
            "seconds": row.get("seconds"),
            "end_reason": res.get("end_reason"),
            "error": row.get("error", ""),
            "replay": f"{base_url}{row['replay_url']}" if row.get("replay_url") else "",
            **{p: parts.get(p) for p in PARTS},
        })
    with (out_dir / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(table)

    prev_name, prev = previous_run(out_dir)
    scores = [r["score"] or 0 for r in table]
    mean = 100 * statistics.mean(scores) if scores else 0
    md = [f"# Run {out_dir.name}", "",
          f"agent `{args.agent}`, {'simulator' if args.sim else args.set}, {len(table)} cases, "
          f"**mean score {mean:.1f}**" + (f" (previous `{prev_name}`)" if prev_name else ""), "",
          "| case | goal | deal | score | vs prev | C deal | C best | rounds | replay |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in table:
        before = prev.get(r["case_id"], {}).get("score")
        diff = f"{(r['score'] or 0) - float(before):+.2f}" if before not in (None, "") else ""
        md.append(
            f"| {r['case_id']} {r['title'][:40]} | {r['goal']} | {r['deal']} | {(r['score'] or 0):.2f} | {diff} | "
            f"{r['c_deal'] or '-'} | {r['c_best'] or '-'} | {r['rounds']} | "
            + (f"[replay]({r['replay']})" if r["replay"] else (r["error"][:60] or "-")) + " |"
        )
    md += ["", "## Per case: levers left on the table and the supplier's reasons", ""]
    for row in rows:
        case, res = row["case"], row.get("result") or {}
        md.append(f"### {case['case_id']}: score {(res.get('score') or 0):.2f}, deal {res.get('deal')}")
        if res.get("terms"):
            md.append(f"Closed on: `{json.dumps(res['terms'], default=str)}`")
        if case["buyer"].get("category_goal") in ("stabilise", "secure_supply") and res.get("deal"):
            md.append("(goal with a relationship charge: the savings below leave it out)")
        for m in missed_levers(case, res):
            md.append(f"- missed: {m['field']} {_label(m['current'])} -> {_label(m['option'])} "
                      f"would have saved {-m['delta_eur']:,.0f} EUR")
        for sid, rnd, source, why in row.get("moves") or []:
            md.append(f"- our move {rnd} to {sid} ({source}): {why}")
        for r in reasons(res):
            md.append(f"- supplier {r}")
        if row.get("error"):
            md.append(f"- error: {row['error']}")
        md.append("")
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")
    return mean


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="evaluate", description=__doc__.split("\n\n")[0])
    ap.add_argument("--agent", default="team.agent:make", help="naive, llm or module:factory")
    ap.add_argument("--cases", nargs="*", help="case IDs (default: every playable case of the set)")
    ap.add_argument("--set", default="practice")
    ap.add_argument("--sim", action="store_true", help="play against the local simulator (free)")
    ap.add_argument("--data", default=None, help="data package folder (default: ARENA_DATA)")
    ap.add_argument("--parallel", type=int, default=5)
    ap.add_argument("--cap", type=float, default=None, help="seconds per case, like a Live Final wave (480)")
    ap.add_argument("--label", default="run")
    ap.add_argument("--yes", action="store_true", help="do not ask before using practice sessions")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    import os

    data = a.data or os.environ.get("ARENA_DATA")
    # Module factories (for example team.compat:make) are intentionally zero-argument so
    # they can also be used by the standard runner. Make an explicit --data path visible to
    # those factories as well as to the built-in agent factory.
    if a.data:
        os.environ["ARENA_DATA"] = a.data
    if a.set == "final":
        raise SystemExit("not for the Live Final: use `arena run --set final`")
    if a.sim:
        client, base_url = start_sim(data)
    else:
        client, base_url = ArenaClient(), os.environ["ARENA_URL"].rstrip("/")
    cases = hosted(client.cases(a.set))
    if a.cases:
        cases = [c for c in cases if c["case_id"] in set(a.cases)]
        if missing := set(a.cases) - {c["case_id"] for c in cases}:
            raise SystemExit(f"not playable here: {sorted(missing)}")
    if not cases:
        raise SystemExit("no cases to play")
    if not a.sim and a.set == "practice" and not a.yes:  # sandbox sessions are free
        left = (client.me().get("practice") or {}).get("left")
        answer = input(f"This uses {len(cases)} practice sessions ({left} left today for the whole team). Go? [y/N] ")
        if answer.strip().lower() not in ("y", "yes", "j", "ja"):
            raise SystemExit("cancelled")

    new_agent = make_agent_factory(a.agent, data)
    out_dir = RUNS / f"{datetime.now():%m%d-%H%M}_{a.label}"
    print(f"playing {len(cases)} cases with {a.agent} ({min(a.parallel, 10)} at once) ...", flush=True)
    with ThreadPoolExecutor(max_workers=min(a.parallel, 10)) as pool:
        rows = list(pool.map(lambda c: play_one(client, c, new_agent, a.cap), cases))
    mean = write_outputs(out_dir, rows, a, base_url)
    for row in rows:
        res = row.get("result") or {}
        print(f"  {row['case']['case_id']:10} score {(res.get('score') or 0):.2f}  deal {res.get('deal')}"
              + (f"  ERROR {row['error'][:80]}" if row.get("error") else ""))
    print(f"mean score {mean:.1f}  ->  {out_dir / 'summary.md'}")
    if not a.sim and client.practice_remaining is not None:
        print(f"practice sessions left today: {client.practice_remaining}")


if __name__ == "__main__":
    sys.exit(main())
