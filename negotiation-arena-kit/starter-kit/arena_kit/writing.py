"""The e-mail: the model writes the words, code puts in the numbers and checks what goes out.

The model returns the parts of an e-mail - salutation, an opening that answers the supplier, the
keys of the data arguments it picked, a bridge to the proposal, one question, a closing. Code then
assembles the e-mail: the arguments word for word (their figures are checked), its own sentences
(a volume commitment, an earlier offer taken up), the options paragraph from the term sheets
themselves (so prose and sheets never disagree), and a signature. Before it goes out, `lint` drops
any sentence of the model's that leaks urgency, a budget or a missing alternative, carries a data
ID, or states a figure that appears nowhere in the material.
"""

import re

ID_RE = re.compile(r"\b(?:idx|quote|offer|sc|qn|po|cost|fc|news|doc|ctr|sup|part):[\w.:@#/-]*\w")
LEAKS = re.compile(
    r"\b(urgent\w*|asap|as soon as possible|immediately|deadline|running (out|low)|time is short|"
    r"no (other )?(alternative|option|choice|source|supplier)s?|only (supplier|source|option)|"
    r"depend(ent|ence)? on you|single source|budget\w*|must[- ]haves?|cannot (afford|wait)|"
    r"line (stop|down)|production (stop|standstill)|stock[- ]?out|desperate\w*|fallback|"
    r"walk[- ]away price|our (limit|ceiling|maximum)|maximum we|best we can pay|total cost|"
    r"category goal|score)\b",
    re.I,
)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
SIGNATURE = "Kind regards\nPurchasing\nHollerau Pumpenwerke GmbH"

SYSTEM = """You write e-mails for the purchasing team of Hollerau Pumpenwerke GmbH, a German pump maker,
to an industrial supplier. The term sheets of this e-mail are already decided by code; you write the
words around them.

Return ONE JSON object and nothing else:
{"salutation": "...", "opening": "...", "claims": ["key", ...], "bridge": "...", "question": "...", "closing": "..."}

- salutation: "Dear <name>," only if that person's name appears in the supplier's e-mails;
  otherwise "Dear colleagues at <short company name>,".
- opening (1-2 sentences): answer what they last wrote - name their point, acknowledge what is fair
  in it, thank them for a move they made. Never ignore their argument; never repeat one they rejected.
- claims: 0-3 keys from ARGUMENTS, best first - only those that fit this supplier and this moment.
  On our first e-mail in a thread, use the strongest one or two. They are inserted word for word;
  do not restate their figures yourself.
- bridge (1-2 sentences, no figures): why the packages make sense for both sides, framed in what this
  supplier cares about (its e-mails, the SUPPLIER DOSSIER). Several packages: say we are flexible on
  the term named in the PLAN and ask which works best. If the PLAN says the supplier asked for a term
  (faster payment, a longer contract, volume), name it and point to the package that gives it.
- question (1 sentence): one useful question we have not asked before, or "". Never ask for a cost
  breakdown - suppliers do not share one.
- closing (1 sentence): warm and professional, in the tone of the CATEGORY GOAL.

Tone may carry honest feeling grounded in facts: disappointment at a large increase or at no movement,
the loyalty and history between the companies. Never invent hardship, never suggest we are in
difficulty, never threaten. No filler such as "balance", "mutual benefit", "synergy", "win-win".
Do not thank them for a step they did not make.

Rules: plain business English, short sentences, 70-130 words for all parts together. No figures,
prices, percentages or data IDs in your own text - code adds them. Never mention deadlines, urgency,
budgets, that we lack alternatives, our internal costs, or any scoring. No placeholders, no sign-off
(code adds the signature)."""


def subject(case, events):
    """Re: the supplier's last subject, so the thread stays one thread."""
    last = next((m for m in reversed(events) if m.get("from") != "buyer" and m.get("subject")), None)
    if last:
        s = last["subject"]
        return s if s.lower().startswith("re:") else f"Re: {s}"
    return case["title"]


def options_paragraph(labelled):
    if not labelled:
        return ""
    if len(labelled) == 1:
        return f"We propose: {labelled[0]}."
    lines = [f"Option {'ABC'[i]}: {text}." for i, text in enumerate(labelled)]
    return "We can offer either of these packages - please choose the one that suits you better:\n" + "\n".join(lines)


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", str(text or "").strip()) if s]


def lint(text, allowed_numbers, dropped=None):
    """`text` without the sentences that leak, cite an ID or state a figure not in the material."""
    keep = []
    for s in sentences(text):
        why = None
        if LEAKS.search(s):
            why = "leak"
        elif ID_RE.search(s):
            why = "data ID"
        else:
            for n in NUMBER.findall(s):
                v = n.replace(",", "")
                if v not in allowed_numbers and not (v.isdigit() and int(v) <= 12):
                    why = f"figure {n}"
                    break
        if why:
            if dropped is not None:
                dropped.append({"sentence": s, "why": why})
        else:
            keep.append(s)
    return " ".join(keep)


def allowed(*texts):
    out = set()
    for t in texts:
        for n in NUMBER.findall(str(t or "")):
            v = n.replace(",", "")
            out |= {v, v.rstrip("0").rstrip(".") if "." in v else v}
    return out


def salutation(raw, company, material):
    raw = str(raw or "").strip()
    default = f"Dear colleagues at {company}," if company else "Dear colleagues,"
    if not raw.startswith("Dear") or LEAKS.search(raw):
        return default
    name = raw[4:].strip(" ,")
    words = [
        w
        for w in re.findall(r"[A-ZÄÖÜ][\wäöüß-]+", name)
        if w not in ("Mr", "Ms", "Mrs", "Dr", "Herr", "Frau", "Colleagues")
    ]
    people = material.replace(company, " ") if company else material  # a company name is not a person
    if words and not all(w in people for w in words):
        return default  # a name we never saw would be invented
    return raw if raw.endswith(",") else raw + ","


def match_key(text, by_key):
    """The entry `text` names: its exact key, or a key it starts with (models like to copy the
    whole line "key: sentence"), the longest first."""
    text = str(text or "").strip().strip("[]")
    if text in by_key:
        return by_key[text]
    for key in sorted(by_key, key=len, reverse=True):
        if text.startswith(key) or text.startswith(f"[{key}]"):
            return by_key[key]
    return None


def assemble(parts, claims_by_key, labelled, company, material, numbers, extras=(), dropped=None):
    """The e-mail body from the model's parts and the code's figures: (body, picked claims)."""
    picked = []
    for k in parts.get("claims") or []:
        if (c := match_key(k, claims_by_key)) and c not in picked and len(picked) < 3:
            picked.append(c)
    args = " ".join(c.sentence + "".join(" " + e for e in c.extra) for c in picked)
    clean = {k: lint(parts.get(k), numbers, dropped) for k in ("opening", "bridge", "question", "closing")}
    paragraphs = [
        salutation(parts.get("salutation"), company, material),
        clean["opening"],
        args,
        " ".join(extras),
        clean["bridge"],
        options_paragraph(labelled),
        clean["question"],
        clean["closing"],
        SIGNATURE,
    ]
    return "\n\n".join(p for p in paragraphs if p), picked


def fallback_parts():
    """When the model fails or is late: a plain, safe e-mail (code picks the arguments)."""
    return {
        "salutation": "",
        "opening": "Thank you for your message and your offer.",
        "claims": [],
        "bridge": "We have looked at your terms carefully and would like to find a solution that works for both sides.",
        "question": "",
        "closing": "We look forward to your reply.",
    }
