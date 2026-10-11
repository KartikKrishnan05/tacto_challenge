"""Concessions: reciprocal, never stuck (section 5), with TeamAgent's step size (R12.5).

Everything is in C. Per thread a `Plan` keeps the anchor (fixed, R4.4; in an RFQ it may only fall,
R12.6), the target of every offer we made and the supplier's C after every reply (one entry per
reply, so a hold shows as a zero step).

  curve        our k-th offer closes 1 - (1 - k/H)^1.6 of the gap from the anchor to their C (R5.1)
  reciprocity  they moved more than MOVE_TOL of C: our step = max(min(planned, 0.5 x theirs), MIN_STEP
               of C) (R5.2); "planned" is at least BREAK_SHARE of the gap left (R12.5); when their step
               was larger than the gap left, we close MEET_SHARE of that gap
  deadlock     they did not move: hold ONCE, then one real step - at least BREAK_SHARE of the gap
               left and MIN_STEP of C (R5.3). Never two holds in a row, never a token step
  still moving their offer meets our target but they conceded more than MIN_STEP of C last round
               with more than 3 rounds left: target = their C - 0.5 x their step (R5.4)
  caps         never above CEILING x their C (R12.5), never above 0.995 x the alternative's C; in an
               RFQ at most 0.99 x the best rival's C and never below the anchor (R5.5)
  no retreat   never below our own last target: when the ceiling falls under it (`at_ceiling`), the
               gap is too small to split and the close rules take its offer
"""

import math
from dataclasses import dataclass, field

MOVE_TOL = 0.001  # R5.2: a move under 0.1 % of C is no move
MIN_STEP = 0.004  # R5.2/R5.3: our smallest visible step, 0.4 % of C
RECIPROCITY = 0.5  # R5.2: we move by at most half of what they moved
BREAK_SHARE = 0.15  # R5.3/R12.5: a real step is at least 15 % of the gap left
CEILING = 0.997  # R12.5: never above 0.997 x their C
ALT_CAP = 0.995  # R5.5
RIVAL_CAP = 0.99  # R5.5
MEET_SHARE = 0.5  # it moved more than the gap left: we close this share of the gap


CURVE_EXP = 1.6  # R5.1


def curve(k, horizon, exp=CURVE_EXP):
    """Share of the gap conceded by our k-th offer (k = 0: the anchor)."""
    return 1 - (1 - min(k, horizon) / horizon) ** exp


@dataclass
class Plan:
    anchor: float | None = None  # in C
    targets: list = field(default_factory=list)  # the target C of every offer we made
    their_costs: list = field(default_factory=list)  # the supplier's C after every reply
    replies: int = 0
    holds: int = 0  # our holds in a row
    # the strategy's knobs (spec.strategy); the defaults are this module's constants
    curve_exp: float = CURVE_EXP
    break_share: float = BREAK_SHARE
    reciprocity: float = RECIPROCITY
    meet_share: float = MEET_SHARE

    def observe(self, c_theirs, replies):
        """Record the supplier's C once per reply (a hold is a zero step)."""
        if replies > self.replies:
            self.their_costs.append(c_theirs)
            self.replies = replies

    def set_anchor(self, c_anchor, rfq=False):
        """Fixed once per thread (R4.4); in an RFQ it may only fall (R12.6)."""
        if self.anchor is None:
            self.anchor = c_anchor
        elif rfq:
            self.anchor = min(self.anchor, c_anchor)
        return self.anchor

    def their_step(self):
        """How far the supplier came toward us in its last reply, in C (0 for a hold)."""
        if len(self.their_costs) < 2:
            return 0.0
        return self.their_costs[-2] - self.their_costs[-1]

    def next_target(self, c_theirs, horizon, rounds_left=99, c_alt=float("inf"), rival_best=None, rfq=False):
        """The target C of our next offer and why: (target, info). Does not record it - `commit` does."""
        anchor = self.anchor if self.anchor is not None else c_theirs
        k = len(self.targets)
        planned = anchor + curve(k, horizon, self.curve_exp) * (c_theirs - anchor)
        step = self.their_step()
        moved = step > MOVE_TOL * c_theirs
        info = {"k": k, "their_step": step, "moved": moved, "kind": "anchor" if not k else None, "ours": None}
        if not k:
            target = planned
        else:
            prev = self.targets[-1]
            gap_left = max(0.0, c_theirs - prev)
            visible = MIN_STEP * c_theirs
            planned_step = max(planned - prev, self.break_share * gap_left)
            if moved:
                ours = max(min(planned_step, self.reciprocity * step), visible)
                if step >= gap_left:
                    # it came further than what is left between us: meet it halfway, not a token step
                    # (P05: it dropped 30.50 -> 28.00, our 4-cent answer made it withdraw)
                    ours = max(ours, self.meet_share * gap_left)
                info["kind"] = "reciprocal"
            elif self.holds >= 1:
                ours = max(self.break_share * gap_left, visible)
                info["kind"] = "deadlock breaker"
            else:
                ours, info["kind"] = 0.0, "hold"
            target = prev + ours
            info["ours"] = ours
        info["still_moving"] = step > MIN_STEP * c_theirs
        if info["still_moving"] and target >= c_theirs and rounds_left > 3:
            # R5.4: still moving means ask again - but never for more than we last offered: going back
            # behind our own last offer reads as a retreat (judgement call, DESIGN_NOTES)
            target = max(c_theirs - 0.5 * step, self.targets[-1] if self.targets else -math.inf)
            info["kind"] = "still moving"
        capped = min(target, CEILING * c_theirs, ALT_CAP * c_alt)
        info["at_ceiling"] = bool(self.targets) and CEILING * c_theirs < self.targets[-1] and capped < target
        target = capped
        if rfq and rival_best is not None:
            target = max(anchor, min(target, RIVAL_CAP * rival_best))
        hard = min(ALT_CAP * c_alt, RIVAL_CAP * rival_best if rfq and rival_best is not None else math.inf)
        if self.targets and target < min(self.targets[-1], hard):
            # never behind our own last offer (P02: their C came within 0.3 % of our 370.85, the ceiling cut the
            # next offer to 370.52, "a step backwards" - and a final offer). At the ceiling the gap is too small
            # to split: the close rules take its offer (closing.decide, at_ceiling)
            target = min(self.targets[-1], hard)  # the alternative and a cheaper rival bid stay hard caps
        return target, info

    def breaker(self, c_theirs):
        """The deadlock-breaking target (R5.3), when a hold would only repeat our last sheets (R13.7)."""
        prev = self.targets[-1] if self.targets else (self.anchor or c_theirs)
        ours = max(self.break_share * max(0.0, c_theirs - prev), MIN_STEP * c_theirs)
        return max(prev, min(prev + ours, CEILING * c_theirs))

    def commit(self, target):
        """Record an offer we sent at `target`; a target equal to the last one is a hold."""
        hold = bool(self.targets) and target <= self.targets[-1] + 1e-6
        self.holds = self.holds + 1 if hold else 0
        self.targets.append(target)
