# Run 1011-0029_sim-dual-learning

agent `dual`, simulator, 3 cases, **mean score 71.0**

| case | goal | deal | score | vs prev | C deal | C best | rounds | replay |
|---|---|---|---|---|---|---|---|---|
| case:S01 Adapter piece HI: a price increase | leverage | True | 0.77 |  | 109036.5 | 105969.08 | 5 | [replay](http://127.0.0.1:58230/replay/sim2) |
| case:S02 Motor bracket HB: two quotes | leverage | True | 0.78 |  | 594139.8 | 577486.85 | 5 | [replay](http://127.0.0.1:58230/replay/sim3) |
| case:S03 Motor IE3 4 kW: framework renewal | routine | True | 0.58 |  | 3935115.0 | 3636482.08 | 3 | [replay](http://127.0.0.1:58230/replay/sim1) |

## Per case: levers left on the table and the supplier's reasons

### case:S01: score 0.77, deal True
Closed on: `{"volume_per_year": 6650, "contract_months": 12, "payment_days": 30, "lead_time_days": 42, "incoterm": "FCA", "index_clause": null, "tooling_eur": 0, "unit_price": 15.97}`
- missed: payment_days 30 -> 90 would have saved 1,416 EUR
- missed: unit_price 15.97 -> 15.81 would have saved 1,071 EUR
- supplier r1 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r2 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r3 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r4 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r5 agreed: A fixed script: it concedes the same step per term sheet, down to its floor.

### case:S02: score 0.78, deal True
Closed on: `{"volume_per_year": 18790, "contract_months": 24, "payment_days": 60, "lead_time_days": 21, "incoterm": "DAP", "index_clause": null, "tooling_eur": 0, "unit_price": 15.81}`
- missed: unit_price 15.81 -> 15.65 would have saved 6,013 EUR
- missed: payment_days 60 -> 90 would have saved 3,961 EUR
- supplier r1 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r1 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r2 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r2 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r3 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r3 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r4 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r4 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r5 agreed: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r5 agreed: A fixed script: it concedes the same step per term sheet, down to its floor.

### case:S03: score 0.58, deal True
Closed on: `{"volume_per_year": 8490, "contract_months": 24, "payment_days": 60, "lead_time_days": 35, "incoterm": "EXW", "index_clause": null, "tooling_eur": 0, "unit_price": 225.22}`
- missed: incoterm EXW -> DAP would have saved 110,879 EUR
- missed: unit_price 225.22 -> 222.97 would have saved 38,205 EUR
- missed: payment_days 60 -> 90 would have saved 25,495 EUR
- supplier r1 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r2 open: A fixed script: it concedes the same step per term sheet, down to its floor.
- supplier r3 agreed: A fixed script: it concedes the same step per term sheet, down to its floor.
