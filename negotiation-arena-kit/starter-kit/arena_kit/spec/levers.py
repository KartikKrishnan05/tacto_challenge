"""What each negotiable term is worth in EUR (R12.3), and what the supplier itself charges for it
(R12.4). Ported from TeamAgent's `team/levers.py`.

For a term sheet every negotiable field is moved to each allowed option, and the price by 1 %; the
change in C is the lever's value in EUR and per committed unit (`price_equiv`). Only levers worth at
least MIN_LEVER_SHARE of C are asked for (coverage always counts): suppliers price every demand in, at
3-7x its value for faster lead time or chargebacks. With the supplier's revealed prices a lever counts
only if it saves us more than the supplier charges; on a tie (within EUR 1) the option closest to its
own value wins - no stricter ask than needed.
"""

import itertools
import statistics

from ..cost import closing_terms, must_haves_ok, total_cost
from .terms import COVERAGE, committed_units, distance, key

PRICE_STEP_PCT = 1.0
MIN_LEVER_SHARE = 0.003  # R12.3: ask only for levers worth at least 0.3 % of C


def revealed_slopes(case, messages, extra=None):
    """What the supplier charges for a term, from its own sheet pairs that differ in one field only:
    {field: relative price change per unit of the field} for numbers (payment days, lead time, ppm),
    {(field, from, to): price ratio} for the rest. Median per field. `extra`: more packages per
    message (prose alternatives, R13.6), as [[terms, ...], ...] aligned with `messages`."""
    fields = [f for f in case.get("negotiable", []) if f != "unit_price"]
    seen = {}
    for n, m in enumerate(messages):
        if m.get("from") == "buyer":
            continue
        sheets = [ts.get("terms") or {} for ts in m.get("term_sheets") or []]
        if extra and n < len(extra):
            # Prose names changed terms only; inherit all others from this e-mail's main sheet.
            main = sheets[0] if sheets else {}
            sheets += [{**main, **t} for t in extra[n] or []]
        sheets = [t for t in sheets if t.get("unit_price")]
        for a, b in itertools.combinations(sheets, 2):
            diff = [f for f in fields if key(a.get(f)) != key(b.get(f))]
            if len(diff) != 1 or diff[0] not in a or diff[0] not in b:
                continue
            f, (va, vb), (pa, pb) = diff[0], (a[diff[0]], b[diff[0]]), (a["unit_price"], b["unit_price"])
            if all(isinstance(v, int | float) and not isinstance(v, bool) for v in (va, vb)):
                if va != vb:
                    seen.setdefault(f, []).append((pb / pa - 1) / (vb - va))
            else:
                seen.setdefault((f, key(va), key(vb)), []).append(pb / pa)
                seen.setdefault((f, key(vb), key(va)), []).append(pa / pb)
    return {k: statistics.median(v) for k, v in seen.items()}


def revealed_rate(slopes):
    """The supplier's cost of money from its own payment-days slope (R2.2): slope x 360, or None.
    Paying later costs it money, so its price rises with the days: a positive slope."""
    s = slopes.get("payment_days")
    return s * 360 if s and s > 0 else None


def lever_table(case, terms, std=None, supplier=None, slopes=None):
    """Every single-field move from `terms`, cheapest for us first:
    [{field, current, option, c, delta_eur, price_equiv, must_haves_ok}]; delta_eur < 0 saves money.
    With `slopes`, a move also carries the price the supplier would charge for it."""
    base_terms = closing_terms(case, terms, std)
    if base_terms.get("unit_price") is None:
        return []
    base = total_cost(case, terms, std, supplier)
    units = committed_units(case, terms, std) or 1.0
    rows = []

    def priced(field, option):
        p, cur = base_terms["unit_price"], base_terms.get(field)
        if not slopes or field == "unit_price":
            return p
        if field in slopes and isinstance(option, int | float) and isinstance(cur, int | float):
            return round(p * (1 + slopes[field] * (option - cur)), 2)
        return round(p * slopes.get((field, key(cur), key(option)), 1.0), 2)

    def add(field, option):
        moved = {**terms, field: option}
        if field != "unit_price":
            moved["unit_price"] = priced(field, option)
        c = total_cost(case, moved, std, supplier)
        rows.append(
            {
                "field": field,
                "current": base_terms.get(field),
                "option": option,
                "c": c,
                "delta_eur": round(c - base, 2),
                "price_equiv": round((c - base) / units, 4),
                "must_haves_ok": must_haves_ok(case, moved, std),
            }
        )

    for field in case.get("negotiable", []):
        if field == "unit_price":
            add(field, round(base_terms["unit_price"] * (1 - PRICE_STEP_PCT / 100), 2))
            continue
        for option in (case.get("term_options") or {}).get(field) or []:
            if key(option) != key(base_terms.get(field)):
                add(field, option)
    return sorted(rows, key=lambda r: r["delta_eur"])


def best_levers(case, terms, std=None, supplier=None, slopes=None):
    """{field: lever row}: the best option per non-price field that saves at least MIN_LEVER_SHARE of
    C (coverage always counts) and keeps the must-haves. With revealed prices, only levers that save
    more than the supplier charges; ties within EUR 1 go to the option closest to its own value."""
    rows, base_c = {}, total_cost(case, terms, std, supplier)
    for row in lever_table(case, terms, std, supplier, slopes):
        big = -row["delta_eur"] >= MIN_LEVER_SHARE * base_c or row["field"] in COVERAGE
        if row["field"] != "unit_price" and row["must_haves_ok"] and row["delta_eur"] < 0 and big:
            rows.setdefault(row["field"], []).append(row)
    best = {}
    for f, rs in rows.items():
        top = min(r["delta_eur"] for r in rs)
        ties = [r for r in rs if r["delta_eur"] - top < 1]
        best[f] = min(ties, key=lambda r: distance(r["option"], r["current"]))
    return best


def label(value):
    if isinstance(value, dict):
        return f"index {value.get('series')} share {value.get('material_share')} band {value.get('deadband_pct')}%"
    return "none" if value is None else str(value)
