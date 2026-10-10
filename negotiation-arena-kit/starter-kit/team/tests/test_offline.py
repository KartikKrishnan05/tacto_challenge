"""Offline checks on recorded practice negotiations (runs/*practice-team-*): no arena API, LLM stubbed.

    uv run --group dev pytest team/tests -q
"""

import glob
import json
import os
from pathlib import Path

import pytest

from arena_kit.cost import complete_offer, cost_breakdown, must_haves_ok
from team.agent import TeamAgent

KIT = Path(__file__).resolve().parents[2]
RUNS = [p for p in sorted(glob.glob(str(KIT / "runs" / "*practice-team-v[4-9]*" / "case_P*.json")))
        if (json.loads(Path(p).read_text(encoding="utf-8")).get("result") or {}).get("trace")]
pytestmark = pytest.mark.skipif(not RUNS, reason="no recorded practice runs in runs/")


def package():
    from arena_kit.data import DataPackage

    root = KIT / os.environ.get("ARENA_DATA", "data/package")
    return DataPackage(root) if (root / "facts.json").is_file() else None


PKG = package()


def stub(system, user):
    raise TimeoutError("LLM stubbed")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def threads_until(d, upto):
    """The threads (arena message shape) as they stood after the first `upto` trace entries."""
    threads = {sid: [] for sid in d["case"]["suppliers"]}
    for step in d["result"]["trace"][:upto]:
        mv = step["move"] or {}
        if step["actor"] == "buyer":
            sid = mv.get("supplier_id") or next(iter(threads))
            threads.setdefault(sid, []).append({"from": "buyer", "body": mv.get("body", ""),
                                                "term_sheets": mv.get("term_sheets") or []})
        else:
            sheets = [{"term_sheet_id": mv["offer_id"], "terms": mv.get("terms") or {}}]
            sheets += [{"term_sheet_id": a["offer_id"], "terms": a["terms"]} for a in mv.get("alternatives") or []]
            threads.setdefault(step["actor"], []).append({
                "from": f"sales@{step['actor']}", "status": mv.get("status"), "term_sheets": sheets,
                "missing_fields": mv.get("missing_fields") or [], "body": mv.get("text", ""), "attachments": []})
    return {s: ev for s, ev in threads.items() if ev}


def agent_for(case):
    a = TeamAgent(stub, PKG)
    a.prepare(case)
    return a


def supplier_states():
    """(file, trace index) after every supplier reply in every recorded run."""
    for path in RUNS:
        d = load(path)
        for i, step in enumerate(d["result"].get("trace") or []):
            if step["actor"] != "buyer":
                yield path, d, i + 1


@pytest.mark.skipif(PKG is None, reason="no data package")
def test_cash_short_only_from_supplier_words():
    flagged, seen = set(), set()
    for path in RUNS:
        case = load(path)["case"]
        a = agent_for(case)
        for sid in case["suppliers"]:
            seen.add(sid)
            if a.supplier_est[sid]["cash_short"]:
                flagged.add(sid)
    assert len(seen) >= 10
    assert len(flagged) <= len(seen) // 2, flagged  # was: every supplier, from our own briefing labels


@pytest.mark.skipif(PKG is None, reason="no data package")
def test_relationship_estimate_close_to_the_arena():
    gaps = []
    for path in RUNS:
        d = load(path)
        r, case = d["result"], d["case"]
        if not r.get("deal") or "relationship" not in (r.get("cost_breakdown") or {}):
            continue
        a = agent_for(case)
        sup = a.supplier(case, r["supplier_id"], {k: v for k, v in r["terms"].items() if v is not None})
        if sup is None:
            continue
        ours = cost_breakdown(case, r["terms"], supplier=sup)["relationship"]
        gaps.append(abs(ours - r["cost_breakdown"]["relationship"]))
    assert gaps
    assert max(gaps) < 5000, sorted(gaps)[-5:]  # was up to 32k with full cost = should-cost


def test_ladder_starts_with_the_cheapest_sheet_on_the_table():
    hits = 0
    for _path, d, upto in supplier_states():
        threads = threads_until(d, upto)
        a = agent_for(d["case"])
        ladder = a.accept_candidates(d["case"], threads)
        rows = [(r["c"], sid, r["id"]) for sid in threads for r in a.thread_sheets(d["case"], threads[sid], sid)
                if r["valid"] and r["latest"]]
        if not rows:
            assert ladder == []
            continue
        best_now = min(rows)
        assert (best_now[1], best_now[2]) in ladder
        first = ladder[0]
        c_first = next(r["c"] for r in a.thread_sheets(d["case"], threads[first[0]], first[0]) if r["id"] == first[1])
        assert c_first <= best_now[0] + 1e-6
        hits += 1
    assert hits > 50


def test_final_offer_is_accepted_when_something_is_acceptable():
    n = 0
    for path, d, upto in supplier_states():
        threads = threads_until(d, upto)
        sid = d["result"]["trace"][upto - 1]["actor"]
        if threads[sid][-1].get("status") != "final_offer":
            continue
        a = agent_for(d["case"])
        plan = a.plan(d["case"], threads[sid], sid, threads)
        rule = a.close_rule(d["case"], threads, sid, plan)
        if a.accept_candidates(d["case"], threads):
            assert rule and rule[0]["message"].get("action", {}).get("type") == "accept", (path, upto, rule)
            n += 1
    assert n >= 20


def test_never_offer_more_than_the_supplier_already_offered():
    for path, d, upto in supplier_states():
        threads = threads_until(d, upto)
        sid = d["result"]["trace"][upto - 1]["actor"]
        if threads[sid][-1].get("status") in ("walked_away", "no_bid"):
            continue
        a = agent_for(d["case"])
        plan = a.plan(d["case"], threads[sid], sid, threads)
        move = a.fallback(d["case"], threads, sid)
        if not move or move.get("idle") or move["message"].get("action"):
            continue
        rows = a.thread_sheets(d["case"], threads[sid], sid)
        valid = [r["c"] for r in rows if r["valid"]]
        its_own = [complete_offer(d["case"], r["terms"]) for r in rows]  # better_earlier re-offers its own sheet
        for ts in move["message"]["term_sheets"]:
            assert must_haves_ok(d["case"], ts["terms"], plan.get("std"))
            if valid and ts["terms"] not in its_own:
                assert a.cost(d["case"], ts["terms"], plan.get("std"), plan.get("sup")) < 0.999 * min(valid), path
