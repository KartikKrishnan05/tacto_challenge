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

## Architecture (all in `negotiation-arena-kit/starter-kit/team/`)
```
case ─► 1 PREPARE  briefing.py   supplier profile + arguments that check out (pure SQL, < 1 s)
     ─► 2 NEGOTIATE agent.py     levers.py (EUR per lever) → planner: sheet A on a concession path +
                                 2 equal-cost alternatives (MESO) → LLM writes the e-mail, code caps
                                 every sheet at the planned C and drops unknown IDs
     ─► 3 CLOSE     agent.py     hard rules: accept on agreed / first final_offer / stall / round budget,
                                 confirm missing terms + full coverage once, take back an earlier cheaper
                                 offer once, never break a must-have, never worse than the alternative
        runner.py                one worker per supplier thread (each thread has its own 60 s clock),
                                 accept retry when a timeout moved the offer ID, closes before a wave cap
   4 MEASURE  evaluate.py        runs cases, summary.md per run (score vs previous, missed levers, reasons)
   5 DELIVER  final.py           Live Final runner, --rehearse, --check (pre-flight)
```

## Run (from `negotiation-arena-kit/starter-kit`)
| What | Command | Cost |
|---|---|---|
| Levers of a case | `uv run python -m team.levers case:P01 --price 135.10` | free |
| Briefing of a case | `uv run python -m team.briefing case:P01` | free |
| Simulator (scripted supplier) | `uv run python -m team.evaluate --sim` | free (LLM cents) |
| Sandbox (real demo suppliers D01, D02) | `uv run python -m team.evaluate --set sandbox` | free |
| All 10 practice cases, final-like | `uv run python -m team.evaluate --label <change> --cap 480 --parallel 10` | 10 sessions |
| Pre-flight | `uv run python -m team.final --check` | free |
| Dress rehearsal | `uv run python -m team.final --rehearse case:D02` | free |
| **Live Final** (start before 12:00, leave running) | `uv run python -m team.final` | - |

Practice score (mean of 10 cases x 100): kit naive/llm agent ~? | v2 13.3 (pushed past final offers -> walk-aways)
| v4 56.3 (accept final offers) | v5 56.7 | v6 48.6 (one accept bug, fixed) | v7 see `runs/`.

## Learned so far
- Suppliers **walk away** when pushed after `final_offer` -> accept it (or confirm its price with our terms once).
- Pushing small levers (e.g. a chargeback worth 0.3 % of C) makes suppliers raise the price far more -> skip them.
- **Uncovered need** is the biggest cost: a 12-month deal on a 24-month need costs ~500k at the fallback price.
- In an RFQ every thread has its own 60 s clock; calls on one session queue on the server.
- Open practice sessions count against the 10 open sessions per key -> the final runner closes them first.
- The LLM tends to concede too fast -> code caps every sheet at the planned C.

## Still open (claim by writing your name)
- Kit baseline on all practice cases for the pitch (`--agent llm`, 10 sessions).
- Per-type playbooks: tune with replays (P02 copper index clause, P10 capacity, P18 index deadband).
- Answer supplier questions with data attachments (they ask "which figures?" - md/csv attachments are read).
- Pitch: score progression per version, one replay story (P01 notice-period argument).

## Conventions
- Code in `negotiation-arena-kit/starter-kit/team/` (keep the vendored kit untouched so it can be updated).
- Numbers from code, words from the model. One change per evaluation run; write the label so others see it.
- Commit messages in English, short (`feat: ...`). Pull before push, never `--force`.
