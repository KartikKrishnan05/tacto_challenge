# SpecAgent completion and review — 11 October 2026

The missing test coverage, design notes, sandbox games, local simulator games and hosted practice
evaluation have been completed. This is **not a full acceptance pass**: only P02, P04 and P11 meet
their rounded score targets, and a deliberately idled P05 bidder incurred a timeout. The remaining
performance gaps and explicit requirement deviations are recorded below, not hidden by the successful
deals or by the team's historical leaderboard bests.

## Verification

Final source: **204 tests passed, 1 skipped**, `ruff check arena_kit tests` clean, `git diff --check`
clean. The skipped test is the separately opted-in hosted smoke test for NaiveAgent/LLMAgent
(`ARENA_TEST_URL`/`ARENA_TEST_KEY`); SpecAgent's requested hosted games actually ran, with the
configured OpenAI `gpt-6-astra` model and the user's explicit permission.

| Requirement | Regression coverage |
|---|---|
| R11.2, R11.8, R11.12 | `tests/test_spec_analyst.py`: grounded extraction, decimal/sign boundaries, dates, nonfinite numbers, inherited prose lock-in, tactic eligibility |
| R11.14–R11.15, R12.17 | `tests/test_spec_clock.py`, `tests/test_spec_analyst.py`: virtual clocks, measured rounds, short/zero waves, early close, writer budget/fallback |
| R12.4, R12.14 | `tests/test_spec_steps.py`: inherited revealed slopes, costly lever exclusion, first RFQ rival citation without model waiting |
| R13.1–R13.7 | `tests/test_spec_steps.py`: one-term menus, ranking, option order, one reopen, real two-package choices after guards, persistent/overriding revealed locks, EN/DE alternatives, no identical resend |
| R13.9, R13.11, R13.15 | `tests/test_spec_claims_email.py`: index windows/rounding, rising-index eligibility, notice/ledger, case-only facts, exact volatility threshold, lint and real salutations |
| R13.16 | `tests/test_spec_clock.py`, `tests/test_spec_runner.py`: bounded rival wait, slow-POST polling, refused-accept refresh/retry, full local API games |
| Evaluation accuracy | `tests/test_spec_evaluate.py`: structured timeouts are detected even without timeout wording |

Existing pricing, coverage, sheet guard, close-rule and recorded-negotiation tests were rerun too.
Additional tests cover cost-neutral normal lead times and numeric equality of locked terms (`0` vs
`0.0`).

## Games and score table

D01 and D02 were played with the configured model: scores **0.786949** and **0.649788**, valid deals,
closed after **138.1** and **200.8** seconds from open. Evidence:
[D01](runs/spec-sandbox-d01/D01.json), [D02](runs/spec-sandbox-d02/D02.json).

All three clock-enabled local simulator cases also ended in valid deals: S01 **0.173886**, S02
**0.171836**, S03 **0.633926**. The final-source model repeat is in
[spec-sim-verified](runs/spec-sim-verified/summary.md). An initial simulator setup omitted the data
facts and produced contradicted IDs; the evaluator was corrected to supply the facts and the games
were repeated cleanly. Simulator scores test plumbing, not hosted negotiation quality.

The hosted model sweep is one complete ten-case run. The code-only column is a separate final-source
fallback benchmark, not a claim that model and non-model negotiations are interchangeable. Targets
are compared at three decimals because the requirements round historical raw scores.
The main model sweep preceded the last lead-time/volatility edits; the affected P05 negotiation was
retested with the lead-time fix, followed by a final-source model simulator repeat and a full
final-source code-only hosted sweep. Source fingerprints preserve these distinctions.

| Case | Target | Final code-only | Model sweep | Model seconds from open | Target met |
|---|---:|---:|---:|---:|---|
| P01 | 0.941 | 0.827854 | 0.631200 | 113.5 | No |
| P02 | 0.507 | 0.506721 | 0.506721 | 121.3 | Yes, rounded |
| P03 | 0.919 | 0.864105 | 0.864105 | 160.7 | No |
| P04 | 0.860 | 0.860445 | 0.860445 | 118.1 | Yes |
| P05 | 0.781 | 0.676181 | 0.521725 | 170.2 | No |
| P07 | 0.773 | 0.753477 | 0.714517 | 144.9 | No |
| P08 | 0.549 | 0.393752 | 0.332313 | 128.1 | No |
| P10 | 0.671 | 0.662905 | 0.647294 | 34.7 | No |
| P11 | 0.710 | 0.709896 | 0.709896 | 322.0 | Yes, rounded |
| P18 | 0.453 | 0.270425 | 0.319096 | 143.6 | No |

Model sweep mean: **0.610731 (61.07/100)**, versus target mean **0.716400 (71.64/100)**.
Final code-only sweep mean: **0.652576 (65.26/100)**; all ten valid deals, maximum closure time
185.6 seconds, but also one deliberately idled P05 bidder timeout. Fast cached supplier responses
in many repeated code-only games are not a guarantee of real-world latency.
The post-fix, model-backed P05 retest improved to **0.573210**, with 35-day lead time, but remains
below target. Its wall time includes pre-open profiling; closure from open was about 203 seconds.
It is a targeted retest, not a replacement ten-case mean.

Raw scores, threads, sessions, audit records, prompts/replies and source fingerprints:
[model sweep](runs/spec-practice-model/summary.md),
[final code-only sweep](runs/spec-practice-verified/summary.md),
[P05 retest](runs/spec-practice-p05-lead-fix/P05.json).
Earlier experiments are preserved in `runs/spec-practice-code`, `runs/spec-practice-fixed` and
`runs/spec-practice-final`; their intermediate regressions are not represented as successful fixes.

## Below-target investigation and fix outcomes

| Case | Evidence / cause | Fix or measured outcome |
|---|---|---|
| P01 | A lower-total-cost later-date package can need a higher unit price. The original generator rejected it. Model play also locked 30-day payment and a January date after the supplier priced an alternative; its cheaper alternative ID was refused by the arena. | Removed the unit-price ceiling while preserving total-cost/floor guards; enforced real choices after guards and persistent revealed locks. Code play improved from 0.759845 to 0.827854 and obtained April, but at EUR 136.85/30 days rather than historical EUR 135.20/60 days. Model play still missed. |
| P03 | Full coverage is correct. The first credible payment choices at the estimated convex-cost target were 30/45 days; the supplier chose 45 and held EUR 430.09. Historical target play achieved 90 days at EUR 432.29. | Fixed inherited prose slopes and actual-price locks. Repeated code/model games still scored 0.864105. The remaining gap is payment/anchor calibration, not a missing coverage or arithmetic fix. |
| P05 | Quote-PDF defaults carried unnecessary 28-day expedited delivery despite a 35-day need. Suppliers explicitly state that 28 days costs overtime. Dralveck countered at EUR 28, then walked after another low counter; Fuchsreiter became the remaining best deal. | Relax cost-neutral expedited lead times without violating must-haves; bounded accept waiting and skipped model grace on terminal replies. Model score improved 0.521725 → 0.573210. Final code play obtained EUR 29.50/35 days and 0.676181. The anchor/concession path still fails to secure the historical EUR 28.42 deal. |
| P07 | Correct 24-month coverage/90-day payment, but supplier stopped at EUR 5.46 with the model and EUR 5.40 without it, against historical EUR 5.37. | Retested after lock/guard/no-resend fixes. Final offers were accepted promptly, not countered past withdrawal. Further opening/wording calibration remains unproven; no numerical bug was found that explains the residual price gap. |
| P08 | Supplier insisted on an index clause; wider deadband menus incurred a price premium and quickly reached final. Model EUR 5.67/5% band differs from historical EUR 5.47/5% band. | Added demanded-series/deadband filtering and persistent revealed-price locks. Code obtained EUR 5.49/0% band and 0.393752; model scored 0.332313. This policy change did not close the gap and some experiments worsened the score. |
| P10 | Coverage/must-have repair held, but the supplier immediately made a valid final offer: EUR 31.90 with model, EUR 31.70 without, versus historical EUR 31.60. | Verified full 23,400 annual volume/24 months and immediate final acceptance. No unsafe counter after final was used to chase the score. Opening lead-time/price calibration remains a strategy gap. |
| P18 | Payment/index sequencing left EUR 5.60/0% band with model or EUR 5.80/5% band in code play. Cheaper historical/alternative IDs were refused, then the current offer was accepted. | Retested the deadband/lock changes and robust accept ladder. Best observed score in this work was 0.319096, not 0.453; the wider-band experiment regressed. Payment/deadband premium calibration remains unresolved. |

These causes distinguish observed supplier behaviour from inferred tuning opportunities. A passing
unit test does not prove a negotiation will reach a historical score, and cross-run differences do
not establish that model wording alone caused the difference.

## Timing, claims, and consistency review

All ten model cases ended in deals, all returned `must_haves_ok: true`, and all closed before 420
seconds (maximum 322.0). There were **no contradicted claims** and **no buyer emails missing the
Hollerau company signature** in the corrected simulator, sandbox or hosted evidence. Model writer
calls in the ten-case sweep took at most 11.7 seconds, with no writer errors.

The model P05 sweep, its lead-time retest and the final code-only sweep each contain one structured
timeout on the deliberately idled, uncompetitive Hrvelec thread. An earlier code-only baseline also
timed out a Fuchsreiter reply.
These are not clean passes of the literal every-reply-within-60-seconds acceptance rule. The runner
now polls slow POSTs after 3 seconds, waits for rivals no longer than 45 seconds after its own reply,
and refreshes on refused accepts. The remaining idle-bidder timing policy conflict is explicit.
Hosted records lack per-email delivery timestamps, so absence of timeout records is evidence, not a
proof of an exact maximum end-to-end reply latency.

[DESIGN_NOTES.md](DESIGN_NOTES.md) collects precedence and judgement calls. Important deviations:
whole-cent quantization prevents literal EUR 1 equal-cost precision on large lots; opening-price-only
cost estimates are exempted from the inherited full-cost anchor/floor; and voluntarily idling an RFQ
bidder can conflict with literal universal reply timing. The demanded-index/revealed-price policy
also needs further performance tuning. None is silently counted as full requirement compliance.

No final-wave sessions were started, no unrelated user files were removed, and no commit was made.
The completed artifacts are ready for review, but score/timing acceptance is not yet fully achieved.
