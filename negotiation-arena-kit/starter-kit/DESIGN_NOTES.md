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

## Learning from experiments (`spec.strategy`, `spec.lab`, `knowledge/`)

The anchor depth, the concession pace and the close gap are knobs (`Strategy`) layered over the
playbook row and section 5's plan. The defaults reproduce the agent exactly (`BASE.row(case)` is the
playbook row itself). `spec.lab` plays named variants on the hosted practice cases and appends every
game to `knowledge/experiments.jsonl`. From that sheet it builds `learned_playbook.json` (per case: its
features and each variant's score gain over base, measured within one mode) and the result sheet
`EXPERIMENTS.md`. `make()` and `spec.evaluate` load the playbook. `SpecAgent()` without `knowledge` plays
the defaults, so tests and recorded negotiations are unaffected.

Choosing per case: same case, then type + goal, then goal, then type, then all cases. A variant needs a
mean gain of at least 0.005 and no loss over 0.05 in the group. Beyond the same case it must also be safe
in every tested case: firm_deep_fast helped P08 but broke the leverage RFQ P05 by 0.18. A group level
where nothing beats base passes on to the broader levels. A case's own games saying base stand, because
they are direct evidence. At the all-cases level a variant must have won two cases and lost none. Ties
go to the variant closest to base.

Replay (`spec.replay`): the hosted suppliers answer identical e-mails identically, so a code-only game
whose e-mails match a recorded game is that game. The lab replays every game against the recorded ones
first and spends a practice session only when the agent sends an e-mail no recorded game sent. Each
real game joins the book. Validated: 88 of 88 replayable recorded games reproduced their exact score.
The three-bidder RFQ P05 depends on reply timing and always needs a session.

Findings that changed the agent (details in `knowledge/EXPERIMENTS.md`):
- `MEET_SHARE` (meet a big supplier jump halfway) overrode every concession setting in P01. Its
  supplier agreed at once to the halfway offer. measured_meet (20 %) gave 0.808 -> 0.859 on P01 but
  hurt P05 and P18, so it is case-specific.
- One term menu, then price rounds (max_steps 1) helped both raw-material cases: P18 0.467 -> 0.513,
  P08 0.305 -> 0.361 (0.593 with a deep anchor). Holding out for a closer match (close_gap x 0.25)
  lifted P02 0.390 -> 0.507. Together (firm_one_step) they lost no case, so firm_one_step is what an
  unseen case gets when its group has no better evidence.
- Changing the order of the term steps gained nothing. The supplier's own ask (payment days in P01's
  quote PDF) goes first anyway, and it answers every equal-cost menu in its own favour.

Validation, code-only: base 0.665, the agent's own picks 0.716, leave-one-out picks 0.682. Leave-one-out
(`lab learned --holdout`) is the honest estimate for hidden cases.

## Listening to the supplier, words that match the numbers

Reading the recorded games (`runs/lab/1011-085641_learned-2`) showed the agent reacting to the supplier's
numbers but not to its words. Every lab game ran code-only, with no model, so the e-mails were the
tactic's template lines. Changes:

- **No step back.** `CEILING` (0.997 x their C) could fall under our own last target: in P02 the next
  offer went from 370.85 to 370.52, the supplier called it "a step backwards" and sent its final offer.
  The target never goes below our last one now. The alternative and, in an RFQ, a cheaper rival bid stay
  hard caps above that rule. When the ceiling is under our last offer the gap is too small to split:
  `closing.decide` lets our offer stand once, then takes its offer (`at_ceiling`). Taking it at once
  cost P02 0.02 (372.05, where the old run's hold drew a final offer of 371.88).
- **Every cost driver.** `drivers_by_code` read one driver per sentence. P18's sentence named LCI-C and
  PPI-C2431, so PPI (+12.3 % at 38 %) was dropped and our "weighted effect" was +2.8 % instead of +7.5 %,
  a figure the supplier could check.
- **Words match numbers.** P08 opened 15 % under today's price next to "the weighted effect is about
  +7.5 %", which argues for more than we offered. Dropping the sentence cost P08 0.09
  (fixes-code-learned): the supplier moves on its own figures. So when our offer is below today's price x
  (1 + X), the sentence takes the figure as an upper bound ("Even taken at face value, the figures you
  gave us ... explain at most about +X %"). `summary:indices`, which states the same figure as ours, is
  not cited then. The data_argument bridge no longer says the options "follow from" the data, and no
  closing line promises to "go through the figures together" (P18's supplier offered a meeting in reply).
- **Refused terms.** The analyst reads term values the supplier says it will not give, by code (a fixed
  price, payment days, a contract length) and by the model (`refused_terms`, a figure only if the e-mail
  has it). Sheets drop them (`sheets.drop_refused`), unless every sheet would go, and the e-mail
  acknowledges the point once. P08 offered a fixed price again after "we cannot offer a fixed price over
  a 24-month period".
- **Answers.** An offer of a call gets a code line (we settle in writing). A request for our figures gets
  the attachment and a line saying so. The model prompt lists the supplier's questions and the opening
  must answer them.
- **Keep arguing.** `email.position_lines` computes an argument without a data ID every round, so it never
  comes back `duplicate`. It uses only what the supplier can check itself: the share it still asks over
  today's price, and our movement since our first offer when it held. Each is used once per thread.
  Claims it could not check (the price risk we carry under an index clause, the value of our volume)
  drew "please provide the data behind your target price" and a binding-forecast demand, and the round
  stalled (fixes-code-learned P08, P18), so they were taken out again. The tactics' code lines have
  variants (`tactics.MORE`), a fallback e-mail never repeats a line the thread has seen, and a single sheet
  gets no line about choosing between options. A leverage or quality case no longer gets the joking tone.

**The model and the numbers.** R9.1 and R11.2 still hold: the model never sets a price. The model reads,
and what it reads now reaches the numbers through code. Its `refused_terms` change which sheets we send,
and its `at_limit` and `wants_close` feed the close rules (OR-ed with the regexes), together with the
prose offers and lever hints it already gave. Letting the model choose a price would make every game
irreproducible (no replay screen, no lab comparison) and would bring back invented figures.
