"""Settling the heavy terms one at a time (R13.1-R13.5), from the MESO agent - with its two failures
fixed (R13.3 reopen a costly lock, R13.4 a step needs a choice).

After coverage is set (R12.2) the remaining non-price terms are ranked by how many EUR they move C,
the playbook's terms first; terms under MIN_WEIGHT of C are skipped, at most MAX_STEPS are kept.
Each step message varies ONE term across up to three packages that all cost us the same C (R13.8);
the value the supplier answers with is locked and the next message varies the next term. When
every term is locked only price is left, and section 6's sheets take over.

Judgement call: payment days, when they are a step term, go first - so the first menu is also the
payment probe of R12.7 and message 1 varies a single term as R13.1 asks.
"""

from collections import Counter

from .sheets import priced, structure_floor
from .terms import COVERAGE, complete, distance, key

MAX_STEPS = 3
MIN_WEIGHT = 0.002  # R13.1: a term that moves C by less than 0.2 % is not worth a round
REOPEN_SHARE = 0.01  # R13.3: a lock costing us 1 % of C against the term's best option is stepped again
MAX_PER_TERM = 2


class Steps:
    """The step plan of one thread."""

    def __init__(self):
        self.order = None  # the terms worth a step, in order (fixed at the first move)
        self.weights = {}
        self.history = {}  # index of our message -> the term it varied, "price" or a one-off kind
        self.skipped = set()  # terms that had no credible choice (R13.4)
        self.revealed_values = {}

    # -- ranking ----------------------------------------------------------------------------------
    def rank(self, ctx, base):
        """R13.1: the non-price, non-coverage terms by EUR weight, the playbook's first."""
        if self.order is not None:
            return self.order
        price = ctx.their_price
        c_base = ctx.c({**base, "unit_price": price})
        first = list((ctx.playbook or {}).get("first") or [])
        weights = []
        for f in ctx.case["negotiable"]:
            if f == "unit_price" or f in COVERAGE:
                continue
            opts = ctx.allowed(f, base)
            if len(opts) < 2:
                continue
            costs = [ctx.c({**base, f: o, "unit_price": price}) for o in opts]
            w = max(costs) - min(costs)
            if w >= MIN_WEIGHT * c_base:
                weights.append((w, f))
                self.weights[f] = w
        weights.sort(key=lambda x: (x[1] != "payment_days", first.index(x[1]) if x[1] in first else len(first), -x[0]))
        self.order = [f for _, f in weights[:MAX_STEPS]]
        return self.order

    # -- what is settled --------------------------------------------------------------------------
    def counts(self):
        return Counter(v for v in self.history.values() if v not in ("price",))

    def locked(self, ctx, events):
        """After each step, the value of its term the supplier answered with (our first package's value
        if it answered without one, or with one that breaks a must-have). A term stepped only once whose
        answer costs us more than REOPEN_SHARE of C against its best option stays open (R13.3)."""
        out = {}
        mine = [i for i, m in enumerate(events) if m.get("from") == "buyer"]
        for k, field in sorted(self.history.items()):
            if field not in ctx.case["negotiable"] or k >= len(mine):
                continue
            i = mine[k]
            ours = ((events[i].get("term_sheets") or [{}])[0].get("terms") or {}).get(field)
            reply = next((m for m in events[i + 1 :] if m.get("from") != "buyer"), None)
            if reply is None:
                continue
            sheets = reply.get("term_sheets") or []
            theirs = sheets[0]["terms"].get(field, ctx.std.get(field, ours)) if sheets else ours
            out[field] = theirs if ctx.ok({**ctx.their, **out, field: theirs}) else ours
        counts = self.counts()
        t = complete(ctx.case, {**ctx.their, **out}, ctx.std)
        c_t = ctx.c(t)
        for field in [f for f in out if counts[f] == 1]:
            costs = [ctx.c({**t, field: o}) for o in ctx.allowed(field, t)]
            if costs and c_t - min(costs) > REOPEN_SHARE * c_t:
                del out[field]
        return out

    def revealed(self, ctx, latest_msg, alternatives):
        """R13.5: a term the supplier priced itself - its latest e-mail offers two packages that differ
        in that term only (its sheet and an alternative sheet or "Alternatively" line) - locked at the
        value that costs us less at the supplier's own prices."""
        sheets = [ts.get("terms") or {} for ts in latest_msg.get("term_sheets") or []]
        if not sheets:
            return dict(self.revealed_values)
        main = complete(ctx.case, sheets[0], ctx.std)
        best = {}
        for alt in [*sheets[1:], *alternatives]:
            full = complete(ctx.case, {**sheets[0], **alt}, ctx.std)
            diff = [f for f in ctx.case["negotiable"] if f != "unit_price" and key(full.get(f)) != key(main.get(f))]
            if len(diff) != 1 or diff[0] in COVERAGE or not ctx.ok(full):
                continue
            f = diff[0]
            choices = [(ctx.c(full), full[f])]
            if ctx.ok(main):
                choices.append((ctx.c(main), main[f]))
            for c, value in choices:
                if f not in best or c < best[f][0]:
                    best[f] = (c, value)
        self.revealed_values.update({f: v for f, (_, v) in best.items()})
        return dict(self.revealed_values)

    def next_field(self, ctx, base, locked, asks=()):
        """The term this message varies: an asked-for step term first, else the next of the order that
        is not locked, not skipped and stepped at most once before; None once only price is left."""
        counts = self.counts()
        order = self.rank(ctx, base)
        for f in [*[a for a in asks if a in order], *order]:
            if f in locked or f in self.skipped or counts[f] >= MAX_PER_TERM:
                continue
            if len(ctx.allowed(f, base)) > 1:
                return f
        return None

    def only_price(self, ctx, base, locked):
        return self.next_field(ctx, base, locked) is None

    # -- the packages of one step -----------------------------------------------------------------
    @staticmethod
    def option_order(ctx, field, base, asked=None):
        """R13.2: the best option for us first, then the one the supplier asked for, its current value,
        the middle option, the rest - three at most, each a different package."""
        opts = ctx.allowed(field, base)
        price = ctx.their_price
        theirs = complete(ctx.case, ctx.their, ctx.std).get(field)
        # cheapest for us first; a tie within EUR 1 goes to the option closest to the supplier's own value
        # - no stricter ask than needed (R12.4)
        ranked = sorted(
            opts, key=lambda o: (round(ctx.c({**base, field: o, "unit_price": price})), distance(o, theirs))
        )
        wanted = [ranked[0], *([asked] if asked is not None else []), theirs, ranked[len(ranked) // 2], *ranked]
        picks = []
        for o in wanted:
            if any(key(o) == key(x) for x in opts) and all(key(o) != key(p) for p in picks) and len(picks) < 3:
                picks.append(o)
        return picks

    def packages(self, ctx, field, base, target, floor_price, asked=None):
        """The step's packages at `target`, each credible (at or above its structure's floor, under the
        supplier's price). R13.4: fewer than two is no step - [] then."""
        out = []
        for o in self.option_order(ctx, field, base, asked):
            combo = {**base, field: o}
            sheet = priced(ctx, combo, target, structure_floor(ctx, combo, base, floor_price))
            if sheet and ctx.ok(sheet) and all(sheet != s for _, s in out):
                out.append((f"{field}={o if not isinstance(o, dict) else o.get('deadband_pct')}", sheet))
        return out if len(out) >= 2 else []

    def record(self, k, kind):
        self.history[k] = kind


def repeats(new_sheets, last_sheets):
    """R13.7: the next message would only repeat our last sheets."""
    if not last_sheets or not new_sheets:
        return False
    a = sorted(key(t) for t in new_sheets)
    b = sorted(key(t) for t in last_sheets)
    return a == b
