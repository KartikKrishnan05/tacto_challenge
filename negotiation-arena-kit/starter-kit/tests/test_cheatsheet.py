import json

from arena_kit import cheatsheet as cs


def _session(root, run, case_id, sid, score):
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
        "case": {"type": "price_increase", "suppliers": ["sup:1"], "buyer": {"category_goal": "leverage"}},
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
            return json.dumps(
                {
                    "lessons": [
                        {"scope": "price_increase", "kind": "do", "lesson": "Cite the index early.", "evidence": "x"},
                        {"scope": "all", "kind": "do", "lesson": "Acme gives half the step.", "evidence": "x"},
                        {"scope": "all", "kind": "avoid", "lesson": "Never accept 96 EUR.", "evidence": "x"},
                    ]
                }
            )
        labels = [line.split()[0] for line in user.split("NEW LESSONS\n")[1].splitlines()]
        if "T1 [" in user:
            filed = [{"lesson": lab, "tip": "T1", "relation": "supports"} for lab in labels]
        else:
            new = {"scope": "price_increase", "kind": "do", "text": "Cite the index early."}
            filed = [{"lesson": labels[0], "new_tip": new}]
            filed += [{"lesson": lab, "same_as": labels[0]} for lab in labels[1:]]
        return json.dumps({"filed": filed})

    return complete


def test_tips_need_sessions_from_several_cases(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setenv("ARENA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    _session(logs, "r1", "case:P01", "s1", 0.6)
    _session(logs, "r2", "case:P01", "s2", 0.7)
    _session(logs, "r3", "case:P01", "s3", 0.8)
    calls = []
    sheet = cs.update(fake_model(calls), log_root=logs, workers=1)
    (tip,) = sheet["tips"]
    assert tip["support"]["sessions"] == 3 and tip["support"]["cases"] == 1
    assert tip["status"] == "hypothesis"  # three sessions, but all of one case

    _session(logs, "r4", "case:P02", "s4", 0.5)
    calls.clear()
    sheet = cs.update(fake_model(calls), log_root=logs, workers=1)
    assert calls.count(cs.LESSON_SYSTEM) == 1  # only the new session is read
    (tip,) = sheet["tips"]
    assert tip["status"] == "established" and tip["support"]["cases"] == 2

    rows = [json.loads(line) for line in (tmp_path / "knowledge" / "sessions.jsonl").read_text().splitlines()]
    lessons = [les["lesson"] for r in rows for les in r["lessons"]]
    assert lessons and all(les == "Cite the index early." for les in lessons)  # names and euros dropped
    facts = rows[0]["facts"]
    assert facts["gap_closed"] == 0.4 and facts["by_supplier"]["sup:1"]["concession_pct"] == 4.0
    assert [n["text"] for n in facts["backoffice"]] == ["The buyer cites an index. I give half."]
    assert "Cite the index early." in (tmp_path / "knowledge" / "cheatsheet.md").read_text()


def test_statistics_without_a_model(tmp_path, monkeypatch):
    monkeypatch.setenv("ARENA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    _session(tmp_path / "logs", "r1", "case:P01", "s1", 0.6)
    _session(tmp_path / "logs", "r2", "case:P02", "s2", 0.8)
    sheet = cs.update(None, log_root=tmp_path / "logs")
    assert sheet["tips"] == [] and sheet["stats_by_case_type"]["price_increase"]["score_mean"] == 0.7
    assert sheet["supplier_mechanics"][0]["cases"] == 2
