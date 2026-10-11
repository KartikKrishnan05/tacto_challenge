"""One wording tactic per e-mail, picked by code (R11.12), ported from DualAgent's `tactics.py`.

A tactic changes the WORDS, never the sheets. Each has a style line for the model and code-written
lines for when the model is slow or absent, so it shows either way. The rules that won:
  competitive_pressure  only in an RFQ or with a real cheaper rival offer - never an invented rival
  data_argument         the supplier's own drivers explain over 1 point less than it asks, or unused
                        checked arguments remain
  lever_trade           a cash-short supplier, or one that named a term it cares about
  appreciation_close    the gap is under 1.5 %, or walk risk >= 0.4
  humor_rapport         only with a relationship-driven supplier in a friendly tone
R11.13, the tactic must agree with the stance: `firm_disappointment` only after a real hold by the
supplier and never at walk risk >= 0.4; the deadlock breaker (R5.3) has priority over the tone, so a
step message never goes out as a disappointed hold.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Tactic:
    name: str
    style: str
    opening: str
    bridge: str
    closing: str


TACTICS = {
    t.name: t
    for t in [
        Tactic(
            "competitive_pressure",
            "Make clear, calmly, that we have other qualified options and compare offers. Refer only to the rival "
            "figures given as ARGUMENTS - never invent a quote, a name or an ID. No threats.",
            "Thank you for your offer, which we have compared with the other offers we hold for this part.",
            "We are weighing several offers for this part; the options below would make yours the one we choose.",
            "We would like to work with you and look forward to an offer we can compare favourably.",
        ),
        Tactic(
            "firm_disappointment",
            "Polite but clearly disappointed: the price is not acceptable as it stands. Name what they did (no or "
            "little movement) without sarcasm, and say what would change our view.",
            "Thank you for your reply - we are disappointed that your price has hardly moved.",
            "As it stands the offer is not acceptable to us; the options below show where we need to get to.",
            "We hope you can take a real step so that we can close this together.",
        ),
        Tactic(
            "measured_lowball",
            "Present our ambitious opening confidently and matter-of-factly: it rests on our analysis. Respectful, "
            "never dismissive of their costs; no apologies for the number.",
            "Thank you for your quote and for explaining your position.",
            "We have analysed the cost situation of this part carefully; the options below reflect that analysis.",
            "We look forward to your view on these options.",
        ),
        Tactic(
            "humor_rapport",
            "Warm and personal, with ONE small, light, friendly remark about the situation (never about them or "
            "their costs). Recall the long relationship where the PROFILE supports it.",
            "Thank you for your message - it is always a pleasure to hear from you.",
            "We would rather spend our time making good pumps together than exchanging e-mails about cents, so we "
            "have tried to make the options below easy to say yes to.",
            "We look forward to your reply and to many more years of good cooperation.",
        ),
        Tactic(
            "lever_trade",
            "Offer a concrete trade: we give on a term the supplier values (payment days, volume, contract length, "
            "effective date, index clause - whatever the options vary) in exchange for price. Name which option "
            "gives them what they asked for.",
            "Thank you for your message and for telling us what matters to you.",
            "We are ready to move on the terms you care about if the price reflects it - each option below is a "
            "different trade, so you can choose what helps you most.",
            "Please tell us which option suits you best.",
        ),
        Tactic(
            "data_argument",
            "Lead with the checked data ARGUMENTS and the cost arithmetic: calm, factual, precise. Let the figures "
            "carry the argument; do not restate them yourself.",
            "Thank you for your letter and for setting out your position.",
            "The data does not support your price as it stands.",
            "We look forward to your revised offer.",
        ),
        Tactic(
            "appreciation_close",
            "Warm and partnership-focused: thank them for the steps they made, say we are close, and nudge gently "
            "toward agreement on the recommended option.",
            "Thank you for your reply and for the steps you have made - we appreciate it.",
            "We are close now; the options below are a fair way to close the remaining gap.",
            "We would be glad to finalise on this basis.",
        ),
    ]
}


# More code-written lines per tactic: a fallback e-mail never repeats a line this thread has already seen
# (P18: "The data does not support your price as it stands" three e-mails in a row; P02 sent the same
# e-mail twice with a new price).
MORE = {
    "competitive_pressure": {
        "opening": ["Thank you for your revised offer."],
        "bridge": [
            "Other offers for this part are on our table; the options below are what it takes to win this business."
        ],
        "closing": ["We would welcome an offer that lets us choose you."],
    },
    "firm_disappointment": {
        "opening": ["Thank you for your reply, which unfortunately leaves your price where it was."],
        "bridge": ["We cannot accept the price as it stands; the options below show the step we need."],
        "closing": ["A real step from your side would let us close this quickly."],
    },
    "measured_lowball": {
        "opening": ["Thank you for your reply."],
        "bridge": ["The options below set out where this part needs to be for us."],
        "closing": ["We look forward to your answer."],
    },
    "humor_rapport": {
        "opening": ["Thank you for your reply - we appreciate the good spirit of these talks."],
        "bridge": ["We would like to get this settled and back to business; the options below should make that easy."],
        "closing": ["We look forward to your reply."],
    },
    "lever_trade": {
        "opening": ["Thank you for your reply."],
        "bridge": [
            "Each option below trades a term against price, so you can pick the one that helps you most.",
            "We have built the options below around the terms that matter to you.",
        ],
        "closing": ["Which of these works best for you?", "We look forward to your choice."],
    },
    "data_argument": {
        "opening": ["Thank you for your reply.", "Thank you for your answer."],
        "bridge": ["The figures still do not support your price.", "On the figures, your price remains too high."],
        "closing": ["We look forward to your answer.", "We look forward to a step from your side."],
    },
    "appreciation_close": {
        "opening": ["Thank you for your reply - we are getting close.", "Thank you for your answer."],
        "bridge": [
            "The remaining gap is small; the options below would close it.",
            "One more step and we have an agreement.",
        ],
        "closing": ["We would be glad to finalise on this basis.", "We hope we can sign on this basis."],
    },
}


CHOICE = re.compile(r"\b(?:each option|options|either|which of these|choose|choice|pick)\b", re.I)
SINGLE = {
    "opening": "Thank you for your reply.",
    "bridge": "Our offer below sets out where we need to be.",
    "closing": "We look forward to your answer.",
}


def fresh(tactic, previous="", single=False):
    """(opening, bridge, closing) of `tactic`: per part the first line not already in our earlier e-mails
    (`previous`, their text joined); the last variant when all are used. `single`: one sheet only, so no
    line about choosing between options (P08: "each option below is a different trade" over one sheet)."""
    out = []
    for part in ("opening", "bridge", "closing"):
        lines = [getattr(tactic, part), *MORE.get(tactic.name, {}).get(part, [])]
        if single:
            lines = [x for x in lines if not CHOICE.search(x)] or [SINGLE[part]]
        out.append(next((x for x in lines if x not in previous), lines[-1]))
    return tuple(out)


def pick(intel, ctx):
    """(tactic name, reason). `intel`: the analyst's snapshot (or None); `ctx`: {first, rfq,
    rival_cheaper, cash_short, gap_pct, cost_gap_pct, data_claims, relationship, asked_for_terms, hold,
    supplier_held}."""
    scores, why = {n: 0.0 for n in TACTICS}, {n: [] for n in TACTICS}

    def add(name, points, reason):
        scores[name] += points
        why[name].append(reason)

    i = intel
    risk = i.walk_risk if i else 0.0
    gap = ctx.get("gap_pct")
    if ctx.get("first"):
        add("measured_lowball", 2.0, "first reply: we anchor")
    if (cg := ctx.get("cost_gap_pct")) is not None and cg > 1.0:
        add("data_argument", 2.5 if ctx.get("first") else 1.0, f"their own drivers explain {cg:.1f} points less")
    if ctx.get("data_claims"):
        add("data_argument", 1.0, f"{ctx['data_claims']} unused data argument(s)")
    if ctx.get("rfq") or ctx.get("rival_cheaper"):
        add("competitive_pressure", 2.0 if ctx.get("rival_cheaper") else 1.0, "a cheaper rival offer is real")
    if ctx.get("cash_short"):
        add("lever_trade", 1.5, "the supplier is short of cash")
    if ctx.get("asked_for_terms") or (i and i.lever_hints):
        add("lever_trade", 1.5, "they named a term they care about")
    if ctx.get("supplier_held") and ctx.get("hold") and not ctx.get("first"):
        add("firm_disappointment", 2.0, "their price did not move and we hold")
    if gap is not None and gap < 1.5 and not ctx.get("first"):
        add("appreciation_close", 2.5, f"gap only {gap:.1f}%")
    if risk >= 0.4:
        add("appreciation_close", 2.0, f"walk risk {risk:.2f}")
        scores["competitive_pressure"] -= 1.0
    if ctx.get("relationship") or (i and i.relationship_style == "relationship"):
        add("humor_rapport", 1.0, "relationship-driven supplier")
        if ctx.get("goal") in ("leverage", "quality_first"):
            # the case's own goal comes first: a leverage or quality case is argued firmly, not with a joke
            # (P08, leverage: two "making good pumps together" e-mails)
            scores["humor_rapport"] -= 1.5
        if i and (i.tone or 0) >= 1 and not (i.unchanged_rounds >= 1):
            add("humor_rapport", 1.0, "friendly tone")
    if i and i.tone is not None and i.tone <= -1:
        scores["humor_rapport"] = -9.0  # never with an annoyed supplier
    if not (ctx.get("rfq") or ctx.get("rival_cheaper")):
        scores["competitive_pressure"] = -9.0  # never an invented rival
    # R11.13: firm disappointment only after a real hold by the supplier, never at walk risk >= 0.4,
    # and never on a message that makes a step
    if not (ctx.get("supplier_held") and ctx.get("hold")) or risk >= 0.4:
        scores["firm_disappointment"] = -9.0
    name = max(TACTICS, key=lambda n: (scores[n], -list(TACTICS).index(n)))
    if scores[name] <= 0:
        return ("measured_lowball" if ctx.get("first") else "lever_trade"), "default"
    return name, "; ".join(why[name])
