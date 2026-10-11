"""Data arguments that check out: sentences with a figure and its IDs, computed from the facts.

The arena checks every cited ID against `facts.json` (api.md, "What counts"): an index change over
two months of one series, at most 12 months apart, ending within the last 12 months of the data; a
quote's or a rival offer's price; a scorecard's on-time delivery or ppm; a should-cost estimate; a
forecast change; a letter's notice against the contract's. Here code computes each figure the way
the arena does and writes one sentence per argument - the language model only chooses among them,
and they go into the e-mail word for word. Each ID counts once per case (a rival's quote once per
supplier), so `Ledger` remembers what has been cited.

Only arguments that argue our side are offered to the model: an index that rose "confirms the
supplier's case" (P02, P04), so it is kept out unless it rose far less than the increase asked for.
"""

import re
from dataclasses import dataclass, field

DATA_END = "2026-09"  # the data is frozen at 2026-09-30


@dataclass
class Claim:
    key: str  # stable across rounds, e.g. "index:PPI-D351"
    kind: str
    ids: list
    sentence: str
    favourable: bool = True  # False: true, but it argues the supplier's side
    note: str = ""  # for the model: why it matters
    figure: float | None = None
    strength: float = 1.0  # how hard it argues our case: ranks the arguments
    extra: list = field(default_factory=list)  # context sentences without IDs, sent with the claim


class Ledger:
    """What has been cited in this case."""

    def __init__(self):
        self.used = set()

    def spent(self, claim, sid):
        return any(self._key(i, sid) in self.used for i in claim.ids)

    def use(self, claim, sid):
        for i in claim.ids:
            self.used.add(self._key(i, sid))

    @staticmethod
    def _key(ref, sid):
        return (sid, ref) if ref.startswith(("quote:", "offer:")) else ref


def safe_query(package, sql, params=()):
    try:
        return package.query(sql, params)
    except Exception:  # no package, no tables, or a table this release does not have
        return []


def _fact(package, case, ref):
    return (case.get("facts") or {}).get(ref) or (package.lookup(ref) if package else None)


def _month_minus(month, n):
    y, m = map(int, month.split("-"))
    m -= n
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}-{m:02d}"


def series_name(package, series):
    row = package.lookup(series) if package else None
    name = ""
    if package and not row:
        rows = safe_query(package, "SELECT name FROM index_series WHERE id = ?", (series,))
        name = rows[0]["name"] if rows else ""
    name = name or (row or {}).get("name") or ""
    if m := re.match(r"Producer prices DE: (.+)", name):
        return f"German producer prices for {m.group(1)}"
    if name.startswith("Labour cost index DE"):
        return "The German labour cost index for manufacturing"
    if m := re.match(r"(Copper|Nickel|Aluminium|Iron ore), monthly", name):
        return f"The {m.group(1).lower()} price"
    if name.startswith("Natural gas Europe"):
        return "The European natural gas price"
    if name.startswith("Day-ahead power price"):
        return "The German day-ahead power price"
    if m := re.match(r"Alloy surcharge ([\d.]+)", name):
        return f"The alloy surcharge for {m.group(1)}"
    if name.startswith("Copper notation"):
        return "The copper notation"
    return f"The {series} index"


def index_change(package, case, series, end=None):
    """(change %, earlier id, later id) over the last 12 months the data has, or None."""
    ids = sorted(
        set(package.ids(f"idx:{series}@") if package else [])
        | {k for k in (case.get("facts") or {}) if k.startswith(f"idx:{series}@")}
    )
    ids = [i for i in ids if i.split("@")[1] <= (end or DATA_END)]
    if not ids:
        return None
    later = ids[-1]
    month = later.split("@")[1]
    if month < _month_minus(DATA_END, 11):
        return None  # too old: the arena reads it as irrelevant
    earlier = f"idx:{series}@{_month_minus(month, 12)}"
    a, b = _fact(package, case, earlier), _fact(package, case, later)
    if not (a and b and a.get("value") and b.get("value") is not None):
        return None
    return round((b["value"] / a["value"] - 1) * 100, 1), earlier, later


def candidates(package, case, sid, their_price=None, rivals=None):
    """Every argument the data gives for this supplier, strongest first.
    `their_price`: the supplier's standing unit price; `rivals`: {offer_id: unit price} of the other
    suppliers in this session."""
    out = []
    parts = case.get("parts", [])
    trigger = _fact(package, case, case["trigger_doc"]) if case.get("trigger_doc") else None
    asked = (trigger or {}).get("asked_pct")

    # 1. The index series that drive the part's cost (or that a clause would follow).
    weights = {}
    if package:
        for p in parts:
            for r in safe_query(package, "SELECT series_id, weight FROM cost_drivers WHERE part_id = ?", (p,)):
                weights[r["series_id"]] = max(weights.get(r["series_id"], 0), r["weight"] or 0)
    for opt in case.get("term_options", {}).get("index_clause", []) or []:
        if opt:
            weights.setdefault(opt["series"], opt.get("material_share", 0))
    for ref in case.get("refs", []):
        if ref.startswith("idx:"):
            weights.setdefault(ref[4:].split("@")[0], 0)
    impact = 0.0
    for series, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        got = index_change(package, case, series)
        if not got:
            continue
        chg, a, b = got
        impact += w * chg
        # a rise argues the supplier's side ("your figures confirm our case") - unless it is far
        # below the increase it asks for
        fav = chg <= 0 or (asked is not None and chg < asked / 2)
        out.append(
            Claim(
                key=f"index:{series}",
                kind="index",
                ids=[a, b],
                figure=chg,
                favourable=fav,
                sentence=f"{series_name(package, series)} changed {chg:+.1f}% "
                f"from {a.split('@')[1]} to {b.split('@')[1]} ({a}, {b}).",
                note=f"weight {w:.0%} in the part's cost" if w else "",
                strength=max(0.0, (w or 0.1) * ((asked or 0) - chg)) if asked else max(0.0, -(w or 0.1) * chg),
            )
        )
    if weights and asked:
        out.append(
            Claim(
                key="summary:indices",
                kind="summary",
                ids=[],
                figure=round(impact, 1),
                favourable=impact < asked,
                sentence=f"Weighted with the cost structure of this part, the index movements explain about "
                f"{impact:+.1f}% on the price - not {asked:g}%.",
                note="no ID: our own arithmetic on the supplier's own drivers",
                strength=5.0,
            )
        )

    # 2. A letter that gives less notice than the contract requires.
    if trigger and trigger.get("days") is not None:
        ctr = next((r for r in case.get("refs", []) if r.startswith("ctr:") and "#" not in r), None)
        cf = _fact(package, case, ctr) if ctr else None
        if cf and cf.get("days") and trigger["days"] < cf["days"]:
            out.append(
                Claim(
                    key="notice",
                    kind="notice",
                    ids=[case["trigger_doc"], ctr],
                    figure=trigger["days"],
                    sentence=f"Your letter gives us only {trigger['days']} days of notice "
                    f"({case['trigger_doc']}, {ctr}).",
                    extra=[f"Our contract requires {cf['days']} days."],
                    note="the increase cannot take effect as announced",
                    strength=5.0,
                )
            )

    # 3. Delivery and quality record.
    num = sid.split(":")[1]
    sc = sorted(
        i
        for i in ([*case.get("refs", [])] + (package.ids(f"sc:{num}@") if package else []))
        if i.startswith(f"sc:{num}@")
    )
    if sc and (row := _fact(package, case, sc[-1])):
        if case.get("type") == "quality" or (row.get("ppm") or 0) >= 1000:
            out.append(
                Claim(
                    key="scorecard",
                    kind="quality",
                    ids=[sc[-1]],
                    figure=row.get("ppm"),
                    favourable=(row.get("ppm") or 0) > 300,
                    sentence=f"Your defect rate over the last four quarters stands at {row.get('ppm')} ppm ({sc[-1]}).",
                    strength=3.0 if case.get("type") == "quality" else 1.0,
                )
            )
        elif row.get("otd_pct") is not None:
            out.append(
                Claim(
                    key="scorecard",
                    kind="delivery",
                    ids=[sc[-1]],
                    figure=row["otd_pct"],
                    favourable=row["otd_pct"] < 95,
                    sentence=f"Your on-time delivery over the last four quarters was {row['otd_pct']:.1f}% ({sc[-1]}).",
                    strength=(95 - row["otd_pct"]) / 5,
                )
            )

    # 4. Competing prices: a rival's offer in this session, or a quote the case points to.
    for oid, price in sorted((rivals or {}).items(), key=lambda kv: kv[1]):
        if their_price is None or price < their_price:
            out.append(
                Claim(
                    key=f"rival:{oid.split('-')[0]}",
                    kind="rival",
                    ids=[oid],
                    figure=price,
                    sentence=f"We hold a competing offer at EUR {price:.2f} per unit ({oid}).",
                    note="reveals that rival's price to this supplier",
                    strength=6.0,
                )
            )
            break
    alt = (case.get("buyer") or {}).get("alternative") or {}
    for ref in [alt.get("ref")] + [r for r in case.get("refs", []) if r.startswith("quote:")]:
        q = _fact(package, case, ref) if ref and ref.startswith("quote:") else None
        if q and q.get("supplier") != sid and q.get("unit_price") and q.get("part") in parts:
            fav = their_price is not None and q["unit_price"] < their_price
            out.append(
                Claim(
                    key=f"quote:{ref}",
                    kind="rival",
                    ids=[ref],
                    figure=q["unit_price"],
                    favourable=fav,
                    sentence=f"We hold a quote from another qualified supplier at EUR {q['unit_price']:.2f} "
                    f"per unit ({ref}).",
                    note="" if fav else "higher than this supplier's price: citing it would weaken us",
                    strength=4.0,
                )
            )
            break

    # 5. Should-cost: only when it leaves the supplier a healthy margin at a lower price.
    for p in parts:
        ref = f"cost:{p.split(':')[1]}"
        if (row := _fact(package, case, ref)) and row.get("unit_cost") and their_price:
            fav = row["unit_cost"] * 1.08 < their_price
            out.append(
                Claim(
                    key="should_cost",
                    kind="should_cost",
                    ids=[ref],
                    figure=row["unit_cost"],
                    favourable=fav,
                    sentence=f"Our should-cost estimate for this part is EUR {row['unit_cost']:.2f} per unit ({ref}).",
                    note="no margin included" if fav else "close to their price: shows they earn little - avoid",
                    strength=3.0,
                )
            )

    # 6. Volume: a growing forecast is a reason to want our business.
    if package:
        for p in parts:
            fcs = package.ids(f"fc:{p.split(':')[1]}@")
            if fcs and (row := _fact(package, case, fcs[-1])) and row.get("change_pct") is not None:
                # only as growth if the volume did grow: a forecast change next to a falling volume
                # reads as overstated
                before = _fact(package, case, fcs[-2]) if len(fcs) > 1 else None
                grew = not before or (row.get("volume_per_year") or 0) >= (before.get("volume_per_year") or 0)
                out.append(
                    Claim(
                        key="forecast",
                        kind="volume",
                        ids=[fcs[-1]],
                        figure=row["change_pct"],
                        favourable=row["change_pct"] > 0 and grew,
                        sentence=f"Our demand forecast for this part has changed "
                        f"{row['change_pct']:+.1f}% ({fcs[-1]}).",
                        note="growing volume: a carrot, not a threat",
                        strength=1.5,
                    )
                )
    out.sort(key=lambda c: -c.strength)
    return out


def usable(claims, ledger, sid):
    """The favourable claims not cited yet."""
    return [c for c in claims if c.favourable and not ledger.spent(c, sid)]
