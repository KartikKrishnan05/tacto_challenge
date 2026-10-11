# SpecAgent pitch deck

## 1. AI buyer for industrial procurement

Hollerau Pumpenwerke negotiates real procurement trade-offs by email. The agent protects total cost,
must-haves and supply continuity while keeping the supplier relationship workable.

## 2. Preparation before the clock

- Build a supplier and product dossier from the case data package.
- Read contracts, historic orders, quotes, quality records, call notes, indices and company context.
- Calculate the buyer alternative and the cost of every relevant term combination.

## 3. Code decides the economics

The model does not invent prices. Python creates valid term sheets, checks must-haves and applies the
cost model. A shrinking concession curve prevents free margin giveaways. Up to three packages show
which trade the supplier values.

## 4. The email stays human

The analyst tracks the supplier's latest offer, constraints, evidence and tone. SpecAgent writes a
short response around the code-selected numbers, keeps contract leverage visible and pivots when a
constraint is rejected.

## 5. Closing discipline

Accept the cheapest valid standing offer that beats the fallback alternative. If no offer clears that
bar, walk away. The runner closes before the wave cap and never reads the leaderboard, event feed or
screen during the final.

## 6. Final operations

Hosted final:

```bash
uv run arena run --set final --agent spec --parallel 10
```

Local rehearsal:

```bash
uv run arena-sim serve --final-rehearsal --data ../data
ARENA_DATA=../data uv run arena run --set final --agent spec --waves 2 --parallel 10
```

The repository contains no credentials. Record the final `git rev-parse HEAD` commit in the ehl.gg
submission description after the code freeze.
