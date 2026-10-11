"""Exercise SpecAgent's full runner, documents and close retry against the local API."""

import pytest
from test_sim import team

from arena_kit.agents.spec import SpecAgent
from arena_kit.client import ArenaError
from arena_kit.play import RemoteTable, play


@pytest.mark.parametrize("case_id", ["case:S01", "case:S02", "case:S03"])
def test_spec_plays_simulator_to_a_valid_deal_with_company_and_clock(sim, tmp_path, case_id):
    client = team(sim[0])
    table = RemoteTable(client)
    agent = SpecAgent(log_dir=tmp_path)
    try:
        result = play(table, case_id, agent)
        assert result["ended"] and result["deal"]
        assert 0 <= result["score"] <= 1
        assert agent.clock.elapsed() < 420
        for sid in agent.case["suppliers"]:
            events = client.thread(table.session_id, sid)["messages"]
            for event in events:
                if event.get("from") == "buyer" and event.get("body"):
                    assert "Hollerau Pumpenwerke GmbH" in event["body"]
        assert list(tmp_path.glob("*.jsonl"))
    finally:
        agent.pool.shutdown(wait=False, cancel_futures=True)


def test_r13_16_refused_accept_refreshes_thread_then_uses_current_offer(sim, tmp_path, monkeypatch):
    client = team(sim[0])
    original_send, original_thread = client.send_message, client.thread
    refused, refreshed = [], []

    def send(session_id, sid, message):
        if message.get("action", {}).get("type") == "accept" and not refused:
            refused.append(message)
            raise ArenaError(422, "offer moved on: reload the thread")
        return original_send(session_id, sid, message)

    def thread(session_id, sid):
        if refused:
            refreshed.append(sid)
        return original_thread(session_id, sid)

    monkeypatch.setattr(client, "send_message", send)
    monkeypatch.setattr(client, "thread", thread)
    agent = SpecAgent(log_dir=tmp_path)
    try:
        result = play(RemoteTable(client), "case:S01", agent)
        assert refused and refreshed and result["deal"]
    finally:
        agent.pool.shutdown(wait=False, cancel_futures=True)
