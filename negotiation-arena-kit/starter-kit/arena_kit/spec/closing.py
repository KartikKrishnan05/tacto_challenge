"""When to close (section 7), the accept ladder (R12.11) and the close guards of DualAgent and
TeamAgent (R11.17-R11.18, R12.12-R12.13, R12.15, R13.13). Pure functions over `View`s - one per live
supplier thread - so the rules are testable on recorded negotiations without the arena.

Accept the cheapest sheet that keeps the must-haves and beats the alternative when the supplier
agreed (R7.1), made a final offer (R7.2), its offer meets our next target - unless it is still
moving with more than 3 rounds left (R7.3) - its offer is as good as what we last offered (R7.4), at
most one round is left (R7.5), or the planned rounds are over (R7.6). Walk away only when nothing on
the table is acceptable and every thread is at a final offer or out of rounds. No deadlock close and
no walk-risk close.
"""

from dataclasses import dataclass, field

EARLIER_SHARE = 0.001  # R12.11: earlier IDs cheaper by 0.1 %+ go before the latest ones
MAX_EARLIER = 3
REVISIT_SHARE = 0.003  # R12.13: an earlier sheet 0.3 % cheaper is offered back once
COVERAGE_SHARE = 0.01  # R11.17: uncovered need over 1 % of C never "meets our target"
TARGET_TOL = 0.002  # R7.3
LAST_TOL = 0.001  # R7.4
MOVING_ROUNDS = 3  # R5.4 / R7.3
HOPELESS_SHARE, HOPELESS_MOVE = 0.08, 0.01  # R12.15
DEAR_SHARE = 0.25  # R8.2
STALL_TOL = 0.001


@dataclass
class Row:
    """One sheet the supplier put on the table: a main sheet or an alternative."""

    id: str
    terms: dict
    c: float  # our C with the margin charge where the goal has one
    valid: bool  # keeps the must-haves and beats the alternative
    latest: bool  # in its latest e-mail
    main: bool = True
    reply: int = 0  # which of its replies (0 = the opening)
    uncovered: float = 0.0  # EUR of uncovered need (and its risk) in C


@dataclass
class View:
    """One live supplier thread, as the close rules see it."""

    sid: str
    status: str | None
    left: int  # messages we may still send
    sent: int  # our messages that reached it
    rows: list = field(default_factory=list)
    target: float | None = None  # our next target C
    last_target: float | None = None  # the target of our last offer
    moving: bool = False  # still conceding more than 0.4 % of C (R5.4)
    only_price: bool = False  # every step term settled (R13.13)
    planned_over: bool = False  # H + 2 offers made (R7.6)
    close_gap: float = 0.005
    replies_c: list = field(default_factory=list)  # C of its best sheet per reply, oldest first
    earlier_used: bool = False  # R12.13 offer-back already made
    repair_used: bool = False  # R12.12 repair already made
    restore_used: bool = False  # R11.17 coverage message already made
    last_step_share: float = 0.0  # how far it moved in its last reply, share of C
    current_round: int = 0  # buyer messages already sent in this thread
    ideal_price: float | None = None  # code-calculated unit-price target for early-accept checks

    @property
    def latest_rows(self):
        return [r for r in self.rows if r.latest]

    @property
    def best(self):
        """Its cheapest valid sheet of the latest e-mail (R3.1), else None."""
        ok = [r for r in self.latest_rows if r.valid]
        return min(ok, key=lambda r: r.c) if ok else None

    @property
    def c_now(self):
        rows = self.latest_rows
        return min((r.c for r in rows if r.valid), default=min((r.c for r in rows), default=float("inf")))


@dataclass
class Close:
    kind: str  # accept | walk | idle | offer_back | repair | restore
    reason: str
    sid: str | None = None
    sheet_id: str | None = None
    candidates: list = field(default_factory=list)  # the accept ladder [(sid, sheet_id)]
    terms: dict | None = None  # offer_back / repair / restore: the terms to send


def blocked(views, row, sid):
    """R11.18: never accept a bidder's unchanged opening while a cheaper rival sheet is on the table."""
    if row.reply != 0 or len(views) < 2:
        return False
    return any(r.valid and r.latest and r.c < row.c for v in views.values() if v.sid != sid for r in v.rows)


def ladder(views):
    """R12.11: every valid sheet ever offered in every live thread, cheapest first; up to MAX_EARLIER
    earlier IDs (cheaper by EARLIER_SHARE+) before the latest ones. A refused accept changes nothing."""
    rows = sorted(
        (r.c, not r.latest, not r.main, v.sid, r.id)
        for v in views.values()
        for r in v.rows
        if r.valid and not blocked(views, r, v.sid)
    )
    now = [x for x in rows if not x[1]]
    if not now:
        return []
    earlier = [x for x in rows if x[1] and x[0] < now[0][0] * (1 - EARLIER_SHARE)][:MAX_EARLIER]
    return list(dict.fromkeys((x[3], x[4]) for x in sorted(earlier + now)))


def guarded_ladder(views, *, force=False):
    """Filter the accept ladder through the anti-capitulation rule."""
    from team.term_guard import anti_capitulation_check

    out = []
    for sid, sheet_id in ladder(views):
        view = views[sid]
        row = next((r for r in view.rows if r.id == sheet_id), None)
        errors = [] if force else anti_capitulation_check(
            row.terms if row else {},
            current_round=view.current_round,
            ideal_target=view.ideal_price,
        )
        if not errors:
            out.append((sid, sheet_id))
    return out


def accept(views, reason, *, force=False):
    cands = guarded_ladder(views, force=force)
    if not cands:
        return None
    return Close("accept", reason, cands[0][0], cands[0][1], cands)


def walk(views, reason):
    sid = next(iter(views)) if views else None
    return Close("walk", reason, sid)


def earlier_cheaper(view):
    """R12.13: once per thread, an earlier valid sheet of its own 0.3 % cheaper than the best of its
    latest e-mail - its terms go back as our sheet."""
    if view.earlier_used or view.left <= 0 or view.status == "final_offer":
        return None
    now = [r.c for r in view.latest_rows if r.valid] or [r.c for r in view.latest_rows]
    before = [r for r in view.rows if not r.latest and r.valid]
    if not now or not before:
        return None
    r = min(before, key=lambda r: r.c)
    return r if r.c <= (1 - REVISIT_SHARE) * min(now) else None


def stalled(view):
    """No cheaper offer over its last three replies after 3+ of our messages (R12.13)."""
    cs = view.replies_c
    if view.sent < 3 or len(cs) < 3:
        return False
    return cs[-1] >= cs[-3] - max(1.0, STALL_TOL * cs[-3])


def covered(view, row):
    return row is not None and row.uncovered <= COVERAGE_SHARE * row.c


def ends(view):
    """At a final offer or out of rounds: nothing more will come from this thread."""
    return view.status in ("final_offer", "agreed") or view.left <= 0


def hopeless(views, view):
    """R12.15 / R8.2 (RFQ, 3+ live bidders): far dearer than the best and not moving - stop writing."""
    if len(views) < 3 or view.best is None:
        return False
    best_all = min((v.best.c for v in views.values() if v.best), default=None)
    if best_all is None:
        return False
    if (
        view.best.c > (1 + HOPELESS_SHARE) * best_all
        and view.last_step_share < HOPELESS_MOVE
        and len(view.replies_c) >= 3
    ):
        return True
    return view.best.c > (1 + DEAR_SHARE) * best_all and view.sent >= 2


def decide(views, sid, must_close=False):
    """The close rule for thread `sid`, or None (negotiate on). `views`: every live thread."""
    v = views[sid]
    rfq = len(views) > 1
    cands = guarded_ladder(views)
    best_sid = cands[0][0] if cands else None
    heard = all(x.sent >= 1 for x in views.values())  # R11.18: every bidder has had an offer
    may_accept = bool(cands) and (not rfq or heard)

    if must_close:  # R11.14: closed by the clock
        return accept(views, "case clock: close", force=True) or walk(views, "case clock: nothing acceptable")

    if v.status == "final_offer":  # R12.12: its last word - accept at once, anything more makes it walk
        if cands and (not rfq or best_sid == sid or all(ends(x) for x in views.values())):
            return accept(views, "final offer")
        if cands and rfq:
            return Close("idle", "final offer, a rival is better", sid)
        if not v.repair_used and v.left > 0 and v.rows:
            return Close("repair", "final offer breaks a must-have or loses to the alternative - one repair", sid)
        if all(ends(x) for x in views.values()):
            return walk(views, "nothing acceptable, every thread at its end")
        return Close("idle", "final offer, nothing acceptable here", sid)

    if v.status == "agreed":  # R7.1, R12.13
        if (r := earlier_cheaper(v)) is not None:
            return Close("offer_back", "its earlier offer was cheaper than the agreed sheet", sid, r.id, terms=r.terms)
        if may_accept and (not rfq or best_sid == sid):
            return accept(views, "agreed to our terms")

    if stalled(v):  # R12.13: back to its earlier, cheaper offer once - never a deadlock close (R5.3, R7)
        if (r := earlier_cheaper(v)) is not None:
            return Close("offer_back", "stalled: back to its own earlier, cheaper offer", sid, r.id, terms=r.terms)

    best = v.best
    if best is not None and v.sent >= 1 and v.target is not None:
        meets = best.c <= v.target * (1 + TARGET_TOL) and not (v.moving and v.left > MOVING_ROUNDS)
        if meets and not covered(v, best):  # R11.17
            if v.left > 0 and not v.restore_used:
                return Close("restore", "meets the target but leaves need uncovered", sid, best.id, terms=best.terms)
            meets = False
        if meets and may_accept and best_sid == sid:
            return accept(views, "its offer meets our next target")
        if (
            v.only_price
            and best.c <= v.target * (1 + v.close_gap)
            and covered(v, best)
            and may_accept
            and best_sid == sid
        ):
            return accept(views, "small gap, only price left")  # R13.13
    if best is not None and v.last_target is not None and best.c <= v.last_target * (1 + LAST_TOL):
        if covered(v, best) and may_accept and best_sid == sid:
            return accept(views, "its offer is as good as what we last offered")  # R7.4

    if (v.left <= 1 and not rfq) or all(x.left <= 1 for x in views.values()):  # R7.5
        if may_accept or (cands and rfq):
            return accept(views, "at most one round left")
        if all(ends(x) for x in views.values()):
            return walk(views, "nothing acceptable and no rounds left")
    if all(x.planned_over for x in views.values()) and may_accept:  # R7.6
        return accept(views, "the planned rounds are over")
    if rfq and hopeless(views, v):
        return Close("idle", "far dearer than the best bidder and not moving", sid)
    if v.left <= 0:
        return (
            Close("idle", "no messages left in this thread", sid)
            if rfq
            else (accept(views, "no messages left") or walk(views, "no messages left, nothing acceptable"))
        )
    return None
