"""Strategy knobs and the learned playbook: what earlier experiments say about how to negotiate a case.

A `Strategy` is a small set of numbers on top of the goal's playbook row and section 5's concession
plan - how deep to anchor, how fast to concede, when to close. `VARIANTS` are the named settings the
lab (`spec.lab`) plays against the practice cases; every game goes into the experiment sheet
(`knowledge/experiments.jsonl`) and `spec.lab build` turns the sheet into `knowledge/learned_playbook.json`.

`choose` reads that playbook for a new case and picks the variant with the best evidence, most specific
match first:

  1  the same case                   (a practice case played again)
  2  the same type and goal          e.g. raw_material / leverage
  3  the same goal                   e.g. stabilise
  4  the same type
  5  all cases                       a variant that won 2+ tested cases and lost none
  6  no learned playbook             base

At each level a variant replaces base only if its mean score gain over base, across the matching cases,
is at least MIN_GAIN and it lost no matching case by more than MAX_LOSS. Beyond the same case, the variant
must also be safe everywhere: no tested case lost by more than MAX_LOSS (sweep-3: firm_deep_fast helped
the leverage cases P08/P18 but lost the leverage RFQ P05 by 0.18 - a hidden case is unseen, so a variant
that can break a case is not risked on one). Ties go to the smaller worst loss, then the variant closest
to base. A group level with no better variant passes on to the next, broader one (the case's own games
answering base stand: that is direct evidence); the last level only takes a
variant that won 2+ cases and lost none anywhere, so passing on never picks one known to hurt. Nothing
found: base. Code decides with this; the model never sees it.
"""

import json
import logging
import math
import os
import statistics
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import playbook

log = logging.getLogger("arena")

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"
PLAYBOOK_FILE = "learned_playbook.json"
MIN_GAIN = 0.005  # a variant must beat base by half a score point on average to replace it
MAX_LOSS = 0.05  # ... and must not have lost any matching case by more than 5 points
MIN_HORIZON = 2
TIE = 0.001  # score differences under this are the same result


@dataclass(frozen=True)
class Strategy:
    """Numbers layered over the playbook row and the concession plan. The defaults are the agent as
    designed (playbook.py, concession.py, prep.ANCHOR_COST_K)."""

    cut_k: float = 1.0  # x the goal's anchor cut (R4.1)
    anchor_cost_k: float = 0.98  # the anchor never goes under the supplier's full cost x this (R4.2)
    horizon_delta: int = 0  # added to section 5's H
    curve_exp: float = 1.6  # the concession curve's exponent (R5.1): higher = bigger early steps
    break_share: float = 0.15  # R5.3 / R12.5: a real step closes at least this share of the gap
    reciprocity: float = 0.5  # R5.2: we move at most this share of what they moved
    meet_share: float = 0.5  # it moved past the gap left: we close this share of the gap
    close_gap_k: float = 1.0  # x the goal's close gap (R13.13)
    # term choice (spec.steps)
    step_order: str = "payment_first"  # payment_first: payment days, the goal's terms, EUR weight; weight: EUR only
    reopen: bool = True  # R13.3: a costly locked term gets a second menu; False: move on to the next term
    max_steps: int = 3  # terms settled one at a time before the price rounds

    def row(self, case):
        """The case's playbook row with the anchor cut, close gap and anchor floor of this strategy.
        The defaults return the row itself, unchanged."""
        base = playbook.row(case)
        if self == Strategy():
            return base
        return {**base, "cut": base["cut"] * self.cut_k, "close_gap": base["close_gap"] * self.close_gap_k,
                "anchor_cost_k": self.anchor_cost_k, "step_order": self.step_order, "reopen": self.reopen,
                "max_steps": self.max_steps}  # fmt: skip

    def horizon(self, case):
        return max(MIN_HORIZON, playbook.horizon(case) + self.horizon_delta)

    def plan_kwargs(self):
        return {"curve_exp": self.curve_exp, "break_share": self.break_share, "reciprocity": self.reciprocity,
                "meet_share": self.meet_share}  # fmt: skip

    def knobs(self):
        return asdict(self)


BASE = Strategy()

# The settings the lab tries. Each one is a hypothesis about how suppliers respond (see `HYPOTHESES`).
VARIANTS = {
    "base": BASE,
    "soft_anchor": Strategy(cut_k=0.5, anchor_cost_k=1.02),
    "deep_anchor": Strategy(cut_k=1.6, anchor_cost_k=0.95),
    "fast_concede": Strategy(horizon_delta=-2, curve_exp=1.2, break_share=0.25, reciprocity=0.75),
    "slow_concede": Strategy(horizon_delta=2, curve_exp=2.2, break_share=0.08, reciprocity=0.35),
    "eager_close": Strategy(close_gap_k=4.0, meet_share=0.75),
    # phase 2, from sweep-1: deep anchoring and fast conceding each helped P08; soft anchors and early closes
    # hurt nearly everywhere
    "deep_fast": Strategy(cut_k=1.6, anchor_cost_k=0.95, horizon_delta=-2, curve_exp=1.2, break_share=0.25,
                          reciprocity=0.75),
    "mid_anchor": Strategy(cut_k=1.3, anchor_cost_k=0.97),
    "firm_close": Strategy(close_gap_k=0.25),
    # phase 3, from sweep-2: firm_close lifted P02 with no loss; deep_fast lifted P07 and P08
    "firm_deep_fast": Strategy(cut_k=1.6, anchor_cost_k=0.95, horizon_delta=-2, curve_exp=1.2, break_share=0.25,
                               reciprocity=0.75, close_gap_k=0.25),
    # phase 4, from the game records: concession pace never applied in P01 (the "meet halfway" rule took over),
    # and the start date - worth EUR 25k at one price - was never offered (payment days went first, twice)
    "measured_meet": Strategy(meet_share=0.2),
    "no_meet": Strategy(meet_share=0.0),
    "weight_order": Strategy(step_order="weight"),
    "no_reopen": Strategy(reopen=False),
    "date_first": Strategy(step_order="weight", reopen=False),
    "one_step": Strategy(max_steps=1),
    "measured_firm": Strategy(meet_share=0.2, close_gap_k=0.25),
    # phase 5, from sweep-4: one_step lifted both raw-material cases (P08, P18), deep_anchor lifted P08
    "deep_one_step": Strategy(cut_k=1.6, anchor_cost_k=0.95, max_steps=1),
    "firm_one_step": Strategy(close_gap_k=0.25, max_steps=1),
}
HYPOTHESES = {
    "base": "The agent as designed (control).",
    "soft_anchor": "Opening closer to the supplier's price keeps it cooperative and avoids walk-aways in"
    " relationship cases (stabilise, secure_supply).",
    "deep_anchor": "A lower opening drags the settlement down where we have leverage (rivals, market data).",
    "fast_concede": "Bigger, earlier steps reach a deal before the supplier hardens or issues a final offer.",
    "slow_concede": "Smaller steps and a longer horizon extract more from suppliers that keep moving.",
    "eager_close": "Accepting once the supplier is within ~2 % of our target avoids losing deals to one"
    " counter too many.",
    "deep_fast": "Deep anchor and fast concession together: the low opening sets the range, the big steps keep"
    " the supplier engaged (both helped P08 alone).",
    "mid_anchor": "A moderately deeper anchor keeps deep_anchor's gain (P08) without its loss on P18.",
    "firm_close": "Since accepting early hurt (eager_close), holding out for a closer match to our target gains more.",
    "firm_deep_fast": "firm_close's gain (P02) and deep_fast's gains (P07, P08) add up in one strategy.",
    "measured_meet": "When the supplier jumps further than the gap left, closing only 20 % of the gap (not 50 %)"
    " keeps us from overshooting: P01's supplier 'agreed' at once to our half-way offer.",
    "no_meet": "Pure reciprocity (half of their step, capped by our curve) without any 'meet it' rule.",
    "weight_order": "Settling the term worth most EUR first (P01: the start date, EUR 25k) beats always opening"
    " with payment days.",
    "no_reopen": "Moving on to the next term instead of a second menu on a locked costly one gets more terms"
    " onto the table (P01 re-asked payment days and never offered April).",
    "date_first": "weight_order and no_reopen together.",
    "one_step": "One term menu, then price rounds: fewer rounds spent on terms the supplier answers in its own"
    " favour.",
    "measured_firm": "measured_meet plus firm_close (the only variant with no loss so far).",
    "deep_one_step": "deep_anchor's P08 gain and one_step's raw-material gains (P08, P18) add up.",
    "firm_one_step": "one_step's raw-material gains plus firm_close's P02 gain, without deep anchoring.",
}


def variant(name):
    if name not in VARIANTS:
        raise KeyError(f"unknown strategy variant {name!r}: {', '.join(VARIANTS)}")
    return VARIANTS[name]


def features(case):
    """What makes cases comparable: type, goal, RFQ or single supplier."""
    b = case.get("buyer") or {}
    return {"type": case.get("type"), "goal": b.get("category_goal"), "rfq": len(case.get("suppliers") or []) > 1}


def load(path=None):
    """The learned playbook, or None when there is none yet (or it is unreadable)."""
    path = Path(path or os.environ.get("SPEC_PLAYBOOK") or KNOWLEDGE_DIR / PLAYBOOK_FILE)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("learned playbook %s unreadable: %s", path, e)
        return None


def gains(entries):
    """{variant: [score gain over base, one per case]} over learned-playbook case entries (`gains` is
    measured against base games of the same mode - code-only or with the model - by `spec.lab`)."""
    out = {}
    for e in entries:
        for name, g in (e.get("gains") or {}).items():
            if name != "base" and name in VARIANTS:
                out.setdefault(name, []).append(g["gain"])
    return out


def changed(name):
    """How many knobs a variant moves away from base."""
    v = VARIANTS[name]
    return sum(1 for k, x in v.knobs().items() if x != getattr(BASE, k))


def safe(entries):
    """The variants that lost no case among `entries` by more than MAX_LOSS."""
    return {name for name, v in gains(entries).items() if min(v) >= -MAX_LOSS}


def best_of(entries, allowed=None, strict=False):
    """(variant, mean gain, cases) of the best supported variant over these case entries; base when
    nothing clears MIN_GAIN without a loss over MAX_LOSS. `allowed`: only these variants may win.
    `strict` (the all-cases level, nothing comparable): it won 2+ cases and lost none. None when the
    entries hold no evidence."""
    g = gains(entries)
    if not g:
        return None
    ranked = sorted(g.items(), key=lambda x: (-round(statistics.mean(x[1]), 6), -min(x[1]), changed(x[0]), x[0]))
    for name, values in ranked:
        mean = statistics.mean(values)
        if mean < MIN_GAIN or min(values) < -MAX_LOSS or (allowed is not None and name not in allowed):
            continue
        if strict and (min(values) < -TIE or sum(x > TIE for x in values) < 2):
            continue
        return name, mean, len(values)
    return "base", 0.0, max(len(v) for v in g.values())


def choose(case, kb, exclude_self=False):
    """(Strategy, variant name, why) for this case from the learned playbook `kb`. `exclude_self`
    ignores the case's own games - a leave-one-out test of how the playbook does on an unseen case."""
    if not kb or not kb.get("cases"):
        return BASE, "base", "no learned playbook"
    f = features(case)
    cid = case.get("case_id")
    others = {k: e for k, e in kb["cases"].items() if not (exclude_self and k == cid)}
    levels = [
        ("same case", [others[cid]] if cid in others else []),
        (f"type {f['type']} + goal {f['goal']}",
         [e for e in others.values() if e.get("type") == f["type"] and e.get("goal") == f["goal"]]),
        (f"goal {f['goal']}", [e for e in others.values() if e.get("goal") == f["goal"]]),
        (f"type {f['type']}", [e for e in others.values() if e.get("type") == f["type"]]),
        ("all cases", list(others.values())),
    ]  # fmt: skip
    everywhere = safe(others.values())
    tested = None
    for label, entries in levels:
        if (got := best_of(entries, None if label == "same case" else everywhere, label == "all cases")) is None:
            continue
        name, mean, n = got
        if name == "base" and label != "same case":  # a broader level may still know a safe one; the case's
            # own games are direct evidence and stand
            tested = tested or f"{label}: no variant beat base across {n} case{'s' if n != 1 else ''}"
            continue
        why = f"{label}: {name} (mean gain {mean:+.3f} over base across {n} case{'s' if n != 1 else ''})"
        return variant(name), name, why
    return BASE, "base", tested or "no comparable case in the learned playbook"


def briefing(case, kb, limit=8):
    """What earlier experiments say about cases like this one, in sentences - recorded at the start of the
    case so its log shows what the agent knew (and the result sheet shows what it learned)."""
    if not kb or not kb.get("cases"):
        return []
    f = features(case)
    out = []
    for label, entries in ((f"goal {f['goal']}", [e for e in kb["cases"].values() if e.get("goal") == f["goal"]]),
                           (f"type {f['type']}", [e for e in kb["cases"].values() if e.get("type") == f["type"]])):
        for name, values in sorted(gains(entries).items(), key=lambda x: -statistics.mean(x[1])):
            m = statistics.mean(values)
            if m >= MIN_GAIN:
                out.append(f"{label}: {name} gained {m:+.3f} over base ({len(values)} cases, worst {min(values):+.3f})")
            elif m <= -0.02:
                out.append(f"{label}: {name} lost {m:+.3f} - avoid")
    return out[:limit]


def describe(strategy):
    """The knobs that differ from base, for the log."""
    d = {k: v for k, v in strategy.knobs().items() if v != getattr(BASE, k)}
    return ", ".join(f"{k}={v}" for k, v in d.items()) or "defaults"


def with_knobs(**knobs):
    """A strategy from explicit knob values (unknown keys ignored, NaN kept out)."""
    ok = {k: v for k, v in knobs.items() if k in BASE.knobs() and not (isinstance(v, float) and math.isnan(v))}
    return replace(BASE, **ok)
