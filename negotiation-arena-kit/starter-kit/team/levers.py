"""Lever ranking: what each negotiable term is worth in EUR, computed with the published cost function.

For a term sheet (a supplier's standing offer, or a deal we closed) every negotiable field is moved to
each allowed option, and the unit price by 1 %. The change in C shows which levers to push first and
what we can give away cheaply. `price_equiv` turns a change into "EUR per unit" so the agent can
trade, e.g. "30 more payment days are worth 0.40 EUR/unit to us".

    uv run python -m team.levers case:P01 --price 135.10      # practice case file, no session used
"""

import argparse
import itertools
import json
import statistics

from arena_kit.cost import closing_terms, must_haves_ok, total_cost

PRICE_STEP_PCT = 1.0


def committed_units(case, terms, standard=None):
    t = closing_terms(case, terms, standard)
    months = t["contract_months"]
    years = min(months / 12 if months else 1.0, case["buyer"]["horizon_years"])
    return t["volume_per_year"] * years


def _label(value):
    if isinstance(value, dict):  # an index clause option
        return f"index {value.get('series')} share {value.get('material_share')} band {value.get('deadband_pct')}%"
    return "none" if value is None else str(value)


def _key(value):
    return json.dumps(value, sort_keys=True, default=str)


def revealed_slopes(case, messages):
    """What the supplier itself charges for a term, from its own sheet pairs that differ in one field only:
    {field: relative price change per unit} for numbers (payment days, lead time, ppm, ...) and
    {(field, from, to): price ratio} for the rest (index clause, effective date, Incoterm). Median per field."""
    fields = [f for f in case.get("negotiable", []) if f != "unit_price"]
    seen = {}
    for m in messages:
        sheets = [ts.get("terms") or {} for ts in m.get("term_sheets") or []]
        sheets = [t for t in sheets if t.get("unit_price")]
        for a, b in itertools.combinations(sheets, 2):
            diff = [f for f in fields if a.get(f) != b.get(f)]
            if len(diff) != 1 or a.get(diff[0]) is None or b.get(diff[0]) is None:
                continue
            f, (va, vb), (pa, pb) = diff[0], (a[diff[0]], b[diff[0]]), (a["unit_price"], b["unit_price"])
            if all(isinstance(v, int | float) and not isinstance(v, bool) for v in (va, vb)):
                seen.setdefault(f, []).append((pb / pa - 1) / (vb - va))
            else:
                seen.setdefault((f, _key(va), _key(vb)), []).append(pb / pa)
                seen.setdefault((f, _key(vb), _key(va)), []).append(pa / pb)
    return {k: statistics.median(v) for k, v in seen.items()}


def lever_table(case, terms, standard=None, supplier=None, slopes=None):
    """Every single-field move from `terms`, cheapest first:
    [{field, current, option, c, delta_eur, price_equiv, must_haves_ok}]. delta_eur < 0 saves money.
    With `slopes` (revealed_slopes), a move also carries the price the supplier would charge for it."""
    base_terms = closing_terms(case, terms, standard)
    if base_terms.get("unit_price") is None:
        return []
    base = total_cost(case, terms, standard, supplier)
    units = committed_units(case, terms, standard) or 1.0
    rows = []

    def priced(field, option):
        p, cur = base_terms["unit_price"], base_terms.get(field)
        if not slopes or field == "unit_price":
            return p
        if field in slopes and isinstance(option, int | float) and isinstance(cur, int | float):
            return round(p * (1 + slopes[field] * (option - cur)), 2)
        return round(p * slopes.get((field, _key(cur), _key(option)), 1.0), 2)

    def add(field, option):
        moved = {**terms, field: option}
        if field != "unit_price":
            moved["unit_price"] = priced(field, option)
        c = total_cost(case, moved, standard, supplier)
        rows.append(
            {
                "field": field,
                "current": base_terms.get(field),
                "option": option,
                "c": c,
                "delta_eur": round(c - base, 2),
                "price_equiv": round((c - base) / units, 4),
                "must_haves_ok": must_haves_ok(case, moved, standard),
            }
        )

    for field in case.get("negotiable", []):
        if field == "unit_price":
            add(field, round(base_terms["unit_price"] * (1 - PRICE_STEP_PCT / 100), 2))
            continue
        for option in case.get("term_options", {}).get(field, []):
            if option != base_terms.get(field):
                add(field, option)
    return sorted(rows, key=lambda r: r["delta_eur"])


def best_per_field(rows):
    """The single best move per field (the biggest saving), biggest first."""
    best = {}
    for r in rows:
        if r["must_haves_ok"] and (r["field"] not in best or r["delta_eur"] < best[r["field"]]["delta_eur"]):
            best[r["field"]] = r
    return sorted(best.values(), key=lambda r: r["delta_eur"])


def render(case, terms, standard=None, supplier=None, limit=8):
    """Short text for a prompt: levers with a saving, and what each move is worth per unit."""
    rows = lever_table(case, terms, standard, supplier)
    if not rows:
        return "LEVERS: no price on the table yet."
    lines = [f"LEVERS (C now {total_cost(case, terms, standard, supplier):,.0f} EUR; negative = saves us money)"]
    for r in rows[:limit]:
        flag = "" if r["must_haves_ok"] else "  [breaks a must-have]"
        lines.append(
            f"- {r['field']}: {_label(r['current'])} -> {_label(r['option'])}: "
            f"{r['delta_eur']:+,.0f} EUR ({r['price_equiv']:+.3f} EUR/unit){flag}"
        )
    return "\n".join(lines)


def main(argv=None):
    from arena_kit.cli import load_env
    from arena_kit.client import ArenaClient

    load_env()
    ap = argparse.ArgumentParser(prog="levers", description="rank the levers of a practice case")
    ap.add_argument("case_id")
    ap.add_argument("--price", type=float, help="unit price to start from (default: current_unit_price)")
    ap.add_argument("--terms", default="{}", help='more terms as JSON, e.g. \'{"payment_days": 30}\'')
    ap.add_argument("--set", default="practice")
    a = ap.parse_args(argv)
    case = next(c for c in ArenaClient().cases(a.set) if c["case_id"] == a.case_id)
    terms = {"unit_price": a.price or case["buyer"]["current_unit_price"], **json.loads(a.terms)}
    print(case["case_id"], case["title"], "| goal:", case["buyer"].get("category_goal"))
    print(render(case, terms, limit=50))


if __name__ == "__main__":
    main()
