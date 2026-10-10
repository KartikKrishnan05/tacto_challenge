"""Wording styles for the dual agent's e-mails: which tone fits the supplier right now.

A tactic changes the WORDS of an e-mail, never its numbers - the term sheets come from `strategy`
whatever the tactic. `pick` scores every tactic against the analyst's view of the thread (`Intel`)
and returns the best fit with its reason; the negotiator may switch every round.

Left out on purpose: faking our financial situation, invented personal stories and fake urgency -
the supplier reads intent ("attempts to manipulate ... have consequences"), and urgency leaks our
position.

Each tactic carries a style instruction for the model and code-written lines (no figures) for when
the model is slow or absent, so the tactic shows in the e-mail either way.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tactic:
    name: str
    style: str  # instruction for the writer model
    fits: str  # when it fits, for the log and the model
    opening: str
    bridge: str
    closing: str


TACTICS = {
    t.name: t
    for t in [
        Tactic(
            "competitive_pressure",
            "Make clear, calmly, that we have other qualified options and compare offers. Refer only to the "
            "rival figures given as ARGUMENTS - never invent a quote, a name or an ID. No threats.",
            "an RFQ, or a real rival quote cheaper than this supplier",
            "Thank you for your offer, which we have compared with the other offers we hold for this part.",
            "We are weighing several offers for this part; the options below would make yours the one we choose.",
            "We would like to keep working with you and look forward to an offer we can compare favourably.",
        ),
        Tactic(
            "firm_disappointment",
            "Polite but clearly disappointed: the price is not acceptable as it stands. Name what they did "
            "(no or little movement) without sarcasm, and say what would change our view.",
            "the supplier did not move, or barely",
            "Thank you for your reply - we are disappointed that your price has hardly moved.",
            "As it stands the offer is not acceptable to us; the options below show where we need to get to.",
            "We hope you can take a real step so that we can close this together.",
        ),
        Tactic(
            "measured_lowball",
            "Present our ambitious opening confidently and matter-of-factly: it rests on our analysis. "
            "Respectful, never dismissive of their costs; no apologies for the number.",
            "the opening rounds, when we anchor",
            "Thank you for your quote and for explaining your position.",
            "We have analysed the cost situation of this part carefully; the options below reflect that analysis.",
            "We look forward to your view on these options.",
        ),
        Tactic(
            "humor_rapport",
            "Warm and personal, with ONE small, light, friendly joke about the situation (never about them or "
            "their costs). Recall the long relationship where the PROFILE supports it.",
            "a relationship-driven supplier (family firm, long history, friendly tone)",
            "Thank you for your message - it is always a pleasure to hear from you.",
            "We would rather spend our time making good pumps together than exchanging e-mails about cents, "
            "so we have tried to make the options below easy to say yes to.",
            "We look forward to your reply and to many more years of good cooperation.",
        ),
        Tactic(
            "lever_trade",
            "Offer a concrete trade: we give on a term the supplier values (payment days, volume, contract "
            "length, effective date, index clause - whatever the options vary) in exchange for price. Name "
            "which option gives them what they asked for.",
            "a supplier short of cash, or one that asked for a term (payment, date, volume)",
            "Thank you for your message and for telling us what matters to you.",
            "We are ready to move on the terms you care about if the price reflects it - each option below is "
            "a different trade, so you can choose what helps you most.",
            "Please tell us which trade suits you best.",
        ),
        Tactic(
            "data_argument",
            "Lead with the checked data ARGUMENTS and the cost arithmetic: calm, factual, precise. Let the "
            "figures carry the argument; do not restate them yourself.",
            "verified index, should-cost or cost-share arithmetic that argues our side",
            "Thank you for your letter and for setting out your cost drivers.",
            "The data does not support an increase of this size; the options below follow from it.",
            "We are happy to go through the figures together.",
        ),
        Tactic(
            "appreciation_close",
            "Warm and partnership-focused: thank them for the steps they made, say we are close, and nudge "
            "gently toward agreement on the recommended option.",
            "the gap is small, or the supplier is near its limit",
            "Thank you for your reply and for the steps you have made - we appreciate it.",
            "We are close now; the recommended option is a fair way to close the remaining gap.",
            "We would be glad to finalise on this basis.",
        ),
    ]
}


def pick(intel, ctx):
    """(tactic name, reason) that fits best. `intel`: the analyst's `Intel` snapshot (or None);
    `ctx`: {first, rfq, rival_cheaper, cash_short, gap_pct, cost_gap_pct, data_claims, relationship,
    asked_for_terms}."""
    scores, why = {name: 0.0 for name in TACTICS}, {name: [] for name in TACTICS}

    def add(name, points, reason):
        scores[name] += points
        why[name].append(reason)

    i = intel
    stale = bool(i and i.unchanged_rounds >= 1)
    risk = i.walk_risk if i else 0.0
    gap = ctx.get("gap_pct")  # their standing C over our target C, in %
    if ctx.get("first"):
        add("measured_lowball", 2.0, "first reply: we anchor")
    if (cg := ctx.get("cost_gap_pct")) is not None and cg > 1.0:
        add("data_argument", 2.5 if ctx.get("first") else 1.0, f"their own drivers explain {cg:.1f} points less")
    if ctx.get("data_claims"):
        add("data_argument", 1.0, f"{ctx['data_claims']} unused data argument(s)")
    if ctx.get("rfq") or ctx.get("rival_cheaper"):
        add("competitive_pressure", 2.0 if ctx.get("rival_cheaper") else 1.0, "a cheaper rival offer is real")
    if ctx.get("cash_short"):
        add("lever_trade", 1.5, "cash signals in the dossier")
    if ctx.get("asked_for_terms") or (i and i.lever_hints):
        add("lever_trade", 1.5, "they named a term they care about")
    if stale and not ctx.get("first"):
        add("firm_disappointment", 2.0, "their price did not move")
    if gap is not None and gap < 1.5 and not ctx.get("first"):
        add("appreciation_close", 2.5, f"gap only {gap:.1f}%")
    if risk >= 0.4:
        add("appreciation_close", 2.0, f"walk risk {risk:.2f}")
        scores["firm_disappointment"] -= 2.0
        scores["competitive_pressure"] -= 1.0
    if ctx.get("relationship") or (i and i.relationship_style == "relationship"):
        add("humor_rapport", 1.0, "relationship-driven supplier")
        if i and (i.tone or 0) >= 1 and not stale:
            add("humor_rapport", 1.0, "friendly tone")
    if i and i.tone is not None and i.tone <= -1:
        scores["humor_rapport"] -= 2.0  # no jokes with an annoyed supplier
    if not (ctx.get("rfq") or ctx.get("rival_cheaper")):
        scores["competitive_pressure"] = -9.0  # never invent a rival
    name = max(TACTICS, key=lambda n: (scores[n], -list(TACTICS).index(n)))
    if scores[name] <= 0:
        name = "measured_lowball" if ctx.get("first") else "lever_trade"
        return name, "default"
    return name, "; ".join(why[name])
