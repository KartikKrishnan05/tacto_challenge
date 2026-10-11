"""Shared helpers for the spec agent's tests: practice case files, recorded negotiations (the best
runs in runs/best_replays, TeamAgent's offline template), a model stub, thread builders."""

import copy
import json
import os
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
CASES = {c["case_id"]: c for c in json.loads((KIT / "data" / "practice_cases.json").read_text(encoding="utf-8"))}
REPLAYS = sorted((KIT / "runs" / "best_replays").glob("P*.json"))


def package():
    from arena_kit.data import DataPackage

    root = KIT / os.environ.get("ARENA_DATA", "data/package")
    return DataPackage(root) if (root / "facts.json").is_file() or (root / "master.json").is_file() else None


PKG = package()


def case(case_id, **buyer):
    """A practice case file (a deep copy), with buyer fields overridden."""
    c = copy.deepcopy(CASES[case_id])
    c["buyer"].update(buyer)
    return c


def no_model(system, user, timeout=None):
    raise AssertionError("the model must not be called here")


def slow_model(system, user, timeout=None):
    raise TimeoutError("model stubbed")


def load(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    d["case"] = CASES[d["case_id"]]
    return d


def threads_until(d, upto):
    """The threads (arena message shape) as they stood after the first `upto` trace entries."""
    threads = {sid: [] for sid in d["case"]["suppliers"]}
    for step in d["result"]["trace"][:upto]:
        mv = step["move"] or {}
        if step["actor"] == "buyer":
            sid = mv.get("supplier_id") or next(iter(threads))
            threads.setdefault(sid, []).append(
                {"from": "buyer", "body": mv.get("body", ""), "term_sheets": mv.get("term_sheets") or []}
            )
        else:
            sheets = [{"term_sheet_id": mv["offer_id"], "terms": mv.get("terms") or {}}]
            sheets += [{"term_sheet_id": a["offer_id"], "terms": a["terms"]} for a in mv.get("alternatives") or []]
            threads.setdefault(step["actor"], []).append({
                "from": f"sales@{step['actor']}", "status": mv.get("status"), "term_sheets": sheets,
                "missing_fields": mv.get("missing_fields") or [], "body": mv.get("text", ""), "attachments": [],
                "claim_results": mv.get("claim_results") or [], "signals": mv.get("signals") or {},
            })  # fmt: skip
    return {s: ev for s, ev in threads.items() if ev}


def supplier_states():
    """(recording, trace index) after every supplier reply in every recorded best run."""
    for path in REPLAYS:
        d = load(path)
        trace = d["result"].get("trace") or []
        n = len(d["case"]["suppliers"])
        for i, step in enumerate(trace):
            if step["actor"] != "buyer" and i + 1 >= n:
                yield d, i + 1


def agent(case_file, threads=None, complete=None):
    from arena_kit.agents.spec import SpecAgent

    a = SpecAgent(complete, PKG, log_dir=KIT / "runs" / "spec-logs" / "tests")
    a.prime(case_file, timeout=0)
    if threads is not None:
        a.prepare(case_file, threads)
    return a


def opening(sid, terms, offer_id=None, body="Please find our offer.", status="open", alternatives=()):
    sheets = [{"term_sheet_id": offer_id or f"offer:{sid.split(':')[1]}-0", "terms": terms}]
    sheets += [{"term_sheet_id": f"{offer_id or 'offer:x-0'}-a{i}", "terms": t} for i, t in enumerate(alternatives)]
    return {"from": f"sales@{sid.split(':')[1]}", "status": status, "term_sheets": sheets, "body": body,
            "attachments": [], "missing_fields": [], "claim_results": [], "signals": {}}  # fmt: skip


def buyer(sheets, body="Dear colleagues, our options."):
    return {"from": "buyer", "body": body, "term_sheets": [{"terms": t} for t in sheets]}
