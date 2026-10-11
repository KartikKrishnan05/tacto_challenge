# Pitch deck: Promptly Done - How our buyer agent negotiates

Final slide deck for the Tacto track pitch (5 min + 2 min Q&A). The slide sources are in
[`slides/`](slides/) (`deck.json` + one HTML file per slide, Slides artifact format); an exported
PDF: [Promptly-Done-Pitch.pdf](Promptly-Done-Pitch.pdf). Speaker notes below, one block per slide.

## 1. Numbers from code. Words from the model.

Hi, we are Promptly Done. You know our scores - so in the next five minutes we show you how we got there. Our whole approach fits in one line: the numbers come from code, the words come from the model.

## 2. Total cost. Not price.

First fact: the arena scores total cost, not the unit price. Payment days, covering the full need, lead time, quality, tooling, index clauses - every term is euros. A cheaper price on a 12-month deal for a two-year need can score zero.

## 3. 90%

Second fact, from mining our own games: about ninety percent of the value is decided by the supplier's answer to our very first e-mail. Plus 0.46 on average - all later replies together add 0.08. So the first message has to carry the complete structure, and code has to get it right.

## 4. Code decides every number.

Here is the architecture. A data layer feeds a prepare step, a negotiation loop with one worker per supplier thread, and a close step. The rule that runs through all of it: code decides every number. The model may read and write words - always with a deadline and a code fallback - but it never sets a price, a term or an accept.

## 5. Arguments the arena can verify.

The data pipeline. Everything - the package's 24 tables, the 208 letters and notes, the market indices, the Sunday release and the facts of a hidden case - goes into one master JSON layer we query as SQL. From it we build a dossier per supplier and claims computed exactly the way the arena checks them: index changes over a twelve-month window, notice periods against the contract - in P01 the letter gave 51 days where 90 were required. A claim that would not check out is never sent.

## 6. Read. Model. Target. Write.

Each round, per supplier thread, runs four steps. The analyst reads its e-mail - prices, refused terms, what it asks for, alternatives written in prose, in English or German - and every figure it extracts must literally appear in the text. The supplier model estimates its full cost and reads its cost of money from its own price pairs. The concession plan sets a target total cost: an anchor by category goal and steps of fifteen percent of the gap, capped by how far the supplier moved. Then code prices the packages to that target and guards them. All of this inside the sixty-second clock.

## 7. One term per e-mail.

The core technique is MESO - multiple equivalent simultaneous offers. Each e-mail varies one term across three packages that cost us exactly the same. Whatever the supplier picks, and how it re-prices, tells us what that term is worth to it. In P02 the supplier priced payment days itself: its money costs 6.7 percent a year, ours 8. So paying more for 90 days is the cheaper deal for us. We lock the term and vary the next one. Price comes last.

## 8. We read every game.

How did we find the rules? We read every trace - our e-mails, the supplier's replies and its reasons - and kept a rule only if we saw it in at least three different cases, because the final uses cases we have never seen. Writing after a final offer lost the supplier eight times out of eight. Almost a third of replies hid a cheaper alternative. Covering the full need multiplied P08's score. And over-asking on lead time costs far more than it is worth.

## 9. The arena is deterministic. So we ran experiments.

Then we found out the arena is deterministic: identical e-mails get identical replies. That turned negotiation into an experiment. Strategy lives in knobs - anchor depth, concession curve, reciprocity, close gap - and we defined nineteen variants. A replay book reuses more than 250 recorded games, so only a move that actually changes costs a practice session. And a variant is adopted only if it gains on average and loses no case by more than five points. The surprise: e-mails written by code beat the model, 0.733 against 0.661. So in the final, code writes.

## 10. story

**0.507 → 0.735**

One story from this morning. In P02 copper rose almost fifty percent, and a safety rule banned index clauses on volatile series. But our cost function already prices a clause - the expected index move and the risk we carry. We removed the gut rule and let the numbers decide. The supplier valued the certainty, P02 went from 0.507 to 0.735, and the other nine cases replayed identically. That is our approach in one slide.

## 11. From 13 to 73 in one night.

Here is the path, all on the ten practice cases. Our first LLM agent scored 13 - it pushed after final offers and suppliers walked away. Hard close rules, full coverage of the need and cost-capped sheets took us to 57. MESO steps, the learned playbook and the lab took us to 72, and this morning's index-clause fix to 73. Every practice case ends in a deal.

## 12. No human in the loop.

The live final runs without us, so robustness was a feature, not an afterthought. Each supplier thread has its own worker and its own sixty-second clock. Accepts are validated, so a refused offer never falls back to a sheet that breaks a must-have. Retries check whether a message already landed - a double message after a final offer would lose the deal. And before a wave's cap it closes on the cheapest valid offer.

## 13. Let code count. Let the supplier reveal. Let data decide.

Three lessons. Let code count: every decision runs through the cost function. Let the supplier reveal: equal-cost packages turn its choices into prices. And let data decide: one change per experiment, kept only if it wins, and only rules that generalize to cases we have never seen. Thank you - we are happy to take your questions.
