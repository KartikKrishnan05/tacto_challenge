# SpecAgent experiment and result sheet

Built 2026-10-11T10:35:04 from **311 games** (0 failed or unscored rows left out) on 10 practice cases, modes: code, model. Regenerate with `python -m arena_kit.spec.lab build`; every game is a line of `experiments.jsonl`, its full session in the `record` path. The agent reads `learned_playbook.json` when it prepares a case (`spec.strategy.choose`).

Rule: a variant replaces base for a group of cases when its mean score gain over base is at least 0.005 and it lost no case in the group by more than 0.05; beyond the same case it must also have lost no tested case by more than 0.05, and at the all-cases level it must have won 2+ cases and lost none. Ties go to the variant closest to base. Code-only games repeat almost exactly (same e-mails, same supplier replies), so one code-only game per case and variant is a measurement, not a sample; games with the model vary and are averaged.

## Hypotheses and verdicts

| Variant | Knobs vs base | Hypothesis | Cases | Mean gain | Won / lost | Verdict |
|---|---|---|---:|---:|---|---|
| base | — | The agent as designed (control). | 10 | — | — | control |
| soft_anchor | cut_k=0.5, anchor_cost_k=1.02 | Opening closer to the supplier's price keeps it cooperative and avoids walk-aways in relationship cases (stabilise, secure_supply). | 10 | -0.091 | 1 / 6 | helps some cases only |
| deep_anchor | cut_k=1.6, anchor_cost_k=0.95 | A lower opening drags the settlement down where we have leverage (rivals, market data). | 10 | -0.011 | 2 / 5 | helps some cases only |
| fast_concede | horizon_delta=-2, curve_exp=1.2, break_share=0.25, reciprocity=0.75 | Bigger, earlier steps reach a deal before the supplier hardens or issues a final offer. | 10 | +0.013 | 4 / 2 | better overall |
| slow_concede | horizon_delta=2, curve_exp=2.2, break_share=0.08, reciprocity=0.35 | Smaller steps and a longer horizon extract more from suppliers that keep moving. | 10 | -0.002 | 2 / 4 | helps some cases only |
| eager_close | meet_share=0.75, close_gap_k=4.0 | Accepting once the supplier is within ~2 % of our target avoids losing deals to one counter too many. | 10 | -0.045 | 0 / 6 | rejected |
| deep_fast | cut_k=1.6, anchor_cost_k=0.95, horizon_delta=-2, curve_exp=1.2, break_share=0.25, reciprocity=0.75 | Deep anchor and fast concession together: the low opening sets the range, the big steps keep the supplier engaged (both helped P08 alone). | 10 | +0.017 | 4 / 3 | helps some cases only |
| mid_anchor | cut_k=1.3, anchor_cost_k=0.97 | A moderately deeper anchor keeps deep_anchor's gain (P08) without its loss on P18. | 10 | -0.019 | 1 / 6 | helps some cases only |
| firm_close | close_gap_k=0.25 | Since accepting early hurt (eager_close), holding out for a closer match to our target gains more. | 10 | +0.011 | 4 / 2 | better overall |
| firm_deep_fast | cut_k=1.6, anchor_cost_k=0.95, horizon_delta=-2, curve_exp=1.2, break_share=0.25, reciprocity=0.75, close_gap_k=0.25 | firm_close's gain (P02) and deep_fast's gains (P07, P08) add up in one strategy. | 10 | +0.011 | 4 / 3 | helps some cases only |
| measured_meet | meet_share=0.2 | When the supplier jumps further than the gap left, closing only 20 % of the gap (not 50 %) keeps us from overshooting: P01's supplier 'agreed' at once to our half-way offer. | 10 | -0.019 | 2 / 4 | helps some cases only |
| no_meet | meet_share=0.0 | Pure reciprocity (half of their step, capped by our curve) without any 'meet it' rule. | 10 | -0.024 | 2 / 4 | helps some cases only |
| weight_order | step_order=weight | Settling the term worth most EUR first (P01: the start date, EUR 25k) beats always opening with payment days. | 10 | +0.000 | 3 / 3 | helps some cases only |
| no_reopen | reopen=False | Moving on to the next term instead of a second menu on a locked costly one gets more terms onto the table (P01 re-asked payment days and never offered April). | 10 | -0.002 | 2 / 4 | helps some cases only |
| date_first | step_order=weight, reopen=False | weight_order and no_reopen together. | 10 | -0.010 | 2 / 5 | helps some cases only |
| one_step | max_steps=1 | One term menu, then price rounds: fewer rounds spent on terms the supplier answers in its own favour. | 10 | -0.003 | 3 / 3 | helps some cases only |
| measured_firm | meet_share=0.2, close_gap_k=0.25 | measured_meet plus firm_close (the only variant with no loss so far). | 10 | +0.010 | 3 / 3 | helps some cases only |
| deep_one_step | cut_k=1.6, anchor_cost_k=0.95, max_steps=1 | deep_anchor's P08 gain and one_step's raw-material gains (P08, P18) add up. | 10 | +0.009 | 2 / 5 | helps some cases only |
| firm_one_step | close_gap_k=0.25, max_steps=1 | one_step's raw-material gains plus firm_close's P02 gain, without deep anchoring. | 10 | +0.014 | 5 / 1 | better overall |

## Score per case and variant (code)

Mean score (games); **bold** = best for the case.

| Case | Type / goal | base | soft_anchor | deep_anchor | fast_concede | slow_concede | eager_close | deep_fast | mid_anchor | firm_close | firm_deep_fast | measured_meet | no_meet | weight_order | no_reopen | date_first | one_step | measured_firm | deep_one_step | firm_one_step |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| case:P01 | price_increase / stabilise | 0.818 (3) | 0.781 (1) | 0.808 (1) | 0.808 (1) | 0.808 (1) | 0.719 (1) | 0.808 (1) | 0.808 (1) | 0.808 (3) | 0.808 (1) | 0.846 (5) | **0.848 (2)** | 0.808 (1) | 0.808 (1) | 0.808 (1) | 0.808 (1) | **0.848 (2)** | 0.808 (1) | 0.808 (1) |
| case:P02 | price_increase / stabilise | 0.391 (3) | 0.264 (1) | 0.390 (1) | 0.394 (1) | 0.390 (1) | 0.264 (1) | 0.390 (1) | 0.390 (1) | 0.504 (6) | 0.507 (1) | 0.390 (1) | 0.390 (1) | 0.390 (1) | 0.390 (1) | 0.390 (1) | 0.390 (1) | **0.678 (4)** | 0.390 (1) | 0.507 (3) |
| case:P03 | renewal / stabilise | **0.919 (8)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (3)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** | **0.919 (1)** |
| case:P04 | renewal / routine | **0.874 (10)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (1)** | **0.874 (2)** |
| case:P05 | rfq / leverage RFQ | 0.734 (7) | 0.363 (1) | **0.757 (1)** | **0.757 (3)** | **0.757 (1)** | 0.661 (1) | **0.757 (1)** | 0.676 (1) | **0.757 (2)** | 0.573 (1) | 0.676 (1) | 0.625 (1) | **0.757 (1)** | **0.757 (3)** | **0.757 (1)** | 0.625 (1) | 0.676 (1) | **0.757 (1)** | **0.757 (1)** |
| case:P07 | bundling / routine | 0.748 (3) | 0.637 (1) | 0.717 (1) | 0.740 (1) | 0.728 (1) | 0.695 (1) | 0.762 (7) | 0.734 (1) | 0.753 (1) | **0.763 (3)** | 0.753 (1) | 0.753 (1) | 0.753 (1) | 0.733 (1) | 0.733 (1) | 0.753 (1) | 0.753 (1) | 0.717 (1) | 0.753 (2) |
| case:P08 | raw_material / leverage | 0.346 (3) | 0.441 (1) | **0.560 (2)** | 0.438 (1) | 0.305 (1) | 0.305 (1) | **0.560 (3)** | 0.305 (1) | 0.305 (1) | **0.560 (1)** | 0.305 (1) | 0.305 (1) | 0.305 (1) | 0.305 (1) | 0.305 (1) | 0.361 (1) | 0.305 (1) | 0.549 (4) | 0.361 (2) |
| case:P10 | capacity / secure_supply | **0.663 (9)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (1)** | **0.663 (2)** |
| case:P11 | quality / quality_first | **0.710 (11)** | 0.543 (1) | 0.626 (1) | **0.710 (1)** | **0.710 (1)** | **0.710 (1)** | 0.626 (1) | 0.626 (1) | **0.710 (1)** | 0.626 (1) | **0.710 (1)** | **0.710 (1)** | **0.710 (1)** | **0.710 (1)** | 0.626 (1) | **0.710 (1)** | **0.710 (1)** | 0.626 (1) | **0.710 (2)** |
| case:P18 | raw_material / leverage | 0.440 (3) | 0.252 (1) | 0.386 (1) | 0.467 (3) | 0.467 (1) | 0.386 (1) | 0.453 (1) | 0.453 (1) | 0.467 (1) | 0.453 (1) | 0.319 (1) | 0.319 (1) | 0.467 (1) | 0.467 (1) | 0.467 (1) | **0.504 (5)** | 0.319 (1) | 0.435 (1) | 0.457 (2) |
| **mean** | | 0.664 | 0.574 | 0.670 | 0.677 | 0.662 | 0.620 | 0.681 | 0.645 | 0.676 | 0.675 | 0.646 | 0.641 | 0.665 | 0.663 | 0.654 | 0.661 | 0.675 | 0.674 | 0.681 |

## Score per case and variant (model)

Mean score (games); **bold** = best for the case.

| Case | Type / goal | base | soft_anchor | deep_anchor | fast_concede | slow_concede | eager_close | deep_fast | mid_anchor | firm_close | firm_deep_fast | measured_meet | no_meet | weight_order | no_reopen | date_first | one_step | measured_firm | deep_one_step | firm_one_step |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| case:P01 | price_increase / stabilise | 0.631 (1) | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | **0.655 (1)** | — | — |
| case:P02 | price_increase / stabilise | 0.435 (1) | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | **0.507 (1)** |
| case:P03 | renewal / stabilise | **0.919 (2)** | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |
| case:P04 | renewal / routine | **0.874 (2)** | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |
| case:P05 | rfq / leverage RFQ | **0.757 (1)** | — | — | — | — | — | — | — | **0.757 (1)** | — | — | — | — | — | — | — | — | — | — |
| case:P07 | bundling / routine | 0.728 (1) | — | — | — | — | — | — | — | — | **0.763 (1)** | — | — | — | — | — | — | — | — | — |
| case:P08 | raw_material / leverage | **0.427 (1)** | — | 0.310 (1) | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |
| case:P10 | capacity / secure_supply | **0.647 (2)** | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |
| case:P11 | quality / quality_first | **0.710 (2)** | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |
| case:P18 | raw_material / leverage | 0.386 (1) | — | — | — | — | — | — | — | — | — | — | — | — | — | — | **0.467 (1)** | — | — | — |
| **mean** | | 0.651 | — | 0.310 | — | — | — | — | — | 0.757 | 0.763 | — | — | — | — | — | 0.467 | 0.655 | — | 0.507 |

## Gain over base by group

| Group | Cases | Best variant | Its mean gain | All variants (mean gain) |
|---|---:|---|---:|---|
| bundling / routine | 1 | firm_deep_fast | +0.025 | firm_deep_fast +0.025, deep_fast +0.014, firm_close +0.006, measured_meet +0.006, no_meet +0.006, weight_order +0.006, one_step +0.006, measured_firm +0.006, firm_one_step +0.006, fast_concede -0.007, mid_anchor -0.014, no_reopen -0.015, date_first -0.015, slow_concede -0.020, deep_anchor -0.031, deep_one_step -0.031, eager_close -0.053, soft_anchor -0.111 |
| capacity / secure_supply | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| goal leverage | 3 | deep_fast | +0.083 | deep_fast +0.083, deep_one_step +0.073, fast_concede +0.047, firm_deep_fast +0.022, firm_one_step +0.018, deep_anchor +0.006, slow_concede +0.003, weight_order +0.003, no_reopen +0.003, date_first +0.003, firm_close -0.001, one_step -0.008, mid_anchor -0.029, eager_close -0.056, measured_meet -0.073, measured_firm -0.073, no_meet -0.090, soft_anchor -0.155 |
| goal quality_first | 1 | base | +0.000 | fast_concede +0.000, slow_concede +0.000, eager_close +0.000, firm_close +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, one_step +0.000, measured_firm +0.000, firm_one_step +0.000, deep_anchor -0.084, deep_fast -0.084, mid_anchor -0.084, firm_deep_fast -0.084, date_first -0.084, deep_one_step -0.084, soft_anchor -0.167 |
| goal routine | 2 | firm_deep_fast | +0.012 | firm_deep_fast +0.012, deep_fast +0.007, firm_close +0.003, measured_meet +0.003, no_meet +0.003, weight_order +0.003, one_step +0.003, measured_firm +0.003, firm_one_step +0.003, fast_concede -0.004, mid_anchor -0.007, no_reopen -0.008, date_first -0.008, slow_concede -0.010, deep_anchor -0.015, deep_one_step -0.015, eager_close -0.026, soft_anchor -0.056 |
| goal secure_supply | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| goal stabilise | 3 | measured_firm | +0.105 | measured_firm +0.105, firm_deep_fast +0.035, firm_close +0.034, firm_one_step +0.028, no_meet +0.010, measured_meet +0.009, fast_concede -0.002, deep_anchor -0.004, slow_concede -0.004, deep_fast -0.004, mid_anchor -0.004, weight_order -0.004, no_reopen -0.004, date_first -0.004, one_step -0.004, deep_one_step -0.004, soft_anchor -0.055, eager_close -0.075 |
| price_increase / stabilise | 2 | measured_firm | +0.157 | measured_firm +0.157, firm_deep_fast +0.053, firm_close +0.052, firm_one_step +0.042, no_meet +0.014, measured_meet +0.013, fast_concede -0.003, deep_anchor -0.005, slow_concede -0.005, deep_fast -0.005, mid_anchor -0.005, weight_order -0.005, no_reopen -0.005, date_first -0.005, one_step -0.005, deep_one_step -0.005, soft_anchor -0.082, eager_close -0.113 |
| quality / quality_first | 1 | base | +0.000 | fast_concede +0.000, slow_concede +0.000, eager_close +0.000, firm_close +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, one_step +0.000, measured_firm +0.000, firm_one_step +0.000, deep_anchor -0.084, deep_fast -0.084, mid_anchor -0.084, firm_deep_fast -0.084, date_first -0.084, deep_one_step -0.084, soft_anchor -0.167 |
| raw_material / leverage | 2 | deep_fast | +0.114 | deep_fast +0.114, firm_deep_fast +0.114, deep_one_step +0.099, fast_concede +0.060, one_step +0.044, firm_one_step +0.016, deep_anchor -0.003, slow_concede -0.007, firm_close -0.007, weight_order -0.007, no_reopen -0.007, date_first -0.007, mid_anchor -0.014, soft_anchor -0.047, eager_close -0.047, measured_meet -0.081, no_meet -0.081, measured_firm -0.081 |
| renewal / routine | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| renewal / stabilise | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| rfq / leverage | 1 | no_reopen | +0.023 | deep_anchor +0.023, fast_concede +0.023, slow_concede +0.023, deep_fast +0.023, weight_order +0.023, no_reopen +0.023, date_first +0.023, deep_one_step +0.023, firm_one_step +0.023, firm_close +0.011, mid_anchor -0.058, measured_meet -0.058, measured_firm -0.058, eager_close -0.073, no_meet -0.110, one_step -0.110, firm_deep_fast -0.161, soft_anchor -0.371 |
| type bundling | 1 | firm_deep_fast | +0.025 | firm_deep_fast +0.025, deep_fast +0.014, firm_close +0.006, measured_meet +0.006, no_meet +0.006, weight_order +0.006, one_step +0.006, measured_firm +0.006, firm_one_step +0.006, fast_concede -0.007, mid_anchor -0.014, no_reopen -0.015, date_first -0.015, slow_concede -0.020, deep_anchor -0.031, deep_one_step -0.031, eager_close -0.053, soft_anchor -0.111 |
| type capacity | 1 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| type price_increase | 2 | measured_firm | +0.157 | measured_firm +0.157, firm_deep_fast +0.053, firm_close +0.052, firm_one_step +0.042, no_meet +0.014, measured_meet +0.013, fast_concede -0.003, deep_anchor -0.005, slow_concede -0.005, deep_fast -0.005, mid_anchor -0.005, weight_order -0.005, no_reopen -0.005, date_first -0.005, one_step -0.005, deep_one_step -0.005, soft_anchor -0.082, eager_close -0.113 |
| type quality | 1 | base | +0.000 | fast_concede +0.000, slow_concede +0.000, eager_close +0.000, firm_close +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, one_step +0.000, measured_firm +0.000, firm_one_step +0.000, deep_anchor -0.084, deep_fast -0.084, mid_anchor -0.084, firm_deep_fast -0.084, date_first -0.084, deep_one_step -0.084, soft_anchor -0.167 |
| type raw_material | 2 | deep_fast | +0.114 | deep_fast +0.114, firm_deep_fast +0.114, deep_one_step +0.099, fast_concede +0.060, one_step +0.044, firm_one_step +0.016, deep_anchor -0.003, slow_concede -0.007, firm_close -0.007, weight_order -0.007, no_reopen -0.007, date_first -0.007, mid_anchor -0.014, soft_anchor -0.047, eager_close -0.047, measured_meet -0.081, no_meet -0.081, measured_firm -0.081 |
| type renewal | 2 | base | +0.000 | soft_anchor +0.000, deep_anchor +0.000, fast_concede +0.000, slow_concede +0.000, eager_close +0.000, deep_fast +0.000, mid_anchor +0.000, firm_close +0.000, firm_deep_fast +0.000, measured_meet +0.000, no_meet +0.000, weight_order +0.000, no_reopen +0.000, date_first +0.000, one_step +0.000, measured_firm +0.000, deep_one_step +0.000, firm_one_step +0.000 |
| type rfq | 1 | no_reopen | +0.023 | deep_anchor +0.023, fast_concede +0.023, slow_concede +0.023, deep_fast +0.023, weight_order +0.023, no_reopen +0.023, date_first +0.023, deep_one_step +0.023, firm_one_step +0.023, firm_close +0.011, mid_anchor -0.058, measured_meet -0.058, measured_firm -0.058, eager_close -0.073, no_meet -0.110, one_step -0.110, firm_deep_fast -0.161, soft_anchor -0.371 |

## What the agent will play

`learned`: the pick for this practice case; `unseen`: the pick if the case were new (its own games left out) - what a hidden case of the same kind gets.

| Case | Base score | Learned pick | Why | Unseen pick | Why |
|---|---:|---|---|---|---|
| case:P01 | 0.725 | no_meet | same case: no_meet (mean gain +0.030 over base across 1 case) | firm_close | type price_increase + goal stabilise: firm_close (mean gain +0.112 over base across 1 case) |
| case:P02 | 0.413 | measured_firm | same case: measured_firm (mean gain +0.287 over base across 1 case) | base | type price_increase + goal stabilise: no variant beat base across 1 case |
| case:P03 | 0.919 | base | same case: base (mean gain +0.000 over base across 1 case) | firm_close | goal stabilise: firm_close (mean gain +0.052 over base across 2 cases) |
| case:P04 | 0.874 | base | same case: base (mean gain +0.000 over base across 1 case) | firm_close | goal routine: firm_close (mean gain +0.006 over base across 1 case) |
| case:P05 | 0.746 | no_reopen | same case: no_reopen (mean gain +0.023 over base across 1 case) | fast_concede | goal leverage: fast_concede (mean gain +0.060 over base across 2 cases) |
| case:P07 | 0.738 | firm_deep_fast | same case: firm_deep_fast (mean gain +0.025 over base across 1 case) | base | goal routine: no variant beat base across 1 case |
| case:P08 | 0.386 | deep_fast | same case: deep_fast (mean gain +0.214 over base across 1 case) | firm_close | type raw_material + goal leverage: firm_close (mean gain +0.027 over base across 1 case) |
| case:P10 | 0.655 | base | same case: base (mean gain +0.000 over base across 1 case) | base | all cases: no variant beat base across 9 cases |
| case:P11 | 0.710 | base | same case: base (mean gain +0.000 over base across 1 case) | base | all cases: no variant beat base across 9 cases |
| case:P18 | 0.413 | one_step | same case: one_step (mean gain +0.072 over base across 1 case) | fast_concede | type raw_material + goal leverage: fast_concede (mean gain +0.092 over base across 1 case) |

## Lessons

- leverage: **deep_fast** helped (+0.083 on average, better in 3 of 3 cases).
- leverage: **deep_one_step** helped (+0.073 on average, better in 2 of 3 cases).
- leverage: **fast_concede** helped (+0.047 on average, better in 3 of 3 cases).
- leverage: **firm_deep_fast** helped (+0.022 on average, better in 2 of 3 cases, but lost one case by -0.161 - not chosen).
- leverage: **firm_one_step** helped (+0.018 on average, better in 3 of 3 cases).
- leverage: **deep_anchor** helped (+0.006 on average, better in 2 of 3 cases, but lost one case by -0.054 - not chosen).
- leverage: mid_anchor hurt (-0.029 on average) - avoid.
- leverage: eager_close hurt (-0.056 on average) - avoid.
- leverage: measured_meet hurt (-0.073 on average) - avoid.
- leverage: measured_firm hurt (-0.073 on average) - avoid.
- leverage: no_meet hurt (-0.090 on average) - avoid.
- leverage: soft_anchor hurt (-0.155 on average) - avoid.
- quality_first: deep_anchor hurt (-0.084 on average) - avoid.
- quality_first: deep_fast hurt (-0.084 on average) - avoid.
- quality_first: mid_anchor hurt (-0.084 on average) - avoid.
- quality_first: firm_deep_fast hurt (-0.084 on average) - avoid.
- quality_first: date_first hurt (-0.084 on average) - avoid.
- quality_first: deep_one_step hurt (-0.084 on average) - avoid.
- quality_first: soft_anchor hurt (-0.167 on average) - avoid.
- routine: **firm_deep_fast** helped (+0.012 on average, better in 1 of 2 cases).
- routine: **deep_fast** helped (+0.007 on average, better in 1 of 2 cases).
- routine: eager_close hurt (-0.026 on average) - avoid.
- routine: soft_anchor hurt (-0.056 on average) - avoid.
- stabilise: **measured_firm** helped (+0.105 on average, better in 2 of 3 cases).
- stabilise: **firm_deep_fast** helped (+0.035 on average, better in 1 of 3 cases).
- stabilise: **firm_close** helped (+0.034 on average, better in 1 of 3 cases).
- stabilise: **firm_one_step** helped (+0.028 on average, better in 1 of 3 cases).
- stabilise: **no_meet** helped (+0.010 on average, better in 1 of 3 cases).
- stabilise: **measured_meet** helped (+0.009 on average, better in 1 of 3 cases).
- stabilise: soft_anchor hurt (-0.055 on average) - avoid.
- stabilise: eager_close hurt (-0.075 on average) - avoid.

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
| 1011-094431_fixes-code-learned | fixes-code-learned | code | 10 | 0.682 | d9efa87c80f1 |
| 1011-094825_fixes-code-base | fixes-code-base | code | 10 | 0.670 | d9efa87c80f1 |
| 1011-095439_fixes2-code-learned | fixes2-code-learned | code | 10 | 0.693 | a4812b8b4d41 |
| 1011-095813_fixes2-model-learned | fixes2-model-learned | model | 10 | 0.661 | a4812b8b4d41 |
| 1011-100623_fixes2-model-base | fixes2-model-base | model | 10 | 0.651 | 4bd607a9c0dd |
| 1011-103115_test-date | test-date | code | 1 | 0.837 | 6190a4580929 |
| 1011-103123_test-index | test-index | code | 1 | 0.735 | 6190a4580929 |
| 1011-103228_test-index-all | test-index-all | code | 10 | 0.733 | b01c99c15b13 |
| 1011-103459_index-default-check | index-default-check | code | 10 | 0.733 | 8edc247380a7 |
