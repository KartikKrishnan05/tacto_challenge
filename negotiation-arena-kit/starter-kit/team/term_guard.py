"""Deterministic economic checks for multi-option term sheets."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

PRICE_TOLERANCE = 0.005


def _price(option: dict[str, Any]) -> float | None:
    value = option.get("unit_price")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), abs_tol=PRICE_TOLERANCE)
    return left == right


def _same_except(left: dict[str, Any], right: dict[str, Any], *ignored: str) -> bool:
    keys = (set(left) | set(right)) - set(ignored)
    return all(_same_value(left.get(key), right.get(key)) for key in keys)


def _has_index(value: Any) -> bool:
    if value is None or value is False or value == "":
        return False
    if isinstance(value, str) and value.strip().lower() in {"none", "fixed", "no", "false"}:
        return False
    return bool(value)


def _label(index: int) -> str:
    return f"Option {chr(ord('A') + index)}"


def validate_minimum_options(options: list[dict[str, Any]], minimum: int = 2) -> list[str]:
    """Require a real choice before a buyer sends a negotiation package."""
    if len(options) >= minimum:
        return []
    return [
        f"Rule 10 (multi-option): only {len(options)} distinct option(s) were generated. Send at least "
        f"{minimum} packages with different non-price terms so the supplier can trade instead of answering "
        "a binary ultimatum."
    ]


def validate_acceptance_offer(
    current: dict[str, Any],
    opening: dict[str, Any] | None,
    *,
    payment_extension_days: int = 30,
) -> list[str]:
    """Block a punitive current offer that exceeds the supplier's opening price.

    A higher price is allowed only when the supplier has documented a material payment-term
    concession.  The opening payment term must be known; an omitted opening term cannot be
    treated as a concession.
    """
    if not opening:
        return []
    opening_price, current_price = _price(opening), _price(current)
    if opening_price is None or current_price is None or current_price <= opening_price + PRICE_TOLERANCE:
        return []
    old_days, new_days = opening.get("payment_days"), current.get("payment_days")
    payment_concession = (
        isinstance(old_days, (int, float))
        and not isinstance(old_days, bool)
        and isinstance(new_days, (int, float))
        and not isinstance(new_days, bool)
        and new_days >= old_days + payment_extension_days
    )
    if payment_concession:
        return []
    return [
        f"Rule 11 (anti-penalty): the current supplier offer is EUR {current_price:.2f}, above its opening "
        f"price of EUR {opening_price:.2f}, without a documented payment extension of at least "
        f"{payment_extension_days} days. Do not accept this punitive premium; use a lower earlier offer or "
        "continue negotiating."
    ]


def validate_effective_date_pricing(options: list[dict[str, Any]]) -> list[str]:
    """Ensure price-increase date alternatives trade price for timing value.

    With all other terms equal, an earlier effective date is worse for the buyer because
    more volume is exposed to the increase.  The earlier-date option therefore needs a
    lower price; a later date may carry a higher price to compensate the supplier.  Equal
    prices or the reverse ordering create a dominated package and invite the supplier to
    select the buyer's worst combination.
    """
    errors: list[str] = []
    valid = [(i, option, _price(option)) for i, option in enumerate(options) if _price(option) is not None]
    for pos, (i, left, left_price) in enumerate(valid):
        for j, right, right_price in valid[pos + 1 :]:
            if not _same_except(left, right, "unit_price", "effective_date"):
                continue
            try:
                left_date, right_date = date.fromisoformat(str(left.get("effective_date"))), date.fromisoformat(
                    str(right.get("effective_date"))
                )
            except (TypeError, ValueError):
                continue
            if left_date == right_date:
                continue
            if left_date > right_date:
                i, j, left_date, right_date, left_price, right_price = (
                    j,
                    i,
                    right_date,
                    left_date,
                    right_price,
                    left_price,
                )
            if left_price < right_price - PRICE_TOLERANCE:
                continue
            errors.append(
                f"Rule 12 (effective-date value): {_label(i)} starts earlier on {left_date.isoformat()} at EUR "
                f"{left_price:.2f}, while {_label(j)} starts later on {right_date.isoformat()} at EUR "
                f"{right_price:.2f}. Calculate the date value with the cost model: the earlier date must have a "
                "lower unit price and the later date may carry a higher price. Do not flatten or reverse the trade."
            )
    return errors


def validate_monotonic_options(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[str]:
    """Prevent a new round from lowering an equivalent package price."""
    errors: list[str] = []
    for current_index, option in enumerate(current):
        price = _price(option)
        if price is None:
            continue
        for _previous_index, old in enumerate(previous):
            old_price = _price(old)
            if old_price is None or not _same_except(option, old, "unit_price"):
                continue
            if price < old_price - PRICE_TOLERANCE:
                errors.append(
                    f"Rule 13 (monotonic concession): {_label(current_index)} repeats the prior-round "
                    f"terms but lowers the price from EUR {old_price:.2f} to EUR {price:.2f}. Keep an equivalent "
                    "package at or above its previous price, or change a term and show the economic trade."
                )
    return errors


def validate_term_options(options: list[dict[str, Any]], rival_price: float | None = None) -> list[str]:
    """Return explicit economic violations for a package of buyer options.

    Comparisons are made only when every other term is equal. This avoids
    assigning a price effect to a bundle that changes several concessions.
    """
    errors: list[str] = []
    valid = [(i, option, _price(option)) for i, option in enumerate(options) if _price(option) is not None]

    # Rule 1: identical terms cannot have two prices.
    for pos, (i, left, left_price) in enumerate(valid):
        for j, right, right_price in valid[pos + 1 :]:
            if _same_except(left, right, "unit_price") and not math.isclose(
                left_price, right_price, abs_tol=PRICE_TOLERANCE
            ):
                errors.append(
                    f"Rule 1 (sanity): {_label(i)} and {_label(j)} have identical terms but different unit "
                    f"prices (EUR {left_price:.2f} vs EUR {right_price:.2f}). Change a non-price term or use "
                    "one price; never present duplicate terms at different prices."
                )

    def compare_order(field: str, relation: str, requirement: str) -> None:
        for pos, (i, left, left_price) in enumerate(valid):
            for j, right, right_price in valid[pos + 1 :]:
                if not _same_except(left, right, "unit_price", field):
                    continue
                lv, rv = left.get(field), right.get(field)
                if not isinstance(lv, (int, float)) or not isinstance(rv, (int, float)) or lv == rv:
                    continue
                if lv > rv:
                    i, j, lv, rv, left_price, right_price = j, i, rv, lv, right_price, left_price
                violates = (
                    right_price <= left_price + PRICE_TOLERANCE
                    if relation == "higher"
                    else right_price >= left_price - PRICE_TOLERANCE
                    if relation == "lower"
                    else right_price > left_price + PRICE_TOLERANCE
                )
                if violates:
                    price_rule = (
                        "a strictly higher" if relation == "higher" else
                        "a strictly lower" if relation == "lower" else
                        "a lower or equal"
                    )
                    errors.append(
                        f"{requirement}: {_label(i)} has {field}={lv} and EUR {left_price:.2f}, while "
                        f"{_label(j)} has {field}={rv} and EUR {right_price:.2f}. The option with {field}={rv} "
                        f"must have {price_rule} unit price when all other terms match."
                    )

    compare_order("payment_days", "higher", "Rule 2 (cost of capital)")
    compare_order("contract_months", "lower", "Rule 3 (commitment discount)")
    compare_order("lead_time_days", "lower_or_equal", "Rule 5 (lead-time flexibility)")

    # Rule 4: an indexed base price must undercut an otherwise identical fixed price.
    for pos, (i, left, left_price) in enumerate(valid):
        for j, right, right_price in valid[pos + 1 :]:
            if not _same_except(left, right, "unit_price", "index_clause"):
                continue
            left_indexed, right_indexed = _has_index(left.get("index_clause")), _has_index(right.get("index_clause"))
            if left_indexed == right_indexed:
                continue
            indexed_i, indexed_price = (i, left_price) if left_indexed else (j, right_price)
            fixed_i, fixed_price = (j, right_price) if left_indexed else (i, left_price)
            if indexed_price >= fixed_price - PRICE_TOLERANCE:
                errors.append(
                    f"Rule 4 (index risk): {_label(indexed_i)} uses an index clause at EUR {indexed_price:.2f}, "
                    f"but fixed-price {_label(fixed_i)} is EUR {fixed_price:.2f} with the same other terms. "
                    "The indexed base price must be strictly lower because the buyer absorbs raw-material risk."
                )

    # Rule 6: use a rival quote when one is available.
    if valid and rival_price is not None and math.isfinite(float(rival_price)):
        cheapest = min(price for _, _, price in valid)
        if cheapest > float(rival_price) + PRICE_TOLERANCE:
            errors.append(
                f"Rule 6 (competitive ceiling): every proposed option is above the rival quote of EUR "
                f"{float(rival_price):.2f}; the cheapest is EUR {cheapest:.2f}. At least one option must be "
                "priced at or below the rival quote so the competitive leverage is real."
            )

    return errors


def validate_strategic_options(
    options: list[dict[str, Any]],
    *,
    min_effective_date: str | None = None,
    price_ceiling: float | None = None,
) -> list[str]:
    """Validate hard buyer constraints derived from the case and data briefing."""
    errors: list[str] = []
    minimum = date.fromisoformat(min_effective_date) if min_effective_date else None
    for index, option in enumerate(options):
        label = _label(index)
        effective = option.get("effective_date")
        if minimum and effective:
            try:
                if date.fromisoformat(str(effective)) < minimum:
                    errors.append(
                        f"Rule 7 (contract notice): {label} starts on {effective}, before the minimum "
                        f"permitted effective date {min_effective_date}. Move the price change to the first "
                        "allowed date or later and cite the notice and contract IDs."
                    )
            except ValueError:
                errors.append(f"Rule 7 (contract notice): {label} has invalid effective_date={effective!r}.")
        price = _price(option)
        if price_ceiling is not None and price is not None and price > price_ceiling + PRICE_TOLERANCE:
            errors.append(
                f"Rule 8 (index ceiling): {label} is EUR {price:.2f}, above the data-backed ceiling of "
                f"EUR {price_ceiling:.2f}. Do not concede above this ceiling without a new verified cost fact."
            )
    return errors

