"""R13.8: the unit price at which a package costs exactly a target C.

Without the supplier's margin charge C is linear in the price: p = (target - C(0)) / (C(1) - C(0)).
With it, C is convex in the price - falling down to the cheapest price, rising below it (a squeezed
supplier costs more than the price saves). The cheapest price is found by golden-section search
between 0.5x and 1.5x the supplier's price, and the target is solved by bisection on the side ABOVE
it: a price below the cheapest one costs more and leaves the supplier weaker.

Prices are whole cents, so a package's C can miss the target by up to half a cent per committed
unit (P08: 189,600 units, up to EUR 948); `tolerance` gives that bound.
"""

import math

from ..cost import total_cost
from .terms import committed_units

GOLDEN = (5**0.5 - 1) / 2


def _c(case, terms, std, supplier, p):
    return total_cost(case, {**terms, "unit_price": p}, std, supplier)


def tolerance(case, terms, std=None):
    """How close to a target C a whole-cent price can get: EUR 1, or half a cent per unit."""
    return max(1.0, 0.005 * committed_units(case, terms, std))


def cheapest_price(case, terms, std, supplier, hint):
    """The price at which these terms cost us least once the margin charge counts (C is convex in
    the price): golden-section search between 0.5x and 1.5x `hint`."""
    if supplier is None:
        return 0.5 * hint
    lo, hi = 0.5 * hint, 1.5 * hint
    for _ in range(80):
        a, b = hi - GOLDEN * (hi - lo), lo + GOLDEN * (hi - lo)
        if _c(case, terms, std, supplier, a) <= _c(case, terms, std, supplier, b):
            hi = b
        else:
            lo = a
    return (lo + hi) / 2


def price_for(case, terms, std, target, supplier=None, hint=None, slack=0.0):
    """The whole-cent unit price at which `terms` cost `target`, or None if no price does.
    `hint`: the supplier's price (the search window with a margin charge). `slack`: a structure whose
    cheapest C misses the target by at most this share is priced at its cheapest price instead (a menu
    keeps its 90-day option when the margin charge puts that structure's minimum just above target)."""
    if supplier is None:
        c0 = _c(case, terms, std, None, 0.0)
        c1 = _c(case, terms, std, None, 1.0)
        if not (math.isfinite(c0) and math.isfinite(c1)) or c1 <= c0:
            return None
        p = (target - c0) / (c1 - c0)
        return round(p, 2) if p > 0 else None
    hint = hint or terms.get("unit_price") or 1.0
    low = cheapest_price(case, terms, std, supplier, hint)
    c_low = _c(case, terms, std, supplier, low)
    if c_low > target + tolerance(case, terms, std):
        if slack and c_low <= target * (1 + slack):
            return round(math.ceil(low * 100) / 100, 2)
        return None  # out of reach: even the cheapest price costs more
    high = 1.5 * hint
    if _c(case, terms, std, supplier, high) <= target:
        return round(high, 2)
    for _ in range(60):
        mid = (low + high) / 2
        if _c(case, terms, std, supplier, mid) < target:
            low = mid
        else:
            high = mid
    p = round((low + high) / 2, 2)
    # whole cents: take the cent closer to the target, never one below the cheapest price
    best = min((p - 0.01, p, p + 0.01), key=lambda q: abs(_c(case, terms, std, supplier, q) - target))
    minimum_cent = math.ceil(cheapest_price(case, terms, std, supplier, hint) * 100) / 100
    return round(max(best, minimum_cent), 2)


def c_min_price(case, terms, std, supplier, lo, hi):
    """The price in [lo, hi] at which `terms` cost us least (with a margin charge the bottom of the
    convex curve; without one, `lo`) - R4.2: never anchor under the price at which C stops falling."""
    if supplier is None:
        return lo
    p = cheapest_price(case, terms, std, supplier, (lo + hi) / 2 if hi > lo else hi)
    return min(max(p, lo), hi)
