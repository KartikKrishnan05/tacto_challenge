"""Compatibility adapter for running the guarded LLM agent through kit runners.

The vendored ``arena_kit.agents.llm.LLMAgent`` remains unchanged so it can still be used as
the baseline.  This module exposes the team's guarded agent through the same factory shape
accepted by ``team.evaluate`` (``module:factory``) and by the standard two-argument
``agent.decide(case, threads)`` protocol.

Use::

    uv run python -m team.evaluate --cases case:P01 --agent team.compat:make --data ../data

The optional ``sid`` argument is preserved for ``team.runner``.  That lets the evaluation
runner use one guarded decision per supplier thread while the ordinary kit runner can call
the adapter with only ``case`` and ``threads``.
"""

from .agent import make as make_team_agent


class GuardedLLMAgent:
    """Thin protocol adapter; all planning and validation stays in :class:`TeamAgent`."""

    def __init__(self, inner=None):
        self._inner = inner or make_team_agent()

    def decide(self, case, threads, sid=None):
        """Delegate a decision after the guarded planner has prepared the case."""
        return self._inner.decide(case, threads, sid)

    def __getattr__(self, name):
        # The kit and team runners inspect optional hooks such as prepare, fallback, log,
        # standing_cost, and accept_candidates. Keep those hooks visible without duplicating
        # the negotiation implementation here.
        return getattr(self._inner, name)


def make():
    """Return a fresh guarded LLM agent for one case."""
    return GuardedLLMAgent()

