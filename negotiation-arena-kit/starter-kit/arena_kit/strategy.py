"""The numbers of a negotiation, decided by code: what to offer, when to accept, when to walk.

Everything is in C, the case's total cost (`cost.total_cost`), with our estimate of the supplier so
that strategic and bottleneck cases price a squeezed supplier too. Each round has a target C:

  anchor     the C of an opening price taken from the data: the lowest of the goal's cut off their
             quote, no increase at all (price increases), the best rival quote and a fair price
             (the supplier's should-cost plus a margin for the goal) - never deeper than
             MAX_ANCHOR_CUT, never below its cost floor or the price at which C stops falling.
             It is priced on OUR reference terms (payment days, volume, contract length): what the
             supplier asks beyond them is never absorbed into the target for free
  concession target = anchor + f(round) × (their C now − anchor), f rising and flattening, so the
             steps shrink - and a supplier that did not move gets half a step
  sheets     up to three term sheets that all cost us that target C: their terms at a lower price,
             a trade the supplier may value (e.g. faster payment) at a lower price still, and our
             preferred terms at a higher price. Which one it answers tells us what it values.
             When the supplier asked for a term (e.g. payment in 15 days): two sheets instead - yes
             to its ask at a lower price that still leaves it better off, or our terms at the target.
"""

import math
import re

from .cost import RELATIONSHIP_GOALS, closing_terms, complete_offer, must_haves_ok, snap, supplier_value, total_cost
from .dossier import safe_query

ANCHOR = {"leverage": 0.12, "routine": 0.08, "quality_first": 0.06, "stabilise": 0.06, "secure_supply": 0.05}
# Which way each field is worth more to the supplier: -1 lower, +1 higher (index_clause: having one).
SUPPLIER_LIKES = {
    "payment_days": -1,
    "effective_date": -1,
    "contract_months": +1,
    "volume_per_year": +1,
    "lead_time_days": +1,
    "ppm_commitment": +1,
    "chargeback_eur_per_complaint": -1,
    "tooling_eur": +1,
    "incoterm": -1,
    "index_clause": +1,
}
INCOTERM_RANK = {"EXW": 0, "FCA": 1, "DAP": 2}  # the supplier carries more freight up the list
# Levers to try first when the profile names none.
TYPE_LEVERS = {
    "price_increase": ["effective_date", "payment_days"],
    "raw_material": ["index_clause", "payment_days", "contract_months"],
    "renewal": ["contract_months", "volume_per_year", "payment_days"],
    "bundling": ["contract_months", "payment_days"],
    "capacity": ["volume_per_year", "lead_time_days", "contract_months"],
    "quality": ["chargeback_eur_per_complaint", "ppm_commitment"],
    "rfq": ["payment_days", "lead_time_days", "tooling_eur"],
}
SAFETY = 0.03  # below the should-cost's own confidence band: we would rather under- than overrate a floor
OPENING_GUESS = "opening price - 10%"
MIN_STEP = 0.004  # our smallest visible concession, 0.4 % of C: below it we hold instead
DECOY_GAP = 0.02  # the lean option: the recommended one 2 % cheaper and one term worse for the supplier
RECIPROCITY = 0.5  # our step answers theirs with at most this share of it
# A term we give the supplier is worth something to it (faster payment: its cost of money, more when
# short of cash) and costs us something (our interest). Between the two lies a gain for both sides;
# the sheet that gives the term splits it, so the supplier is better off taking it and so are we.
SURPLUS_SHARE = 0.5  # our share of that gain
ROUND_HORIZON = 6  # rounds per thread we plan for: the Live Final's cap leaves about this many
# The opening comes from the data. A fair price is the supplier's should-cost plus a margin the goal
# allows (relationship goals: the case's sustainable margin - below it C rises again).
FAIR_MARGIN = {"leverage": 0.05, "routine": 0.06, "quality_first": 0.08}
MAX_ANCHOR_CUT = 0.25  # never open more than 25 % under their price: below that it is not credible
CASH_SHORT_PREMIUM = 0.06  # a supplier short of cash values money this much more per year than we do
# Fields with a buyer reference (the plan): what the supplier asks beyond it is a give from us.
REFERENCE_FIELDS = ("payment_days", "volume_per_year", "contract_months", "lead_time_days", "index_clause")


def options(case, field, std):
    opts = case.get("term_options", {}).get(field)
    if opts:
        return list(opts)
    value = std.get(field, case.get("fixed_terms", {}).get(field))
    return [value] if value is not None else []


def _order(field, values):
    """Values from least to most attractive for the supplier."""
    if field == "index_clause":
        return sorted(values, key=lambda v: (v is not None, -(v or {}).get("deadband_pct", 0)))
    if field == "incoterm":
        return sorted(values, key=lambda v: -INCOTERM_RANK.get(v, 1))
    if not all(isinstance(v, int | float | str) for v in values):
        return values
    return sorted(values, reverse=SUPPLIER_LIKES.get(field, 1) < 0)


def step(case, field, value, std, toward_supplier=True, n=1):
    """The option `n` steps from `value` toward what the supplier (or we) prefer, or None."""
    vals = _order(field, options(case, field, std))
    if value not in vals or len(vals) < 2:
        return None
    i = vals.index(value) + (n if toward_supplier else -n)
    return vals[i] if 0 <= i < len(vals) else None


class Estimate:
    """Our view of one supplier: its cost floor and its cost of money, from the data and the thread."""

    def __init__(self, full_cost=None, interest_rate=0.08, source="none", opening=None, point=None):
        self.full_cost, self.interest_rate, self.source = full_cost, interest_rate, source
        self.opening = opening or {}  # its opening term sheet: standard terms when no quote PDF says
        self.point = point if point is not None else full_cost  # the cost estimate itself (fair price)

    def supplier(self, case, std):
        if self.full_cost is None:
            return None
        standard = closing_terms(case, {}, {**self.opening, **std})
        return {"full_cost": self.full_cost, "interest_rate": self.interest_rate, "standard": standard}


def breakdown_cost(text):
    """Full cost per unit from a cost-breakdown XLSX (as `docs.read_document` reads it): the sum of
    its EUR elements except margin and totals."""
    total, found = 0.0, False
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 2 or re.search(r"margin|profit|ebit|unit price|total|sum|price", cells[0], re.I):
            continue
        try:
            total += float(cells[1])
            found = True
        except ValueError:
            continue
    return round(total, 2) if found and total > 0 else None


def estimate(case, sid, package, threads_events, opening, cash_short=False):
    """Full cost: the supplier's own cost breakdown, else the should-cost estimate at the low end of
    its confidence band, else 10 % under its opening price. Cost of money: the buyer's rate, more for
    a supplier short of cash. `opening`: its opening term sheet.

    The estimate errs low on purpose. It only prices the supplier's health (strategic and bottleneck
    goals), and the supplier guards its own floor - it never sells below its cost. A floor set too
    high makes every lower price look costly and the agent stops pushing (P01, 10 Oct: it accepted
    136.30 with the arena charging nothing for the supplier's health; 133.70 was reachable)."""
    rate = case["buyer"].get("interest_rate", 0.08) + (CASH_SHORT_PREMIUM if cash_short else 0.0)
    opening = {k: v for k, v in (opening or {}).items() if k != "unit_price"}
    for m in reversed(threads_events or []):
        for att in m.get("attachments") or []:
            if "breakdown" in (att.get("name") or "").lower() and att.get("text"):
                if (fc := breakdown_cost(att["text"])) is not None:
                    return Estimate(fc, rate, "cost breakdown", opening)
    if package:
        for p in case.get("parts", []):
            rows = safe_query(
                package, "SELECT supplier_id, unit_cost, confidence_pct FROM should_cost WHERE part_id = ?", (p,)
            )
            rows.sort(key=lambda r: r["supplier_id"] != sid)
            if rows and rows[0]["unit_cost"]:
                low = 1 - (rows[0].get("confidence_pct") or 5) / 100 - SAFETY
                point = rows[0]["unit_cost"]
                return Estimate(round(point * low, 2), rate, "should-cost, low end", opening, point=point)
    price = None
    for m in threads_events or []:
        for s in (m.get("term_sheets") or []) if m.get("from") != "buyer" else []:
            price = price or s["terms"].get("unit_price")
    if price:
        return Estimate(round(price / 1.10, 2), rate, OPENING_GUESS, opening)
    return Estimate(None, rate, opening=opening)


def cost(case, terms, std, est):
    sup = est.supplier(case, std) if est and case["buyer"].get("category_goal") in RELATIONSHIP_GOALS else None
    return total_cost(case, terms, std, sup)


def price_at(case, combo, std, est, target, lo, hi):
    """The unit price at which `combo` costs `target` - on the side where a higher price costs more
    (below the C-minimising price a lower price only hurts). None if `target` is out of reach."""

    def c(p):
        return cost(case, {**combo, "unit_price": round(p, 2)}, std, est)

    # C falls, then rises, in price (the relationship term); find the bottom on a grid.
    grid = [lo + (hi - lo) * i / 40 for i in range(41)]
    best = min(grid, key=c)
    if c(best) > target:
        return None
    a, b = best, hi
    if c(b) < target:
        return round(hi, 2)
    for _ in range(50):
        mid = (a + b) / 2
        a, b = (mid, b) if c(mid) < target else (a, mid)
    return math.floor(a * 100) / 100


def c_alternative(case):
    alt = case["buyer"].get("alternative") or {}
    if alt.get("kind") in (None, "none") or not (alt.get("terms") or {}).get("unit_price"):
        return math.inf
    return total_cost(case, alt["terms"])


def acceptable(case, terms, std):
    return must_haves_ok(case, terms, std) and total_cost(case, terms, std) < c_alternative(case)


def concession(k, horizon=ROUND_HORIZON):
    """Share of the gap conceded by our k-th offer: 0, then shrinking steps, 1 at the horizon."""
    return 1 - (1 - min(k, horizon) / horizon) ** 1.6


class Plan:
    """Our side of one thread: the target C of every offer we made."""

    def __init__(self):
        self.anchor = None
        self.targets = []
        self.their_costs = []
        self.holds = 0  # rounds in a row we held our position
        self.replies = 0  # supplier messages counted into their_costs


def c_min_price(case, combo, std, est, lo, hi):
    """The price at which `combo` costs us least (relationship goals: below it the supplier's margin
    costs more than the price saves), on a grid between `lo` and `hi`."""
    if not est or est.full_cost is None or case["buyer"].get("category_goal") not in RELATIONSHIP_GOALS:
        return lo
    grid = [lo + (hi - lo) * i / 80 for i in range(81)]
    return round(min(grid, key=lambda p: (cost(case, {**combo, "unit_price": round(p, 2)}, std, est), -p)), 2)


def fair_price(case, est):
    """What the data says the part is worth: the supplier's cost plus the margin the goal allows."""
    if not est or not est.point or est.source == OPENING_GUESS:
        return None
    b = case["buyer"]
    if b.get("category_goal") in RELATIONSHIP_GOALS and b.get("sustainable_margin_pct"):
        margin = b["sustainable_margin_pct"] / 100
    else:
        margin = FAIR_MARGIN.get(b.get("category_goal"), 0.06)
    return round(est.point / (1 - margin), 2)  # margin on the price, as the arena counts it


def anchor_price(case, terms, est, their_price, combo=None, std=None):
    """Our opening price, from the data (see the module doc). `their_price`: the best quote on the
    table (an RFQ anchors every bidder to it). `combo`/`std`: the terms the price is for - with them
    the anchor never goes below the price at which C stops falling."""
    goal = case["buyer"].get("category_goal", "routine")
    candidates = [their_price * (1 - ANCHOR.get(goal, 0.08))]
    current = case["buyer"].get("current_unit_price")
    if case.get("type") in ("price_increase", "raw_material") and current and current < their_price:
        candidates.append(current)  # an announced increase: start from no increase at all
    if (fair := fair_price(case, est)) is not None:
        candidates.append(fair)
    p = max(min(candidates), their_price * (1 - MAX_ANCHOR_CUT))
    if est and est.full_cost and est.source != OPENING_GUESS:
        p = max(p, est.full_cost * 1.01)  # below its cost it never sells: an anchor there is noise
    if combo is not None:
        p = max(p, c_min_price(case, combo, std or {}, est, p, their_price))
    return round(min(p, their_price), 2)


def reference_combo(case, combo, std, est, price):
    """`combo` with every field the supplier pulled beyond our reference (the buyer's plan: payment
    days, volume, contract length, lead time, no index clause) put back where that saves us money.
    The target is priced on these terms, so a supplier's ask is never conceded for free."""
    plan = complete_offer(case, {"index_clause": None})
    out = dict(combo)
    for f in REFERENCE_FIELDS:
        if f not in case["negotiable"] or f not in out or plan.get(f) == out[f]:
            continue
        if f != "index_clause" and plan.get(f) is None:
            continue
        moved = {**out, f: plan.get(f)}
        if must_haves_ok(case, moved, std) and cost(case, {**moved, "unit_price": price}, std, est) < cost(
            case, {**out, "unit_price": price}, std, est
        ):
            out = moved
    return out


NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def supplier_asks(case, their_terms, std, est, hints=()):
    """{field: value} the supplier asked for beyond our reference terms: in its term sheet, or in its
    e-mail (`hints`: the analyst's lever hints, e.g. {"field": "payment_days", "hint": "wants payment
    within 15 days"}; numbers are snapped to the case's options)."""
    base = mutual_combo(case, their_terms, std, est)  # a move that helps both sides is no ask
    price = their_terms.get("unit_price") or 0
    ref = reference_combo(case, base, std, est, price) if price else base
    asks = {f: base[f] for f in REFERENCE_FIELDS if f in base and base[f] != ref.get(f)}
    opts = case.get("term_options", {})
    for h in hints or ():
        f = h.get("field")
        if f in asks or f not in case["negotiable"] or f == "unit_price" or not opts.get(f):
            continue
        if not all(isinstance(o, int | float) for o in opts[f]):
            continue
        nums = [float(n.replace(",", ".")) for n in NUMBER.findall(str(h.get("hint") or ""))]
        nums = [n for n in nums if min(opts[f]) * 0.5 <= n <= max(opts[f]) * 1.5]
        if not nums:
            continue
        value = snap(case, f, nums[0])
        if price and value != base.get(f):
            worse = cost(case, {**base, f: value, "unit_price": price}, std, est) > cost(
                case, {**base, "unit_price": price}, std, est
            )
            if worse and must_haves_ok(case, {**base, f: value}, std):
                asks[f] = value
    return asks


def supplier_rate(case, est):
    return est.interest_rate if est else case["buyer"].get("interest_rate", 0.08)


def indifference_price(case, give, reference, ref_price, std, est):
    """The price for `give` that leaves the supplier exactly as well off in cash as `reference` at
    `ref_price` (`supplier_value` at its estimated cost of money). Below it the supplier would rather
    take `reference`. None if it cannot be computed."""
    standard = closing_terms(case, {}, {**((est.opening if est else {}) or {}), **(std or {})})
    sup = {"standard": standard, "interest_rate": supplier_rate(case, est), "full_cost": 0.0}
    try:
        v_ref = supplier_value(case, {**reference, "unit_price": ref_price}, sup)
        v0 = supplier_value(case, {**give, "unit_price": 0.0}, sup)
        v1 = supplier_value(case, {**give, "unit_price": 1.0}, sup)
    except (KeyError, TypeError, ValueError):
        return None
    if v1 - v0 <= 0:
        return None
    return (v_ref - v0) / (v1 - v0)


def give_price(p_equal, p_indifferent):
    """The price for a sheet that gives the supplier a term. `p_equal`: the price at which it costs us
    as much as the sheet without the give; `p_indifferent`: the price at which the supplier is as well
    off as without it. When the term is worth more to the supplier than it costs us, the gap is shared
    (SURPLUS_SHARE): both are better off with the give. Otherwise equal cost for us - nothing to gain,
    and the supplier picks by preference."""
    if p_equal is None:
        return None
    if p_indifferent is None or p_indifferent >= p_equal:
        return p_equal
    return math.floor((p_equal - SURPLUS_SHARE * (p_equal - p_indifferent)) * 100) / 100


def base_combo(case, terms, std):
    """Their standing non-price terms, completed and snapped to the case's options."""
    full = complete_offer(case, {**std, **terms})
    opts = case.get("term_options", {})
    # the option itself, not an equal value of another type (a quote PDF reads 0 as 0.0)
    return {k: next((o for o in opts.get(k) or [] if o == v), v) for k, v in full.items() if k != "unit_price"}


def mutual_combo(case, their_terms, std, est):
    """Their non-price terms, moved wherever the supplier's preferred side also saves us money -
    e.g. a longer contract that covers our whole need. Both sides gain: it replaces their terms as
    the structure we negotiate the price on."""
    base = base_combo(case, their_terms, std)
    price = their_terms.get("unit_price") or 0

    def c(combo):
        return cost(case, {**combo, "unit_price": price}, std, est)

    out = dict(base)
    for f in [f for f in base if f in case["negotiable"]]:
        v = step(case, f, out[f], std, toward_supplier=True)
        while v is not None and c({**out, f: v}) < c(out):
            out[f] = v
            v = step(case, f, v, std, toward_supplier=True)
    return out


def sheets(case, std, est, their_terms, target, levers, floor, asks=None):
    """Up to three term sheets around `target` (our C):
    their terms    their non-price terms plus every gain for both sides (`mutual_combo`), at the
                   price that costs us `target`
    a give         one lever moved the supplier's way, which costs us something - for a lower price
                   that shares the gain when the term is worth more to it than it costs us
    our terms      every term at our best - for a higher price, still costing `target`
    When the supplier asked for terms (`asks`, see `supplier_asks`), `ask_sheets` instead.
    """
    base = mutual_combo(case, their_terms, std, est)
    their_price = their_terms.get("unit_price") or 0
    lo, hi = max(0.01, floor), their_price * 1.02
    if asks and (out := ask_sheets(case, std, est, base, their_price, target, asks, lo, hi)):
        return out

    def c(combo):
        return cost(case, {**combo, "unit_price": their_price}, std, est)

    gives = []
    for f in levers:
        if f in base and (v := step(case, f, base[f], std, toward_supplier=True)) is not None:
            gives.append((f"{f} toward them", {**base, f: v}))
    ours = dict(base)
    for f in [f for f in base if f in case["negotiable"]]:  # each term at the option that costs us least
        ours[f] = min(options(case, f, std) or [ours[f]], key=lambda v, f=f: (c({**ours, f: v}), v != ours[f]))
    # Our terms first: the package we want anchors the e-mail; theirs and a trade follow.
    candidates = [*([("our terms", ours)] if ours != base else []), ("their terms", base), *gives]
    for f in [f for f in base if f in case["negotiable"]]:  # one term our way, for a higher price
        if (v := step(case, f, base[f], std, toward_supplier=False)) is not None:
            candidates.append((f"{f} toward us", {**base, f: v}))
    out, seen, cap = [], [], None
    for label, combo in candidates:
        p = price_at(case, combo, std, est, target, lo, hi)
        if label == "their terms":
            cap = p
        elif p is not None and cap and label.endswith("toward them"):
            # a term given to the supplier buys a lower price: the gain from the trade, shared
            p = give_price(p, indifference_price(case, combo, base, cap, std, est))
        if p is None or p >= their_price or p < lo:
            continue
        t = complete_offer(case, {**combo, "unit_price": p})
        if t in seen or not must_haves_ok(case, t, std):
            continue
        seen.append(t)
        out.append((label, t))
        if len(out) == 3:
            break
    return with_decoy(case, std, out, levers, lo)


def ask_sheets(case, std, est, base, their_price, target, asks, lo, hi):
    """Two sheets when the supplier asked for something: [their ask, our reference].
    their ask   exactly what it asked for (e.g. payment in 15 days), at a lower price: between what
                the ask costs us (equal C) and what it is worth to the supplier (`give_price`), so
                taking it leaves both sides better off than the reference
    reference   our reference terms at the target price - the contrast shows what the ask is worth
    No decoy here: a third sheet that takes the ask back would blur the trade. [] if it does not work."""
    ref = reference_combo(case, base, std, est, their_price)
    give = complete_offer(case, {**ref, **asks})
    p_ref = price_at(case, ref, std, est, target, lo, hi)
    if p_ref is None or p_ref >= their_price or give == complete_offer(case, ref):
        return []
    p = give_price(price_at(case, give, std, est, target, lo, hi), indifference_price(case, give, ref, p_ref, std, est))
    if p is None:
        return []
    p = max(p, lo)
    out = []
    for label, combo, price in (("their ask", give, p), ("reference", ref, p_ref)):
        t = complete_offer(case, {**combo, "unit_price": round(price, 2)})
        if t["unit_price"] < their_price and must_haves_ok(case, t, std) and t not in [x for _, x in out]:
            out.append((label, t))
    return out if out and out[0][0] == "their ask" else []


def with_decoy(case, std, out, levers, floor):
    """Small, medium, large: [lean, recommended, other]. The lean option is the recommended one a
    little cheaper and with the term the supplier values most moved our way - clearly worse for it,
    so the recommended option in the middle looks like the sensible choice (decoy and compromise
    effect). Safe by construction: if the supplier takes the lean option, that is better for us."""
    if not out:
        return out
    middle = next((t for label, t in out if label == "their terms"), out[0][1])
    lean = dict(middle)
    fields = [f for f in [*levers, *case["negotiable"]] if f in lean and f != "unit_price"]
    for f in dict.fromkeys(fields):
        if (v := step(case, f, lean[f], std, toward_supplier=False)) is not None:
            lean[f] = v
            break
    lean = complete_offer(case, {**lean, "unit_price": round(middle["unit_price"] * (1 - DECOY_GAP), 2)})
    if lean["unit_price"] < floor or lean in [t for _, t in out] or not must_haves_ok(case, lean, std):
        return out
    rest = [(label, t) for label, t in out if t is not middle][:1]
    return [("lean", lean), ("recommended", middle), *rest]


def summarise_terms(case, terms):
    """What a term sheet means in words, for the e-mail (code writes it, so it matches the sheet)."""
    t = closing_terms(case, terms)
    parts = []
    for f in case["negotiable"]:
        v = terms.get(f, t.get(f))
        if f == "unit_price":
            parts.insert(0, f"EUR {v:.2f} per unit")
        elif f == "payment_days":
            parts.append(f"payment {v} days net")
        elif f == "effective_date":
            parts.append(f"new price from {v}")
        elif f == "contract_months":
            parts.append("a single order" if v == 0 else f"{v}-month agreement")
        elif f == "volume_per_year":
            parts.append(f"{v:,} pcs per year")
        elif f == "lead_time_days":
            parts.append(f"lead time {v} days")
        elif f == "index_clause":
            parts.append(
                "fixed price, no index clause"
                if not v
                else f"index clause on {v['series']} ({v['material_share']:.0%} material share, "
                f"{v['deadband_pct']:g}% deadband, base {v['base_month']})"
            )
        elif f == "incoterm":
            parts.append(f"delivery {v}")
        elif f == "tooling_eur":
            parts.append(f"tooling EUR {v:,.0f}")
        elif f == "ppm_commitment":
            parts.append(f"quality commitment {v} ppm")
        elif f == "chargeback_eur_per_complaint":
            parts.append(f"EUR {v:,.0f} chargeback per complaint")
        else:
            parts.append(f"{f} {v}")
    return ", ".join(parts)
