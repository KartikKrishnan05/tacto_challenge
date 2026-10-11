"""The spec agent's building blocks (NEW_AGENT_REQUIREMENTS.md). Code decides every number here; the
language model only writes words (`email`, `analyst`, `dossier`), and every model call has a
code-written fallback.

    prep        section 0.7: check the case file, C of every opening, coverage, levers, anchor, floor
    playbook    the goal's settings: one merged anchor table (R4.1 + R13.10), TYPE_LEVERS
    terms       term-sheet helpers: complete a sheet, options, which way a term helps the supplier
    estimate    section 2: our estimate of the supplier (full cost, cost of money)
    pricing     R13.8: the price at which a package costs exactly the target C
    levers      R12.3-R12.4: what each term is worth in EUR; the supplier's revealed prices
    concession  section 5: anchor, curve, reciprocity, deadlock breaker, caps
    sheets      sections 3, 6, 11.3, 12.4: term sheets around a target C, and their guards
    steps       R13.1-R13.7: settling the heavy terms one at a time
    prose       R13.6: the supplier's offers stated in words (English and German)
    analyst     R11.1-R11.8: what each supplier e-mail tells us, in the background
    tactics     R11.12-R11.13: one wording tactic per e-mail
    closing     section 7, R11.17-R11.18, R12.10-R12.13, R13.13: accept, walk, the accept ladder
    clock       R11.14-R11.16, R12.17: the case clock and the model's time budget
    dossier     section 1: what the data says about a supplier, and its profile
    email       section 9, R12.20-R12.21, R13.14-R13.15: the e-mail around the sheets
    llm         model calls with hard timeouts
    runner      R12.18, R13.16: one worker per supplier thread
"""
