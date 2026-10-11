"""Section 7 close rules, the accept ladder (R12.11), final offers (R12.12), offer-back (R12.13),
coverage before accepting (R11.17), RFQ bidders heard (R11.18) and a small gap (R13.13)."""

from arena_kit.spec.closing import Row, View, decide, ladder


def row(id_, c, latest=True, valid=True, main=True, reply=1, uncovered=0.0):
    return Row(id_, {"unit_price": c / 1000}, c, valid, latest, main, reply, uncovered)


def view(sid="sup:1", status="open", left=8, sent=1, rows=None, target=900_000.0, last_target=None, **kw):
    rows = rows if rows is not None else [row(f"offer:{sid[4:]}-1", 1_000_000)]
    return View(sid, status, left, sent, rows, target, last_target, **kw)


def one(v):
    return {v.sid: v}


# --- R7 ---------------------------------------------------------------------------------------------
def test_agreed_is_accepted():
    c = decide(one(view(status="agreed")), "sup:1")
    assert c.kind == "accept" and c.reason == "agreed to our terms"


def test_final_offer_is_accepted_at_once():
    """R7.2 / R12.12: any message after a final offer made it walk 8/8 - accept."""
    c = decide(one(view(status="final_offer")), "sup:1")
    assert c.kind == "accept" and c.sheet_id == "offer:1-1"


def test_offer_meeting_our_target_is_accepted():
    c = decide(one(view(rows=[row("offer:1-2", 900_500)], target=900_000)), "sup:1")
    assert c.kind == "accept" and "target" in c.reason


def test_still_moving_with_rounds_left_is_not_accepted():
    """R7.3 / R5.4: meets the target but still conceding with more than 3 rounds left - ask again."""
    v = view(rows=[row("offer:1-2", 900_500)], target=900_000, moving=True, left=6)
    assert decide(one(v), "sup:1") is None
    v.left = 3
    assert decide(one(v), "sup:1").kind == "accept"


def test_as_good_as_our_last_offer_is_accepted():
    """R7.4."""
    v = view(rows=[row("offer:1-3", 950_000)], target=920_000, last_target=950_500)
    assert decide(one(v), "sup:1").reason == "its offer is as good as what we last offered"


def test_last_round_and_planned_rounds_accept():
    """R7.5 and R7.6."""
    assert decide(one(view(left=1, target=800_000)), "sup:1").reason == "at most one round left"
    assert decide(one(view(planned_over=True, target=800_000)), "sup:1").reason == "the planned rounds are over"


def test_no_close_on_a_two_round_hold():
    """R5.3 / R7: both sides held two rounds - no deadlock close; negotiate on."""
    v = view(sent=4, target=800_000, replies_c=[1_000_000, 1_000_000, 1_000_000], last_target=800_000)
    assert decide(one(v), "sup:1") is None


def test_stalled_offers_back_its_earlier_cheaper_sheet_once():
    """R12.13: stalled -> its earlier, cheaper sheet once; never a deadlock accept."""
    rows = [row("offer:1-1", 980_000, latest=False, reply=1), row("offer:1-4", 1_000_000, reply=4)]
    v = view(sent=4, rows=rows, target=800_000, replies_c=[1_000_000, 1_000_000, 1_000_000])
    c = decide(one(v), "sup:1")
    assert c.kind == "offer_back" and c.sheet_id == "offer:1-1"
    v.earlier_used = True
    assert decide(one(v), "sup:1") is None


def test_agreed_but_its_earlier_sheet_was_cheaper():
    """R12.13: 'agreed' only echoes our sheet - offer its earlier, 0.3 % cheaper sheet back first."""
    rows = [row("offer:1-1", 990_000, latest=False), row("offer:1-2", 1_000_000)]
    c = decide(one(view(status="agreed", rows=rows)), "sup:1")
    assert c.kind == "offer_back" and c.sheet_id == "offer:1-1"


def test_walk_only_when_nothing_acceptable_and_every_thread_ends():
    v = view(status="final_offer", rows=[row("offer:1-2", 1_100_000, valid=False)], repair_used=True)
    assert decide(one(v), "sup:1").kind == "walk"
    v2 = view(rows=[row("offer:1-2", 1_100_000, valid=False)], left=5, target=800_000)
    assert decide(one(v2), "sup:1") is None  # rounds left: no walk


# --- R12.12: final offer, one repair only -------------------------------------------------------------
def test_final_offer_breaking_a_must_have_gets_one_repair():
    v = view(status="final_offer", rows=[row("offer:1-3", 950_000, valid=False)])
    c = decide(one(v), "sup:1")
    assert c.kind == "repair"
    v.repair_used = True
    assert decide(one(v), "sup:1").kind == "walk"


# --- R12.11: the accept ladder ------------------------------------------------------------------------
def test_ladder_cheapest_first_with_earlier_ids_before_latest():
    a = view("sup:1", rows=[row("offer:1-1", 980_000, latest=False), row("offer:1-2", 1_000_000),
                            row("offer:1-2-a", 990_000, main=False)])  # fmt: skip
    b = view("sup:2", rows=[row("offer:2-2", 995_000), row("offer:2-1", 999_900, latest=False)])
    lad = ladder({"sup:1": a, "sup:2": b})
    assert lad[0] == ("sup:1", "offer:1-1")  # an earlier, cheaper ID first
    assert ("sup:2", "offer:2-1") not in lad  # earlier but not 0.1 % cheaper than the best latest
    assert [x[1] for x in lad[1:]] == ["offer:1-2-a", "offer:2-2", "offer:1-2"]


def test_ladder_skips_invalid_sheets():
    a = view("sup:1", rows=[row("offer:1-1", 900_000, valid=False), row("offer:1-2", 1_000_000)])
    assert ladder({"sup:1": a}) == [("sup:1", "offer:1-2")]


# --- R11.17: coverage before accepting ------------------------------------------------------------------
def test_offer_leaving_need_uncovered_never_meets_the_target():
    """P04: an offer whose uncovered need costs more than 1 % of C - one message restoring coverage."""
    v = view(rows=[row("offer:1-2", 900_000, uncovered=94_000)], target=905_000)
    c = decide(one(v), "sup:1")
    assert c.kind == "restore"
    v.restore_used = True
    assert decide(one(v), "sup:1") is None


# --- R11.18: every RFQ bidder hears from us before we close ---------------------------------------------
def test_rfq_no_accept_before_every_bidder_has_an_offer():
    a = view("sup:1", rows=[row("offer:1-1", 900_000)], target=905_000, sent=1)
    b = view("sup:2", rows=[row("offer:2-0", 950_000, reply=0)], sent=0, target=905_000)
    assert decide({"sup:1": a, "sup:2": b}, "sup:1") is None
    b.sent = 1
    assert decide({"sup:1": a, "sup:2": b}, "sup:1").kind == "accept"


def test_rfq_unchanged_opening_not_accepted_while_a_cheaper_rival_is_on_the_table():
    a = view("sup:1", rows=[row("offer:1-0", 950_000, reply=0)])
    b = view("sup:2", rows=[row("offer:2-0", 940_000, reply=0, valid=False), row("offer:2-1", 930_000)])
    lad = ladder({"sup:1": a, "sup:2": b})
    assert ("sup:1", "offer:1-0") not in lad and lad[0] == ("sup:2", "offer:2-1")


def test_rfq_final_offer_taken_only_when_best_on_the_table():
    a = view("sup:1", status="final_offer", rows=[row("offer:1-3", 960_000)])
    b = view("sup:2", rows=[row("offer:2-2", 940_000)])
    assert decide({"sup:1": a, "sup:2": b}, "sup:1").kind == "idle"
    a.rows = [row("offer:1-3", 930_000)]
    c = decide({"sup:1": a, "sup:2": b}, "sup:1")
    assert c.kind == "accept" and c.sid == "sup:1"


# --- R13.13: small gap once only price is left ----------------------------------------------------------
def test_small_gap_accepted_once_only_price_is_left():
    v = view(rows=[row("offer:1-4", 904_000)], target=900_000, close_gap=0.005)
    assert decide(one(v), "sup:1") is None
    v.only_price = True
    assert decide(one(v), "sup:1").reason == "small gap, only price left"


def test_clock_forces_the_close():
    """R11.14: closed by 420 s - the ladder, else walk."""
    assert decide(one(view(target=500_000)), "sup:1", must_close=True).kind == "accept"
    v = view(rows=[row("offer:1-1", 1_200_000, valid=False)])
    assert decide(one(v), "sup:1", must_close=True).kind == "walk"
