"""Reproducible rehearsals with results, full sessions, threads and a target table.

python -m arena_kit.spec.evaluate --set sandbox --out runs/spec-sandbox
python -m arena_kit.spec.evaluate --sim --out runs/spec-sim
python -m arena_kit.spec.evaluate --set practice --parallel 5 --out runs/spec-practice
"""

import argparse
import hashlib
import json
import logging
import os
import socket
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..agents.spec import SpecAgent, make
from ..cli import load_env
from ..client import ArenaClient
from ..data import DataPackage
from ..play import RemoteTable, hosted, play
from . import strategy as strat

TARGETS = {"P01": .941, "P02": .507, "P03": .919, "P04": .860, "P05": .781,
           "P07": .773, "P08": .549, "P10": .671, "P11": .710, "P18": .453}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def audit(threads):
    """Audit complete arena threads, including timeout records absent from email bodies."""
    replies, contradicted, timed_out, missing_company, timeouts = [], [], [], [], []
    for sid, thread in threads.items():
        events = thread.get("messages", [])
        timeouts.extend({"supplier_id": sid, "timeout": t} for t in thread.get("timeouts") or [])
        for msg in events:
            if msg.get("from") == "buyer":
                if msg.get("body") and "Hollerau" not in msg["body"]:
                    missing_company.append(sid)
            else:
                replies.append(msg.get("status"))
                contradicted += [r for r in msg.get("claim_results") or [] if r.get("result") == "contradicted"]
                if "timeout" in (msg.get("body") or "").lower():
                    timed_out.append(sid)
    return {"reply_statuses": replies, "contradicted": contradicted,
            "timeout_mentions": timed_out, "timeouts": timeouts,
            "buyer_without_company": missing_company}


def evaluate(client, cases, out, package, parallel=5, no_model=False, learned=True):
    out.mkdir(parents=True, exist_ok=False)  # never replace an earlier experiment
    files = sorted(Path("arena_kit").rglob("*.py"))
    metadata = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_sha256": hashlib.sha256(b"".join(str(p).encode() + p.read_bytes() for p in files)).hexdigest(),
        "model": None if no_model else os.environ.get("LLM_MODEL"),
        "provider": None if no_model else os.environ.get("LLM_PROVIDER"),
        "cases": [c["case_id"] for c in cases],
        "learned_playbook": learned,
    }
    kb = strat.load() if learned else None
    write_json(out / "metadata.json", metadata)
    results = {}

    def one(case):
        cid = case["case_id"]
        table = RemoteTable(client, case)
        agent = (SpecAgent(package=package, log_dir=out / "logs", knowledge=kb) if no_model
                 else make(package, learned=learned))  # fmt: skip
        agent.log_dir = out / "logs"
        started = time.monotonic()
        record = {"case_id": cid, "case": case}
        try:
            result = play(table, cid, agent)
            record.update(result=result, wall_s=round(time.monotonic() - started, 2), session_id=table.session_id)
            record["standard_terms"] = {s: o.std for s, o in agent.prep.openings.items()}
            record["strategy"] = {"name": agent.strategy_name, "why": agent.strategy_why,
                                  "knobs": agent.strategy.knobs()}  # fmt: skip
            if table.session_id:
                state = client.session(table.session_id)
                threads = {sid: client.thread(table.session_id, sid) for sid in case["suppliers"]}
                record.update(session=state, threads=threads, audit=audit(threads))
        except Exception as error:
            record.update(error=f"{type(error).__name__}: {error}", session_id=table.session_id)
            logging.exception("%s failed", cid)
        finally:
            agent.pool.shutdown(wait=False, cancel_futures=True)
        write_json(out / f"{cid.split(':')[-1]}.json", record)
        score = (record.get("result") or {}).get("score")
        print(f"{cid}: deal={(record.get('result') or {}).get('deal')} score={score} "
              f"elapsed={record.get('wall_s')} error={record.get('error')}", flush=True)
        return cid, record

    with ThreadPoolExecutor(max_workers=min(parallel, 10)) as pool:
        pending = [pool.submit(one, c) for c in cases]
        for future in as_completed(pending):
            cid, record = future.result()
            results[cid] = record
            write_json(out / "results.json", results)
    lines = ["# SpecAgent evaluation", "",
             "| Case | Score | Target | Meets rounded target | Deal | Seconds from open |",
             "|---|---:|---:|---|---|---:|"]
    scores = []
    for cid, record in sorted(results.items()):
        r = record.get("result") or {}
        score, target = r.get("score"), TARGETS.get(cid.split(":")[-1])
        if score is not None:
            scores.append(score)
        logs = [x for x in agent_logs(out, cid) if x.get("event") == "result"]
        elapsed = logs[-1].get("elapsed") if logs else None
        passed = "—" if target is None or score is None else str(round(score, 3) >= target)
        lines.append(f"| {cid} | {score if score is not None else '—'} | {target or '—'} | {passed} | "
                     f"{r.get('deal')} | {elapsed} |")
    if scores:
        lines += ["", f"Mean score: {statistics.mean(scores):.6f} ({100 * statistics.mean(scores):.2f}/100)."]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results


def agent_logs(out, cid):
    return [json.loads(line) for p in sorted((out / "logs").glob(f"*_{cid.replace(':', '_')}.jsonl"))
            for line in p.read_text().splitlines()]


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="practice", choices=["practice", "sandbox"])
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--parallel", type=int, default=5)
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--no-learned", action="store_true", help="play the default strategy, not the learned playbook")
    ap.add_argument("--capture-session", help="capture an existing ended session; requires exactly one --ids case")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    package = DataPackage(os.environ.get("ARENA_DATA", "data/package"))
    package.lookup("")
    server = None
    if args.sim:
        import uvicorn

        from ..sim import Sim, create_app, load_cases

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        state = Sim(load_cases(), package.facts)
        server = uvicorn.Server(uvicorn.Config(create_app(state), log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(.01)
        client = ArenaClient(url, rate=9)
        key = client.register("SpecAgent rehearsal", "sim")["api_key"]
        client = ArenaClient(url, key)
    else:
        client = ArenaClient()
        print(json.dumps({"quota": client.me().get("practice")}), flush=True)
    try:
        cases = hosted(client.cases(args.set))
        if args.ids:
            ids = {i if i.startswith("case:") else f"case:{i}" for i in args.ids}
            cases = [c for c in cases if c["case_id"] in ids]
            if {c["case_id"] for c in cases} != ids:
                raise ValueError("one or more requested cases are unavailable")
        if args.capture_session:
            if len(cases) != 1:
                raise ValueError("capture requires exactly one case")
            case = cases[0]
            state = client.session(args.capture_session)
            if state.get("case_id") != case["case_id"] or state.get("status") != "ended":
                raise ValueError("capture session must be ended and match the requested case")
            threads = {sid: client.thread(args.capture_session, sid) for sid in case["suppliers"]}
            args.out.mkdir(parents=True, exist_ok=False)
            record = {"case_id": case["case_id"], "session_id": args.capture_session, "session": state,
                      "result": state.get("result"), "threads": threads,
                      "audit": audit(threads)}
            write_json(args.out / f"{case['case_id'].split(':')[-1]}.json", record)
        else:
            evaluate(client, cases, args.out, package, args.parallel, args.no_model, not args.no_learned)
    finally:
        if server:
            server.should_exit = True


if __name__ == "__main__":
    main()
