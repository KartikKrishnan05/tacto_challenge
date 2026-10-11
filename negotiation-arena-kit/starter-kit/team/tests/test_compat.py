from team.compat import GuardedLLMAgent


class StubAgent:
    def __init__(self):
        self.log = []

    def decide(self, case, threads, sid=None):
        return {"case": case, "threads": threads, "sid": sid}

    def prepare(self, case):
        return case


def test_compat_adapter_preserves_team_runner_signature_and_hooks():
    inner = StubAgent()
    agent = GuardedLLMAgent(inner)

    assert agent.decide({"case_id": "case:P01"}, {}, "sup:1")["sid"] == "sup:1"
    assert agent.prepare({"case_id": "case:P01"})["case_id"] == "case:P01"
    assert agent.log is inner.log

