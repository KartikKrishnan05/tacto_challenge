"""Our estimate of the supplier (section 2): its full cost and its cost of money.

The estimate only prices the supplier's health - the margin charge that C carries in strategic and
bottleneck cases (R2.3) - and the shared surplus of a give (R6.2). Calibration (R2.1): its own cost
breakdown if it sent one, else should-cost / FULL_COST_K, else its opening price / 1.10. Neither too
high (a high floor made BuyerAgent stop pushing, P01) nor too low (P02: the arena charged EUR 124,110
for a price BuyerAgent's low estimate saw as free).
"""

import logging
import re
from dataclasses import dataclass, field

from ..cost import RELATIONSHIP_GOALS, closing_terms, total_cost

log = logging.getLogger("arena")

FULL_COST_K = 1.02  # full cost = should-cost / 1.02: fits the arena's relationship charge over 31 deals
OPENING_K = 1.10  # no data at all: full cost = opening price / 1.10
CASH_PREMIUM = 0.06  # R2.2: a supplier short of cash values money 6 points more than we do
RATE_BOUNDS = (0.03, 0.40)  # R2.2: a revealed rate is clamped to this


@dataclass
class Estimate:
    full_cost: float | None = None
    point: float | None = None  # the cost estimate itself (should-cost), for the fair price (R4.1)
    source: str = "none"  # breakdown | should-cost | opening | none
    rate: float = 0.08  # its cost of money per year
    usual_rate: float = 0.08
    opening: dict = field(default_factory=dict)  # its opening terms: standard terms the PDF left out

    def supplier(self, case, std=None):
        """The `supplier` argument of `total_cost` - only where the goal carries a margin charge."""
        if self.full_cost is None or (case.get("buyer") or {}).get("category_goal") not in RELATIONSHIP_GOALS:
            return None
        return self.as_supplier(case, std)

    def as_supplier(self, case, std=None):
        standard = closing_terms(
            case, {}, {**{k: v for k, v in self.opening.items() if k != "unit_price"}, **(std or {})}
        )
        return {
            "standard": standard,
            "interest_rate": self.rate,
            "usual_rate": self.usual_rate,
            "full_cost": self.full_cost or 0.0,
        }


def breakdown_cost(text):
    """Full cost per unit from a cost-breakdown XLSX (as `docs.read_document` reads it): the sum of
    its EUR elements without margin and totals."""
    total, found = 0.0, False
    for line in (text or "").splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 2 or re.search(r"margin|profit|ebit|unit price|total|sum|price|share|%", cells[0], re.I):
            continue
        try:
            total += float(cells[1])
            found = True
        except ValueError:
            continue
    return round(total, 2) if found and total > 0 else None


def should_cost(package, case):
    """The data's should-cost for the case's parts (summed over a bundle), or None."""
    costs = []
    for part in case.get("parts") or []:
        num = str(part).split(":", 1)[-1]
        fact = (case.get("facts") or {}).get(f"cost:{num}") or (package.lookup(f"cost:{num}") if package else None)
        value = (fact or {}).get("unit_cost")
        if value is None and package is not None:
            try:
                rows = package.query(
                    "SELECT unit_cost FROM should_cost WHERE part_id = ? ORDER BY estimate_date DESC", (part,)
                )
            except Exception:
                rows = []
            value = rows[0]["unit_cost"] if rows else None
        if value is None:
            return None
        costs.append(float(value))
    return sum(costs) if costs else None


def estimate(case, package, events=None, opening=None, cash_short=False, revealed_rate=None, market_low=None):
    """The estimate for one supplier thread. `events`: the thread so far (a cost-breakdown XLSX in it
    wins); `opening`: its opening term sheet; `revealed_rate`: its own price of payment days (R12.4);
    `market_low`: the lowest price any supplier has put on the table in the case.

    Judgement call: a should-cost at or above the lowest price on the table is not this market's cost
    (P05: should-cost 36.87, bids from 31.50, a deal at 28.42) - R2.1's third rung applies to that
    lowest price instead, or every floor would sit at the supplier's own price."""
    rate = float((case.get("buyer") or {}).get("interest_rate") or 0.08)
    usual = rate
    if cash_short:
        rate += CASH_PREMIUM
    if revealed_rate:
        rate = min(max(revealed_rate, RATE_BOUNDS[0]), RATE_BOUNDS[1])
    opening = dict(opening or {})
    for m in reversed(events or []):
        if m.get("from") == "buyer":
            continue
        for att in m.get("attachments") or []:
            if "breakdown" in (att.get("name") or "").lower() and att.get("text"):
                if (fc := breakdown_cost(att["text"])) is not None:
                    return Estimate(fc, fc, "breakdown", rate, usual, opening)
    sc = should_cost(package, case)
    # The full cost (should-cost / K), not the should-cost itself, is what a price can never go under:
    # P02's should-cost 374.95 sits above its 370.96 quote, but its full cost 367.60 is exactly what the
    # arena's margin charge implies (deal 371.88 at 90 days) - dropping it made the floor 337 and the
    # agent pushed until the supplier walked.
    if sc is not None and not (market_low and sc / FULL_COST_K >= market_low):
        return Estimate(round(sc / FULL_COST_K, 2), sc, "should-cost", rate, usual, opening)
    price = (
        min(p for p in (opening.get("unit_price"), market_low) if p)
        if opening.get("unit_price") or market_low
        else None
    )
    if price:
        return Estimate(round(price / OPENING_K, 2), None, "opening", rate, usual, opening)
    return Estimate(None, None, "none", rate, usual, opening)


def cost(case, terms, std, est):
    """C of terms for us: the published cost function, with the margin charge where the goal has one."""
    try:
        return total_cost(case, terms, std, est.supplier(case, std) if est else None)
    except Exception as e:  # a sheet we cannot price is never the cheapest
        log.debug("cost failed: %s", e)
        return float("inf")


def plain_cost(case, terms, std):
    """C without the margin charge (what the alternative is compared against)."""
    try:
        return total_cost(case, terms, std)
    except Exception:
        return float("inf")
