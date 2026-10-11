"""Deterministic economic checks for multi-option term sheets."""

from __future__ import annotations

import math
from datetime import date, timedelta
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


def validate_contractual_notice(
    options: list[dict[str, Any]],
    minimum_notice_days: int | None,
    *,
    current_date: date | str | None = None,
) -> list[str]:
    """Enforce the contractual lead time before a price change can take effect.

    ``current_date`` is injectable so recorded negotiations and tests can use the date
    on which notice was received.  In a live run it defaults to today's date.  The
    check is deliberately independent of the prose: every structured option must
    carry the corrected date or it is blocked.
    """
    if minimum_notice_days is None:
        return []
    try:
        days = int(minimum_notice_days)
    except (TypeError, ValueError):
        return [
            f"Rule 14 (contractual notice): minimum_notice_days={minimum_notice_days!r} is invalid. "
            "Supply a non-negative integer from the verified contract."
        ]
    if days < 0:
        return [
            f"Rule 14 (contractual notice): minimum_notice_days={days} cannot be negative. "
            "Use the verified contract notice period."
        ]
    try:
        received = date.fromisoformat(str(current_date)) if current_date is not None else date.today()
    except (TypeError, ValueError):
        return [
            f"Rule 14 (contractual notice): current_date={current_date!r} is not a valid ISO date. "
            "Use YYYY-MM-DD so the contract floor can be calculated."
        ]
    earliest = received + timedelta(days=days)
    errors: list[str] = []
    for index, option in enumerate(options):
        effective = option.get("effective_date")
        if effective is None:
            continue
        try:
            effective_date = date.fromisoformat(str(effective))
        except (TypeError, ValueError):
            errors.append(
                f"Rule 14 (contractual notice): {_label(index)} has invalid effective_date={effective!r}. "
                f"Use the first contract-compliant date, {earliest.isoformat()}, or later."
            )
            continue
        if effective_date < earliest:
            errors.append(
                f"Rule 14 (contractual notice): {_label(index)} starts on {effective_date.isoformat()}, only "
                f"{(effective_date - received).days} days after notice on {received.isoformat()}, but the "
                f"contract requires at least {days} days. Enforce effective_date >= {earliest.isoformat()} "
                "in the structured term sheet and state that corrected date in the e-mail."
            )
    return errors


def anti_capitulation_check(
    terms: dict[str, Any],
    *,
    current_round: int,
    ideal_target: float | None,
    threshold_pct: float = 0.01,
) -> list[str]:
    """Prevent accepting an early supplier offer before it reaches our ideal target.

    ``ideal_target`` is the unit-price target calculated by the cost planner.  The
    exception is intentionally narrow: an early acceptance is allowed only when the
    supplier is within ``threshold_pct`` above that target.  A missing target does
    not manufacture a number; callers should continue negotiating instead.
    """
    try:
        round_no = int(current_round)
    except (TypeError, ValueError):
        round_no = 0
    if round_no >= 3:
        return []
    if ideal_target is None:
        return [
            f"Rule 15 (anti-capitulation): do not accept in round {round_no} because the cost planner supplied "
            "no ideal unit-price target. Send a counter-offer or hold the standing terms until a calculated "
            "target is available."
        ]
    price = _price(terms)
    if price is None:
        return [
            "Rule 15 (anti-capitulation): cannot validate the supplier's offer because "
            f"unit_price={terms.get('unit_price')!r} "
            "is missing or invalid. Do not accept; request a complete counter-offer."
        ]
    try:
        target = float(ideal_target)
        tolerance = max(0.0, float(threshold_pct))
    except (TypeError, ValueError):
        return [
            f"Rule 15 (anti-capitulation): ideal_target={ideal_target!r} is not a valid calculated unit-price target. "
            "Do not accept until the cost planner supplies a target."
        ]
    if not math.isfinite(target) or target <= 0:
        return [
            f"Rule 15 (anti-capitulation): ideal_target={ideal_target!r} is not a finite positive calculated target. "
            "Do not accept an early offer without a valid target."
        ]
    ceiling = target * (1.0 + tolerance)
    if price <= ceiling + PRICE_TOLERANCE:
        return []
    return [
        f"Rule 15 (anti-capitulation): do not accept in round {round_no}. The supplier asks EUR {price:.2f} per unit, "
        f"above the calculated ideal target EUR {target:.2f} (early-accept ceiling EUR {ceiling:.2f}). "
        "Send a counter-offer or hold the standing terms and keep testing the supplier's floor; acceptance is "
        "allowed before round 3 only when the price is within the target tolerance."
    ]


def _buyer_term_improved(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Return buyer-valued terms that improved between two packages."""
    improved: list[str] = []
    for field, direction in (
        ("payment_days", 1),
        ("lead_time_days", -1),
        ("contract_months", 1),
        ("effective_date", 1),
    ):
        old, new = previous.get(field), current.get(field)
        if old is None or new is None:
            continue
        if field == "effective_date":
            try:
                old, new = date.fromisoformat(str(old)), date.fromisoformat(str(new))
            except (TypeError, ValueError):
                continue
        elif (
            not isinstance(old, (int, float))
            or isinstance(old, bool)
            or not isinstance(new, (int, float))
            or isinstance(new, bool)
        ):
            continue
        if (new > old if direction > 0 else new < old):
            improved.append(field)
    return improved


def validate_reciprocity(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> list[str]:
    """Block a higher buyer price unless a buyer-valued term improves with it.

    Each current package is compared with the closest previous package on the
    non-price terms.  This catches both identical-term giveaways and packages that
    quietly raise price while changing a term in the supplier's favour.
    """
    if not previous:
        return []
    errors: list[str] = []
    for current_index, option in enumerate(current):
        price = _price(option)
        if price is None:
            continue
        candidates = [(i, old, _price(old)) for i, old in enumerate(previous) if _price(old) is not None]
        if not candidates:
            continue
        # Minimise the number of non-price differences so A/B/C packages are
        # compared with their natural equivalent across rounds.
        def distance(item, current=option):
            _i, old, _old_price = item
            fields = set(old) | set(current)
            return sum(
                not _same_value(old.get(field), current.get(field))
                for field in fields
                if field != "unit_price"
            )

        previous_index, old, old_price = min(candidates, key=distance)
        if price <= old_price + PRICE_TOLERANCE:
            continue
        improved = _buyer_term_improved(old, option)
        if improved:
            continue
        errors.append(
            f"Rule 16 (reciprocity): {_label(current_index)} raises unit price from EUR {old_price:.2f} in the "
            f"closest prior package {_label(previous_index)} to EUR {price:.2f}, but no buyer-valued term improved. "
            "A higher price requires at least one of payment_days increasing, lead_time_days decreasing, "
            "contract_months increasing, or effective_date moving later. Hold the previous price or add one of "
            "those concessions explicitly before sending."
        )
    return errors
