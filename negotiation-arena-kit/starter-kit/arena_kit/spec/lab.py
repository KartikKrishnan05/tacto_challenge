"""The lab: play strategy variants on the practice cases, keep every game in the experiment sheet, and
learn from it which strategy to play on a new case.

    # experiments: every variant on every hosted practice case (code-only: no model credits)
    python -m arena_kit.spec.lab run --variants base soft_anchor deep_anchor --parallel 8 --label sweep-1
    # the learned playbook's own pick per case; --holdout ignores the case's own games (an unseen case)
    python -m arena_kit.spec.lab learned --holdout --label holdout-1
    # rebuild the sheet and the playbook from the games recorded so far (run/learned do it at the end)
    python -m arena_kit.spec.lab build

Files (all in `knowledge/`, committed so the next session and the final start from them):

    experiments.jsonl      the experiment sheet: one line per game - case, features, variant, knobs, mode,
                           score, deal, costs, rounds, source fingerprint
    experiments.csv        the same as a table
    learned_playbook.json  per practice case: its features, base score and every variant's gain over base
                           - what `spec.strategy.choose` reads when the agent prepares a case
    EXPERIMENTS.md         the result sheet: hypotheses, the score grid, gains by goal and type, what the
                           playbook picks per case, every batch

Full sessions, threads and agent logs of every game go to `runs/lab/<batch>/`. Each hosted game uses one
practice session of the team's quota; the arena allows one open session per case, so a case's games
run one after another and different cases run in parallel.
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import statistics
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..agents.spec import SpecAgent, make
from ..cli import load_env
from ..client import ArenaClient
from ..data import DataPackage
from ..play import RemoteTable, hosted, play, quota_used_up
from . import strategy as strat
from .evaluate import audit, write_json
from .replay import Book, ReplayTable

log = logging.getLogger("arena")
KIT = Path(__file__).resolve().parents[2]
SHEET = "experiments.jsonl"
_write = threading.Lock()


def source_fingerprint():
    files = sorted((KIT / "arena_kit").rglob("*.py"))
    return hashlib.sha256(b"".join(str(p.relative_to(KIT)).encode() + p.read_bytes() for p in files)).hexdigest()[:12]


def git_head():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, cwd=KIT).strip()
    except Exception:
        return None


def read_sheet(kdir):
    path = Path(kdir) / SHEET
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_row(kdir, row):
    with _write:
        Path(kdir).mkdir(parents=True, exist_ok=True)
        with open(Path(kdir) / SHEET, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


# --- playing -------------------------------------------------------------------------------------------
class Budget:
    """At most `n` real practice sessions in this batch (None: no limit)."""

    def __init__(self, n=None):
        self.n, self.used, self.lock = n, 0, threading.Lock()

    def take(self):
        with self.lock:
            if self.n is not None and self.used >= self.n:
                return False
            self.used += 1
            return True


def play_one(client, case, package, out, kdir, meta, name=None, model=False, kb=None, holdout=False, book=None,
             budget=None):  # fmt: skip
    """One game of `case`: variant `name`, or (name None) the learned playbook's pick. One sheet row.
    Code-only games are first replayed from `book` (spec.replay); only a game that leaves the recorded
    games needs a practice session - and gets one while `budget` lasts (else None, nothing recorded)."""
    cid = case["case_id"]
    if name is None:
        s, picked, why = strat.choose(case, kb, exclude_self=holdout)
        chosen_by = "holdout" if holdout else "learned"
    else:
        s, picked, why, chosen_by = strat.variant(name), name, "fixed for this run", "fixed"
    log_dir = out / "logs"

    def game(table):
        agent = make(package, strategy=s) if model else SpecAgent(package=package, log_dir=log_dir, strategy=s)
        agent.log_dir = log_dir
        started = time.monotonic()
        record = {"case_id": cid, "variant": picked, "chosen_by": chosen_by, "why": why, "knobs": s.knobs()}
        try:
            result = play(table, cid, agent)
            record.update(result=result, session_id=table.session_id)
            if table.session_id:
                threads = {sid: table.client.thread(table.session_id, sid) for sid in case["suppliers"]}
                record.update(threads=threads, audit=audit(threads))
        except Exception as e:
            record.update(error=f"{type(e).__name__}: {e}", session_id=table.session_id)
            log.exception("%s %s failed", cid, picked)
            if quota_used_up(e):
                record["quota"] = True
        finally:
            agent.pool.shutdown(wait=False, cancel_futures=True)
        record["wall_s"] = round(time.monotonic() - started, 1)
        return record

    record, replayed, diverged = None, False, None
    if book is not None and not model and book.get(cid):
        table = ReplayTable(book.get(cid), case, client)
        record = game(table)
        if table.diverged is None and not record.get("error"):
            replayed = True
            record.update(replay=True, session_id=None)
        else:
            diverged, record = table.diverged or record.get("error"), None
    if record is None:
        if budget is not None and not budget.take():
            print(f"{cid} {picked:<13} ({chosen_by}) needs a session ({diverged}) - budget used up", flush=True)
            return None
        record = game(RemoteTable(client, case))
        if book is not None and not model and record.get("threads"):
            book.add(cid, record)  # the next game of this batch can replay this one
    stem = f"{cid.split(':')[-1]}__{picked}__{chosen_by}{'__replay' if replayed else ''}"
    path = out / f"{stem}.json"
    n = 2
    while path.exists():
        path, n = out / f"{stem}_{n}.json", n + 1
    write_json(path, record)
    r = record.get("result") or {}
    buyer_msgs = sum(1 for t in (record.get("threads") or {}).values() for m in t.get("messages", [])
                     if m.get("from") == "buyer")  # fmt: skip
    row = {**meta, "t": time.strftime("%Y-%m-%dT%H:%M:%S"), **strat.features(case), "case_id": cid,
           "n_suppliers": len(case.get("suppliers") or []), "variant": picked, "chosen_by": chosen_by, "why": why,
           "knobs": s.knobs(), "mode": "model" if model else "code", "score": r.get("score"), "deal": r.get("deal"),
           "supplier_id": r.get("supplier_id"), "c_open": r.get("c_open"), "c_deal": r.get("c_deal"),
           "c_best": r.get("c_best"), "c_alt": r.get("c_alt"), "end_reason": r.get("end_reason"),
           "terms": r.get("terms"), "buyer_messages": buyer_msgs, "wall_s": record["wall_s"],
           "session_id": record.get("session_id"), "error": record.get("error"), "replay": replayed,
           "contradicted": len((record.get("audit") or {}).get("contradicted") or []),
           "timeouts": len((record.get("audit") or {}).get("timeouts") or []),
           "record": str(path.relative_to(KIT)) if path.is_relative_to(KIT) else str(path)}  # fmt: skip
    if not record.get("quota"):
        append_row(kdir, row)
    how = "replayed, no session" if replayed else f"session {record.get('session_id')}"
    print(f"{cid} {picked:<15} ({chosen_by}) score={r.get('score')} deal={r.get('deal')} [{how}] "
          f"wall={record['wall_s']}s error={record.get('error')}", flush=True)  # fmt: skip
    return row


def run_batch(client, cases, package, jobs, label, kdir, parallel=8, model=False, kb=None, holdout=False,
              book=None, budget=None):  # fmt: skip
    """`jobs`: {case_id: [variant name or None (learned)]}. One thread per case runs its games in order,
    at most `parallel` games at once."""
    out = KIT / "runs" / "lab" / f"{time.strftime('%m%d-%H%M%S')}_{label}"
    out.mkdir(parents=True, exist_ok=False)
    meta = {"batch": out.name, "label": label, "git_head": git_head(), "source": source_fingerprint(),
            "model_name": os.environ.get("LLM_MODEL") if model else None}  # fmt: skip
    write_json(out / "metadata.json", {**meta, "jobs": jobs, "holdout": holdout})
    by_id = {c["case_id"]: c for c in cases}
    gate, stop, rows = threading.Semaphore(min(parallel, 10)), threading.Event(), []

    def case_chain(cid):
        for name in jobs[cid]:
            if stop.is_set():
                return
            with gate:
                row = play_one(client, by_id[cid], package, out, kdir, meta, name, model, kb, holdout, book, budget)
            if row is None:
                continue
            rows.append(row)
            if row.get("error") and "practice limit" in str(row["error"]):
                stop.set()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for f in [pool.submit(case_chain, cid) for cid in jobs]:
            f.result()
    scores = [r["score"] for r in rows if r.get("score") is not None]
    sessions = sum(1 for r in rows if not r.get("replay"))
    mean = statistics.mean(scores) if scores else float("nan")
    print(f"batch {out.name}: {len(rows)} games ({sessions} practice sessions, {len(rows) - sessions} replayed),"
          f" mean score {mean:.4f}")
    return rows


# --- learning --------------------------------------------------------------------------------------------
def usable(rows):
    return [r for r in rows if not r.get("error") and r.get("score") is not None and r.get("variant") in strat.VARIANTS]


def build_playbook(rows):
    """Per case: features, mean score per (mode, variant), and every variant's gain over base - measured
    within one mode (code-only or model) and averaged over the modes that have both."""
    rows = usable(rows)
    cells = defaultdict(list)
    for r in rows:
        cells[(r["case_id"], r["mode"], r["variant"])].append(r["score"])
    cases = {}
    for r in rows:
        cases.setdefault(r["case_id"], {"type": r.get("type"), "goal": r.get("goal"), "rfq": r.get("rfq"),
                                        "n_suppliers": r.get("n_suppliers"), "scores": {}, "gains": {}})  # fmt: skip
    for (cid, mode, name), scores in cells.items():
        cases[cid]["scores"].setdefault(mode, {})[name] = {"mean": round(statistics.mean(scores), 6),
                                                           "n": len(scores), "best": round(max(scores), 6)}  # fmt: skip
    for e in cases.values():
        per = defaultdict(list)
        for s in e["scores"].values():
            if "base" not in s:
                continue
            for name, v in s.items():
                if name != "base":
                    per[name].append((v["mean"] - s["base"]["mean"], v["n"]))
        e["gains"] = {name: {"gain": round(statistics.mean(g for g, _ in v), 6), "n": sum(n for _, n in v)}
                      for name, v in per.items()}  # fmt: skip
        base = [s["base"]["mean"] for s in e["scores"].values() if "base" in s]
        e["base"] = round(statistics.mean(base), 6) if base else None
    return {
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "games": len(rows),
        "rules": {"min_gain": strat.MIN_GAIN, "max_loss": strat.MAX_LOSS},
        "variants": {n: {"knobs": v.knobs(), "hypothesis": strat.HYPOTHESES.get(n, "")}
                     for n, v in strat.VARIANTS.items()},  # fmt: skip
        "cases": dict(sorted(cases.items())),
    }


def _fmt(x, nd=3, sign=False):
    if x is None:
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def report(kb, rows):
    """EXPERIMENTS.md: the result sheet."""
    ok = usable(rows)
    names = [n for n in strat.VARIANTS if any(r["variant"] == n for r in ok)]
    cases = kb["cases"]
    modes = sorted({r["mode"] for r in ok})
    lines = [
        "# SpecAgent experiment and result sheet",
        "",
        f"Built {kb['built']} from **{len(ok)} games** ({len(rows) - len(ok)} failed or unscored rows left out)"
        f" on {len(cases)} practice cases, modes: {', '.join(modes) or '—'}. Regenerate with"
        " `python -m arena_kit.spec.lab build`; every game is a line of `experiments.jsonl`, its full session in"
        " the `record` path. The agent reads `learned_playbook.json` when it prepares a case"
        " (`spec.strategy.choose`).",
        "",
        f"Rule: a variant replaces base for a group of cases when its mean score gain over base is at least"
        f" {strat.MIN_GAIN} and it lost no case in the group by more than {strat.MAX_LOSS}; beyond the same case it"
        f" must also have lost no tested case by more than {strat.MAX_LOSS}, and at the all-cases level it must have"
        " won 2+ cases and lost none. Ties go to the variant closest to base. Code-only games repeat"
        " almost exactly (same e-mails, same supplier replies), so one code-only game per case and variant is a"
        " measurement, not a sample; games with the model vary and are averaged.",
        "",
        "## Hypotheses and verdicts",
        "",
        "| Variant | Knobs vs base | Hypothesis | Cases | Mean gain | Won / lost | Verdict |",
        "|---|---|---|---:|---:|---|---|",
    ]
    all_gains = strat.gains(cases.values())
    for n in strat.VARIANTS:
        g = all_gains.get(n, [])
        if n == "base":
            tested = sum(1 for e in cases.values() if e["base"] is not None)
            lines.append(f"| base | — | {strat.HYPOTHESES['base']} | {tested} | — | — | control |")
            continue
        won, lost = sum(x > 0.001 for x in g), sum(x < -0.001 for x in g)
        if not g:
            verdict = "not tested yet"
        elif statistics.mean(g) >= strat.MIN_GAIN and min(g) >= -strat.MAX_LOSS:
            verdict = "better overall"
        elif won:
            verdict = "helps some cases only"
        else:
            verdict = "rejected"
        mean = _fmt(statistics.mean(g) if g else None, sign=True)
        lines.append(f"| {n} | {strat.describe(strat.VARIANTS[n])} | {strat.HYPOTHESES.get(n, '')} | {len(g)} | "
                     f"{mean} | {won} / {lost} | {verdict} |")  # fmt: skip
    for mode in modes:
        lines += ["", f"## Score per case and variant ({mode})", "",
                  "Mean score (games); **bold** = best for the case.", "",
                  "| Case | Type / goal | " + " | ".join(names) + " |",
                  "|---|---|" + "---:|" * len(names)]  # fmt: skip
        for cid, e in cases.items():
            s = e["scores"].get(mode, {})
            if not s:
                continue
            top = max(v["mean"] for v in s.values())
            cells = []
            for n in names:
                v = s.get(n)
                cell = "—" if v is None else f"{v['mean']:.3f} ({v['n']})"
                cells.append(f"**{cell}**" if v is not None and v["mean"] == top else cell)
            kind = f"{e['type']} / {e['goal']}{' RFQ' if e['rfq'] else ''}"
            lines.append(f"| {cid} | {kind} | " + " | ".join(cells) + " |")
        means = []
        for n in names:
            vals = [e["scores"][mode][n]["mean"] for e in cases.values() if n in e["scores"].get(mode, {})]
            means.append(f"{statistics.mean(vals):.3f}" if vals else "—")
        lines.append("| **mean** | | " + " | ".join(means) + " |")
    lines += ["", "## Gain over base by group", "",
              "| Group | Cases | Best variant | Its mean gain | All variants (mean gain) |",
              "|---|---:|---|---:|---|"]  # fmt: skip
    groups = defaultdict(list)
    for e in cases.values():
        groups[f"goal {e['goal']}"].append(e)
        groups[f"type {e['type']}"].append(e)
        groups[f"{e['type']} / {e['goal']}"].append(e)
    for label in sorted(groups):
        es = groups[label]
        got = strat.best_of(es)
        g = strat.gains(es)
        ranked = sorted(g.items(), key=lambda x: -statistics.mean(x[1]))
        detail = ", ".join(f"{n} {statistics.mean(v):+.3f}" for n, v in ranked)
        lines.append(f"| {label} | {len(es)} | {got[0] if got else '—'} | {_fmt(got[1] if got else None, sign=True)} | "
                     f"{detail or '—'} |")  # fmt: skip
    lines += ["", "## What the agent will play", "",
              "`learned`: the pick for this practice case; `unseen`: the pick if the case were new (its own games"
              " left out) - what a hidden case of the same kind gets.", "",
              "| Case | Base score | Learned pick | Why | Unseen pick | Why |",
              "|---|---:|---|---|---|---|"]  # fmt: skip
    for cid, e in cases.items():
        case = {"case_id": cid, "type": e["type"], "buyer": {"category_goal": e["goal"]},
                "suppliers": [None] * (e.get("n_suppliers") or 1)}  # fmt: skip
        _, n1, w1 = strat.choose(case, kb)
        _, n2, w2 = strat.choose(case, kb, exclude_self=True)
        lines.append(f"| {cid} | {_fmt(e['base'])} | {n1} | {w1} | {n2} | {w2} |")
    lessons = learned_lessons(kb)
    if lessons:
        lines += ["", "## Lessons", "", *[f"- {x}" for x in lessons]]
    lines += ["", "## Batches", "", "| Batch | Label | Mode | Games | Mean score | Source |",
              "|---|---|---|---:|---:|---|"]  # fmt: skip
    by_batch = defaultdict(list)
    for r in rows:
        by_batch[r.get("batch")].append(r)
    for b, rs in sorted(by_batch.items(), key=lambda x: str(x[0])):
        sc = [r["score"] for r in rs if r.get("score") is not None]
        lines.append(f"| {b} | {rs[0].get('label')} | {rs[0].get('mode')} | {len(rs)} | "
                     f"{_fmt(statistics.mean(sc) if sc else None)} | {rs[0].get('source')} |")  # fmt: skip
    return "\n".join(lines) + "\n"


def learned_lessons(kb):
    """Plain sentences from the gains: which variant helps or hurts which goal."""
    out = []
    by_goal = defaultdict(list)
    for e in kb["cases"].values():
        by_goal[e["goal"]].append(e)
    for goal, es in sorted(by_goal.items()):
        g = strat.gains(es)
        for n, v in sorted(g.items(), key=lambda x: -statistics.mean(x[1])):
            m = statistics.mean(v)
            if m >= strat.MIN_GAIN:
                risk = f", but lost one case by {min(v):.3f} - not chosen" if min(v) < -strat.MAX_LOSS else ""
                out.append(f"{goal}: **{n}** helped ({m:+.3f} on average, better in {sum(x > 0.001 for x in v)}"
                           f" of {len(v)} case{'s' if len(v) != 1 else ''}{risk}).")  # fmt: skip
            elif m <= -0.02:
                out.append(f"{goal}: {n} hurt ({m:+.3f} on average) - avoid.")
    return out


def build(kdir):
    rows = read_sheet(kdir)
    kb = build_playbook(rows)
    kdir = Path(kdir)
    write_json(kdir / strat.PLAYBOOK_FILE, kb)
    (kdir / "EXPERIMENTS.md").write_text(report(kb, rows), encoding="utf-8")
    keys = ["batch", "label", "t", "case_id", "type", "goal", "rfq", "variant", "chosen_by", "mode", "score", "deal",
            "supplier_id", "c_open", "c_deal", "c_best", "c_alt", "buyer_messages", "wall_s", "end_reason",
            "contradicted", "timeouts", "error", "source", "session_id", "record"]  # fmt: skip
    with open(kdir / "experiments.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys + list(strat.BASE.knobs()), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, **(r.get("knobs") or {})})
    print(f"learned playbook: {len(kb['cases'])} cases from {kb['games']} games -> {kdir}")
    return kb


# --- command line ----------------------------------------------------------------------------------------
def main():
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "learned", "build"])
    ap.add_argument("--variants", nargs="*", default=list(strat.VARIANTS), help="run: the variants to play")
    ap.add_argument("--ids", nargs="*", help="cases (P01 ...); default every hosted practice case")
    ap.add_argument("--repeat", type=int, default=1, help="games per case and variant")
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--model", action="store_true", help="the model writes and reads e-mails (costs credits)")
    ap.add_argument("--holdout", action="store_true", help="learned: ignore each case's own games")
    ap.add_argument("--label", default="lab")
    ap.add_argument("--knowledge", type=Path, default=strat.KNOWLEDGE_DIR)
    ap.add_argument("--sessions", type=int, help="at most this many real practice sessions (replays are free)")
    ap.add_argument("--no-replay", action="store_true", help="play every game for real")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    if args.command == "build":
        build(args.knowledge)
        return
    for n in args.variants:
        strat.variant(n)
    package = DataPackage(os.environ.get("ARENA_DATA", "data/package"))
    package.lookup("")
    client = ArenaClient()
    cases = hosted(client.cases("practice"))
    if args.ids:
        ids = {i if i.startswith("case:") else f"case:{i}" for i in args.ids}
        cases = [c for c in cases if c["case_id"] in ids]
    kb = strat.load(args.knowledge / strat.PLAYBOOK_FILE) if args.command == "learned" else None
    per_case = (args.variants if args.command == "run" else [None]) * args.repeat
    jobs = {c["case_id"]: list(per_case) for c in cases}
    planned = sum(len(v) for v in jobs.values())
    left = (client.me().get("practice") or {}).get("left")
    cap = min(x for x in (args.sessions, left, planned) if x is not None)
    print(json.dumps({"planned_games": planned, "session_cap": cap, "quota_left": left}), flush=True)
    book = None if args.no_replay or args.model else Book.from_rows(read_sheet(args.knowledge))
    run_batch(client, cases, package, jobs, args.label, args.knowledge, args.parallel, args.model, kb, args.holdout,
              book, Budget(cap))  # fmt: skip
    build(args.knowledge)


if __name__ == "__main__":
    main()
