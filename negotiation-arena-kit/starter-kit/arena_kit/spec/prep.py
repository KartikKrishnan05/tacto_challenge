"""What to compute from the case JSON before the first message (section 0.7).

  1  check the case file (`normalise`): every negotiable field but the price has options, no field is
     both fixed and negotiable, must-haves name real fields; values outside the practice ranges of
     section 0 are logged; values the API itself rules out fall back to defaults. Never crashes.
  2  C of every opening quote with the supplier's standard terms filled in, and C_alt
  3  coverage: does contract_months x volume_per_year cover the need over the horizon - and which
     option closes the gap and lowers C (R12.1-R12.2)
  4  the lever table (R12.3), the playbook row (R13.10), the anchor (R4, R13.12) and the floor (R6.5)
  5  the must-have filter every sheet and every accept passes through
  6  the clock: round_limit and, in the final, seconds_left

Judgement call on "fall back to defaults": the arena prices C with the case's own values, so a value
merely outside the practice range (e.g. an interest rate of 10 %) is used as given and logged; only a
value the API rules out (a horizon over 3 years, a negative price, a wrong type, a missing field) is
replaced by a default.
"""

import copy
import logging
import math
from dataclasses import dataclass, field

from ..cost import OPS, must_haves_ok, total_cost
from ..docs import standard_terms
from . import estimate as est_mod
from . import levers, playbook, pricing
from .clock import CaseClock
from .sheets import Ctx, cover, floor, repair
from .terms import COVERAGE, complete, uncovered_units

log = logging.getLogger("arena")

TERM_FIELDS = {
    "unit_price", "payment_days", "lead_time_days", "volume_per_year", "contract_months", "effective_date",
    "index_clause", "incoterm", "tooling_eur", "ppm_commitment", "chargeback_eur_per_complaint",
}  # fmt: skip
TYPES = {"price_increase", "renewal", "rfq", "bundling", "raw_material", "capacity", "quality"}
GOAL_RATES = {"leverage": (3, 1), "routine": (5, 2), "quality_first": (8, 2), "stabilise": (10, 4),
              "secure_supply": (25, 3)}  # fmt: skip  # (fallback_risk_pct, index_risk_pct)
# field: (practice range - log outside, API range - default outside, default)
BUYER_RANGES = {
    "need_per_year": ((3420, 94800), (1e-9, math.inf), None),
    "horizon_years": ((1, 2), (1e-9, 3), 1.0),
    "current_unit_price": ((4.20, 412.74), (0.0, math.inf), None),
    "fallback_unit_price": ((1.0, 1000.0), (0.0, math.inf), None),
    "interest_rate": ((0.08, 0.08), (0.0, 1.0), 0.08),
    "reference_payment_days": ((60, 60), (0, 120), 60),
    "need_lead_time_days": ((5, 59), (0, 3650), None),
    "late_cost_per_day": ((40, 2400), (0, math.inf), 0.0),
    "cost_per_defect": ((0, 780), (0, math.inf), 0.0),
    "sustainable_margin_pct": ((0.5, 10), (0, 100), None),
    "relationship_eur_per_point": ((5000, 31920), (0, math.inf), None),
    "fallback_risk_pct": ((3, 25), (0, 1000), None),
    "index_risk_pct": ((1, 4), (0, 100), None),
}


def _num(v):
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def normalise(case):
    """(a checked copy of the case, warnings). Never raises on a field outside section 0's ranges."""
    w = []
    c = copy.deepcopy(case) if isinstance(case, dict) else {}
    c.setdefault("case_id", "case:?")
    if c.get("type") not in TYPES:
        w.append(f"type {c.get('type')!r} unknown: levers from `negotiable`")
    neg = [f for f in c.get("negotiable") or [] if isinstance(f, str)]
    if "unit_price" not in neg:
        w.append("negotiable without unit_price: added")
        neg = ["unit_price", *neg]
    for f in neg:
        if f not in TERM_FIELDS:
            w.append(f"negotiable field {f!r} is not a known term")
    c["negotiable"] = neg
    c["fixed_terms"] = dict(c.get("fixed_terms") or {}) if isinstance(c.get("fixed_terms"), dict) else {}
    c["term_options"] = dict(c.get("term_options") or {}) if isinstance(c.get("term_options"), dict) else {}
    for f in [f for f in c["fixed_terms"] if f in neg]:
        w.append(f"{f} is both fixed and negotiable: negotiable wins")
        c["fixed_terms"].pop(f)
    for f in neg:
        if f != "unit_price" and not c["term_options"].get(f):
            w.append(f"negotiable {f} has no term_options: the supplier's standard value is sent")
    for f, opts in list(c["term_options"].items()):
        if not isinstance(opts, list):
            w.append(f"term_options[{f}] is not a list: dropped")
            c["term_options"].pop(f)
    rl = c.get("round_limit")
    if not (_num(rl) and rl >= 1):
        w.append(f"round_limit {rl!r}: default 10")
        c["round_limit"] = 10
    elif rl != 10:
        w.append(f"round_limit {rl} (practice: 10)")
    for k in ("suppliers", "parts", "refs"):
        c[k] = [x for x in c.get(k) or [] if isinstance(x, str)]
    if not c["suppliers"]:
        w.append("no suppliers in the case file")
    b = c["buyer"] = dict(c.get("buyer") or {})
    goal = b.get("category_goal")
    if goal not in playbook.PLAYBOOK:
        w.append(f"category_goal {goal!r} unknown: routine row")
    for k, ((lo, hi), (alo, ahi), default) in BUYER_RANGES.items():
        v = b.get(k)
        if v is None:
            if k in ("sustainable_margin_pct", "relationship_eur_per_point"):
                continue  # null = no margin charge (section 0.4)
            if k in ("fallback_risk_pct", "index_risk_pct"):
                b[k] = GOAL_RATES.get(goal, GOAL_RATES["routine"])[0 if k == "fallback_risk_pct" else 1]
                w.append(f"{k} missing: {b[k]} for the goal")
                continue
            if default is not None:
                b[k] = default
                w.append(f"{k} missing: default {default}")
            else:
                w.append(f"{k} missing")
            continue
        if not _num(v) or not (alo <= v <= ahi):
            fallback = default if default is not None else (
                GOAL_RATES.get(goal, GOAL_RATES["routine"])[0 if k == "fallback_risk_pct" else 1]
                if k in ("fallback_risk_pct", "index_risk_pct") else None)  # fmt: skip
            w.append(f"{k} = {v!r} is not allowed: default {fallback}")
            b[k] = fallback
        elif not (lo <= v <= hi):
            w.append(f"{k} = {v} outside the practice range {lo}-{hi}")
    if _num(b.get("horizon_years")):
        b["horizon_years"] = float(b["horizon_years"])
    if not _num(b.get("need_per_year")):
        b["need_per_year"] = c["fixed_terms"].get("volume_per_year") or 1
        w.append(f"need_per_year unusable: {b['need_per_year']}")
    if not _num(b.get("current_unit_price")) and _num(b.get("fallback_unit_price")):
        b["current_unit_price"] = b["fallback_unit_price"]
    if not _num(b.get("fallback_unit_price")) and _num(b.get("current_unit_price")):
        b["fallback_unit_price"] = b["current_unit_price"]
        w.append("fallback_unit_price unusable: the current price")
    if b.get("need_lead_time_days") is None:
        b["need_lead_time_days"] = c["fixed_terms"].get("lead_time_days") or 9999
    freight = b.get("freight_eur_per_unit") if isinstance(b.get("freight_eur_per_unit"), dict) else {}
    for inc in ("EXW", "FCA", "DAP"):
        if not _num(freight.get(inc)):
            freight[inc] = 0.0 if inc == "DAP" else max([x for x in freight.values() if _num(x)] or [0.0])
            w.append(f"freight for {inc} missing: {freight[inc]}")
    b["freight_eur_per_unit"] = freight
    if not isinstance(b.get("index_paths"), dict):
        b["index_paths"] = {}
    musts = []
    for m in b.get("must_haves") or []:
        if isinstance(m, dict) and m.get("field") in TERM_FIELDS and m.get("op") in OPS and "value" in m:
            musts.append(m)
        else:
            w.append(f"must-have {m!r} names no real field or operator: ignored")
    b["must_haves"] = musts
    alt = b.get("alternative") if isinstance(b.get("alternative"), dict) else {}
    if alt.get("kind") not in ("quote", "current_contract", "none"):
        w.append(f"alternative kind {alt.get('kind')!r}: treated as none")
        alt = {"kind": "none", "ref": None, "terms": {}}
    if alt.get("kind") != "none" and not _num((alt.get("terms") or {}).get("unit_price")):
        w.append("alternative without a price: treated as none")
        alt = {"kind": "none", "ref": None, "terms": {}}
    b["alternative"] = alt
    for msg in w:
        log.info("%s case check: %s", c.get("case_id"), msg)
    return c, w


def c_alternative(case):
    alt = case["buyer"].get("alternative") or {}
    if alt.get("kind") in (None, "none") or not (alt.get("terms") or {}).get("unit_price"):
        return math.inf
    try:
        return total_cost(case, alt["terms"])
    except Exception:
        return math.inf


@dataclass
class Opening:
    sid: str
    terms: dict
    std: dict
    c: float
    keeps_must_haves: bool
    estimate: est_mod.Estimate
    levers: list = field(default_factory=list)
    best_levers: dict = field(default_factory=dict)
    uncovered_units: float = 0.0
    coverage_fix: dict = field(default_factory=dict)  # {field: option} that closes the gap and lowers C
    coverage_saving: float = 0.0
    anchor: float | None = None  # price
    anchor_c: float | None = None
    floor: float | None = None


@dataclass
class Prep:
    case: dict
    warnings: list
    goal: str
    row: dict
    type_levers: list
    horizon: int
    c_alt: float
    openings: dict
    c_open: float
    clock: CaseClock

    def keeps_must_haves(self, terms, std=None):
        return must_haves_ok(self.case, terms, std)


def opening_terms(events):
    """The supplier's opening sheet from its first e-mail (term sheets, else the structured `terms`)."""
    for m in events or []:
        if m.get("from") == "buyer":
            continue
        sheets = m.get("term_sheets") or []
        if sheets and sheets[0].get("terms"):
            return sheets[0]["terms"]
        if m.get("terms"):
            return m["terms"]
    return {}


def anchor_price(case, ctx, their_price, best_quote, combo):
    """R4.1 + R4.2 + R13.12: the lowest of the goal's cut off the best quote, the current price (no
    increase at all in a price increase / raw material case), the fair price (should-cost / (1 -
    margin)) - lifted to the credibility limits: at most 25 % under its price, never under its full
    cost x 1.01, never under the price at which C stops falling."""
    b, row = case["buyer"], playbook.row(case)
    cands = [best_quote * (1 - playbook.anchor_cut(case))]
    current = b.get("current_unit_price")
    if case.get("type") in ("price_increase", "raw_material") and current and current < their_price:
        cands.append(current)
    est = ctx.est
    if est and est.point and est.source in ("should-cost", "breakdown"):
        margin = (b.get("sustainable_margin_pct") or 0) / 100 if row["fair"] is None else row["fair"]
        if margin:
            cands.append(est.point / (1 - margin))
    p = max(min(cands), their_price * (1 - playbook.MAX_ANCHOR_CUT))
    if est and est.full_cost and est.source in ("should-cost", "breakdown"):
        p = max(p, est.full_cost * 1.01)
    p = max(p, pricing.c_min_price(case, combo, ctx.std, ctx.supplier, p, their_price))
    return round(min(p, their_price), 2)


def prepare(case, threads=None, package=None, volatile=None, seconds_left=None, cash_short=None):
    """Section 0.7 for a case: with `threads` (the opening e-mails, quote PDFs read) every supplier's
    opening is priced; without them (before the session) a notional opening at the current price is
    used, so the preparation runs on the case file alone."""
    try:
        return _prepare(case, threads, package, volatile, seconds_left, cash_short or {})
    except Exception as e:  # a case file we cannot prepare must not cost the case: log and go minimal
        log.exception("%s: preparation failed (%s) - minimal defaults", (case or {}).get("case_id"), e)
        c, w = normalise(case)
        return Prep(c, [*w, f"preparation failed: {e}"], c["buyer"].get("category_goal") or "routine",
                    playbook.row(c), playbook.type_levers(c), playbook.horizon(c), c_alternative(c), {}, math.inf,
                    CaseClock(seconds_left))  # fmt: skip


def _prepare(case, threads, package, volatile, seconds_left, cash_short):
    c, warnings = normalise(case)
    b, row = c["buyer"], playbook.row(c)
    goal = b.get("category_goal") if b.get("category_goal") in playbook.PLAYBOOK else playbook.DEFAULT_GOAL
    c_alt = c_alternative(c)
    if threads:
        quotes = {sid: (opening_terms(ev), standard_terms(ev), ev) for sid, ev in threads.items()}
    else:
        price = b.get("current_unit_price") or b.get("fallback_unit_price") or 1.0
        quotes = {sid: ({"unit_price": price}, {}, []) for sid in c["suppliers"]}
    best_quote = min((q[0].get("unit_price") for q in quotes.values() if q[0].get("unit_price")), default=None)
    openings = {}
    for sid, (terms, std, events) in quotes.items():
        if not terms.get("unit_price"):
            warnings.append(f"{sid} quotes no price")
            continue
        est = est_mod.estimate(c, package, events, terms, cash_short=cash_short.get(sid, False), market_low=best_quote)
        full = complete(c, terms, std)
        ctx = Ctx(c, std, est, full, row, volatile)
        cost = ctx.c(full)
        op = Opening(sid, full, std, cost, must_haves_ok(c, full, std), est)
        sup = ctx.supplier
        op.levers = levers.lever_table(c, full, std, sup)
        op.best_levers = levers.best_levers(c, full, std, sup)
        op.uncovered_units = uncovered_units(c, full, std)
        base = {k: v for k, v in repair(c, full, std).items() if k != "unit_price"}
        # whether covering the need pays depends on the price we get for it (P08: not at its price,
        # below the fallback at ours) - so coverage is judged at our anchor
        first = anchor_price(c, ctx, full["unit_price"], best_quote or full["unit_price"], base)
        fixed = cover(ctx, base, first)
        op.coverage_fix = {f: fixed[f] for f in COVERAGE if f in fixed and fixed.get(f) != base.get(f)}
        combo = {**base, **op.coverage_fix}
        if op.coverage_fix:
            op.coverage_saving = ctx.c({**base, "unit_price": first}) - ctx.c({**combo, "unit_price": first})
        op.anchor = anchor_price(c, ctx, full["unit_price"], best_quote or full["unit_price"], combo)
        op.anchor_c = ctx.c({**combo, "unit_price": op.anchor})
        op.floor = round(floor(ctx), 2)
        openings[sid] = op
    valid = [o.c for o in openings.values() if o.keeps_must_haves]
    return Prep(c, warnings, goal, row, playbook.type_levers(c), playbook.horizon(c), c_alt, openings,
                min(valid, default=math.inf), CaseClock(seconds_left))  # fmt: skip


def summary(prep):
    """One line per supplier for the log."""
    out = [f"{prep.case['case_id']} {prep.case.get('type')}/{prep.goal}: C_alt "
           + (f"{prep.c_alt:,.0f}" if math.isfinite(prep.c_alt) else "none")]  # fmt: skip
    for o in prep.openings.values():
        lv = ", ".join(f"{f}->{levers.label(r['option'])} {-r['delta_eur']:,.0f}" for f, r in o.best_levers.items())
        out.append(
            f"  {o.sid}: C {o.c:,.0f} (price {o.terms['unit_price']}, "
            f"must-haves {'ok' if o.keeps_must_haves else 'BROKEN'})"
            f", uncovered {o.uncovered_units:,.0f} units, coverage {o.coverage_fix or '-'}, anchor {o.anchor}"
            f", floor {o.floor}, estimate {o.estimate.source} {o.estimate.full_cost}; levers: {lv or '-'}"
        )
    return "\n".join(out)
