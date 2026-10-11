"""Term-sheet helpers: a valid sheet, the options of a term, which way a term helps the supplier."""

import json
from datetime import date

from ..cost import closing_terms

# Which way each term is worth more to the supplier: +1 higher, -1 lower (index_clause: having one).
SUPPLIER_LIKES = {
    "payment_days": -1, "effective_date": -1, "contract_months": +1, "volume_per_year": +1,
    "lead_time_days": +1, "ppm_commitment": +1, "chargeback_eur_per_complaint": -1, "tooling_eur": +1,
    "incoterm": -1, "index_clause": +1,
}  # fmt: skip
INCOTERM_RANK = {"EXW": 0, "FCA": 1, "DAP": 2}  # the supplier carries more freight up the list
COVERAGE = ("contract_months", "volume_per_year")  # terms that decide how much of the need is covered


def key(value):
    """A hashable, order-free form of a term value (index clauses are dicts)."""
    return json.dumps(value, sort_keys=True, default=str)


def same(a, b):
    """Two term sheets equal up to a cent."""
    if set(a) != set(b):
        return False
    for k in a:
        x, y = a[k], b[k]
        if isinstance(x, int | float) and isinstance(y, int | float) and not isinstance(x, bool):
            if abs(x - y) > 0.005:
                return False
        elif key(x) != key(y):
            return False
    return True


def distance(a, b):
    """How far an option is from a value: numbers by difference, anything else 0 if equal else 1."""
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return abs(a - b)
    return 0 if key(a) == key(b) else 1


def _as_date(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def snap_option(options, value):
    """The allowed option equal to `value` (the option object itself), else the nearest one."""
    if not options:
        return value
    for o in options:
        if key(o) == key(value) or (
            isinstance(o, int | float) and isinstance(value, int | float) and not isinstance(value, bool) and o == value
        ):
            return o
    if isinstance(value, int | float) and all(isinstance(o, int | float) for o in options):
        return min(options, key=lambda o: (abs(o - value), o))
    if isinstance(value, str) and (d := _as_date(value)) and all(isinstance(o, str) and _as_date(o) for o in options):
        return min(options, key=lambda o: abs((_as_date(o) - d).days))
    if isinstance(value, dict):  # an index clause that is not an option: the same series, else none
        same_series = [o for o in options if isinstance(o, dict) and o.get("series") == value.get("series")]
        if same_series:
            return min(same_series, key=lambda o: abs(o.get("deadband_pct", 0) - value.get("deadband_pct", 0)))
    return None if None in options else options[0]


def options(case, field, std=None):
    """The values a term may take: the case's options, else the supplier's standard value."""
    opts = (case.get("term_options") or {}).get(field)
    if opts:
        return list(opts)
    value = (std or {}).get(field, (case.get("fixed_terms") or {}).get(field))
    return [value] if value is not None or field == "index_clause" else []


def complete(case, terms, std=None):
    """A valid term sheet: exactly the negotiable fields; what `terms` leaves out from the supplier's
    standard terms, the fixed terms and the buyer's plan; every value snapped to an allowed option.
    A field without options keeps the supplier's standard value (api.md, Terms)."""
    std = std or {}
    base = closing_terms(case, {"unit_price": None}, std)
    freight = (case.get("buyer") or {}).get("freight_eur_per_unit") or {}
    out = {}
    for field in case["negotiable"]:
        if field in terms:
            value = terms[field]
        elif field == "incoterm" and not std.get("incoterm") and freight:
            value = min(freight, key=freight.get)  # when asking, ask for the cheapest freight
        else:
            value = base.get(field)
        if field == "unit_price":
            out[field] = round(float(value), 2) if value is not None else None
            continue
        opts = (case.get("term_options") or {}).get(field)
        if opts:
            value = snap_option(opts, value)
        elif field in std:
            value = std[field]
        out[field] = value
    return out


def order(field, values):
    """Values from least to most attractive for the supplier."""
    if field == "index_clause":
        return sorted(values, key=lambda v: (v is not None, -(v or {}).get("deadband_pct", 0)))
    if field == "incoterm":
        return sorted(values, key=lambda v: -INCOTERM_RANK.get(v, 1))
    if not all(isinstance(v, int | float | str) for v in values):
        return list(values)
    return sorted(values, reverse=SUPPLIER_LIKES.get(field, 1) < 0)


def step(case, field, value, std=None, toward_supplier=True, n=1):
    """The option `n` steps from `value` toward what the supplier (or we) prefer, or None."""
    vals = order(field, options(case, field, std))
    keys = [key(v) for v in vals]
    if key(value) not in keys or len(vals) < 2:
        return None
    i = keys.index(key(value)) + (n if toward_supplier else -n)
    return vals[i] if 0 <= i < len(vals) else None


def liked(field, opts):
    """The option the supplier likes best."""
    return order(field, opts)[-1] if opts else None


def committed_units(case, terms, std=None):
    t = closing_terms(case, terms, std)
    months = t.get("contract_months") or 0
    years = min(months / 12 if months else 1.0, case["buyer"]["horizon_years"])
    return (t.get("volume_per_year") or 0) * years


def uncovered_units(case, terms, std=None):
    """Need the terms leave uncovered over the horizon (R12.2, R11.17)."""
    b = case["buyer"]
    t = closing_terms(case, terms, std)
    months = t.get("contract_months") or 0
    years = min(months / 12 if months else 1.0, b["horizon_years"])
    covered = min(t.get("volume_per_year") or 0, b["need_per_year"]) * years
    return max(0.0, b["need_per_year"] * b["horizon_years"] - covered)
