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


def stub(system, user, **kw):
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


def test_revealed_payment_slope_from_main_vs_alternative():
    from team.levers import revealed_slopes

    case = {"negotiable": ["unit_price", "payment_days", "effective_date"]}
    msg = {"term_sheets": [{"terms": {"unit_price": 140.2, "payment_days": 90, "effective_date": "2027-04-01"}},
                           {"terms": {"unit_price": 135.5, "payment_days": 14, "effective_date": "2027-04-01"}}]}
    slope = revealed_slopes(case, [msg])["payment_days"]
    assert 0.1 < slope * 360 < 0.2  # about 16 % a year: what paying later costs it


def test_no_stricter_ask_than_needed():
    """Equal C: ask for the option closest to the supplier's value (lead 42 for a 42-day need, not 35)."""
    for path in RUNS:
        d = load(path)
        case = d["case"]
        if "lead_time_days" not in case["negotiable"]:
            continue
        a = agent_for(case)
        need = case["buyer"]["need_lead_time_days"]
        options = case["term_options"].get("lead_time_days", [])
        threads = threads_until(d, 1 + len(case["suppliers"]) - 1)
        for sid, ev in threads.items():
            plan = a.plan(case, ev, sid, threads)
            for s in plan["sheets"]:
                stricter = [o for o in options if o < need]
                if stricter and s["lead_time_days"] in stricter:
                    assert a.cost(case, {**s, "lead_time_days": min(o for o in options if o >= need)},
                                  plan["std"], plan["sup"]) > a.cost(case, s, plan["std"], plan["sup"]) + 1, path


def test_repeated_claims_are_dropped_keeping_paragraphs():
    a = TeamAgent(stub, None)
    threads = {"sup:1": [{"from": "buyer", "body": "Index fell (idx:A@2025-08, idx:A@2026-08)."}]}
    body = "Dear team,\n\nIndex fell (idx:A@2025-08, idx:A@2026-08). Your OTD was 80% (sc:1@2026Q3).\n\nRegards"
    out = a.drop_repeated_claims(threads, "sup:1", body)
    assert "idx:A" not in out and "sc:1@2026Q3" in out and out.startswith("Dear team,\n\n")


def test_two_broken_must_haves_are_both_repaired():
    case = {"negotiable": ["unit_price", "lead_time_days", "volume_per_year"],
            "term_options": {"lead_time_days": [28, 35, 42, 56], "volume_per_year": [1000, 1500, 2000]},
            "buyer": {"must_haves": [{"field": "lead_time_days", "op": "<=", "value": 42},
                                     {"field": "volume_per_year", "op": ">=", "value": 1800}],
                      "need_per_year": 2000, "horizon_years": 1, "reference_payment_days": 60,
                      "need_lead_time_days": 42, "freight_eur_per_unit": {"EXW": 1.0, "FCA": 0.5, "DAP": 0.0}}}
    sheet = TeamAgent.keep_must_haves(case, {"unit_price": 10.0, "lead_time_days": 56, "volume_per_year": 1000}, {})
    assert sheet["lead_time_days"] == 42 and sheet["volume_per_year"] == 2000  # nearest options that keep both
