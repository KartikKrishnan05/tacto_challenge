"""One engine, settings per category goal (R13.10), with the anchor table of R4.1 merged in.

The merged anchor table (R13.10 asks for one). Two sources disagreed on the opening cut:
  R4.1 (BuyerAgent, data-driven)   leverage 12 %, routine 8 %, quality_first 6 %, stabilise 6 %,
                                   secure_supply 5 % off the best quote
  R13.10 (MESO, fixed shares)      leverage 0.85, routine 0.90, quality_first 0.92, stabilise the
                                   margin-optimal price, secure_supply 0.95 of the opening
Choice: the cut is the DEEPER of the two (leverage 15 %, routine 10 %, quality_first 8 %, stabilise
6 %, secure_supply 5 %). Both held up in their best runs (MESO's 0.85 for P18's 0.453, TeamAgent's
14 % + 4 for P05's 0.781), and the anchor never stands alone: R4.1 still takes the lowest of this
cut, the current price (price increases) and the fair price, and R4.2 then lifts it to the
credibility limits - at most 25 % under the supplier's price, never under its full cost x 1.01,
never under the price at which C stops falling. That last limit IS MESO's "margin-optimal price" for
stabilise (and secure_supply), so MESO's stabilise anchor is kept as a floor rather than a cut.
An RFQ adds RFQ_EXTRA (R12.6). The anchor is set once per thread (R4.4); in an RFQ it may only fall.

`horizon` is section 5's H per goal, tuned from MESO's `concede` column (R13.10 "Not taken from"):
a faster MESO pace (secure_supply 0.70) gets a shorter horizon; the others keep H = 6.
"""

# Levers to try first when the profile names none (R1.5).
TYPE_LEVERS = {
    "price_increase": ["effective_date", "payment_days"],
    "raw_material": ["index_clause", "payment_days", "contract_months"],
    "renewal": ["contract_months", "volume_per_year", "payment_days"],
    "bundling": ["contract_months", "payment_days"],
    "capacity": ["volume_per_year", "lead_time_days", "contract_months"],
    "quality": ["chargeback_eur_per_complaint", "ppm_commitment"],
    "rfq": ["payment_days", "lead_time_days", "tooling_eur"],
}
RFQ_EXTRA = 0.04  # R12.6: competition, open lower
MAX_ANCHOR_CUT = 0.25  # R4.2: never more than 25 % under the supplier's price
RFQ_HORIZON = 5  # R5.1: H = 5 in an RFQ

#   cut         share off the best quote (merged table above)
#   fair        margin on the price for the fair price (None: the case's sustainable margin)
#   horizon     section 5's H
#   first       terms settled first (R13.1), the rest by EUR weight
#   index       index clauses we offer: any, deadband (>= 3 % only), avoid
#   margin      C carries the supplier's margin charge (R2.3; the arena's rule for these goals)
#   walk        False: take the best sheet that beats the alternative rather than walk (R13.10)
#   close_gap   R13.13: accept once only price is left and the supplier is this close to our target
#   tone        for the writer
PLAYBOOK = {
    "leverage": {
        "cut": 0.15, "fair": 0.05, "horizon": 6, "first": ["payment_days", "index_clause"], "index": "any",
        "margin": False, "walk": True, "close_gap": 0.005,
        "tone": "A competitive category: argue firmly on price with market data; we have options.",
    },
    "routine": {
        "cut": 0.10, "fair": 0.06, "horizon": 6, "first": ["contract_months", "payment_days"], "index": "avoid",
        "margin": False, "walk": True, "close_gap": 0.01,
        "tone": "A routine category: keep it short and simple; we value a lean multi-year deal.",
    },
    "stabilise": {
        "cut": 0.06, "fair": None, "horizon": 6,
        "first": ["volume_per_year", "contract_months", "payment_days"], "index": "deadband",
        "margin": True, "walk": False, "close_gap": 0.005,
        "tone": "A strategic supplier: acknowledge real cost pressure and aim for a fair, stable long-term deal;"
        " do not fight a justified increase, ask for transparency instead.",
    },
    "secure_supply": {
        "cut": 0.05, "fair": None, "horizon": 5,
        "first": ["volume_per_year", "lead_time_days", "contract_months"], "index": "avoid",
        "margin": True, "walk": False, "close_gap": 0.015,
        "tone": "A bottleneck part: securing the full volume and lead time matters most; be cooperative and a"
        " reliable partner.",
    },
    "quality_first": {
        "cut": 0.08, "fair": 0.08, "horizon": 6, "first": ["ppm_commitment", "chargeback_eur_per_complaint"],
        "index": "avoid", "margin": False, "walk": True, "close_gap": 0.005,
        "tone": "A quality escalation: a binding defect commitment and chargebacks come before price; refer to the"
        " quality record.",
    },
}  # fmt: skip
DEFAULT_GOAL = "routine"  # section 0.5: an unknown goal plays the routine row

GOAL_WORDS = {
    "stabilise": "a strategic supplier: we want a healthy, reliable partner and stable prices, not the last cent",
    "secure_supply": "a bottleneck part: security of supply matters most; a supplier that is squeezed is a risk",
    "quality_first": "quality first: defects and late deliveries cost us more than a little price",
    "leverage": "leverage: competition and data should bring the price down",
    "routine": "a routine part: a fair market price with little effort",
}


def row(case):
    """The playbook row for the case's goal (routine when the goal is unknown)."""
    return PLAYBOOK.get((case.get("buyer") or {}).get("category_goal"), PLAYBOOK[DEFAULT_GOAL])


def type_levers(case):
    """The case type's levers; for an unknown type, the negotiable fields themselves (section 0.5)."""
    if case.get("type") in TYPE_LEVERS:
        return [f for f in TYPE_LEVERS[case["type"]] if f in case.get("negotiable", [])]
    return [f for f in case.get("negotiable", []) if f != "unit_price"]


def horizon(case):
    return RFQ_HORIZON if len(case.get("suppliers") or []) > 1 else row(case)["horizon"]


def anchor_cut(case):
    """The share off the best quote: the merged table, plus RFQ_EXTRA in an RFQ (R12.6)."""
    return row(case)["cut"] + (RFQ_EXTRA if len(case.get("suppliers") or []) > 1 else 0.0)
