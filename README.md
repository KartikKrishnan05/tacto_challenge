# Promptly Done: Tacto Negotiation Arena (EHL Grand Finale 2026)

AI buyer agent for the [Tacto Negotiation Arena](https://arena.tacto.ai/).
The official kit is vendored in `negotiation-arena-kit/`.

## Run

```bash
cd negotiation-arena-kit/starter-kit
cp .env.example .env   # ARENA_API_KEY, LLM_PROVIDER=openai, LLM_MODEL, LLM_API_KEY
uv sync --all-extras
uv run arena play case:P01 --agent llm
```

## Team tools (`negotiation-arena-kit/starter-kit/team/`)

Run from `negotiation-arena-kit/starter-kit`:

```bash
uv run python -m team.levers case:P01 --price 135.10        # what each lever is worth in EUR (no session used)
uv run python -m team.evaluate --sim --agent llm             # local simulator: free
uv run python -m team.evaluate --agent llm --label my-change # all hosted practice cases (10 sessions, asks first)
```

`team.evaluate` writes `runs/<time>_<label>/summary.md`: score per case vs. the previous run, the levers
left on the table and the supplier's reasons. Our practice quota is 200 sessions per day for the whole team.
