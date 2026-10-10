import json
import re

from arena_kit import cheatsheet as cs

GOOD = {"kind": "do", "when": "the supplier cites a cost index", "action": "check the index yourself",
        "why": "they concede when the figure is wrong", "evidence": "x"}  # fmt: skip


def _session(root, run, case_id, sid, score, typ="price_increase"):
    d = root / run / f"case-{case_id.split(':')[1]}"
    d.mkdir(parents=True)
    supplier = [
        {"from": "sales@1", "body": "Our price is 100.", "term_sheets": [{"terms": {"unit_price": 100.0}}]},
        {"from": "buyer", "body": "Index data says less.", "term_sheets": []},
        {"from": "sales@1", "body": "We can do 96.", "term_sheets": [{"terms": {"unit_price": 96.0}}]},
    ]
    session = {
        "session_id": sid,
        "case_id": case_id,
        "replay_url": f"https://arena.example/replay/{sid}",
        "case": {"type": typ, "suppliers": ["sup:1"], "buyer": {"category_goal": "leverage"}},
        "result": {"deal": True, "supplier_id": "sup:1", "score": score,
                   "c_open": 100.0, "c_deal": 96.0, "c_best": 90.0},
        "threads": {"sup:1": supplier},
        "backoffice": [
            {"round": 1, "supplier": "Acme GmbH", "from": "Owner", "to": "team",
             "text": "The buyer cites an index. I give half."},
            {"round": 1, "supplier": "Acme GmbH", "from": "Owner", "to": "team", "text": "quote attached (q-1.pdf)"},
        ],
    }  # fmt: skip
    (d / "session.json").write_text(json.dumps(session))


def fake_model(calls):
    def complete(system, user):
        calls.append(system)
        if system == cs.LESSON_SYSTEM:
            leaky = [
                {**GOOD, "when": "Acme cites a cost index"},  # a supplier name
                {**GOOD, "action": "never accept 96 EUR"},  # a euro amount
                {**GOOD, "action": "ask for 90 days"},  # a term value
                {**GOOD, "when": "in a price_increase case"},  # a case-type label
            ]
            return json.dumps({"lessons": [GOOD, *leaky]})
        if system == cs.CONSOLIDATE_SYSTEM:
            return json.dumps({"merge": [], "rewrite": []})
        labels = [line.split()[0] for line in user.split("NEW LESSONS\n")[1].splitlines()]
        if "T1 [" in user:
            filed = [{"lesson": lab, "tip": "T1", "relation": "supports"} for lab in labels]
        else:
            new = {k: GOOD[k] for k in ("kind", "when", "action", "why")}
            filed = [{"lesson": labels[0], "new_tip": new}]
            filed += [{"lesson": lab, "same_as": labels[0]} for lab in labels[1:]]
        return json.dumps({"filed": filed})

    return complete


def test_a_tip_is_established_only_across_cases_and_case_types(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setenv("ARENA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    for i, score in enumerate((0.6, 0.7, 0.8)):
        _session(logs, f"r{i}", "case:P01", f"s{i}", score)
    calls = []
    (tip,) = cs.update(fake_model(calls), log_root=logs, workers=1)["tips"]
    assert tip["support"]["sessions"] == 3 and tip["status"] == "hypothesis"  # one case only

    _session(logs, "r3", "case:P02", "s3", 0.5)
    _session(logs, "r4", "case:P03", "s4", 0.5)
    (tip,) = cs.update(fake_model(calls), log_root=logs, workers=1)["tips"]
    assert tip["support"]["cases"] == 3 and tip["status"] == "hypothesis"  # three cases, one case type

    _session(logs, "r5", "case:P04", "s5", 0.5, typ="rfq")
    calls.clear()
    (tip,) = cs.update(fake_model(calls), log_root=logs, workers=1)["tips"]
    assert calls.count(cs.LESSON_SYSTEM) == 1  # only the new session is read
    assert tip["status"] == "established" and tip["support"]["case_types"] == 2

    rows = [json.loads(line) for line in (tmp_path / "knowledge" / "sessions.jsonl").read_text().splitlines()]
    assert all(len(r["lessons"]) == 1 for r in rows)  # names, euros, term values and labels dropped
    facts = rows[0]["facts"]
    assert facts["gap_closed"] == 0.4 and facts["by_supplier"]["sup:1"]["concession_pct"] == 4.0
    assert [n["text"] for n in facts["backoffice"]] == ["The buyer cites an index. I give half."]
    md = (tmp_path / "knowledge" / "cheatsheet.md").read_text()
    assert "When the supplier cites a cost index: check the index yourself" in md
    assert "P01" not in md and "price_increase" not in md  # the cases stay in the diagnostics


def test_a_do_tip_whose_supporters_score_worse_is_doubtful():
    means = {"case:A": (0.6, 3), "case:B": (0.6, 3), "case:C": (0.6, 3)}
    ev = [{"case_id": c, "type": t, "score": 0.4, "relation": "supports"} for c, t in zip(means, "xyz", strict=True)]
    tip = cs._status({"kind": "do", "evidence": ev}, means)
    assert tip["support"]["lift_vs_same_case"] == -0.2 and tip["status"] == "doubtful"
    assert cs._status({"kind": "avoid", "evidence": ev}, means)["status"] == "established"


def test_consolidation_merges_tips_and_moves_their_lessons():
    tips = {
        "T1": {"id": "T1", "kind": "do", "when": "a", "action": "b", "why": "", "evidence": [{"x": 1}]},
        "T2": {"id": "T2", "kind": "do", "when": "a2", "action": "b2", "why": "", "evidence": [{"x": 2}]},
    }
    known = {"s": {"lessons": [{"tip": "T2"}]}}
    reply = {"merge": [{"into": "T1", "from": ["T2"], "when": "w", "action": "act", "why": "y"}], "rewrite": []}
    cs._consolidate(lambda system, user: json.dumps(reply), tips, known)
    assert list(tips) == ["T1"] and len(tips["T1"]["evidence"]) == 2 and tips["T1"]["when"] == "w"
    assert known["s"]["lessons"][0]["tip"] == "T1"


def test_diagnostics_need_several_cases_per_group(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    for i in range(3):
        _session(tmp_path / "logs", f"r{i}", f"case:P0{i}", f"s{i}", 0.5 + i / 10)
    sheet = cs.update(None, log_root=tmp_path / "logs")
    diag = json.loads((tmp_path / "knowledge" / "diagnostics.json").read_text())
    assert sheet["tips"] == [] and diag["results"]["all"]["all"]["score_mean"] == 0.6
    assert diag["results"]["competition"] == {"one supplier": diag["results"]["all"]["all"]}
    assert diag["supplier_reasoning"][0]["cases"] == 3


def test_negotiating_never_touches_the_cheatsheet():
    """The cheatsheet is built by hand and read by no agent: nothing that plays a case may import it."""
    from pathlib import Path

    root = Path(cs.__file__).resolve().parents[1]
    players = [
        p for p in [*(root / "arena_kit").rglob("*.py"), *(root / "team").rglob("*.py")] if p.name != "cheatsheet.py"
    ]
    hits = [str(p.relative_to(root)) for p in players if re.search(r"\bcheatsheet\b|knowledge/", p.read_text())]
    assert hits == []
