# AGENTS.md: Team "Promptly Done", Tacto Negotiation Arena (EHL Grand Finale 2026)

Read this first. It is the shared context for every agent (Claude Code, Codex) and every teammate.

## The challenge in short
- We build an **AI buyer agent** for Hollerau Pumpenwerke that negotiates by e-mail with Tacto's hosted
  supplier bots (https://arena.tacto.ai). Official kit, brief and API docs: `negotiation-arena-kit/`
  (vendored from github.com/tacto-ai/negotiation-arena-kit). Read `brief/brief.md` sections 5, 6, 7, 10.
- **Score = total cost C in EUR, not the unit price.** C includes price x volume, uncovered need at the
  fallback price, payment-days interest, freight (Incoterm), late delivery, quality (PPM, chargebacks),
  tooling, index clause, and in `stabilise`/`secure_supply` cases a charge for squeezing the supplier's
  margin. Case score = (C_open - C_deal) / (C_open - C_best), 0..1. Broken must-have = 0. No deal = alternative.
- Limits: 60 s per message, round limit per thread (default 10), 3 term sheets per message.
  Data IDs cited in e-mails are checked (`verified` helps, `contradicted` costs trust).
- **Practice quota: 200 sessions per 24 h for the whole team** (10 hosted practice cases = 10 sessions per full run).
- **Sun 08:00** new market data (`uv run arena data`). **Sun 12:00 code freeze + Live Final**: 5 waves x 8 min,
  no human in the loop, the agent must not read `/v1/events`, `/screen`, `/leaderboard`, `/results`.
  Submit on ehl.gg: repo + README + commit hash of the final agent, short description, slides.
  Top 10 pitch from 13:00 (5 min + 2 min Q&A).

## Setup
```bash
cd negotiation-arena-kit/starter-kit
cp .env.example .env        # ARENA_API_KEY (one team key), LLM_PROVIDER=openai, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
uv sync --all-extras
uv run arena data           # data package into data/package
uv run arena me             # quota left
```
Never commit `.env` or print keys. Entire must stay enabled (`entire enable` after cloning).
Default model `gpt-6-luna` (cheap, fast); compare with `gpt-6.1-sol` later.

## Done
| Tool | What | Run (from `starter-kit`) |
|---|---|---|
| `team/levers.py` | EUR value of every negotiable lever from a term sheet (official cost function), also per unit | `uv run python -m team.levers case:P01 --price 135.10` |
| `team/evaluate.py` | Plays cases in parallel, writes `runs/<time>_<label>/summary.md`: score vs previous run, levers left on the table, supplier reasons from the trace | `uv run python -m team.evaluate --sim` (free) / `--agent llm --label x` (10 sessions, asks first) |

Baseline: kit `llm` agent on P01 = 0.70 (missed: later effective_date worth ~19k EUR).

## To build (in this order; claim one by writing your name next to it)
1. **Our agent `team/agent.py`** (start from `arena_kit/agents/llm.py`): `render()` adds `levers.render()` for every
   standing term sheet; strategy per `category_goal` and case `type`; plug in via `--agent team.agent:make`.
2. **Supplier profile**: before round 1, pull the supplier's notes, letters, news, scorecards, invoices from the
   data package (`arena_kit.data.DataPackage`, `lookup(id)`) and summarise what moves it (cash-short? full capacity?).
3. **Argument finder**: facts with IDs and exact figures (index moves, should-cost, other quotes, quality issues)
   to cite; only facts that check out.
4. **Offer planner**: 2-3 term sheets per message that trade cheap-for-us vs. valuable-for-them levers; a
   concession schedule over the rounds; estimate the supplier's value with `arena_kit.cost.supplier_value`
   to avoid the relationship charge.
5. **Close policy**: when to accept / push once more / walk (status `final_offer`, rounds left, alternative,
   RFQ: play suppliers against each other), always must-haves ok, read the quote PDF standard terms.
6. **Speed guard**: every move well under 60 s (LLM timeout + fallback move), test with `--parallel 10`.
7. **Final runner `team/final.py`**: `arena run --set final` only knows `naive|llm`, so we need a runner that calls
   `arena_kit.play.play_waves` with our agent. Rehearse Sunday 09:00.
8. **Lessons loop**: an LLM reads the last runs' `summary.md` and proposes one prompt/strategy change at a time.
9. **Pitch material**: score progression per version, one replay story.

## Conventions
- Code in `negotiation-arena-kit/starter-kit/team/` (keep the vendored kit untouched so it can be updated).
- Numbers from code, words from the model. One change per evaluation run; write the label so others see it.
- Commit messages in English, short (`feat: ...`). Pull before push, never `--force`.
