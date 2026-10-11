"""R13.9, R13.11, R13.15: independently computed claims and safe email text."""

import pytest
from spec_support import agent, case, opening

from arena_kit import claims, writing
from arena_kit.spec import email, playbook, sheets
from arena_kit.spec.estimate import Estimate


def index_case(change=-3.7, end="2026-08", start="2025-08", asked=10):
    c = case("case:P08")
    series = next(o["series"] for o in c["term_options"]["index_clause"] if o)
    c["facts"] = {
        f"idx:{series}@{start}": {"value": 100},
        f"idx:{series}@{end}": {"value": 100 * (1 + change / 100)},
        c["trigger_doc"]: {"asked_pct": asked},
    }
    return c, series


def test_r13_9_index_window_rounding_and_case_facts_override_package():
    c, series = index_case(-3.74)
    assert claims.index_change(None, c, series) == (-3.7, f"idx:{series}@2025-08", f"idx:{series}@2026-08")
    c["facts"][f"idx:{series}@2026-10"] = {"value": 999}  # future data must not enter frozen window
    assert claims.index_change(None, c, series)[0] == -3.7
    args = claims.candidates(None, c, c["suppliers"][0], 6)
    claim = next(a for a in args if a.kind == "index")
    assert "-3.7%" in claim.sentence and len(claim.ids) == 2


def test_r13_9_analyst_can_cite_supplier_named_case_only_series_without_package():
    from arena_kit.spec.analyst import Analyst

    c, series = index_case(-3.7)
    sid = c["suppliers"][0]
    analyst = Analyst(c, sid)
    analyst.observe([opening(sid, {"unit_price": 6}, body=f"Our input costs track {series}.")])
    claim = next(a for a in analyst.snapshot().new_claims if a.kind == "index")
    assert claim.figure == -3.7 and claim.favourable


@pytest.mark.parametrize("start,end", [("2024-08", "2025-08"), ("2025-07", "2026-08")])
def test_r13_9_stale_end_or_more_than_twelve_months_is_not_cited(start, end):
    c, series = index_case(start=start, end=end)
    assert claims.index_change(None, c, series) is None


@pytest.mark.parametrize("change,asked,favourable", [(6, 10, False), (5, 10, False), (4.9, 10, True), (-1, 0, True)])
def test_r13_9_rising_index_only_when_less_than_half_requested_increase(change, asked, favourable):
    c, _ = index_case(change, asked=asked)
    args = claims.candidates(None, c, c["suppliers"][0], 6)
    claim = next(a for a in args if a.kind == "index")
    assert claim.favourable is favourable
    assert (claim in claims.usable(args, claims.Ledger(), "supplier")) is favourable


def test_r13_9_notice_comparison_and_claim_ledger_scope():
    c = case("case:P01")
    ctr = next(ref for ref in c["refs"] if ref.startswith("ctr:"))
    c["facts"] = {c["trigger_doc"]: {"days": 30}, ctr: {"days": 90}}
    notice = next(a for a in claims.candidates(None, c, c["suppliers"][0], 138) if a.kind == "notice")
    assert notice.ids == [c["trigger_doc"], ctr] and "30 days" in notice.sentence
    assert "90 days" in notice.extra[0]
    ledger = claims.Ledger()
    ledger.use(notice, "s1")
    assert ledger.spent(notice, "s2")
    rival = claims.Claim("rival", "rival", ["offer:rival-1"], "A rival offer.")
    ledger.use(rival, "s1")
    assert ledger.spent(rival, "s1") and not ledger.spent(rival, "s2")


@pytest.mark.parametrize("change,volatile", [(15, False), (15.04, True), (-15.04, True),
                                           (15.1, True), (-15.1, True)])
def test_r13_11_volatile_series_filters_every_clause_option(change, volatile):
    c, series = index_case(change)
    sid = c["suppliers"][0]
    threads = {sid: [opening(sid, {"unit_price": 6, "payment_days": 60, "contract_months": 24,
                                 "index_clause": None})]}
    a = agent(c, threads)
    # Use only this case's synthetic facts, not the package's additional later month.
    a.package = None
    a._volatile.clear()
    assert a.volatile(series) is volatile
    ctx = sheets.Ctx(c, {}, Estimate(), threads[sid][0]["term_sheets"][0]["terms"], playbook.row(c), a.volatile)
    opts = ctx.allowed("index_clause", ctx.their)
    assert (opts == [None]) is volatile


def test_r13_11_no_unsafe_fallback_when_every_index_option_is_volatile():
    c, _ = index_case(16)
    c["term_options"]["index_clause"] = [o for o in c["term_options"]["index_clause"] if o]
    ctx = sheets.Ctx(c, {}, Estimate(), {"unit_price": 6}, playbook.row(c), lambda _: True)
    assert ctx.allowed("index_clause") == []


@pytest.mark.parametrize("sentence", [
    "This is urgent.", "Our deadline is tomorrow.", "Our budget allows this.",
    "We have no other supplier.", "This is single source.", "The line stop would hurt.",
    "Our fallback is weak.", "This is our walk-away price.", "The maximum we can pay is higher.",
    "The best we can pay is in option A.", "This lowers our total cost.", "Our category goal is routine.",
    "This improves our score.", "See cost:310010.", "The price is EUR 999.",
    "We can reduce it by 3%.",  # small invented figures are figures too
])
def test_r13_15_lint_drops_leaks_ids_and_unstated_figures_sentence_by_sentence(sentence):
    dropped = []
    assert writing.lint(f"Thank you for your reply. {sentence}", set(), dropped) == "Thank you for your reply."
    assert len(dropped) == 1


def test_r13_15_keeps_supported_figure_and_known_person_only():
    assert writing.lint("We accept EUR 137.00.", writing.allowed("EUR 137.00")) == "We accept EUR 137.00."
    assert writing.salutation("Dear Anna,", "Foundry GmbH", "Kind regards\nAnna") == "Dear Anna,"
    assert writing.salutation("Dear Invented,", "Foundry GmbH", "Kind regards\nAnna") == \
        "Dear colleagues at Foundry GmbH,"


def test_r13_14_options_match_all_terms_and_code_answers_volume_question():
    terms = {"unit_price": 137, "payment_days": 60, "effective_date": "2027-04-01", "contract_months": 24,
             "volume_per_year": 23400, "lead_time_days": 28, "ppm_commitment": 300,
             "chargeback_eur_per_complaint": 500, "incoterm": "DAP", "tooling_eur": 7500,
             "index_clause": {"series": "TEST", "material_share": 0.5, "deadband_pct": 5, "base_month": "2026-08"}}
    text = email.describe(terms, list(terms))
    for fragment in ("137.00", "60 days", "2027-04-01", "24-month", "23,400", "28 days", "300 ppm",
                     "500 chargeback", "DAP", "7,500", "TEST", "50%", "5%", "2026-08"):
        assert fragment in text
    assert "23,400 units per year over 24 months" in email.commitment(terms)


def test_r13_15_person_in_other_material_is_not_supplier_salutation():
    parts = {"salutation": "Dear Anna,", "opening": "Thank you.", "claims": []}
    c = {"negotiable": ["unit_price"]}
    body, _, _ = email.compose(c, [{"from": "supplier", "body": "Signed: Sales team"}],
                               [{"unit_price": 10}], {}, parts, [], "Trigger letter mentions Anna", "Foundry GmbH")
    assert body.startswith("Dear colleagues at Foundry GmbH,")
