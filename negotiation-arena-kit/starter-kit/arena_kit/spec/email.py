"""The e-mail around the term sheets (section 9, R12.20-R12.21, R13.14-R13.15).

The model writes words only (R9.1): salutation, opening, the keys of the arguments it picks, bridge,
question, closing. Code puts in everything that must match the numbers - the arguments word for word,
its own sentences (the weighted cost arithmetic, a volume commitment, an earlier offer taken up, the
rival's offer in an RFQ) and the options paragraph built from the term sheets themselves (`describe`)
- then lints what the model wrote: no leaks, no figure that is not in the material, no data ID, no
unknown ID, no repeated claim (R13.15, R9.7, R12.20). Without the model (late, failed, or no time
left) the tactic's code-written lines carry the e-mail, with the two best arguments (R9.5, R10.1).
"""

import base64
import json
import re

from .. import writing
from .prose import sentences

ID_RE = re.compile(r"\b(?:idx|quote|offer|sc|qn|po|cost|fc|news|doc|ctr):[\w.:@#/-]*\w")
COMPANY_RE = re.compile(
    r"^(?:[^,]{2,60},\s*)?(.+?\b(?:GmbH(?: & Co\. KG)?|KG|AG|SE|Ltd\.?|Kft\.?|s\.r\.o\.|Sp\. z o\.o\.|S\.A\.|S\.p\.A\.|"
    r"Srl|S\.r\.l\.|B\.V\.|N\.V\.|Inc\.?|LLC|AB|Oy|A/S|e\.K\.|OHG|GbR)\.?)\s*$"
)
VOLUME_ASK = re.compile(r"which volume|binding (?:higher )?volume|volume commitment|volume can you commit|"
                        r"exact volume|annual requirement", re.I)  # fmt: skip
ASK_DATA = re.compile(
    r"send (?:us )?the data|which figures|share the (?:detailed )?(?:forecast )?data|provide the "
    r"(?:specific )?(?:forecast|data|figures|volume data)|data behind|verify (?:this|it|these)",
    re.I,
)
SIGNATURE = writing.SIGNATURE

SYSTEM = """You write e-mails for the purchasing team of Hollerau Pumpenwerke GmbH, a German pump maker,
to an industrial supplier. The term sheets of this e-mail are already decided by code; you write the
words around them.

Return ONE JSON object and nothing else:
{"salutation": "...", "opening": "...", "claims": ["key", ...], "bridge": "...", "question": "...", "closing": "..."}

- salutation: "Dear <name>," only if that person's name appears in the supplier's e-mails; otherwise
  "Dear colleagues at <short company name>,".
- opening (1-2 sentences): answer what they last wrote - name their point, acknowledge what is fair in
  it, thank them for a move they made. Never ignore their argument; never repeat one they rejected.
- claims: 0-3 keys from ARGUMENTS, best first - only those that fit this supplier and this moment. On
  our first e-mail use the strongest one or two. They are inserted word for word; do not restate them.
- bridge (1-2 sentences, no figures): why the options make sense for both sides, in terms of what this
  supplier cares about (PROFILE). Several options: say we are flexible on the term they vary and ask
  which works best. If THE MOVE says they asked for a term, name it and point to the option that gives it.
- question (1 sentence): one question from QUESTIONS, or "".
- closing (1 sentence): warm and professional, in the tone the PROFILE asks for.

Follow THE MOVE: if we hold, say so; do not thank them for a step they did not make. Tone may carry
honest feeling grounded in facts (disappointment at no movement, the history between the companies).
Never invent hardship, never threaten, no filler ("balance", "mutual benefit", "synergy", "win-win").
Never promise documents, data, calls or meetings we would send or hold later - this e-mail is all we send.

STRATEGIC PLAYBOOK (apply this to every reply)
- Hard constraints: read the supplier's latest message for absolute operational limits or policies. If a
  term was rejected as impossible, do not put it in the next request. Acknowledge the limit briefly and
  pivot the trade to another variable that matters to us, such as payment, price, contract coverage or
  a service protection.
- Total cost of ownership: when a verified delivery, quality or service problem is raised, connect it to
  our total cost of ownership: buffer stock, downtime, inspection and disruption risk. Do not argue that
  the metric changes the supplier's factory cost. Explain what protection we require in the package, such
  as a chargeback, service level or price offset.
- If-then reciprocity: never describe a price movement as unconditional. State the exchange in words:
  any movement toward the supplier's price is conditional on a specific buyer-valued non-price term in
  the corresponding package. If the supplier gave no such term, hold our previous position and ask what
  it can trade.
- Shrinking concessions: make each successive movement smaller and signal that movement is limited. Do
  not volunteer a new concession merely to keep the conversation going.
- Approval framing: ground resistance in what our internal pricing or finance approval can authorize,
  using verified market evidence and the case policy. Avoid claiming that a number is simply "fair" and
  never disclose an internal budget or ceiling.
- Human pacing: answer the supplier's latest point specifically. Vary the opening, acknowledgement and
  closing across rounds; never reuse a generic paragraph when the supplier's position has changed.

STRUCTURED-TERM ALIGNMENT (mandatory): prose reasoning and structured term sheets are one commitment. If
you identify a contractual notice breach, calculate the first compliant effective date and put that date
or a later date in every option; never mention the breach and then offer the illegal date. Before any
price increase, compare the option with the previous buyer menu. A higher unit price is permitted only
when the same option also improves a buyer-valued term (later payment, shorter lead time, longer contract,
or later effective date). If no term is improved, hold the previous price and ask for a supplier trade.
An accept action before round 3 is permitted only when the supplier's price is within the calculated ideal
target tolerance; otherwise continue with a counter-offer or hold. Treat every guardrail error as a
correction instruction, not as a reason to flatten prices or omit the structured package.

Rules: plain business English, short sentences, 70-130 words for all parts together. No figures,
prices, percentages or data IDs in your own text - code adds them. Never mention deadlines, urgency,
budgets, that we lack alternatives, our internal costs, or any scoring. No placeholders, no sign-off."""


# --- the options paragraph (R13.14) ----------------------------------------------------------------
def describe(terms, fields):
    """A term sheet in words - code writes it from the sheet, so prose and sheets never disagree."""
    out = []
    for f in fields:
        v = terms.get(f)
        if v is None and f != "index_clause":
            continue
        if f == "unit_price":
            out.append(f"EUR {v:,.2f} per unit")
        elif f == "payment_days":
            out.append(f"payment {v} days net")
        elif f == "effective_date":
            out.append(f"new price from {v}")
        elif f == "contract_months":
            out.append("a single order" if v == 0 else f"{v}-month agreement")
        elif f == "volume_per_year":
            out.append(f"{v:,} units per year")
        elif f == "lead_time_days":
            out.append(f"lead time {v} days")
        elif f == "index_clause":
            out.append("fixed price, no index clause" if not v else
                       f"index clause on {v['series']} ({v['material_share']:.0%} material share, "
                       f"{v['deadband_pct']:g}% deadband, base {v['base_month']})")  # fmt: skip
        elif f == "ppm_commitment":
            out.append(f"quality commitment {v} ppm")
        elif f == "chargeback_eur_per_complaint":
            out.append(f"EUR {v:g} chargeback per complaint")
        elif f == "incoterm":
            out.append(f"Incoterm {v}")
        elif f == "tooling_eur":
            out.append(f"tooling EUR {v:,.0f}")
        else:
            out.append(f"{f.replace('_', ' ')} {v}")
    return ", ".join(out)


def shown_fields(case, sheets, theirs):
    """The terms the options paragraph spells out: those that differ between our sheets, then those
    that differ from the supplier's sheet, then the rest - every negotiable term (R13.14)."""

    def k(v):
        return json.dumps(v, sort_keys=True, default=str)

    fields = [f for f in case["negotiable"] if f != "unit_price"]
    varying = [f for f in fields if len({k(s.get(f)) for s in sheets}) > 1]
    differ = [f for f in fields if sheets and k(sheets[0].get(f)) != k((theirs or {}).get(f))]
    return ["unit_price", *dict.fromkeys(varying + differ + fields)]


def options_paragraph(case, sheets, theirs):
    fields = shown_fields(case, sheets, theirs)
    labelled = [describe(t, fields) for t in sheets]
    if not labelled:
        return ""
    if len(labelled) == 1:
        return f"We propose: {labelled[0]}."
    lines = [f"Option {'ABC'[i]}: {text}." for i, text in enumerate(labelled)]
    return "We can offer either of these packages - please choose the one that suits you better:\n" + "\n".join(lines)


# --- who we write to (R9.6) ---------------------------------------------------------------------
def company(name_from_data, events):
    """The supplier's name: from the data, else its subject ("Quote from X:") or signature - never the
    bare supplier ID."""
    if name_from_data:
        return name_from_data
    for m in events:
        if m.get("from") == "buyer":
            continue
        if found := re.search(r"Quote from (.+?):", m.get("subject") or ""):
            return found.group(1).strip()
        for line in reversed((m.get("body") or "").splitlines()):
            if found := COMPANY_RE.match(line.strip()):
                return found.group(1).strip()
    return "your team"


# --- questions (R9.4) -----------------------------------------------------------------------------
BREAKDOWN_Q = "Could you share a cost breakdown for this part - material, conversion, overhead?"
TYPE_Q = {
    "quality": "Could you send us the 8D report with root cause and containment for the recent complaints?",
    "capacity": "Could you send a delivery schedule with the monthly quantities you can commit to?",
    "rfq": "What is the shortest lead time you could commit to, and is there a cheaper design or material variant?",
    "price_increase": "Which cost elements of this part have actually risen since our last price agreement?",
    "renewal": "Which volume or contract length would let you improve the price further?",
    "bundling": "Which further parts or volumes would make a bundle more attractive for you?",
    "raw_material": "Which base month and deadband would you consider fair for an index clause?",
}


def _gist(q):
    return " ".join(re.findall(r"[a-z]+", str(q).lower())[:7])


def questions(case, events, profile_questions=()):
    """The questions not asked yet, in order: a cost breakdown once (unless asked or received), the
    case type's question, the profile's."""
    ours = " ".join(m.get("body", "") or "" for m in events if m.get("from") == "buyer").lower()
    got = " ".join(a.get("name", "") for m in events for a in m.get("attachments") or []).lower()
    out = []
    if "breakdown" not in ours and "breakdown" not in got:
        out.append(BREAKDOWN_Q)
    if (q := TYPE_Q.get(case.get("type"))) and _gist(q) not in " ".join(re.findall(r"[a-z]+", ours)):
        out.append(q)
    for q in profile_questions or []:
        if _gist(q) and _gist(q) not in " ".join(re.findall(r"[a-z]+", ours)):
            out.append(str(q))
    return out


def pick_question(raw, allowed):
    """The model's question if it is one of `allowed` (by its words), else None: one per e-mail, never
    repeated (R9.4)."""
    words = set(re.findall(r"[a-z]{4,}", str(raw or "").lower()))
    if not words:
        return None
    for q in allowed:
        qw = set(re.findall(r"[a-z]{4,}", q.lower()))
        if qw and len(words & qw) / len(qw) >= 0.4:
            return str(raw).strip()
    return None


# --- the stance (R9.3) ------------------------------------------------------------------------------
def stance(first, hold, moved_pct):
    if first:
        return "We open: our options are ambitious; give the reasons, not apologies."
    if hold:
        if moved_pct > 0.2:
            return ("We HOLD our position: they are still coming down. Acknowledge their step briefly, then say "
                    "clearly that the price is still too high and why.")  # fmt: skip
        return ("We HOLD our position because they did not move. Polite but firm; do not thank them for "
                "flexibility they did not show; give one concrete reason or ask what would let them move.")  # fmt: skip
    return "We make a modest step - this costs us and we expect a step back."


# --- code sentences --------------------------------------------------------------------------------
def weighted_sentence(intel):
    """R11.4: the cost-share arithmetic by the supplier's own figures, or None."""
    if intel is None:
        return None
    shared = [d for d in intel.cost_drivers if d.get("share_pct") is not None]
    if not shared or intel.cost_impact_pct is None or intel.asked_pct is None or (intel.cost_gap_pct or 0) <= 1.0:
        return None
    items = ", ".join(f"{d.get('series') or d['name']} {d['change_pct']:+.1f}% at {d['share_pct']:g}% of your cost"
                      for d in shared)  # fmt: skip
    return (f"By the figures you gave us - {items} - the weighted effect on the price is about "
            f"{intel.cost_impact_pct:+.1f}%, not the {intel.asked_pct:g}% you ask for.")  # fmt: skip


def commitment(sheet):
    """R13.15: our answer to "which volume can you commit to?", from our own sheet."""
    v, m = sheet.get("volume_per_year"), sheet.get("contract_months")
    if v is None and m is None:
        return None
    parts = [f"{v:,} units per year" if v else None, f"over {m} months" if m else None]
    return "We can commit to " + " ".join(p for p in parts if p) + ", as set out in the term sheets."


def rival_line(price, offer_id):
    """R12.14: the cheapest valid rival offer, as a checkable claim."""
    return f"A competing supplier has offered EUR {price:.2f} per unit ({offer_id})."


# --- the prompt (R9.2) -------------------------------------------------------------------------------
def render(case, sid, events, plan, profile, intel, feedback, args, allowed_questions, tactic, extras, company_name):
    last = next((m for m in reversed(events) if m.get("from") != "buyer"), {})
    ours = [m for m in events if m.get("from") == "buyer"]
    prof = {k: profile.get(k) for k in ("persona", "tone", "pressures", "wants", "avoid", "angle") if profile.get(k)}
    view = {}
    if intel is not None:
        view = {"constraints": intel.constraints, "lever_hints": intel.lever_hints, "their_questions": intel.questions,
                "relationship_style": intel.relationship_style}  # fmt: skip
    attachments = "\n".join(
        f"[their attachment {a.get('name')}]\n{(a.get('text') or '')[:1200]}"
        for a in last.get("attachments") or []
        if not (a.get("name") or "").startswith("quote")
    )
    move = (f"message {len(ours) + 1} in this thread. "
            + (f"Since our last e-mail they moved {plan['moved_pct']:.1f}% toward us. " if ours else "")
            + plan["stance"] + " " + plan.get("note", ""))  # fmt: skip
    parts = [
        f"CASE: {case.get('title')} ({case.get('type')}). Supplier: {company_name}.",
        f"PROFILE: {json.dumps(prof, ensure_ascii=False)}" if prof else "",
        f"TACTIC: {tactic.name} - {tactic.style}",
        f"WHAT OUR ANALYST SEES: {json.dumps(view, ensure_ascii=False, default=str)}" if view else "",
        f"THEIR LAST E-MAIL (from {last.get('from')}, status {last.get('status')}, subject {last.get('subject')!r}):\n"
        f"{(last.get('body') or '')[:2500]}",
        attachments,
        f"HOW OUR ARGUMENTS CHECKED OUT SO FAR: {json.dumps(feedback)}" if feedback else "",
        "OUR LAST E-MAILS (oldest first):\n" + "\n---\n".join((m.get("body") or "")[:700] for m in ours[-2:])
        if ours
        else "This is our first reply to their quote.",
        f"THE MOVE: {move}\nThe options (code inserts them after your bridge):\n{plan['options']}",
        f"CODE SENTENCES (already in the e-mail, do not repeat): {' '.join(extras)}" if extras else "",
        "ARGUMENTS ([key] sentence - note; answer with the keys only):\n"
        + ("\n".join(f"[{a.key}] {a.sentence} - {a.note}" for a in args[:6]) or "(none - write without data)"),
        "QUESTIONS (pick one or none):\n" + ("\n".join(allowed_questions) or "(none)"),
    ]
    return "\n\n".join(p for p in parts if p)


# --- assembly ---------------------------------------------------------------------------------------
def code_parts(tactic, question):
    return {"salutation": "", "opening": tactic.opening, "claims": None, "bridge": tactic.bridge,
            "question": question or "", "closing": tactic.closing, "fallback": True}  # fmt: skip


def pick_claims(parts, args, first=False, n_fallback=2):
    by_key = {a.key: a for a in args}
    if parts.get("fallback") or (first and not parts.get("claims")):
        keys = [a.key for a in args if a.ids][:n_fallback]  # R9.5: even a fallback carries the two best
    else:
        keys = parts.get("claims") or []
    picked = []
    for k in keys:
        if (c := writing.match_key(k, by_key)) and c not in picked and len(picked) < 3:
            picked.append(c)
    return picked


def drop_repeated(body, used_ids):
    """R12.20: drop a sentence whose IDs were all cited before (it would come back `duplicate`)."""
    out = []
    for para in body.split("\n\n"):
        keep = []
        for line in para.split("\n"):
            parts = []
            for s in sentences(line) or [line]:
                ids = ID_RE.findall(s)
                if not ids or not all(i in used_ids for i in ids):
                    parts.append(s)
            if parts:
                keep.append(" ".join(parts))
        if keep:
            out.append("\n".join(keep))
    return "\n\n".join(out)


def compose(case, events, sheets, theirs, parts, args, material, company_name, extras=(), analysis=(),
            used_ids=(), known=lambda ref: True, allowed_questions=(), first=False):  # fmt: skip
    """The e-mail body from the model's parts and code's sentences: (body, picked claims, dropped)."""
    options = options_paragraph(case, sheets, theirs)
    picked = [c for c in pick_claims(parts, args, first) if not (analysis and c.key == "summary:indices")]
    numbers = writing.allowed(material, *(a.sentence for a in args), options, *extras, *analysis)
    dropped = []
    clean = {k: writing.lint(parts.get(k), numbers, dropped) for k in ("opening", "bridge", "closing")}
    q = parts.get("question") if parts.get("fallback") else pick_question(parts.get("question"), allowed_questions)
    question = writing.lint(q, numbers, dropped) if q else ""
    claims_text = " ".join(c.sentence + "".join(" " + e for e in c.extra) for c in picked)
    supplier_words = "\n".join(str(m.get("body") or "") for m in events if m.get("from") != "buyer")
    hello = writing.salutation(parts.get("salutation"), company_name, supplier_words)
    body = "\n\n".join(p for p in [
        hello, clean["opening"], " ".join(x for x in (*analysis, claims_text) if x),
        " ".join(x for x in (clean["bridge"], *extras) if x), options, question, clean["closing"], SIGNATURE,
    ] if p)  # fmt: skip

    def keep_known(m):
        return m.group(0) if known(m.group(0)) else ""

    body = re.sub(r"[ \t]*\(\s*[,;\s]*\)", "", ID_RE.sub(keep_known, body))
    body = drop_repeated(body, set(used_ids)).strip()[:8000]
    return body, picked, dropped


def data_attachment(events, body, fact):
    """R12.21: when the supplier asks for our figures, attach every ID we cited with its values."""
    last = next((m for m in reversed(events) if m.get("from") != "buyer"), {})
    if not ASK_DATA.search(str(last.get("body") or "")):
        return None
    mine = [str(m.get("body") or "") for m in events if m.get("from") == "buyer"]
    ids = list(dict.fromkeys(i for text in [body, *mine] for i in ID_RE.findall(text)))
    rows = [(i, f) for i in ids if (f := fact(i))]
    if not rows:
        return None
    lines = ["# Data behind our figures (Hollerau Pumpenwerke, Purchasing)", "", "| ID | data |", "|---|---|"]
    lines += [f"| {i} | {json.dumps(f, ensure_ascii=False, default=str)[:300]} |" for i, f in rows[:20]]
    content = base64.b64encode("\n".join(lines).encode("utf-8")).decode("ascii")
    return {"name": "our-figures.md", "type": "md", "content_b64": content}
