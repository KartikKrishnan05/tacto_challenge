# SpecAgent design decisions

The implementation follows `NEW_AGENT_REQUIREMENTS.md`. This file collects choices previously
recorded only in docstrings and records how requirements that overlap are reconciled.
Code controls prices, term sheets, accept/walk decisions, claims and deadlines. The model profiles
suppliers, extracts what they said and writes the surrounding email text. Every model call has a
deadline and a code fallback.

## Rule precedence

| Overlap | Decision and reason |
|---|---|
| R4.1 vs R13.10 anchors | Use the deeper cut: leverage 15%, routine 10%, quality 8%, stabilise 6%, secure supply 5%; RFQs add 4 points. Apply the current-price/fair-price candidates and credibility limits afterwards. The relationship cost minimum is a floor, rather than an independent second anchor. |
| R5.2 reciprocity vs R12.5 15% steps | The 15% gap rule determines the planned step; reciprocity caps it when the supplier moved less. The 0.4% visible-step minimum wins over that cap, as R5.2 explicitly specifies. A nonmoving supplier gets one hold, then a real step. |
| R5.4 vs no retreat | Continue negotiating while a supplier is still moving, but do not ask below our previous target merely because the supplier improved. Supplier and alternative caps can still lower the target. |
| R6 menus vs R13.1 sequential terms | After coverage, heavy terms use a single-term equal-cost menu. Price rounds use the reference/give/decoy menu. A supplier ask may replace that menu with two clear trade packages. |
| R13.1 goal order vs R12.7 payment probe | A payment term worth stepping goes first, so the first menu also reveals payment preference. Remaining terms follow goal priority, then EUR weight; at most three terms. |
| R13.3 reopen vs R13.4 choice | A costly term gets at most one reopen. A menu with fewer than two credible packages is skipped and never recorded as a step or a lock. Its best value stays available in price rounds. |
| R13.5 revealed price vs an earlier menu lock | A supplier's own differently priced alternative overrides the earlier menu pick when its actual package costs us less. Persist that revealed value across replies without alternatives. Compare numeric terms by value, so `0` and `0.0` are the same lock. |
| R12.8 deadband vs R13.5 actual prices | New demanded-index menus use the supplier's own series and the cheapest deadband at equal unit price. Later supplier-priced alternatives are compared at their actual package costs; a cheaper zero-deadband package can win that revealed-price comparison. This can lose versus negotiating a wider band, and must be measured rather than assumed beneficial. |
| R12.12 final offer vs continued steps | A valid final offer closes at once. Only an unacceptable final gets one repair at its own price. Tone, walk risk and deadlock are never independent close rules. |
| R12.16 wait for rivals vs R13.16 60 seconds | Wait only until 45 seconds after the chosen supplier's reply, leaving 15 seconds to accept. A slow POST is polled from the thread after 3 seconds; waiting 70 seconds could itself exceed the reply clock. |
| R11.14 vs R11.15 clock | No new counter after 360 seconds, and no counter earlier if its estimated round cannot finish by 420. Writer time is also capped by that remaining round budget. Clock starts immediately after session open, before document reading/preparation. A shorter wave moves both marks earlier; zero seconds forces a close. |

## Cost estimates and prices

Supplier full cost comes from its breakdown, otherwise should-cost / 1.02, otherwise opening / 1.10.
For an RFQ whose bids are below should-cost, the lowest opening bid replaces stale should-cost as
the fallback estimate. Later concessions cannot disable the margin penalty: market calibration uses
opening bids, not the lowest price reached during negotiation. Only stabilise and secure-supply
goals include the supplier margin penalty.
The inherited implementation trusts only breakdown/should-cost estimates for the full-cost anchor
and credibility floor. With an opening-price-only estimate it uses the 75%-of-quote floor instead.
This is an additional deviation from literal R4.2/R6.5, which do not exempt an opening estimate;
retained as an explicit tuning decision, not silently described as full compliance.

A missing alternative (`kind: none`) leaves the accept ceiling infinite. A real alternative is
priced with its actual terms. Offers must beat it both with the estimated supplier penalty and
without it. Coverage is requested when it lowers cost; subsequent packages cannot reduce committed
coverage. Coverage repair can explicitly restore the full horizon and annual need.

Without a margin penalty, solve price from the linear cost function. With it, use golden-section
search for the cost minimum and bisection above that minimum. Prices must use two decimals, so the
literal R13.8 “within EUR 1” is often mathematically impossible: changing the price by EUR 0.01 on
189,600 units changes spend by EUR 1,896. Tests therefore use max(EUR 1, EUR 0.005 × committed units)
for rounding error. This is a documented deviation, not a claim of literal compliance.

A package need not have a lower unit price than the supplier's offer: a later effective date or
better payment terms can lower total cost despite a higher price. Credibility floors still apply.
When rounding near the convex cost minimum, use the first whole cent at or above that minimum.
Do not retain a gratuitous expedited lead time from the quote PDF: if an allowed normal lead time
still meets the need and must-haves at equal cost, request that instead. This addresses P05's explicit
overtime premium for 28 days when 35 days is sufficient; genuinely valuable speed is not relaxed.

## Evidence and wording

Practice ranges are warnings, not replacement values: a valid case with an unusual interest rate
must use that rate. Missing/invalid fields receive defensive defaults; every warning is logged.
Unknown case types use their actual negotiable fields; unknown goals use the routine playbook.
When a field is both fixed and negotiable in a malformed case, the negotiable definition wins and
the conflict is logged.

Prose alternatives inherit unchanged terms from the main offer before revealed prices are computed.
They cannot be accepted by an invented offer ID: packages found only in prose are offered back for
confirmation. An ambiguous index clause is skipped. Dates and all numeric leaves returned by the
analyst must occur in the supplier's text, and nonfinite figures are rejected.

Index arguments use two months of one series, twelve months apart, ending in the most recent
twelve-month window of the frozen September 2026 data. Missing endpoints or old windows produce no
claim. Positive changes are excluded unless below half the supplier's requested increase. Each data
ID counts once per case; rival quote/offer IDs count once per supplier. Hidden-case facts override
package facts and also participate in volatility checks. A series moving more than 15% is excluded
from offered index clauses.
Volatility uses unrounded endpoint values, unlike the one-decimal claim sentence: 15.04% is over
the risk threshold even though its printed claim would say 15.0%.

Code writes the options directly from the sheets, answers volume questions and inserts checked
claims. Lint drops leaking sentences, data IDs and unsupported figures, including small integers.
Personal salutations require a name in supplier emails; otherwise use the company name. Model
wording cannot introduce terms or change the action.

## Rehearsal and practical limits

`python -m arena_kit.spec.evaluate` saves full sessions, thread histories, decision logs, a source
fingerprint and a score table in a new directory. `--sim` runs all three local cases over HTTP with
the clock enabled; `--set sandbox` plays D01/D02; `--set practice` selects hosted cases only. The
default parallelism is five cases, below the ten-open-session API limit.
The evaluator audits the API's structured timeout records, not just timeout wording in an email.
New evaluations also save the case definition and parsed opening standard terms for reproducibility.

Targets in the requirements are rounded to three decimals (for example the historical P01 score
0.940823 is written as 0.941). Evaluation compares three-decimal scores and retains exact raw values.
A historical team best is not evidence that this agent reached that target. The final report must
distinguish previous bests, current results, and unresolved shortfalls.

Clock guards use measured rounds capped at 90 seconds and the slowest of the last three plus ten
seconds. They cannot guarantee a server responds or commits an accept by 420 seconds during a
network outage. Hosted logs must be checked for actual closure times, refusals, timeouts and
contradicted claims; simulator scores validate plumbing, not hosted negotiation quality.
There is a policy collision between R12.15 (stop writing to a hopeless bidder) and literal
"every reply within 60 seconds" acceptance. A deliberately idled, non-selected bidder can receive
an arena timeout while other negotiations continue. Keep such timeouts visible in the report;
do not count them as a clean timing pass merely because the chosen deal closes promptly.
