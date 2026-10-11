"""Term sheets around a target C (sections 3 and 6, R11.9-R11.11, R12.2, R12.7-R12.10).

`Ctx` is one thread as the sheets see it: the case, the supplier's standard terms, our estimate of
it, and the supplier's best sheet on the table. Every price comes from `pricing.price_for`, so every
sheet costs exactly the target (R13.8); every sheet passes `guard` (R11.11) and `no_regress` (R12.10)
before it goes out.

  structure    the supplier's terms, broken must-haves repaired (R11.9), coverage locked (R12.2),
               moved where both sides gain (R3.2); its asks beyond our reference put back where that
               saves us money (R3.3)
  price round  up to three sheets at the target: our terms, their terms, a give (R6.1) with a shared
               surplus (R6.2); lean / recommended / other with a decoy (R6.4); two sheets when the
               supplier asked for something (R6.3); its own terms just under its price when nothing
               fits (R6.6)
  floors       no sheet under max(full cost x 0.97, 75 % of its price) (R6.5); faster payment may go
               lower, priced at the supplier's cost of money (R12.9)
"""

import itertools
import math
from dataclasses import dataclass, field

from ..cost import OPS, must_haves_ok, supplier_value
from . import estimate as est_mod
from . import pricing
from .terms import COVERAGE, complete, distance, key, options, same, step

SURPLUS_SHARE = 0.5  # R6.2: our share of the gain from a give
DECOY_GAP = 0.02  # R6.4: the lean sheet is the recommended one 2 % cheaper, one term worse for them
FLOOR_COST_K = 0.97  # R6.5: never under the estimated full cost x 0.97
FLOOR_PRICE_K = 0.75  # R6.5: never more than 25 % under the supplier's price
OUT_OF_REACH_K = 0.99  # R6.6: its own terms at 99 % of its price
REGRESS_TOL = 0.001  # R12.10: within 0.1 % of an offer the supplier made is no offer
VOLATILE_PCT = 15.0  # R13.11
REFERENCE_FIELDS = ("payment_days", "volume_per_year", "contract_months", "lead_time_days", "index_clause")


@dataclass
class Ctx:
    case: dict
    std: dict
    est: est_mod.Estimate
    their: dict  # the supplier's best sheet on the table (R3.1), completed
    playbook: dict
    volatile: object = None  # series -> bool (R13.11)
    locked: dict = field(default_factory=dict)

    def c(self, terms):
        return est_mod.cost(self.case, terms, self.std, self.est)

    @property
    def supplier(self):
        return self.est.supplier(self.case, self.std) if self.est else None

    @property
    def their_price(self):
        return self.their.get("unit_price") or 0.0

    @property
    def c_theirs(self):
        return self.c(self.their)

    def ok(self, terms):
        clause = terms.get("index_clause")
        if "index_clause" in self.case["negotiable"] and clause and self.volatile and self.volatile(clause["series"]):
            return False
        return must_haves_ok(self.case, terms, self.std) and all(
            terms.get(f) == v for f, v in self.locked.items()
        )

    def allowed(self, field, terms=None):
        """The options of a term we may offer: valid for the must-haves; index clauses as the playbook
        allows (any / deadband >= 3 % / avoid) and never on a volatile series (R13.11)."""
        opts = options(self.case, field, self.std)
        if field in self.locked:
            return [self.locked[field]]
        if terms is not None:
            opts = [o for o in opts if self.ok({**terms, field: o})] or opts
        if field != "index_clause":
            return opts
        stance = (self.playbook or {}).get("index", "any")
        keep = [o for o in opts if o is None or not (self.volatile and self.volatile(o["series"]))]
        if stance == "avoid":
            keep = [o for o in keep if o is None] or keep
        elif stance == "deadband":
            keep = [o for o in keep if o is None or o.get("deadband_pct", 0) >= 3] or keep
        theirs = self.their.get("index_clause")
        if theirs:
            own = [o for o in keep if o and o["series"] == theirs["series"]]
            if own:
                best = min(own, key=lambda o: (self.c({**self.their, "index_clause": o}), -o["deadband_pct"]))
                # R12.8: a demanded index is offered on its cheapest deadband, not a gratuitous 0%.
                keep = [o for o in keep if o is None or key(o) == key(best)]
        return keep


# --- structure -----------------------------------------------------------------------------------
def repair(case, terms, std=None):
    """R11.9: each broken must-have moved to the nearest allowed value that keeps it."""
    out, std = dict(terms), std or {}
    for m in (case.get("buyer") or {}).get("must_haves") or []:
        if must_haves_ok(case, out, std) or m["field"] not in case.get("negotiable", []):
            continue
        current = {**std, **out}.get(m["field"])
        allowed = [o for o in (case.get("term_options") or {}).get(m["field"]) or [m["value"]]
                   if o is not None and OPS[m["op"]](o, m["value"])]  # fmt: skip
        if allowed:
            near = isinstance(current, int | float) and all(isinstance(o, int | float) for o in allowed)
            out[m["field"]] = min(allowed, key=lambda o: abs(o - current)) if near else allowed[0]
    return out


def cover(ctx, combo, price):
    """R12.2: the coverage terms (contract months x volume) at the joint option that costs us least at
    `price` - committing the full need for the full horizon whenever that lowers C. Ties (within EUR 1)
    go to the values closest to the supplier's own. Trusts the table when coverage does not pay
    (P08: a fallback price below the offer)."""
    fields = [f for f in COVERAGE if f in ctx.case["negotiable"] and (ctx.case.get("term_options") or {}).get(f)]
    if not fields:
        return dict(combo)
    grids = [[(f, o) for o in ctx.allowed(f, combo)] for f in fields]
    best, best_c = dict(combo), ctx.c({**combo, "unit_price": price})
    for pick in itertools.product(*grids):
        t = {**combo, **dict(pick)}
        if not ctx.ok(t):
            continue
        c = ctx.c({**t, "unit_price": price})
        closer = sum(distance(t[f], combo.get(f)) for f in fields) < sum(
            distance(best[f], combo.get(f)) for f in fields
        )
        if c < best_c - 1 or (abs(c - best_c) <= 1 and closer):
            best, best_c = t, c
    return best


def base_combo(ctx, terms):
    """Their non-price terms, completed from the standard terms and snapped to the case's options."""
    full = complete(ctx.case, {k: v for k, v in terms.items()}, ctx.std)
    full.pop("unit_price", None)
    return full


def mutual_combo(ctx, terms, price=None):
    """R3.2: their non-price terms moved toward what the supplier prefers for as long as that also
    lowers our C (e.g. a longer contract that covers our whole need)."""
    out = base_combo(ctx, terms)
    price = price if price is not None else terms.get("unit_price") or ctx.their_price

    def c(combo):
        return ctx.c({**combo, "unit_price": price})

    for f in [f for f in out if f in ctx.case["negotiable"]]:
        v = step(ctx.case, f, out[f], ctx.std, toward_supplier=True)
        while v is not None and ctx.ok({**out, f: v}) and c({**out, f: v}) < c(out) - 1e-6:
            out[f] = v
            v = step(ctx.case, f, v, ctx.std, toward_supplier=True)
    return out


def reference_combo(ctx, combo, price):
    """R3.3: terms the supplier pulled beyond our reference (the buyer's plan) put back where that
    saves us money - what it asks for is paid for in price."""
    plan = complete(ctx.case, {"index_clause": None}, {})
    out = dict(combo)
    for f in REFERENCE_FIELDS:
        if f not in ctx.case["negotiable"] or f not in out or key(plan.get(f)) == key(out[f]):
            continue
        if f != "index_clause" and plan.get(f) is None:
            continue
        moved = {**out, f: plan.get(f)}
        if ctx.ok(moved) and ctx.c({**moved, "unit_price": price}) < ctx.c({**out, "unit_price": price}):
            out = moved
    return out


def structure(ctx, price):
    """The structure we negotiate the price on: their terms repaired (R11.9), coverage locked at OUR
    price `price` (R12.2 - whether covering the need pays depends on the price we get for it, P08), then
    moved where both sides gain at THEIR price (R3.2, as BuyerAgent: at our anchor the margin charge
    would pull a date the supplier already gave us back toward it)."""
    base = base_combo(ctx, repair(ctx.case, ctx.their, ctx.std))
    base = cover(ctx, base, price)
    base = mutual_combo(ctx, {**base, "unit_price": ctx.their_price}, ctx.their_price)
    # R12.3: do not request overtime merely because the quote PDF lists a faster lead time.
    # Relax only to an on-time allowed value, without raising our cost or breaking a must-have.
    field = "lead_time_days"
    need = ctx.case["buyer"].get("need_lead_time_days")
    if field in base and field in ctx.case["negotiable"] and isinstance(need, int | float):
        on_time = [o for o in ctx.allowed(field, base) if isinstance(o, int | float) and o <= need]
        if on_time:
            normal = max(on_time)
            candidate = {**base, field: normal}
            if normal > base[field] and ctx.ok(candidate) and ctx.c({**candidate, "unit_price": price}) <= \
                    ctx.c({**base, "unit_price": price}) + 1:
                base = candidate
    return base


# --- prices --------------------------------------------------------------------------------------
def floor(ctx):
    """R6.5: max(estimated full cost x 0.97, 75 % of the supplier's price)."""
    cut = FLOOR_PRICE_K * ctx.their_price
    if ctx.est and ctx.est.full_cost and ctx.est.source != "opening":
        return max(ctx.est.full_cost * FLOOR_COST_K, cut)
    return cut


def indifference_price(ctx, give, reference, ref_price):
    """The price for `give` that leaves the supplier exactly as well off in cash as `reference` at
    `ref_price` (`supplier_value` at its estimated cost of money). None if it cannot be computed."""
    sup = ctx.est.as_supplier(ctx.case, ctx.std) if ctx.est else None
    if sup is None:
        return None
    try:
        v_ref = supplier_value(ctx.case, {**reference, "unit_price": ref_price}, sup)
        v0 = supplier_value(ctx.case, {**give, "unit_price": 0.0}, sup)
        v1 = supplier_value(ctx.case, {**give, "unit_price": 1.0}, sup)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if v1 - v0 <= 0:
        return None
    return (v_ref - v0) / (v1 - v0)


def give_price(p_equal, p_indifferent):
    """R6.2: a give priced halfway between our equal-cost price and the supplier's indifference
    price, when the term is worth more to it than it costs us; else equal cost."""
    if p_equal is None:
        return None
    if p_indifferent is None or p_indifferent >= p_equal:
        return p_equal
    return math.floor((p_equal - SURPLUS_SHARE * (p_equal - p_indifferent)) * 100) / 100


def structure_floor(ctx, combo, base, floor_price):
    """R12.9: a floor per structure - the price that pays the supplier as much as `base` at the floor
    (faster payment is worth more to it, so that sheet may go lower), never under 75 % of its price."""
    p = indifference_price(ctx, combo, base, floor_price)
    if p is None:
        return floor_price
    return max(FLOOR_PRICE_K * ctx.their_price, min(floor_price, p))


def priced(ctx, combo, target, lo):
    """`combo` at the whole-cent price that costs `target`, if that price is credible: at or above `lo`
    and with lower total cost than the supplier's package. A higher unit price can still be a
    concession when improved terms lower total cost. Else None."""
    p = pricing.price_for(ctx.case, combo, ctx.std, target, ctx.supplier, ctx.their_price)
    if p is None or p < lo - 0.005:
        return None
    sheet = complete(ctx.case, {**combo, "unit_price": p}, ctx.std)
    return sheet if ctx.c(sheet) < ctx.c_theirs else None


# --- price-round sheets (R6) -----------------------------------------------------------------------
def our_terms(ctx, base, open_fields):
    """Every open term at the option that costs us least (R6.1 "our terms")."""
    price = ctx.their_price
    ours = dict(base)
    for f in open_fields:
        opts = ctx.allowed(f, ours)
        if opts:
            ours[f] = min(opts, key=lambda v, f=f: (ctx.c({**ours, f: v, "unit_price": price}), key(v) != key(ours[f])))
    return ours


def price_sheets(ctx, base, target, levers, floor_price, open_fields=(), asks=None):
    """Up to three sheets at `target` (R6.1): our terms first, their terms, a give - arranged lean /
    recommended / other with the decoy (R6.4). When the supplier asked for terms, `ask_sheets`."""
    if asks and (out := ask_sheets(ctx, base, target, asks, floor_price)):
        return out
    candidates = []
    ours = our_terms(ctx, base, open_fields)
    if ours != base:
        candidates.append(("our terms", ours))
    candidates.append(("their terms", base))
    for f in levers:
        if (
            f in base
            and f not in COVERAGE
            and (v := step(ctx.case, f, base[f], ctx.std, toward_supplier=True)) is not None
        ):
            if ctx.ok({**base, f: v}) and (f != "index_clause" or v in ctx.allowed(f)):
                candidates.append((f"{f} toward them", {**base, f: v}))
                break
    if not any(label.endswith("toward them") for label, _ in candidates):
        # a free give: more coverage than the horizon needs (a longer contract) costs us nothing - a
        # commitment beyond the horizon neither costs nor earns - and is security for the supplier
        for f in COVERAGE:
            v = step(ctx.case, f, base[f], ctx.std, toward_supplier=True) if f in base and f in levers else None
            if v is not None and ctx.ok({**base, f: v}):
                at = {**base, "unit_price": ctx.their_price}
                if ctx.c({**at, f: v}) <= ctx.c(at) + 1:
                    candidates.append((f"{f} toward them", {**base, f: v}))
                    break
    out, cap = [], None
    for label, combo in candidates:
        lo = structure_floor(ctx, combo, base, floor_price)
        sheet = priced(ctx, combo, target, lo)
        if label == "their terms" and sheet:
            cap = sheet["unit_price"]
        if sheet and cap and label.endswith("toward them"):
            p = give_price(sheet["unit_price"], indifference_price(ctx, combo, base, cap))
            sheet = complete(ctx.case, {**combo, "unit_price": max(p, lo)}, ctx.std) if p else None
        if sheet and ctx.ok(sheet) and not any(same(sheet, s) for _, s in out):
            out.append((label, sheet))
    return with_decoy(ctx, out, levers, floor_price)


def ask_sheets(ctx, base, target, asks, floor_price):
    """R6.3: the supplier asked for something - exactly its ask at a lower price (shared surplus), and
    our reference terms at the target. No third sheet that blurs the trade. [] if it does not work."""
    price = ctx.their_price
    ref = reference_combo(ctx, base, price)
    give = {**ref, **asks}
    if not ctx.ok(give) or same(complete(ctx.case, give, ctx.std), complete(ctx.case, ref, ctx.std)):
        return []
    ref_sheet = priced(ctx, ref, target, floor_price)
    if ref_sheet is None:
        return []
    lo = structure_floor(ctx, give, ref, floor_price)
    p_equal = pricing.price_for(ctx.case, give, ctx.std, target, ctx.supplier, price)
    p = give_price(p_equal, indifference_price(ctx, give, ref, ref_sheet["unit_price"]))
    if p is None:
        return []
    ask_sheet = complete(ctx.case, {**give, "unit_price": max(p, lo)}, ctx.std)
    if ask_sheet["unit_price"] >= price:
        return []
    return [("their ask", ask_sheet), ("reference", ref_sheet)]


def with_decoy(ctx, out, levers, floor_price):
    """R6.4: [lean, recommended, other]. The lean sheet is the recommended one DECOY_GAP cheaper with
    the term the supplier values most moved our way - safe: if it takes the lean one, we gain."""
    if not out:
        return out
    middle = next((t for label, t in out if label == "their terms"), out[0][1])
    lean = dict(middle)
    fields = [f for f in [*levers, *ctx.case["negotiable"]] if f in lean and f != "unit_price" and f not in COVERAGE]
    moved = False
    for f in dict.fromkeys(fields):
        v = step(ctx.case, f, lean[f], ctx.std, toward_supplier=False)
        if v is not None and ctx.ok({**lean, f: v}) and (f != "index_clause" or v in ctx.allowed(f)):
            lean[f], moved = v, True
            break
    if not moved:
        return out
    lean = complete(ctx.case, {**lean, "unit_price": round(middle["unit_price"] * (1 - DECOY_GAP), 2)}, ctx.std)
    if lean["unit_price"] < floor_price or any(same(lean, t) for _, t in out) or not ctx.ok(lean):
        return out
    rest = [(label, t) for label, t in out if t is not middle][:1]
    return [("lean", lean), ("recommended", middle), *rest]


def out_of_reach(ctx, base, floor_price):
    """R6.6: no sheet fits the target - its own terms at max(floor, 99 % of its price), if they keep
    the must-haves."""
    p = max(floor_price, OUT_OF_REACH_K * ctx.their_price)
    t = complete(ctx.case, {**base, "unit_price": round(p, 2)}, ctx.std)
    return [("their terms", t)] if ctx.ok(t) and t["unit_price"] < ctx.their_price else []


def just_under(ctx, base):
    """The last resort when no sheet is credible: its own terms (repaired) half a percent under its
    price - a real, valid offer rather than an empty e-mail."""
    t = complete(ctx.case, {**base, "unit_price": round(0.995 * ctx.their_price, 2)}, ctx.std)
    return [("their terms", t)] if ctx.ok(t) and t["unit_price"] < ctx.their_price else []


# --- variants of the first menu ---------------------------------------------------------------------
def payment_probe(ctx, sheet, target, floor_price, slopes=None):
    """R12.7: the sheet with payment days at the supplier's own value, or at 30 days, at equal C - its
    pick reveals its cost of money. None if it does not fit."""
    if "payment_days" not in ctx.case["negotiable"]:
        return None
    pay = ctx.allowed("payment_days", sheet)
    own = complete(ctx.case, ctx.their, ctx.std).get("payment_days")
    want = 30 if ("payment_days" in (slopes or {}) or own == sheet.get("payment_days")) else own
    other = min(pay, key=lambda d: abs(d - (want or 30))) if pay else None
    if other is None or other == sheet.get("payment_days"):
        return None
    combo = {**{k: v for k, v in sheet.items() if k != "unit_price"}, "payment_days": other}
    lo = structure_floor(ctx, combo, {k: v for k, v in sheet.items() if k != "unit_price"}, floor_price)
    return priced(ctx, combo, target, lo)


def index_variant(ctx, sheet, target, floor_price):
    """R12.8: the supplier insists on an index clause - offer its own series with the deadband that
    costs us least (never a 0 % deadband we can avoid), within the playbook and never volatile."""
    theirs = ctx.their.get("index_clause")
    if "index_clause" not in ctx.case["negotiable"] or not theirs or sheet.get("index_clause"):
        return None
    same_series = [o for o in ctx.allowed("index_clause") if o and o.get("series") == theirs.get("series")]
    if not same_series:
        return None
    combo = {k: v for k, v in sheet.items() if k != "unit_price"}
    best = min(same_series, key=lambda o: (ctx.c({**combo, "index_clause": o, "unit_price": ctx.their_price}),
                                          -o.get("deadband_pct", 0)))  # fmt: skip
    return priced(ctx, {**combo, "index_clause": best}, target, floor_price)


# --- guards ----------------------------------------------------------------------------------------
def lock_coverage(ctx, sheets, cover_values):
    """R12.2: once sheet A covers the need, no sheet covers less."""
    out = []
    for label, t in sheets:
        t = dict(t)
        for f, v in (cover_values or {}).items():
            if isinstance(v, int | float) and (t.get(f) or 0) < v:
                t[f] = v
        out.append((label, t))
    return out


def guard(ctx, sheets, target, supplier_sheets, last_sheets=None):
    """R11.11 / R6.7, code's last word on every sheet: it costs us at most the target (within the
    whole-cent tolerance), is cheaper for us than the supplier's standing offer, is not one of the
    supplier's own sheets and keeps the must-haves. If nothing survives: our last sheets."""
    own = [complete(ctx.case, t, ctx.std) for t in supplier_sheets]
    c_theirs, out = ctx.c_theirs, []
    for label, t in sheets:
        if t is None or t.get("unit_price") is None:
            continue
        c = ctx.c(t)
        tol = pricing.tolerance(ctx.case, t, ctx.std)
        if not math.isfinite(c) or c > target + tol or c >= c_theirs:
            continue
        if any(same(t, s) for s in own) or not ctx.ok(t):
            continue
        out.append((label, t))
    if not out and last_sheets:
        return [(label, t) for label, t in last_sheets if ctx.ok(t)]
    return out


def no_regress(ctx, sheets, supplier_costs):
    """R12.10: never send a sheet that costs us as much as one the supplier already offered (within
    0.1 %). Returns the sheets that are left (maybe none)."""
    if not supplier_costs:
        return sheets
    best = min(supplier_costs)
    return [(label, t) for label, t in sheets if ctx.c(t) < (1 - REGRESS_TOL) * best]


def validate_menu(
    ctx,
    sheets,
    last_sheets=None,
    rival_price=None,
    *,
    minimum_notice_days=None,
    current_date=None,
):
    """Run the shared buyer term guardrails over a menu before it reaches the e-mail.

    SpecAgent computes all numbers itself, but this second boundary is intentional: it catches
    duplicate prices, backwards term trades, date-value errors, and backwards concessions at the
    exact point where a menu is about to be sent.  The import is lazy so the sheet-generation
    module remains usable on its own in the kit's unit tests.
    """
    from team.term_guard import (
        validate_contractual_notice,
        validate_effective_date_pricing,
        validate_minimum_options,
        validate_monotonic_options,
        validate_reciprocity,
        validate_term_options,
    )

    options = [terms for _, terms in sheets]
    errors = validate_minimum_options(options)
    errors.extend(validate_term_options(options, rival_price=rival_price))
    if ctx.case.get("type") == "price_increase":
        errors.extend(validate_effective_date_pricing(options))
    if last_sheets:
        errors.extend(validate_monotonic_options([terms for _, terms in last_sheets], options))
        errors.extend(validate_reciprocity([terms for _, terms in last_sheets], options))
    errors.extend(validate_contractual_notice(options, minimum_notice_days, current_date=current_date))
    return errors
