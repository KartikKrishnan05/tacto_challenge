"""Section 5: reciprocity (R5.2), the deadlock breaker (R5.3), still moving (R5.4), caps (R5.5), and
the step size of R12.5."""

import pytest

from arena_kit.spec.concession import BREAK_SHARE, CEILING, MIN_STEP, Plan, curve


def plan(anchor, targets, their_costs):
    p = Plan(anchor=anchor, targets=list(targets), their_costs=list(their_costs), replies=len(their_costs))
    return p


def test_curve_shrinks_and_reaches_the_gap_at_the_horizon():
    steps = [curve(k + 1, 6) - curve(k, 6) for k in range(6)]
    assert curve(0, 6) == 0 and curve(6, 6) == pytest.approx(1)
    assert all(a > b for a, b in zip(steps, steps[1:], strict=False))  # steps shrink over time


def test_reciprocity_we_move_by_at_most_half_their_step():
    """R5.2: they moved 20k (2 %): our step is min(planned, 0.5 x 20k) = 10k."""
    p = plan(900_000, [900_000], [1_000_000, 980_000])
    target, info = p.next_target(980_000, horizon=6)
    assert info["kind"] == "reciprocal"
    assert target == pytest.approx(910_000)


def test_reciprocity_is_always_visible():
    """R5.2: a small real move (0.3 %) is answered with at least 0.4 % of C - never a token step."""
    p = plan(900_000, [900_000], [1_000_000, 997_000])
    target, info = p.next_target(997_000, horizon=6)
    assert info["kind"] == "reciprocal"
    assert target - 900_000 == pytest.approx(MIN_STEP * 997_000)


def test_deadlock_breaker_holds_once_then_makes_a_real_step():
    """R5.3: no move -> hold once; still no move -> at least 15 % of the gap left and 0.4 % of C."""
    p = plan(900_000, [900_000], [1_000_000, 1_000_000])
    target, info = p.next_target(1_000_000, horizon=6)
    assert info["kind"] == "hold" and target == pytest.approx(900_000)
    p.commit(target)
    assert p.holds == 1
    p.observe(1_000_000, 3)
    target, info = p.next_target(1_000_000, horizon=6)
    assert info["kind"] == "deadlock breaker"
    assert target - 900_000 == pytest.approx(max(BREAK_SHARE * 100_000, MIN_STEP * 1_000_000))
    p.commit(target)
    assert p.holds == 0  # never two holds in a row


def test_a_step_we_choose_is_at_least_15_percent_of_the_gap():
    """R12.5: the floor of a step we make is 15 % of the gap left (here the curve would give less)."""
    p = plan(900_000, [900_000] * 5, [1_000_000] * 5 + [940_000])
    target, info = p.next_target(940_000, horizon=6)  # their step 60k: half of it caps at 30k
    assert info["kind"] == "reciprocal"
    assert target - 900_000 >= BREAK_SHARE * 40_000 - 1e-6


def test_still_moving_means_ask_again():
    """R5.4: their offer meets our target but they still concede (> 0.4 % of C) with more than 3 rounds
    left: target = their C - 0.5 x their step (never behind our own last offer)."""
    p = plan(900_000, [996_500], [1_006_000, 1_000_000])
    target, info = p.next_target(1_000_000, horizon=6, rounds_left=6)
    assert info["kind"] == "still moving"
    assert target == pytest.approx(1_000_000 - 0.5 * 6_000)
    assert target >= 996_500
    # with 3 rounds or fewer left, no asking again: the offer meets our target
    target, info = p.next_target(1_000_000, horizon=6, rounds_left=3)
    assert info["kind"] != "still moving" and target == pytest.approx(CEILING * 1_000_000)


def test_caps_alternative_and_rfq_rival():
    """R5.5: never above 0.995 x C_alt; in an RFQ at most 0.99 x the best rival, never below the anchor."""
    p = plan(900_000, [], [1_000_000])
    p.targets = [950_000]
    p.their_costs = [1_000_000, 960_000]
    target, _ = p.next_target(960_000, horizon=5, c_alt=950_000)
    assert target <= 0.995 * 950_000 + 1e-6
    target, _ = p.next_target(960_000, horizon=5, rival_best=920_000, rfq=True)
    assert target <= 0.99 * 920_000 + 1e-6 and target >= 900_000


def test_rfq_anchor_only_falls():
    """R12.6: the anchor is fixed (R4.4); in an RFQ it may move down, never up."""
    p = Plan()
    assert p.set_anchor(900_000) == 900_000
    assert p.set_anchor(850_000) == 900_000  # single thread: fixed
    q = Plan()
    q.set_anchor(900_000, rfq=True)
    assert q.set_anchor(950_000, rfq=True) == 900_000
    assert q.set_anchor(850_000, rfq=True) == 850_000
