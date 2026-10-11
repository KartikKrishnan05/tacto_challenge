# SpecAgent experiment and result sheet

Built 2026-10-11T08:56:57 from **239 games** (0 failed or unscored rows left out) on 10 practice cases, modes: code. Regenerate with `python -m arena_kit.spec.lab build`; every game is a line of `experiments.jsonl`, its full session in the `record` path. The agent reads `learned_playbook.json` when it prepares a case (`spec.strategy.choose`).

Rule: a variant replaces base for a group of cases when its mean score gain over base is at least 0.005 and it lost no case in the group by more than 0.05; beyond the same case it must also have lost no tested case by more than 0.05, and at the all-cases level it must have won 2+ cases and lost none. Ties go to the variant closest to base. Code-only games repeat almost exactly (same e-mails, same supplier replies), so one code-only game per case and variant is a measurement, not a sample; games with the model vary and are averaged.

## Hypotheses and verdicts

| Variant | Knobs vs base | Hypothesis | Cases | Mean gain | Won / lost | Verdict |
|---|---|---|---:|---:|---|---|
| base | — | The agent as designed (control). | 10 | — | — | control |
| soft_anchor | cut_k=0.5, anchor_cost_k=1.02 | Opening closer to the supplier's price keeps it cooperative and avoids walk-aways in relationship cases (stabilise, secure_supply). | 10 | -0.091 | 1 / 6 | helps some cases only |
| deep_anchor | cut_k=1.6, anchor_cost_k=0.95 | A lower opening drags the settlement down where we have leverage (rivals, market data). | 10 | +0.005 | 1 / 3 | helps some cases only |
| fast_concede | horizon_delta=-2, curve_exp=1.2, break_share=0.25, reciprocity=0.75 | Bigger, earlier steps reach a deal before the supplier hardens or issues a final offer. | 10 | +0.012 | 2 / 1 | better overall |
| slow_concede | horizon_delta=2, curve_exp=2.2, break_share=0.08, reciprocity=0.35 | Smaller steps and a longer horizon extract more from suppliers that keep moving. | 10 | -0.003 | 0 / 1 | rejected |
| eager_close | meet_share=0.75, close_gap_k=4.0 | Accepting once the supplier is within ~2 % of our target avoids losing deals to one counter too many. | 10 | -0.045 | 0 / 5 | rejected |
| deep_fast | cut_k=1.6, anchor_cost_k=0.95, horizon_delta=-2, curve_exp=1.2, break_share=0.25, reciprocity=0.75 | Deep anchor and fast concession together: the low opening sets the range, the big steps keep the supplier engaged (both helped P08 alone). | 10 | +0.017 | 2 / 2 | helps some cases only |
| mid_anchor | cut_k=1.3, anchor_cost_k=0.97 | A moderately deeper anchor keeps deep_anchor's gain (P08) without its loss on P18. | 10 | -0.020 | 0 / 4 | rejected |
| firm_close | close_gap_k=0.25 | Since accepting early hurt (eager_close), holding out for a closer match to our target gains more. | 10 | +0.012 | 1 / 0 | better overall |
| firm_deep_fast | cut_k=1.6, anchor_cost_k=0.95, horizon_delta=-2, curve_exp=1.2, break_share=0.25, reciprocity=0.75, close_gap_k=0.25 | firm_close's gain (P02) and deep_fast's gains (P07, P08) add up in one strategy. | 10 | +0.010 | 3 / 3 | helps some cases only |
| measured_meet | meet_share=0.2 | When the supplier jumps further than the gap left, closing only 20 % of the gap (not 50 %) keeps us from overshooting: P01's supplier 'agreed' at once to our half-way offer. | 10 | -0.018 | 1 / 2 | helps some cases only |
| no_meet | meet_share=0.0 | Pure reciprocity (half of their step, capped by our curve) without any 'meet it' rule. | 10 | -0.023 | 1 / 2 | helps some cases only |
| weight_order | step_order=weight | Settling the term worth most EUR first (P01: the start date, EUR 25k) beats always opening with payment days. | 10 | +0.000 | 0 / 0 | rejected |
| no_reopen | reopen=False | Moving on to the next term instead of a second menu on a locked costly one gets more terms onto the table (P01 re-asked payment days and never offered April). | 10 | -0.002 | 0 / 1 | rejected |
| date_first | step_order=weight, reopen=False | weight_order and no_reopen together. | 10 | -0.010 | 0 / 2 | rejected |
| one_step | max_steps=1 | One term menu, then price rounds: fewer rounds spent on terms the supplier answers in its own favour. | 10 | -0.003 | 2 / 1 | helps some cases only |
| measured_firm | meet_share=0.2, close_gap_k=0.25 | measured_meet plus firm_close (the only variant with no loss so far). | 10 | -0.006 | 2 / 2 | helps some cases only |
| deep_one_step | cut_k=1.6, anchor_cost_k=0.95, max_steps=1 | deep_anchor's P08 gain and one_step's raw-material gains (P08, P18) add up. | 10 | +0.014 | 1 / 3 | helps some cases only |
| firm_one_step | close_gap_k=0.25, max_steps=1 | one_step's raw-material gains plus firm_close's P02 gain, without deep anchoring. | 10 | +0.022 | 3 / 0 | better overall |

## Score per case and variant (code)

Mean score (games); **bold** = best for the case.

| Case | Type / goal | base | soft_anchor | deep_anchor | fast_concede | slow_concede | eager_close | deep_fast | mid_anchor | firm_close | firm_deep_fast | measured_meet | no_meet | weight_order | no_reopen | date_first | one_step | measured_firm | deep_one_step | firm_one_step |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| case:P01 | price_increase / stabilise | 0.808 (2) | 0.781 (1) | 0.808 (1) | 0.808 (1) | 0.808 (1) | 0.719 (1) | 0.808 (1) | 0.808 (1) | 0.808 (3) | 0.808 (1) | **0.859 (2)** | **0.859 (1)** | 0.808 (1) | 0.808 (1) | 0.808 (1) | 0.808 (1) | **0.859 (1)** | 0.808 (1) | 0.808 (1) |
| case:P02 | price_increase / stabilise | 0.390 (2) | 0.264 (1) | 0.390 (1) | 0.394 (1) | 0.390 (1) | 0.264 (1) | 0.390 (1) | 0.390 (1) | **0.507 (5)** | **0.507 (1)** | 0.390 (1) | 0.390 (1) | 0.390 (1) | 0.390 (1) | 0.390 (1) | 0.390 (1) | **0.507 (1)** | 0.390 (1) | **0.507 (2)** |
| case:P03 | renewal / stabilise | **0.919 (3)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (3)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** |
| case:P04 | renewal / routine | **0.874 (5)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (2)** |
| case:P05 | rfq / leverage RFQ | **0.757 (5)** | 0.363 (1) | **0.757 (1)** | **0.757 (3)** | **0.757 (1)** | 0.661 (1) | **0.757 (1)** | 0.676 (1) | **0.757 (1)** | 0.573 (1) | 0.676 (1) | 0.625 (1) | **0.757 (1)** | **0.757 (1)** | **0.757 (1)** | 0.625 (1) | 0.676 (1) | **0.757 (1)** | **0.757 (1)** |
| case:P07 | bundling / routine | 0.753 (2) | 0.637 (1) | 0.717 (1) | 0.740 (1) | 0.728 (1) | 0.695 (1) | **0.763 (5)** | 0.734 (1) | 0.753 (1) | **0.763 (1)** | 0.753 (1) | 0.753 (1) | 0.753 (1) | 0.733 (1) | 0.733 (1) | 0.753 (1) | 0.753 (1) | 0.717 (1) | 0.753 (2) |
| case:P08 | raw_material / leverage | 0.305 (2) | 0.441 (1) | 0.560 (2) | 0.438 (1) | 0.305 (1) | 0.305 (1) | 0.560 (1) | 0.305 (1) | 0.305 (1) | 0.560 (1) | 0.305 (1) | 0.305 (1) | 0.305 (1) | 0.305 (1) | 0.305 (1) | 0.361 (1) | 0.305 (1) | **0.593 (2)** | 0.361 (2) |
| case:P10 | capacity / secure_supply | **0.663 (4)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (2)** |
| case:P11 | quality / quality_first | **0.710 (6)** | 0.543 (1) | 0.626 (1) | **0.710 (1)** | **0.710 (1)** | **0.710 (1)** | 0.626 (1) | 0.626 (1) | **0.710 (1)** | 0.626 (1) | **0.710 (1)** | **0.710 (1)** | **0.710 (1)** | **0.710 (1)** | 0.626 (1) | **0.710 (1)** | **0.710 (1)** | 0.626 (1) | **0.710 (2)** |
| case:P18 | raw_material / leverage | 0.467 (2) | 0.252 (1) | 0.386 (1) | 0.467 (3) | 0.467 (1) | 0.386 (1) | 0.453 (1) | 0.453 (1) | 0.467 (1) | 0.453 (1) | 0.319 (1) | 0.319 (1) | 0.467 (1) | 0.467 (1) | 0.467 (1) | **0.513 (2)** | 0.319 (1) | 0.435 (1) | **0.513 (1)** |
| **mean** | | 0.665 | 0.574 | 0.670 | 0.677 | 0.662 | 0.620 | 0.681 | 0.645 | 0.676 | 0.675 | 0.647 | 0.642 | 0.665 | 0.663 | 0.654 | 0.662 | 0.659 | 0.678 | 0.686 |

## Gain over base by group

| Group | Cases | Best variant | Its mean gain | All variants (mean gain) |
|---|---:|---|---:|---|
| bundling / routine | 1 | deep_fast | +0.009 | deep_fast +0.009, firm_deep_fast +0.009, firm_close +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, one_step +0.000, measured_firm +0.000, firm_one_step +0.000, fast_concede -0.013, mid_anchor -0.019, no_reopen -0.021, date_first -0.021, slow_concede -0.026, deep_anchor -0.037, deep_one_step -0.037, eager_close -0.058, soft_anchor -0.117 |
| capacity / secure_supply | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| goal leverage | 3 | deep_one_step | +0.085 | deep_one_step +0.085, deep_fast +0.080, deep_anchor +0.058, fast_concede +0.044, firm_one_step +0.034, firm_deep_fast +0.019, slow_concede +0.000, firm_close +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step -0.010, mid_anchor -0.032, eager_close -0.059, measured_meet -0.076, measured_firm -0.076, no_meet -0.093, soft_anchor -0.158 |
| goal quality_first | 1 | base | +0.000 | fast_concede +0.000, slow_concede +0.000, eager_close +0.000, firm_close +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, one_step +0.000, measured_firm +0.000, firm_one_step +0.000, deep_anchor -0.084, deep_fast -0.084, mid_anchor -0.084, firm_deep_fast -0.084, date_first -0.084, deep_one_step -0.084, soft_anchor -0.167 |
| goal routine | 2 | base | +0.000 | deep_fast +0.005, firm_deep_fast +0.005, firm_close +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, one_step +0.000, measured_firm +0.000, firm_one_step +0.000, fast_concede -0.006, mid_anchor -0.010, no_reopen -0.010, date_first -0.010, slow_concede -0.013, deep_anchor -0.018, deep_one_step -0.018, eager_close -0.029, soft_anchor -0.058 |
| goal secure_supply | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| goal stabilise | 3 | measured_firm | +0.056 | measured_firm +0.056, firm_close +0.039, firm_deep_fast +0.039, firm_one_step +0.039, measured_meet +0.017, no_meet +0.017, fast_concede +0.001, deep_anchor +0.000, slow_concede +0.000, deep_fast +0.000, mid_anchor +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, deep_one_step +0.000, soft_anchor -0.051, eager_close -0.072 |
| price_increase / stabilise | 2 | measured_firm | +0.084 | measured_firm +0.084, firm_close +0.058, firm_deep_fast +0.058, firm_one_step +0.058, measured_meet +0.025, no_meet +0.025, fast_concede +0.002, deep_anchor +0.000, slow_concede +0.000, deep_fast +0.000, mid_anchor +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, deep_one_step +0.000, soft_anchor -0.077, eager_close -0.107 |
| quality / quality_first | 1 | base | +0.000 | fast_concede +0.000, slow_concede +0.000, eager_close +0.000, firm_close +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, one_step +0.000, measured_firm +0.000, firm_one_step +0.000, deep_anchor -0.084, deep_fast -0.084, mid_anchor -0.084, firm_deep_fast -0.084, date_first -0.084, deep_one_step -0.084, soft_anchor -0.167 |
| raw_material / leverage | 2 | deep_one_step | +0.128 | deep_one_step +0.128, deep_fast +0.120, firm_deep_fast +0.120, deep_anchor +0.087, fast_concede +0.066, one_step +0.051, firm_one_step +0.051, slow_concede +0.000, firm_close +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, mid_anchor -0.007, soft_anchor -0.040, eager_close -0.040, measured_meet -0.074, no_meet -0.074, measured_firm -0.074 |
| renewal / routine | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| renewal / stabilise | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| rfq / leverage | 1 | base | +0.000 | deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, deep_fast +0.000, firm_close +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, deep_one_step +0.000, firm_one_step +0.000, mid_anchor -0.081, measured_meet -0.081, measured_firm -0.081, eager_close -0.096, no_meet -0.132, one_step -0.132, firm_deep_fast -0.184, soft_anchor -0.394 |
| type bundling | 1 | deep_fast | +0.009 | deep_fast +0.009, firm_deep_fast +0.009, firm_close +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, one_step +0.000, measured_firm +0.000, firm_one_step +0.000, fast_concede -0.013, mid_anchor -0.019, no_reopen -0.021, date_first -0.021, slow_concede -0.026, deep_anchor -0.037, deep_one_step -0.037, eager_close -0.058, soft_anchor -0.117 |
| type capacity | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| type price_increase | 2 | measured_firm | +0.084 | measured_firm +0.084, firm_close +0.058, firm_deep_fast +0.058, firm_one_step +0.058, measured_meet +0.025, no_meet +0.025, fast_concede +0.002, deep_anchor +0.000, slow_concede +0.000, deep_fast +0.000, mid_anchor +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, deep_one_step +0.000, soft_anchor -0.077, eager_close -0.107 |
| type quality | 1 | base | +0.000 | fast_concede +0.000, slow_concede +0.000, eager_close +0.000, firm_close +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, one_step +0.000, measured_firm +0.000, firm_one_step +0.000, deep_anchor -0.084, deep_fast -0.084, mid_anchor -0.084, firm_deep_fast -0.084, date_first -0.084, deep_one_step -0.084, soft_anchor -0.167 |
| type raw_material | 2 | deep_one_step | +0.128 | deep_one_step +0.128, deep_fast +0.120, firm_deep_fast +0.120, deep_anchor +0.087, fast_concede +0.066, one_step +0.051, firm_one_step +0.051, slow_concede +0.000, firm_close +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, mid_anchor -0.007, soft_anchor -0.040, eager_close -0.040, measured_meet -0.074, no_meet -0.074, measured_firm -0.074 |
| type renewal | 2 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| type rfq | 1 | base | +0.000 | deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, deep_fast +0.000, firm_close +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, deep_one_step +0.000, firm_one_step +0.000, mid_anchor -0.081, measured_meet -0.081, measured_firm -0.081, eager_close -0.096, no_meet -0.132, one_step -0.132, firm_deep_fast -0.184, soft_anchor -0.394 |

## What the agent will play

`learned`: the pick for this practice case; `unseen`: the pick if the case were new (its own games left out) - what a hidden case of the same kind gets.

| Case | Base score | Learned pick | Why | Unseen pick | Why |
|---|---:|---|---|---|---|
| case:P01 | 0.808 | measured_meet | same case: measured_meet (mean gain +0.051 over base across 1 case) | firm_close | type price_increase + goal stabilise: firm_close (mean gain +0.117 over base across 1 case) |
| case:P02 | 0.390 | firm_close | same case: firm_close (mean gain +0.117 over base across 1 case) | firm_one_step | all cases: firm_one_step (mean gain +0.011 over base across 9 cases) |
| case:P03 | 0.919 | base | same case: base (mean gain +0.000 over base across 1 case) | firm_close | goal stabilise: firm_close (mean gain +0.058 over base across 2 cases) |
| case:P04 | 0.874 | base | same case: base (mean gain +0.000 over base across 1 case) | firm_one_step | all cases: firm_one_step (mean gain +0.024 over base across 9 cases) |
| case:P05 | 0.757 | base | same case: base (mean gain +0.000 over base across 1 case) | fast_concede | goal leverage: fast_concede (mean gain +0.066 over base across 2 cases) |
| case:P07 | 0.753 | deep_fast | same case: deep_fast (mean gain +0.009 over base across 1 case) | firm_one_step | all cases: firm_one_step (mean gain +0.024 over base across 9 cases) |
| case:P08 | 0.305 | deep_one_step | same case: deep_one_step (mean gain +0.288 over base across 1 case) | firm_one_step | type raw_material + goal leverage: firm_one_step (mean gain +0.046 over base across 1 case) |
| case:P10 | 0.663 | base | same case: base (mean gain +0.000 over base across 1 case) | firm_one_step | all cases: firm_one_step (mean gain +0.024 over base across 9 cases) |
| case:P11 | 0.710 | base | same case: base (mean gain +0.000 over base across 1 case) | firm_one_step | all cases: firm_one_step (mean gain +0.024 over base across 9 cases) |
| case:P18 | 0.467 | one_step | same case: one_step (mean gain +0.046 over base across 1 case) | fast_concede | type raw_material + goal leverage: fast_concede (mean gain +0.133 over base across 1 case) |

## Lessons

- leverage: **deep_one_step** helped (+0.085 on average, better in 1 of 3 cases).
- leverage: **deep_fast** helped (+0.080 on average, better in 1 of 3 cases).
- leverage: **deep_anchor** helped (+0.058 on average, better in 1 of 3 cases, but lost one case by -0.081 - not chosen).
- leverage: **fast_concede** helped (+0.044 on average, better in 1 of 3 cases).
- leverage: **firm_one_step** helped (+0.034 on average, better in 2 of 3 cases).
- leverage: **firm_deep_fast** helped (+0.019 on average, better in 1 of 3 cases, but lost one case by -0.184 - not chosen).
- leverage: mid_anchor hurt (-0.032 on average) - avoid.
- leverage: eager_close hurt (-0.059 on average) - avoid.
- leverage: measured_meet hurt (-0.076 on average) - avoid.
- leverage: measured_firm hurt (-0.076 on average) - avoid.
- leverage: no_meet hurt (-0.093 on average) - avoid.
- leverage: soft_anchor hurt (-0.158 on average) - avoid.
- quality_first: deep_anchor hurt (-0.084 on average) - avoid.
- quality_first: deep_fast hurt (-0.084 on average) - avoid.
- quality_first: mid_anchor hurt (-0.084 on average) - avoid.
- quality_first: firm_deep_fast hurt (-0.084 on average) - avoid.
- quality_first: date_first hurt (-0.084 on average) - avoid.
- quality_first: deep_one_step hurt (-0.084 on average) - avoid.
- quality_first: soft_anchor hurt (-0.167 on average) - avoid.
- routine: eager_close hurt (-0.029 on average) - avoid.
- routine: soft_anchor hurt (-0.058 on average) - avoid.
- stabilise: **measured_firm** helped (+0.056 on average, better in 2 of 3 cases).
- stabilise: **firm_close** helped (+0.039 on average, better in 1 of 3 cases).
- stabilise: **firm_deep_fast** helped (+0.039 on average, better in 1 of 3 cases).
- stabilise: **firm_one_step** helped (+0.039 on average, better in 1 of 3 cases).
- stabilise: **measured_meet** helped (+0.017 on average, better in 1 of 3 cases).
- stabilise: **no_meet** helped (+0.017 on average, better in 1 of 3 cases).
- stabilise: soft_anchor hurt (-0.051 on average) - avoid.
- stabilise: eager_close hurt (-0.072 on average) - avoid.

## Batches

| Batch | Label | Mode | Games | Mean score | Source |
|---|---|---|---:|---:|---|
| 1011-080739_smoke | smoke | code | 1 | 0.874 | 9d70d1ce2d70 |
| 1011-080828_sweep-1 | sweep-1 | code | 60 | 0.645 | 9d70d1ce2d70 |
| 1011-081928_sweep-2 | sweep-2 | code | 30 | 0.668 | d5e8a024ffd7 |
| 1011-082432_sweep-3 | sweep-3 | code | 10 | 0.675 | 7f1e68cd8850 |
| 1011-082724_learned-1 | learned-1 | code | 10 | 0.703 | ca4cbc0b9396 |
| 1011-082802_holdout-1 | holdout-1 | code | 10 | 0.665 | ca4cbc0b9396 |
| 1011-084158_sweep-4 | sweep-4 | code | 70 | 0.656 | 6a443e996b91 |
| 1011-085057_release-sunday-x2 | release-sunday-x2 | code | 8 | 0.684 | 6a443e996b91 |
| 1011-085252_sweep-5 | sweep-5 | code | 20 | 0.682 | d24d6ddbe63c |
| 1011-085641_learned-2 | learned-2 | code | 10 | 0.716 | 09d973225baf |
| 1011-085655_holdout-2 | holdout-2 | code | 10 | 0.682 | 09d973225baf |
