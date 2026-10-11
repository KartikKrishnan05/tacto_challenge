"""Evaluation must not hide reply-clock violations behind a successful deal."""

from arena_kit.spec.evaluate import audit


def test_audit_reports_structured_timeouts_even_without_email_warning():
    report = audit({"sup:1": {
        "messages": [{"from": "supplier", "body": "Our final offer.", "status": "final_offer"}],
        "timeouts": [{"round": 2, "kind": "reply_clock"}],
    }})
    assert report["timeouts"] == [{"supplier_id": "sup:1", "timeout": {"round": 2, "kind": "reply_clock"}}]
    assert not report["timeout_mentions"]


def test_audit_checks_claims_company_and_timeout_text():
    report = audit({"sup:1": {"messages": [
        {"from": "buyer", "body": "Please confirm."},
        {"from": "supplier", "body": "Timeout warning", "status": "open",
         "claim_results": [{"result": "contradicted", "data_id": "bad"}]},
    ]}})
    assert report["buyer_without_company"] == ["sup:1"]
    assert report["contradicted"] == [{"result": "contradicted", "data_id": "bad"}]
    assert report["timeout_mentions"] == ["sup:1"]
    assert not report["timeouts"]
