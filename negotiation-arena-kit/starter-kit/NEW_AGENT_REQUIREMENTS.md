# New buyer agent - requirements

What the next agent must do, taken from the three agents on `feature/team-agent` that hold the best
practice scores: `BuyerAgent` (`arena_kit/agents/buyer.py`, `arena_kit/strategy.py`,
`arena_kit/dossier.py`) for sections 1-10, `DualAgent` (`arena_kit/agents/dual.py`,
`arena_kit/intel.py`, `arena_kit/tactics.py`) for section 11, and `TeamAgent` (`team/agent.py`,
`team/levers.py`, `team/briefing.py`, `team/runner.py`, `team/final.py`; findings in `AGENTS.md`) for
section 12. Section 13 takes the MESO agent on this branch (`feature/meso-agent`:
`arena_kit/agents/llm.py`, `arena_kit/claims.py`, `arena_kit/writing.py`, `arena_kit/play.py`; run
logs in `runs/meso*/`). Each requirement is something that held up in the practice arena. Source
files in sections 1-12 refer to `feature/team-agent`.

## 0. What the agent is given: the case JSON

Everything the agent decides is computed from the case file (`POST /v1/sessions` → `case`, or
`GET /v1/cases?set=…`; the 19 practice files are in `data/practice_cases.json`) plus the suppliers'
opening quotes. Read every value from the file - never hard-code one - but expect the shapes and
ranges below: they hold in all 19 practice cases, and anything outside them is a signal to log and
fall back to defaults, not to crash. Source: `brief/api.md` (Case file, Terms, Cost and score).

### 0.1 A real case file (`case:P01`)

```json
{
  "case_id": "case:P01", "set": "practice", "type": "price_increase", "hosted": true,
  "title": "Brandlhuber announces +9.5 % on HN volute casings",
  "briefing": "You negotiate for Hollerau Pumpenwerke with Gießerei Brandlhuber GmbH & Co. KG. ... Need: 8150 pc a year. Category goal: stabilise a strategic supply ... Fallback if you walk away: the second-source quote quote:7200001. Must-haves: none. Prepare from the data package.",
  "trigger_doc": "doc:20260911-70110-P01",
  "parts": ["part:310010"],
  "suppliers": ["sup:70110"],
  "negotiable": ["unit_price", "effective_date", "payment_days"],
  "fixed_terms": {"lead_time_days": 42, "volume_per_year": 8150, "contract_months": 12,
                  "index_clause": null, "incoterm": "FCA", "tooling_eur": 0},
  "term_options": {"effective_date": ["2026-11-01", "2027-01-01", "2027-04-01"],
                   "payment_days": [14, 30, 45, 60, 90]},
  "buyer": {
    "need_per_year": 8150, "horizon_years": 1.0,
    "current_unit_price": 126.53, "fallback_unit_price": 136.65,
    "interest_rate": 0.08, "reference_payment_days": 60,
    "need_lead_time_days": 45, "late_cost_per_day": 420, "cost_per_defect": 216,
    "category_goal": "stabilise",
    "fallback_risk_pct": 10, "index_risk_pct": 4,
    "sustainable_margin_pct": 3, "relationship_eur_per_point": 15470,
    "freight_eur_per_unit": {"EXW": 2.19, "FCA": 1.9, "DAP": 0.0},
    "index_paths": {},
    "must_haves": [],
    "alternative": {"kind": "quote", "ref": "quote:7200001",
                    "terms": {"unit_price": 143.22, "payment_days": 60, "lead_time_days": 70,
                              "volume_per_year": 8150, "contract_months": 12,
                              "effective_date": "2027-01-01", "index_clause": null,
                              "incoterm": "FCA", "tooling_eur": 1500, "ppm_commitment": 2376}}
  },
  "round_limit": 10,
  "refs": ["doc:20260911-70110-P01", "ctr:4600070110", "sc:70110@2026Q3", "quote:7200001"]
}
```

### 0.2 Top-level fields: what to expect, what uses it

| Field | Expect | Used by |
|---|---|---|
| `case_id`, `set` | `case:P01`…`P19` (practice), `case:D01`/`D02` (sandbox), hidden IDs in `final` | cache keys (R13.17), logs |
| `hosted` | practice only; `true` on the 10 playable cases (P01-P05, P07, P08, P10, P11, P18) | - |
| `type` | `price_increase`, `renewal`, `rfq`, `bundling`, `raw_material`, `capacity`, `quality` | `TYPE_LEVERS` (R1.5), case questions (R9.4), anchor (R4.1, R13.12) |
| `title`, `briefing` | prose; repeats need, goal, need lead time, fallback and must-haves in words. The structured `buyer` block is the source of truth - never parse numbers from the briefing | profile prompt (R1.3), writer context (R9.2) |
| `trigger_doc` | a `doc:` ID (the supplier's letter / RFQ document) | data arguments - notice vs contract (R13.9) |
| `parts` | 1 part, except bundling (P07: 4, P15: 3) - still **one** `unit_price` per sheet | dossier (R1.1), should-cost (R2.1) |
| `suppliers` | 1 supplier; 2-3 = an RFQ (P05: 3, P06: 2; sandbox D02: 2) | RFQ handling (sections 8, 12.6) |
| `negotiable` | always contains `unit_price`, plus 2-3 of the fields below. A term sheet must carry **exactly** these fields or it is a `422` | sheets (R6, R11.11), lever table (R12.3) |
| `fixed_terms` | the case's other fields with their values (fields named in neither list - e.g. `ppm_commitment` outside quality cases - take the supplier's standard terms) | C computation, `describe` (R13.14) |
| `term_options` | allowed values per negotiable field except `unit_price` - send nothing else | lever table (R12.3), steps (R13.1), repair (R11.9) |
| `round_limit` | 10 in every case so far - read it, do not assume it | horizon H (R5.1), close rules (R7.5-R7.6) |
| `refs` | 3-5 starting IDs (`doc`, `ctr`, `sc`, `quote`); "a start, not the whole story" | dossier (R1.1), arguments (R9.5) |
| `facts` | **hidden cases only**: case-specific facts in `facts.json` format, citable in that case | merge into the claim data before computing arguments (R13.9) |
| `wave`, `theme`, `open`, `seconds_left` | **final only**; `seconds_left` until the 8-minute wave cap | case clock (R11.14-R11.16) - read `seconds_left`, don't assume 480 |

### 0.3 Negotiable fields and their options (all practice cases)

| Field | Options seen | Notes |
|---|---|---|
| `unit_price` | free, EUR with 2 decimals | the only free field |
| `payment_days` | always `[14, 30, 45, 60, 90]` | reference is 60; fixed at 30-60 where not negotiable |
| `contract_months` | always `[12, 24, 36]` | with `horizon_years` = 2, 12 months leaves half the need uncovered (R12.2) |
| `volume_per_year` | three steps around the need, e.g. `[16379, 19890, 23400]` (≈ 70 / 85 / 100 % or 80 / 100 / 120 %) | where fixed it equals `need_per_year` |
| `effective_date` | `["2026-11-01", "2027-01-01", "2027-04-01"]` (price_increase) | the horizon starts at the earliest option; later = more units at `current_unit_price` |
| `lead_time_days` | 3-5 values, e.g. `[28, 35, 42, 56]` | compare with `need_lead_time_days`; often a must-have |
| `index_clause` | `null` + one series at deadband 0 / 3 / 5 %, fixed `material_share` (0.2-0.8) and `base_month` `"2026-08"` | the clause object must match an option exactly |
| `ppm_commitment` | `[300, 800, 1500, 2500]` (quality) | priced with `cost_per_defect` and the chargeback |
| `chargeback_eur_per_complaint` | `[0, 150, 300, 500]` (quality) | capped at `cost_per_defect` in C |
| `tooling_eur` | `[0, 7500, 15000]` (P06) | one-off, added to C |
| `incoterm` | `EXW` / `FCA` / `DAP`; fixed in every practice case | freight from `freight_eur_per_unit`; DAP is always 0.0 |

### 0.4 The `buyer` block: what to expect

| Field | Range seen | Pattern to rely on |
|---|---|---|
| `need_per_year` | 3,420 - 94,800 | units; with `horizon_years` the total need = N × H |
| `horizon_years` | `1` / `1.0` or `2` / `2.0` (API allows ≤ 3) | int **or** float - normalise; 1-year cases have `contract_months` fixed at 12 |
| `current_unit_price` | 4.20 - 412.74 | equals the fallback price in an RFQ (new part) |
| `fallback_unit_price` | 1.00 - 1.25 × current | 1.25 × in the capacity cases: walking is expensive |
| `interest_rate` | always 0.08 | our cost of money (R2.2) |
| `reference_payment_days` | always 60 | paying earlier than 60 days costs us interest |
| `need_lead_time_days`, `late_cost_per_day` | 5-59 days; EUR 40-2,400/day | capacity cases carry the high late cost |
| `cost_per_defect` | 0 - 780 | 0 in raw_material cases: ppm is worthless there |
| `category_goal` | `stabilise`, `secure_supply`, `quality_first`, `leverage`, `routine` | selects the playbook row (R13.10) and the anchor cut (R4.1) |
| `fallback_risk_pct` | follows the goal: leverage 3, routine 5, quality_first 8, stabilise 10, secure_supply 25 | uncovered need is the costliest mistake in secure_supply |
| `index_risk_pct` | follows the goal: leverage 1, routine / quality_first 2, secure_supply 3, stabilise 4 | a clause costs us this share of indexed spend on top of the path |
| `sustainable_margin_pct`, `relationship_eur_per_point` | 0.5-10 %; EUR 5,000-31,920 per point-year - **only** for stabilise and secure_supply, `null` otherwise | the margin charge (R2.3, R13.8); treat `null` as "no charge" |
| `freight_eur_per_unit` | `{EXW, FCA, DAP}`, DAP always 0.0 | per committed unit |
| `index_paths` | `{}` or one series: `AS-14404` +0.06, `CU-NOTATION` +0.05, `PPI-C241` +0.03 | the expected move of the clause's series; a series missing here has path 0 |
| `must_haves` | `[]` or one rule: `lead_time_days <= 42` (P05, P09), `<= 84` (P06), `volume_per_year >= 22230` (P10), `ppm_commitment <= 1500` (P11) | a deal that breaks one scores **0** - check every sheet and every accept against them (R6.7, R11.9) |
| `alternative` | `kind` = `quote` (`ref` = `quote:72000NN`, full `terms`), `current_contract` (`ref` = a `ctr:` ID **or `null`**, full `terms`), or `none` (`ref` null, `terms` `{}`) | C_alt = C(alternative terms); with `none`, no deal = uncovered need at the fallback price, score 0 |

### 0.5 Case types as they appear

| Type | Goals seen | Negotiable besides price | Typical must-have / alternative |
|---|---|---|---|
| `price_increase` | stabilise, leverage | effective_date, payment_days, index_clause or volume | quote or none |
| `renewal` | stabilise, routine | payment_days, contract_months, volume, index_clause | quote or none; H = 2 |
| `rfq` | leverage, stabilise | payment_days, lead_time_days, tooling_eur | lead-time must-have; `alternative: none` |
| `bundling` | routine | payment_days, contract_months | `current_contract`; 3-4 parts, one price |
| `raw_material` | leverage | index_clause, payment_days, contract_months | quote; `index_paths` set; `cost_per_defect` 0 |
| `capacity` | secure_supply | volume_per_year, lead_time_days, contract_months / payment_days | volume or lead-time must-have; fallback 1.25 × |
| `quality` | quality_first | ppm_commitment, chargeback, (lead_time_days) | ppm must-have or `current_contract` |

A hidden case may combine these differently or bring a new type: take the levers from `negotiable`
when `type` is unknown, and the `routine` playbook row when `category_goal` is unknown.

### 0.6 The opening quotes and the replies

`POST /v1/sessions` also returns `opening`, one entry per supplier:

```json
"sup:70240": {"offer_id": "offer:70240-0", "round": 0, "status": "open",
              "terms": {"unit_price": 31.5, "lead_time_days": 52},
              "alternatives": [],
              "missing_fields": ["payment_days"],
              "claims": [], "claim_results": [],
              "signals": {"reply_delay_days": 4, "validity_days": 30},
              "text": "Hello, Thank you for your enquiry. ..."}
```

- **`terms` can be partial.** Fields in `missing_fields` close on the supplier's **standard terms**
  (in the quote PDF) - fill them from the PDF before computing C; never assume our reference values.
- **An opening quote can break a must-have** (P05: 52 days against `<= 42`) - repair it (R11.9).
- `alternatives`: `[{offer_id, terms}]` - extra packages, acceptable like the main sheet (R12.11).
- Replies (`POST …/messages`) carry `status` (`open`, `final_offer`, `agreed`, `walked_away`,
  `no_bid`), `term_sheets` (its standing offer, `offer:` ID), `claim_results` per argument,
  `signals`, `missing_fields` and `attachments` (cost breakdown XLSX, 8D PDF, delivery schedule).

### 0.7 What to compute from the case JSON before the first message

1. Validate: every `negotiable` field except `unit_price` has `term_options`; no field is both
   fixed and negotiable; must-haves name real fields.
2. C(terms) with `arena_kit.total_cost`, the opening C of every supplier (standard terms filled),
   and C_alt from `buyer.alternative`.
3. Coverage: does `contract_months` × `volume_per_year` cover `need_per_year` × `horizon_years`?
   If an option closes the gap and lowers C, it goes into message 1 (R12.1-R12.2).
4. The lever table (R12.3) over `term_options`, the playbook row for `category_goal` (R13.10), the
   anchor (R4, R13.12) and the floor (R2.1, R6.5).
5. The must-have filter every sheet and every accept passes through.
6. The clock: `round_limit` and, in the final, `seconds_left`.

## Evidence

From `knowledge/sessions.jsonl` on `feature/team-agent` (practice sessions, 10 Oct 2026).

### Where BuyerAgent was better

| Case | Type / goal | BuyerAgent | DualAgent (best of its runs) | Why BuyerAgent did as well or better |
|---|---|---|---|---|
| P11 | quality / quality_first | **0.589** | 0.536 (3 runs) | Same price (EUR 46.00), quality cost EUR 5,724 vs 10,732; it kept going (7 e-mails) where DualAgent closed on "deadlock" after 5 |
| P18 | raw_material / leverage | **0.419** | 0.319 (3 runs) | Price EUR 260,700 vs 265,440; index clause EUR 1,173 vs 7,167; supplier conceded 9.8 % vs 8.2 % and answered "counter" in rounds 1-3 (DualAgent drew "hold" in rounds 2-3) |
| P01 | price_increase / stabilise | 0.931 | 0.941 | Within 0.01; no early-payment cost in its last two runs (DualAgent paid ~EUR 7.2k in 4 of 5 runs) |
| P04, P10 | - | 0.860, 0.655 | 0.860, 0.655 | Tie |

### Where DualAgent was better

| Case | Type / goal | DualAgent | BuyerAgent | Why DualAgent did better |
|---|---|---|---|---|
| P01 | price_increase / stabilise | **0.941** (best of all runs) | 0.931 | Data argument twice, rapport, an appreciative close; deal at 135.20 with the price from April 2027 |
| P02 | price_increase / stabilise | **0.385** | 0.211 | Kept the supplier healthy: relationship charge EUR 92 vs **124,110**, no index risk (BuyerAgent: EUR 17,158). BuyerAgent's lower price (351 vs 373) cost far more than it saved |
| P03 | renewal / stabilise | **0.919** (same in 3 runs) | 0.900 | Lower total spend (EUR 3,959,776 vs 3,967,013) in 4 e-mails instead of 6 |
| P05 | rfq / leverage | **0.444** | 0.372 | Talked to all three bidders (2-3 e-mails each), cited the cheaper rival, closed with the cheapest bidder (70240) - BuyerAgent wrote only once to it and closed with 70210 |
| P07 | bundling / routine | **0.721** | 0.702 | EUR 5.45 vs 5.48 after 5 e-mails, appreciative close |
| P08 | raw_material / leverage | **0.343** | 0.332 | EUR 5.61 vs 5.67 |

The runs were hours apart (BuyerAgent 18:4x, DualAgent 21:xx) and the shared `strategy.py` changed
in between, so part of a gap can come from the code version, not the agent design. Section 11 takes
only what is DualAgent's own.

### Where TeamAgent was better

TeamAgent's runs are not in `sessions.jsonl` (it logs through `team.evaluate`). These best runs
(`runs/best_runs.json`, `runs/best_replays/`) are its own by their e-mails: the signature
"Hollerau Pumpenwerke, Purchasing" from its prompt, its code-written planned-move text word for word
(P10), and run times matching its commits (v4-v9, 10 Oct 18:00-01:30). P07 is the least certain.

| Case | Type / goal | TeamAgent | DualAgent | BuyerAgent | What won it |
|---|---|---|---|---|---|
| P05 | rfq / leverage | **0.781** | 0.444 | 0.372 | All three bidders in message 1; each told the cheapest rival's offer ID and price ("we did not know that - a fair point"); lead time repaired to the must-have; payment probed with 90/60/30 sheets; two bidders `agreed` in round 2, it took the cheaper (EUR 28.42 vs 30.39) |
| P08 | raw_material / leverage | **0.549** | 0.343 | 0.332 | 24-month cover of the 2-year need from message 1 (the quote was 12 months); answered "we cannot fix the price for two years" with a 5 % deadband index clause instead of 0 %; accepted the final offer |
| P07 | bundling / routine | **0.773** | 0.721 | 0.702 | 24- and 36-month packages from message 1, payment 90 days from the contract (ctr:4600070610); a step every round; accepted the final offer at EUR 5.37 |
| P10 | capacity / secure_supply | **0.671** | 0.655 | 0.655 | Full need (23,400/year) for 24 months in message 1, with a lead-time trade; accepted the final offer |
| P04 | renewal / routine | 0.860 | 0.860 | 0.860 | Tie: full requirement, 24 months, 90 days in message 1 |

The team's practice average (mean of 10 cases × 100, 8-minute cap): v2 13.3 (pushed past final
offers, walk-aways) → v4 56.3 (accept final offers) → **v9 56.9 with all 10 deals**.

### Where the MESO agent was better

From `runs/meso3/` (11 Oct 02:52) and `runs/meso4/` (11 Oct 03:25) on `feature/meso-agent`:

| Case | Type / goal | MESO | Best before | What won it |
|---|---|---|---|---|
| P11 | quality / quality_first | **0.710** (meso3 and meso4) | 0.672 | Step 1 offered 300 / 800 / 1,500 ppm at equal cost; the supplier took **300 ppm** (the earlier best deal had 1,500 ppm). Step 2 settled the chargeback, then 3 price rounds. Same deal in both runs |
| P18 | raw_material / leverage | **0.453** (meso3) | 0.419 | Step 1: payment 90 / 30 / 45 days at equal cost → 90 locked; step 2: index clause options → locked; then one price round and the final offer at EUR 5.40 |
| P03 | renewal / stabilise | 0.919 (meso3) | 0.919 | Tie with DualAgent's best |

### MESO failures not to repeat

- **P10, 0.0 (meso4):** no coverage move in message 1; the final offer kept 19,890/year for 12
  months, nothing was acceptable, it walked. TeamAgent's 0.671 put the full need for 24 months into
  message 1 (R12.2).
- **P04, 0.056 (meso4):** accepted a 12-month final offer on a 2-year need - the same failure as
  DualAgent's P04 (R11.17, R12.2).
- **P05, 0.0 (meso3):** accepted the best final offer 4.5 minutes after its thread's last move,
  while other threads were still talking; the arena answered `409 Conflict` and the case ended
  without a deal (R13.16).
- **P01, 0.159 (meso4):** every "step" carried a single package (the others fell under the floor),
  so the supplier had no choice to reveal anything; it kept the earliest effective date, a term worth
  EUR 39k, and the deal closed at EUR 137.90 (best runs: 135.20 with the price from April 2027)
  (R13.4).

### DualAgent failures not to repeat

- **P04, 0.056:** accepted a final offer that covered only half the need (EUR 94,000 uncovered need +
  4,700 fallback risk). Its other two P04 runs scored 0.86.
- **P05, 0.0:** one e-mail to one bidder, then accepted 70210 at its opening EUR 36.00 ("their offer
  meets our target"); the two other bidders were never written to.
- **P11 and P18:** P11 closed itself on "deadlock" with terms still open; on P18 it held twice, the
  supplier held back, and the final offer came after 4 e-mails (see the BuyerAgent table above).

## 1. Prepare before the clock

- **R1.1 Supplier dossier.** Before the session's first move, gather for every supplier everything
  in the data package: call and meeting notes, letters, past negotiations (asked vs settled), its
  financials, our payment record, scorecards, should-cost, the part's cost drivers and contract
  clauses (`dossier.gather`, Sunday release included). Cache it per supplier and parts.
- **R1.2 Cash signals.** Scan the dossier for cash-pressure words (cash, liquidity, skonto, prepay,
  credit line, working capital, insolvency, short-time work, "within N days") and record the hits.
- **R1.3 One profile call per supplier, in parallel.** A model reads the dossier plus the computed
  data arguments and returns JSON only:
  `persona, tone, pressures[], wants[], levers[], avoid[], cash_short, claim_ranking[], questions[], angle`.
  Timeout 90 s for all profiles together.
- **R1.4 Validate the profile.** `levers` are mapped onto the case's negotiable field names and
  `claim_ranking` onto real argument keys; anything that does not match is dropped. `cash_short` is
  true if the model says so, or if the dossier has cash signals and payment_days is the top lever.
- **R1.5 Default profile.** If the call fails or times out, use a default profile: polite, factual,
  constructive tone; levers = payment_days first when cash signals exist, then the case type's levers
  (`TYPE_LEVERS` below). A missing profile never costs the case.

`TYPE_LEVERS`: price_increase → effective_date, payment_days · raw_material → index_clause,
payment_days, contract_months · renewal → contract_months, volume_per_year, payment_days · bundling →
contract_months, payment_days · capacity → volume_per_year, lead_time_days, contract_months ·
quality → chargeback_eur_per_complaint, ppm_commitment · rfq → payment_days, lead_time_days, tooling_eur.

## 2. Our estimate of the supplier

- **R2.1 Full cost, calibrated.** In this order: the supplier's own cost-breakdown XLSX (sum of EUR
  elements, without margin and totals); else should-cost / 1.02 (fits the arena's relationship charge
  over 31 recorded deals - `FULL_COST_K` in `team/agent.py` and the current MESO agent); else opening
  price / 1.10. Not too high: a high floor made BuyerAgent stop pushing (P01, 10 Oct: accepted 136.30
  where 133.70 was reachable). Not too low either: with its "low end of the confidence band"
  estimate BuyerAgent saw no cost in squeezing P02's supplier to EUR 351 - the arena charged EUR
  124,110 for it (score 0.211; DualAgent 0.385 at EUR 373).
- **R2.2 Cost of money.** The buyer's interest rate, plus 6 points for a supplier short of cash.
  Once the supplier has priced payment days itself (two of its sheets differ only in payment), use
  that revealed rate instead (slope × 360, clamped to 3-40 %) - see R12.4.
- **R2.3 Margin charge only where the goal has one.** C includes the supplier's margin penalty only
  for stabilise and secure_supply.

## 3. What we negotiate on

- **R3.1 Best sheet on the table.** For each thread, take the supplier's cheapest offer for us among
  its standing sheet *and* its alternatives, preferring ones that keep the must-haves.
- **R3.2 Both-sides-gain structure (`mutual_combo`).** Move each of the supplier's non-price terms
  toward what the supplier prefers for as long as that also lowers our C (e.g. a longer contract that
  covers our whole need). The price is negotiated on this structure, not on the quote as sent.
- **R3.3 Asks are never free (`reference_combo`).** Terms the supplier pulled beyond our reference
  (payment days, volume, contract length, lead time, no index clause) are put back where that saves
  us money before pricing the target - what it asks for is paid for in price.

## 4. The opening anchor

- **R4.1 Anchor from the data.** The lowest of: the goal's cut off the best quote (leverage 12 %,
  routine 8 %, quality_first 6 %, stabilise 6 %, secure_supply 5 %); the current price (no increase
  at all) for price_increase and raw_material cases; the fair price = should-cost / (1 − margin)
  (leverage 5 %, routine 6 %, quality_first 8 %; the sustainable margin for relationship goals).
- **R4.2 Credibility limits.** Never more than 25 % under the supplier's price; never under its
  estimated full cost × 1.01; never under the price at which C stops falling.
- **R4.3 RFQ.** Every bidder is anchored to the best quote in the case, not its own (D02: anchored
  to its own 17.10, the dearer bidder agreed to 15.09 at once; about 13 was reachable).
- **R4.4 Fixed anchor.** The anchor is set once per thread.

## 5. Concessions: reciprocal, never stuck

- **R5.1 Curve.** Our k-th offer closes `1 − (1 − k/H)^1.6` of the gap from the anchor to the
  supplier's C; H = 6 (5 in an RFQ). Steps shrink over time.
- **R5.2 Reciprocity.** Track the supplier's C after every reply (one entry per reply, so a hold shows
  as a zero step). If it moved more than 0.1 % of C: our step = max(min(planned, 0.5 × its step),
  0.4 % of C). We move by less than it did, but always visibly.
- **R5.3 Deadlock breaker (the key difference from DualAgent).** If the supplier did not move, hold
  **once**; the next round make one real step to break the deadlock: at least 15 % of the C gap left
  (TeamAgent's measure, R12.5), and never less than 0.4 % of C. Never stall two rounds, never close
  just because both sides held. Never a token step (D01: "a nominal adjustment of one cent" was
  called out).
- **R5.4 Still moving means ask again.** If the supplier's offer meets our target but it is still
  conceding (> 0.4 % of C last round) and more than 3 rounds are left, set the target to
  `their C − 0.5 × their last step` and keep negotiating.
- **R5.5 Caps.** The target never exceeds 0.995 × the alternative's C. In an RFQ it is at most 0.99 ×
  the best rival's C, and never below the anchor.

## 6. Term sheets

- **R6.1 Up to three sheets, all at the target C** (`strategy.sheets`): our terms (every term at its
  best for us, at a higher price), their terms (the both-sides-gain structure), and a give (one lever
  from the profile moved toward the supplier, for a lower price). "Our terms" goes first.
- **R6.2 Shared surplus on a give.** When a term is worth more to the supplier than it costs us (e.g.
  faster payment to a cash-short supplier), price the give halfway between our equal-cost price and the
  supplier's indifference price (`give_price`, `SURPLUS_SHARE = 0.5`). Both sides are better off
  taking it.
- **R6.3 The supplier asked for something.** Send two sheets instead: exactly its ask at a lower
  price (shared surplus), and our reference terms at the target price. No third sheet that blurs the
  trade (`ask_sheets`).
- **R6.4 Decoy.** Arrange the sheets as lean / recommended / other. The lean one is the recommended
  sheet 2 % cheaper with the supplier's most-valued term moved our way, so the middle one looks
  sensible. It is safe: if the supplier takes the lean one, that is better for us (`with_decoy`).
- **R6.5 Sheet floor.** No sheet below max(estimated full cost × 0.97, 75 % of the supplier's price).
  P18, 10 Oct: a give at 28 % under was not credible and the supplier did not move.
- **R6.6 Out of reach.** If no sheet fits the target, send the supplier's own terms at
  max(floor, 99 % of its price), provided the must-haves hold.
- **R6.7 Every sheet keeps the must-haves** and is cheaper than what the supplier asks.

## 7. When to close

Accept the cheapest standing sheet that keeps the must-haves and beats the alternative when:

- **R7.1** the supplier agreed to our terms;
- **R7.2** it made a final offer (single thread; in an RFQ only if every thread is final or this is
  the best offer on the table);
- **R7.3** its offer meets our next target - unless it is still moving with more than 3 rounds left
  (R5.4);
- **R7.4** its offer is as good as what we last offered;
- **R7.5** at most one round is left in the thread (or in every RFQ thread);
- **R7.6** the planned rounds are over (H + 2 offers in every thread).

Walk away only when nothing on the table is acceptable and every thread is at a final offer or out of
rounds. **No** deadlock close and **no** walk-risk close: those ended DualAgent's P11 and P18 early.

## 8. RFQ handling

- **R8.1** Plan every live thread each round and write all their e-mails in parallel, so no thread
  idles into the 60-second rule.
- **R8.2** With more than two live threads, stop answering bidders more than 25 % dearer than the
  best once we have sent them two offers.
- **R8.3** Rival prices are cited as checkable arguments (`offer:` IDs), once per supplier.

## 9. The e-mail

- **R9.1 The model writes words only** (the kit's `writing.SYSTEM` JSON parts: salutation, opening,
  argument keys, bridge, question, closing). Code inserts the arguments word for word and the options
  paragraph from the term sheets, then lints (no leaks, no figures that are not in the material, no
  unknown IDs).
- **R9.2 What the writer sees:** the supplier's profile (persona, tone, pressures, wants, avoid,
  angle); its last e-mail with sender, status, subject and non-quote attachments; how our arguments
  have checked out so far; our last two e-mails; how far it moved toward us in % since our last
  e-mail; the options; up to 6 arguments ranked by the profile's `claim_ranking`; the questions.
- **R9.3 Plain stance per move** (`_stance`):
  - first e-mail: "We open: our options are ambitious; give the reasons, not apologies."
  - hold while they are still coming down: acknowledge the step briefly, say the price is still too
    high and why;
  - hold because they did not move: polite but firm, do not thank them for flexibility they did not
    show, give one concrete reason or ask what would let them move;
  - step: "a modest step - this costs us and we expect a step back."
- **R9.4 Questions.** One per e-mail at most, never repeated: a cost breakdown once (unless already
  asked or received - an open supplier sends one, and R2.1 uses it), then the case type's question
  (8D report, delivery schedule, shortest lead time, which cost elements rose, which volume or term
  would improve the price, bundle parts, fair base month and deadband), then the profile's questions.
- **R9.5 Arguments.** From `claims.candidates` (code-computed, checked IDs). Each ID once per case;
  an argument that came back `contradicted` is never used again; news the profile ranks as helpful
  counts as favourable. Even a fallback e-mail carries the two best arguments.
- **R9.6 Company name** from the data, else from the supplier's subject ("Quote from X:") or
  signature - never the bare supplier ID (D02: "Dear colleagues at sup:DEMO-J1").
- **R9.7 Allowed figures** come only from the supplier's e-mails, the trigger letter, the contract
  clauses, our arguments and the options paragraph.

## 10. Robustness and timing

- **R10.1** The writer has 35 s; a late or failed call is replaced by a code-written plain e-mail
  with the same sheets and arguments.
- **R10.2** A planning error falls back to the naive move; a hung model call never holds the case.
- **R10.3** Record every argument's `claim_results` and feed them into the next round.
- **R10.4** Log per move: target C, the supplier's C, the estimate, each sheet with its C, the prompt,
  the reply, dropped sentences and the close reason.

## 11. From DualAgent

### 11.1 A background analyst per supplier thread (`intel.Analyst`)

- **R11.1 Two layers per supplier e-mail.** At once, by code: standing sheet and its move since the
  last round, status, signals (reply delay, quote validity), claim results, missing fields, who wrote
  (role), offers stated only in prose, cost drivers the supplier names ("LCI-C rose 2.7 % ... 30 % of
  our cost"), a rough tone. In the background, one model extraction call per e-mail (JSON): the same
  items read better, plus constraints, lever hints, relationship style, withdraw language.
- **R11.2 The model reads, it never sets a number.** Every figure the extraction returns must appear
  in the e-mail, or it is dropped.
- **R11.3 Short grace, never late.** The negotiator waits at most 12 s for the model layer, then
  uses whatever snapshot is ready.
- **R11.4 Turn the supplier's own drivers against its ask.** Weight each named driver by its cost
  share (Σ change × share) and compare with the increase asked for (`cost_gap_pct`). Every index the
  supplier names that the data covers becomes a checked argument (`new_claims`), favourable when it
  rose ≤ 1 % or less than half the increase asked for. P01's 0.941 opened this way: "the weighted
  effect is about +2.4 %, not the 9.5 % you ask for".
- **R11.5 Walk risk as a signal, not a close rule.** Score 0..1 from: final offer, price unchanged
  1-2 rounds, rising reply delay, shrinking quote validity, escalation to the boss, a drop in tone,
  withdraw language. Use it only to (a) switch to an appreciative close tone (≥ 0.4) and (b) put a
  better prose offer of the supplier's on the table (R11.8, ≥ 0.5). It does **not** close the case
  (R7 still decides).
- **R11.6 New parts.** Parts named in the supplier's e-mails beyond the case file get a dossier too.

### 11.2 More from the supplier's own words

- **R11.7 Asks from the e-mail, not only the sheet.** `supplier_asks` also takes the analyst's lever
  hints ("we need payment within 15 days"), snaps the number to the case's options and treats it as an
  ask (R6.3) when it costs us something. A supplier asking for faster payment is marked cash-short and
  the estimate is redone with the higher cost of money (R2.2).
- **R11.8 Lock in prose offers.** When the supplier states a better offer only in its text (not in a
  term sheet) and it keeps the must-haves and beats the alternative, send it back as our first term
  sheet - at once if it meets our target, or under pressure (≤ 2 rounds left, walk risk ≥ 0.5, or the
  case clock near its end). The arena accepts only standing sheets, so this turns words into a deal.

### 11.3 Sheets

- **R11.9 Repair broken must-haves before building sheets.** Move each must-have field in the
  supplier's terms to the nearest allowed value that keeps it (P05: a 52-day lead time against
  "≤ 42" left no sheet to send).
- **R11.10 Hold the cost, not the sheets.** When we hold (R5.3), rebuild the sheets on the supplier's
  new terms at the same target C. Sending the same three sheets again read as "no adjustments" and drew
  a final offer (P04, 10 Oct).
- **R11.11 Code's last word on every sheet** (`_guard_sheets`): costs us at most the target, is
  cheaper for us than the supplier's standing offer, is not one of the supplier's own sheets, keeps
  the must-haves. If nothing survives, send our last sheets rather than nothing.

### 11.4 Wording tactics (words only)

- **R11.12 One tactic per e-mail, picked by code** from the analyst's snapshot (`tactics.pick`). It
  changes the words, never the sheets, and every tactic has code-written lines so it still shows when
  the model is slow. Keep the rules that won:
  - `competitive_pressure` only in an RFQ or with a real cheaper rival offer, never an invented rival
    (P05 0.444);
  - `data_argument` when the supplier's own drivers explain over 1 point less than it asks, or unused
    checked arguments remain (P01 0.941);
  - `lever_trade` for a cash-short supplier or one that named a term it cares about;
  - `appreciation_close` when the gap is under 1.5 % or walk risk ≥ 0.4 (P07 0.721, P01);
  - `humor_rapport` only with a relationship-driven supplier in a friendly tone, never with an
    annoyed one.
- **R11.13 Tactic and stance must agree.** `firm_disappointment` only after a real hold by the
  supplier (R9.3), and never at walk risk ≥ 0.4. `measured_lowball` / `firm_disappointment` were in
  the P18 run that drew "hold" twice: the deadlock breaker (R5.3) has priority over the tone.

### 11.5 Case clock (Live Final)

- **R11.14 Clock from case open.** No new counter-offer after 360 s, closed by 420 s, so every case
  ends inside the 8-minute wave cap.
- **R11.15 Measure the round time** (our send to the supplier's reply, last three rounds, each capped
  at 90 s; 45 s before any is measured) plus 10 s margin, and do not start a round that cannot finish
  before 420 s.
- **R11.16 After 360 s:** lock in a better prose offer if a round still fits (R11.8); repeat our last
  offer only when nothing on the table is acceptable yet; otherwise close by R7.

### 11.6 Guards from DualAgent's failures

- **R11.17 Coverage before accepting.** An offer whose uncovered need costs more than 1 % of C never
  counts as "meets our target". With rounds left, send one message at its price with coverage restored
  instead (P04: EUR 94,000 uncovered need, score 0.056).
- **R11.18 RFQ: every bidder hears from us before we close.** No accept until each live bidder has had
  at least one offer from us, and never accept a bidder's unchanged opening while a cheaper rival quote
  is on the table (P05: 0.0 after one e-mail; 0.444 when all three were worked).

## 12. From TeamAgent

### 12.1 Message 1 carries the deal

- **R12.1 Put the whole structure into the first message.** Mined from ~80 recorded practice
  negotiations (`AGENTS.md`): the supplier's reply to our first message gained +0.46 score on
  average, replies 2-4 together +0.08. The first message already carries full coverage (R12.2), the
  payment variants, the rival's offer (RFQ) and the strongest arguments. Every best TeamAgent run
  (P04, P05, P07, P08, P10) did this.
- **R12.2 Coverage lock.** Commit the full need for the full horizon from message 1 whenever the
  lever table says covering it lowers C (P08: 24 months on a 2-year need; 12-month packages scored
  0.08 vs 0.44 there; P10: the volume was cut below the must-have in 4/4 runs without it). Once our
  sheet A covers the need, no sheet we send covers less - not even one the model writes. Trust the
  table when coverage does **not** pay: P08's fallback EUR 5.72 was below the offer's 5.98.

### 12.2 Levers priced in euros

- **R12.3 Lever table** (`team/levers.py`). For the supplier's standing sheet, move every negotiable
  field to each allowed option, and the price by 1 %; the change in C is the lever's value in EUR and
  in EUR per unit (`price_equiv`). Ask only for levers worth at least 0.3 % of C (coverage always
  counts): suppliers price every demand in, at 3-7× its value for faster lead time or chargebacks -
  ask only what the need requires.
- **R12.4 Revealed lever prices.** When two of the supplier's sheets (main and alternative) differ in
  one term, that pair is its own price for the term (`revealed_slopes`). With it, a lever counts only
  if it saves us more than the supplier charges for it; on a tie (within EUR 1) take the option closest
  to the supplier's own value - no stricter ask than needed. Its payment slope is its cost of money
  (R2.2).

### 12.3 Anchor and steps

- **R12.5 Steps in C, at least 15 % of the gap.** Mined: messages without a step drew a final offer
  37 % of the time, steps of at least 15 % of the gap 17 %. TeamAgent's path gives 0 / 15 / 30 / 45 /
  60 % of the gap from anchor to the supplier's C by our 1st ... 5th message, never less than 15 % of
  what was left after our last message, and never above 0.997 × the supplier's C. In this agent the
  15 % is the size of the deadlock-breaking step (R5.3) and the floor of a step we choose to make;
  the reciprocity cap (R5.2) still limits our step when the supplier moved less.
- **R12.6 RFQ anchor only falls.** The anchor is the lowest price anyone has put on the table in the
  case, cut by the goal's share plus 4 points for competition; in an RFQ it may move down when a
  rival goes lower, never up (R4.4 otherwise).

### 12.4 Sheets

- **R12.7 Payment probe.** One sheet of the first menu varies payment days (to the supplier's own
  value, or to 30 days) at equal C: its pick reveals its cost of money (P05: 90/60/30 sheets; the
  bidder came back at 30 days and EUR 28.42).
- **R12.8 Index clause on the supplier's terms.** When the supplier insists on an index clause, offer
  its own series with the deadband that costs us least, never a 0 % deadband we can avoid (P08: its
  0 % became 5 % in the final offer).
- **R12.9 A floor per structure.** Each variant has its own price floor - faster payment is worth
  more to the supplier, so that sheet may go lower. A give worth more than the price room (e.g. half
  the need uncovered) is no offer.
- **R12.10 Never regress.** Never send a sheet that costs us as much as one the supplier has already
  offered (within 0.1 %). If nothing cheaper is left: offer back its earlier, cheaper sheet once, else
  accept.

### 12.5 Closing

- **R12.11 Accept ladder.** Accept from every sheet the supplier ever offered in every live thread -
  main sheets and alternatives - cheapest valid first; up to three earlier IDs (cheaper by 0.1 %+)
  before the latest ones; a refused accept changes nothing, try the next. Mined: the cheapest sheet
  ever offered was often not the one taken (P01 7/7 runs: 0.586 vs 0.550); 30 % of replies carry an
  alternative, 38 % of those are cheaper for us than the main sheet.
- **R12.12 A final offer is accepted at once.** Any message after a `final_offer` → walked away 8/8;
  accepting it → deal 38/38. Only when nothing on the table is acceptable (it breaks a must-have or
  loses to our alternative) send **one** repair message: its price, accepted, with the nearest
  must-have-keeping option and full coverage (`confirm_final`).
- **R12.13 `agreed` or stalled → its earlier cheaper sheet, once.** `agreed` only echoes our last
  sheet: if one of its earlier sheets costs us 0.3 % less, offer exactly those terms back first.
  Stalled = no cheaper offer over its last three replies after 3+ of our messages.

### 12.6 RFQ

- **R12.14 First message by code, with the rival.** In an RFQ the first message to each bidder goes
  out without waiting for the model (three model calls at once would miss the clock) and carries the
  cheapest valid rival offer: "A competing supplier has offered EUR X per unit (offer:...)" (P05).
- **R12.15 Idle hopeless threads.** With 3+ live bidders, stop writing to one whose best valid sheet
  is more than 8 % dearer than the best anywhere and that moved less than 1 % in its last reply.
- **R12.16 Wait before an accept.** Before accepting in an RFQ, wait for replies still in flight from
  other bidders - one of them may be cheaper.

### 12.7 Time and robustness

- **R12.17 Model time budget.** The model gets 50 s minus the time since the supplier's reply minus
  15 s for every other thread with a call in flight; under 10 s left, the planned move goes out
  without it.
- **R12.18 Runner** (`team/runner.py`, `team/final.py`): one worker per supplier thread; calls retried,
  and a send that may have landed is checked against the arena before it is repeated; a worker never
  dies on an error; a case is done only when the arena ended the session; before a wave cap, close on
  the accept ladder; a crashed runner resumes its open sessions; `--check` pre-flight and `--rehearse`
  on the sandbox.

### 12.8 Data hygiene

- **R12.19 Cash-short only from the supplier's own words**, never from our briefing labels or from
  its echo of our e-mail.
- **R12.20 Claims, sentence by sentence.** Drop a sentence whose IDs were all cited before (a repeat
  comes back `duplicate`: 18 % of TeamAgent's claims did before this rule); drop IDs the data does not have.
  Price-history claims (our past order prices) are context, not arguments: they "hold, but do not
  change our calculation" in 41 of 46 replies.
- **R12.21 Data attachment on request.** When the supplier asks for our figures ("which figures?",
  "send us the data", "verify this"), attach `our-figures.md`: every ID we cited with its values from
  the data package.

### Not taken from TeamAgent

- **The model proposing term sheets and accept/walk.** TeamAgent let the model re-mix the sheets and
  then clamped them back to the plan; here code decides every number (sections 5-7, R9.1). Its
  close rules (R12.11-R12.13) are code, and are taken.
- **The fixed 0/15/30/45/60 % schedule as the only concession rule**: the curve and reciprocity of
  section 5 stay; R12.5 sets the step size.

## 13. From the MESO agent

### 13.1 Settling terms one at a time

- **R13.1 Sequential equal-cost steps for the heavy terms.** After coverage is set (R12.2), rank the
  remaining non-price terms by how many EUR they move C (the playbook's terms first, R13.10), skip any
  under 0.2 % of C, keep at most 3. Each message varies **one** term across up to three packages that
  all cost us the same C; the value the supplier answers with is locked, and the next message varies
  the next term; when every term is locked, only price is left. This is how the R6 sheets are built in
  the first rounds; R6's give / ask / decoy sheets apply in the price rounds and to asked-for terms.
  P11: 300 ppm locked from a 300 / 800 / 1,500 menu → 0.710 twice.
- **R13.2 Option order in a step:** the best option for us first, then the one the supplier asked for
  (if any), its current value, the middle option, the rest - three at most, each a different package.
- **R13.3 Reopen a costly lock.** A term stepped only once whose locked value costs us more than 1 %
  of C against its best option gets one more step (P01: the earliest effective date was worth 3.5 %
  of C).
- **R13.4 A step needs a choice.** A step with fewer than two credible packages (the rest under the
  floor or not cheaper than the supplier's sheet) is not a step: keep the term open, move it to the
  price round with our best value, or skip it - never "lock" a heavy term by sending it alone (P01,
  0.159).
- **R13.5 A term the supplier prices itself is locked without a step** (`revealed`): when its
  e-mail offers two packages that differ in one term only - its sheet and an "Alternatively, with
  30 days: EUR ..." line - lock that term at the value that costs us less at the supplier's own
  prices.
- **R13.6 Read the supplier's prose alternatives** (`prose_alternatives`): "Alternatively, with
  <terms>: EUR <price> per unit" in English and German (Zahlungsziel, Lieferzeit,
  Preisgleitklausel), for payment days, effective date, lead time, volume, contract months, ppm,
  chargeback and Incoterm; a line whose index clause cannot be pinned to an option is skipped. These
  packages feed R12.11 (accept ladder) and R13.5.
- **R13.7 No identical resend.** If the next message would only repeat our last sheets, the thread
  is done (a repeat draws a final offer) - close by R7 or offer back an earlier, cheaper sheet once.

### 13.2 Correct numbers

- **R13.8 Solve the price exactly.** Every package is priced so its C equals the target, not by a
  rule of thumb:
  - without the supplier's margin charge C is linear in the price: `p = (target − C(0)) / (C(1) − C(0))`;
  - with it, C is convex in the price: find the cheapest price by golden-section search between 0.5×
    and 1.5× the supplier's price, and solve for the target by bisection on the side **above** it
    (a price below the cheapest one costs more and leaves the supplier weaker).
  Tests: each sheet of a step has the same C within EUR 1.
- **R13.9 Data arguments computed the way the arena checks them** (`claims.py`): an index change uses
  two months of one series at most 12 months apart, ending within the last 12 months of the data
  (frozen at 2026-09); figures rounded as the sentence states them, next to their IDs. Only arguments
  that argue our side are offered: an index that **rose** confirms the supplier's case and is kept out
  unless it rose less than half the increase asked for (P02, P04). Kinds: index changes, the weighted
  summary of the part's own cost drivers ("explain about +x %, not y %", no ID), a letter giving less
  notice than the contract requires, scorecard quality and delivery, rival quotes and offers,
  should-cost, forecast volume. Each ID once per case; a rival's offer once per supplier.

### 13.3 Playbook per category goal

- **R13.10 One engine, settings per goal** (`PLAYBOOK`):

  | Goal | Anchor (× opening) | Concede | Terms first | Index clause | Margin charge | Walk | Close gap |
  |---|---|---|---|---|---|---|---|
  | leverage | 0.85 | 0.85 | payment, index | any | no | yes | 0.5 % |
  | routine | 0.90 | 0.80 | contract months, payment | avoid | no | yes | 1.0 % |
  | stabilise | margin-optimal price | 0.80 | volume, contract months, payment | deadband ≥ 3 % | yes | no | 0.5 % |
  | secure_supply | 0.95 | 0.70 | volume, lead time, contract months | avoid | yes | no | 1.5 % |
  | quality_first | 0.92 | 0.80 | ppm, chargeback | avoid | no | yes | 0.5 % |

  The anchors here and in R4.1 must be reconciled into one table before implementation (R4.1's cuts
  come from BuyerAgent and are data-driven; these are MESO's fixed shares). "Walk: no" means:
  stabilise and secure_supply take the best sheet that beats the alternative rather than walk.
- **R13.11 No index clause on a volatile series.** Never offer an index clause on a series that moved
  more than 15 % in its last 12 months - a clause on it is a bet.
- **R13.12 Price-increase anchor:** in a price_increase case the anchor is the current price - no
  increase at all - when it is below the quote.
- **R13.13 Accept on a small gap:** once only price is left, accept when the supplier's C is within
  the playbook's close gap of our target.

### 13.4 The e-mail

- **R13.14 Code writes what must match the sheets** (`writing.py`): the options paragraph is built
  from the term sheets themselves (`describe`: price, payment, effective date, months, volume, lead
  time, index clause with series, share, deadband and base month, ppm, chargeback, Incoterm, tooling),
  so prose and sheets never disagree; code adds its own sentences (a volume commitment, an earlier
  offer taken up) and the signature.
- **R13.15 Lint before sending.** Drop any sentence of the model's that leaks urgency, a deadline, a
  budget, a missing alternative ("no other supplier", "single source", "line stop", "fallback"), our
  limit ("walk-away price", "maximum we", "best we can pay"), "total cost", "category goal" or
  "score"; that carries a data ID; or that states a figure found nowhere in the material. Salutation by
  name only if that name appears in the supplier's e-mails, else "Dear colleagues at <company>". A
  volume question ("which volume can you commit?") is answered by code with the number from our sheet.

### 13.5 Never lose a reply or an accept

- **R13.16 Reply and accept inside 60 s.** A slow POST reads the supplier's reply from the thread
  instead of waiting on the call, so the 60-second rule is not missed (P05 once lost two agreed deals
  to replies held back this way). In an RFQ a final offer that is the best on the table is accepted
  within 60 s of its reply, even while other threads are still talking - waiting for in-flight
  replies (R12.16) never runs past that clock (P05, meso3: accepted after 4.5 minutes → `409`, no deal).
  On a `422` for an accept (the offer moved on) reload the thread and decide again.
- **R13.17 Cached data layer.** Build the master data file once (`arena-build-master-data`) and cache
  each case's negotiation dossier per data revision, so preparation for a case is a cache hit
  (milliseconds) and the Sunday release is picked up automatically.

### Not taken from the MESO agent

- **Locking whatever the supplier picks on a heavy term**, without R13.3/R13.4 (P01, 0.159).
- **The fixed `(1 − concede^k)` curve as the only concession rule** - section 5 (curve, reciprocity,
  deadlock breaker) stays. R13.10's `concede` column is MESO's pace per goal, a reference for tuning
  section 5's horizon per goal.

## Acceptance

- Unit tests for R5.2-R5.4 (reciprocity, deadlock breaker, still-moving), R6.2-R6.5 (surplus,
  ask sheets, decoy, floor) and R7 (close rules, including "no close on a two-round hold").
- Unit tests for R11.2 (figures not in the e-mail dropped), R11.8 (prose offer locked in), R11.9-R11.11
  (repair, rebuild on hold, sheet guard), R11.12 (no competitive pressure without a rival), R11.14-R11.15
  (clock) and R11.17-R11.18 (coverage, every RFQ bidder answered).
- Unit tests for R12.2 (coverage lock), R12.4 (revealed slopes), R12.10 (never regress), R12.11 (accept
  ladder order), R12.12 (final offer accepted, one repair only), R12.14 (RFQ message 1 cites the
  rival) and R12.17 (model budget). TeamAgent's offline tests on recorded negotiations
  (`team/tests/test_offline.py`) are the template.
- Unit tests for R13.1-R13.5 (one term per step, option order, reopen, a step needs two packages,
  revealed terms), R13.6 (prose alternatives in English and German), R13.7 (no identical resend),
  R13.8 (equal C within EUR 1, price above the C-minimum with the margin charge), R13.9 (index window,
  a rising index kept out), R13.11 (volatile series), R13.15 (lint) and R13.16 (an RFQ final offer
  accepted inside 60 s).
- Practice runs, the best recorded run per case (`runs/best_runs.json`, `runs/meso3`, `runs/meso4`):
  P01 ≥ 0.941, P02 ≥ 0.507, P03 ≥ 0.919, P04 ≥ 0.86, P05 ≥ 0.781, P07 ≥ 0.773, P08 ≥ 0.549,
  P10 ≥ 0.671, P11 ≥ 0.710, P18 ≥ 0.453 (mean 71.6). The P02 best run (0.507) is in no agent's
  session log and is not TeamAgent's; its e-mails use the shared writer, so it is probably a BuyerAgent
  or DualAgent run with logging off. Treat that target as a stretch goal.
- All 10 practice cases end in a deal (TeamAgent v9 did).
- Every message goes out within 60 s; no `contradicted` claims; no e-mail without a company name;
  every case closed by 420 s after it opens.
