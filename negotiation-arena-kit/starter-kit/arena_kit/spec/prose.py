"""The supplier's offers stated in words (R13.6, R11.1).

`prose_alternatives` reads the "Alternatively, with <terms>: EUR <price> per unit" lines suppliers
write next to their term sheet - in English and German (Alternativ, mit Zahlungsziel / Lieferzeit /
Preisgleitklausel ...) - for payment days, effective date, lead time, volume, contract months, ppm,
chargeback, Incoterm and the index clause. A line whose index clause cannot be pinned to one of the
case's options is skipped. These packages feed the accept ladder's offer-back (R12.11, R12.13), the
revealed lock (R13.5) and the revealed slopes (R12.4).

`prose_offers` is the looser reading DualAgent's analyst did by code: any sentence with offer words
and a price, with the terms it names.
"""

import re

from .terms import key

LEAD = r"(?:alternatively|alternativ|or alternatively|oder alternativ)"
ALT_RE = re.compile(
    LEAD + r",?\s+(?:with|mit|bei)\s+(?P<cond>.+?):\s*(?:€|EUR)?\s?(?P<price>\d[\d.,]*)\s*(?:€|EUR)?\s*"
    r"(?:per unit|per piece|per pc|pro stück|je stück|pro einheit|per kg|pro kg|/\s*stück)",
    re.I,
)
PAYMENT_RE = re.compile(
    r"(?:zahlungsziel|zahlung(?:sbedingung)?(?:en)?|payment(?: terms?)?(?: of| in| at)?)\s+(?:von\s+)?(\d{1,3})\s+"
    r"(?:days|tage|tagen)|(\d{1,3})\s+(?:days|tage)\s+(?:net|netto)",
    re.I,
)
LEAD_RE = re.compile(r"(?:lieferzeit|lead[- ]time)(?: of| von)?\s+(\d{1,3})\s+(?:days|tage|tagen)", re.I)
MONTHS_RE = re.compile(r"\b(\d{1,2})[- ](?:months?|monate?n?)\b", re.I)
VOLUME_RE = re.compile(
    r"\b(\d{1,3}(?:[.,]\d{3})+|\d{3,7})\s+(?:pcs|pieces|units|stück|stk|kg)\.?\s+(?:per|a|pro|im|je)\s+(?:year|jahr)",
    re.I,
)
PPM_RE = re.compile(r"(\d{1,5})\s*ppm", re.I)
CHARGEBACK_RE = re.compile(
    r"(?:chargeback|rückbelastung|belastung)(?: of| von)?\s*(?:€|EUR)\s?(\d[\d.,]*)|(?:€|EUR)\s?(\d[\d.,]*)\s+"
    r"(?:chargeback|per complaint|je reklamation|pro reklamation)",
    re.I,
)
INCOTERM_RE = re.compile(r"\b(EXW|FCA|DAP)\b")
ISO_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
DOTTED_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d\d)\b")
LONG_DATE_RE = re.compile(r"\b(\d{1,2})\.?\s+([A-Za-zäÄ]+)\s+(20\d\d)\b")
CLAUSE_RE = re.compile(r"preisgleitklausel|index(?:ation)? clause|indexklausel|index-?bindung", re.I)
NO_CLAUSE_RE = re.compile(
    r"\b(?:no|without|ohne|keine?)\s+(?:an?\s+)?(?:preisgleitklausel|index(?:ation)? clause|indexklausel)|"
    r"\bfixed price\b|\bfestpreis\b",
    re.I,
)
DEADBAND_RE = re.compile(r"(?:deadband|totband|schwelle)\s*(?:of|von)?\s*(\d+(?:[.,]\d+)?)\s*%|(\d+(?:[.,]\d+)?)\s*%\s*"
                         r"(?:deadband|totband)", re.I)  # fmt: skip
MONTH_NAMES = {
    name: i + 1
    for i, names in enumerate(
        [
            ("january", "januar", "jan"), ("february", "februar", "feb"), ("march", "märz", "maerz", "mar"),
            ("april", "apr"), ("may", "mai"), ("june", "juni", "jun"), ("july", "juli", "jul"),
            ("august", "aug"), ("september", "sep", "sept"), ("october", "oktober", "oct", "okt"),
            ("november", "nov"), ("december", "dezember", "dec", "dez"),
        ]
    )
    for name in names
}  # fmt: skip
AMOUNT = r"(\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)"
PRICE = re.compile(rf"(?:€|EUR)\s?{AMOUNT}|{AMOUNT}\s?(?:€|EUR\b|euros?\b)", re.I)
NOT_THEIRS = re.compile(r"\byou (?:offered|proposed|suggest\w*)\b|\byour (?:offer|price|proposal) of\b|\bihr angebot\b",
                        re.I)  # fmt: skip
OFFER_WORDS = re.compile(
    r"\b(alternatively|alternativ|we (?:can|could|would|propose|offer)|our (?:price|offer|angebot)|beside this|"
    r"in return|price of|lower our price|wir (?:können|bieten)|unser (?:preis|angebot))\b",
    re.I,
)


def number(text):
    """'137.00', '137,00', '1.234,56', '1,234.56', '23.400' -> float."""
    text = str(text).strip().replace(" ", "").rstrip(".,")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text):
        return float(re.sub(r"[.,]", "", text))
    if "," in text and "." in text:
        dec = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if dec == "," else ",", "").replace(dec, ".")
        return float(text)
    return float(text.replace(",", "."))


def parse_date(text):
    if m := ISO_RE.search(text):
        return m.group(1)
    if m := DOTTED_RE.search(text):
        d, mon, y = (int(x) for x in m.groups())
        if 1 <= d <= 31 and 1 <= mon <= 12:
            return f"{y}-{mon:02d}-{d:02d}"
    for d, mon, y in LONG_DATE_RE.findall(text):
        if (month := MONTH_NAMES.get(mon.lower())) and 1 <= int(d) <= 31:
            return f"{y}-{month:02d}-{int(d):02d}"
    return None


def pin_clause(case, text):
    """(ok, clause): the index-clause option a text names - None for "no clause" - or (False, None)
    when it names a clause that cannot be pinned to exactly one option."""
    if NO_CLAUSE_RE.search(text):
        return True, None
    opts = [o for o in (case.get("term_options") or {}).get("index_clause") or [] if o]
    named = [o for o in opts if re.search(rf"(?<![\w-]){re.escape(o['series'])}(?![\w-])", text, re.I)]
    if not named and len({o["series"] for o in opts}) == 1:
        named = opts  # the case has one series: "a Preisgleitklausel" means it
    if m := DEADBAND_RE.search(text):
        band = number(m.group(1) or m.group(2))
        named = [o for o in named if abs(o.get("deadband_pct", 0) - band) < 1e-6]
    if len({key(o) for o in named}) == 1:
        return True, named[0]
    return False, None


def terms_in(case, cond):
    """The negotiable terms one condition text names: {field: value}, or None if it names an index
    clause that cannot be pinned."""
    negotiable, t = case.get("negotiable", []), {}
    if "index_clause" in negotiable and (CLAUSE_RE.search(cond) or NO_CLAUSE_RE.search(cond)):
        ok, clause = pin_clause(case, cond)
        if not ok:
            return None
        t["index_clause"] = clause
    if "payment_days" in negotiable and (m := PAYMENT_RE.search(cond)):
        t["payment_days"] = int(m.group(1) or m.group(2))
    if "effective_date" in negotiable and (d := parse_date(cond)):
        t["effective_date"] = d
    if "lead_time_days" in negotiable and (m := LEAD_RE.search(cond)):
        t["lead_time_days"] = int(m.group(1))
    if "volume_per_year" in negotiable and (m := VOLUME_RE.search(cond)):
        t["volume_per_year"] = int(number(m.group(1)))
    if "contract_months" in negotiable and (m := MONTHS_RE.search(cond)):
        t["contract_months"] = int(m.group(1))
    if "ppm_commitment" in negotiable and (m := PPM_RE.search(cond)):
        t["ppm_commitment"] = int(m.group(1))
    if "chargeback_eur_per_complaint" in negotiable and (m := CHARGEBACK_RE.search(cond)):
        t["chargeback_eur_per_complaint"] = number(m.group(1) or m.group(2))
    if "incoterm" in negotiable and (m := INCOTERM_RE.search(cond)):
        t["incoterm"] = m.group(1)
    return t


def prose_alternatives(case, message):
    """The supplier's "Alternatively, with <terms>: EUR <price> per unit" lines as term dicts."""
    out = []
    body = re.sub(r"\s*\n\s*", " ", str(message.get("body") or message.get("text") or ""))
    for m in ALT_RE.finditer(body):
        t = terms_in(case, m.group("cond"))
        if t is None or not t:  # a price alone says nothing about which terms it is for
            continue
        try:
            t["unit_price"] = number(m.group("price"))
        except ValueError:
            continue
        out.append(t)
    return out


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", str(text or "").strip()) if s]


def prose_offers(case, body):
    """Every sentence that offers a price, with the terms it names (DualAgent's code reading)."""
    out = []
    for s in sentences(re.sub(r"\s*\n\s*", " ", body or "")):
        if NOT_THEIRS.search(s) or not OFFER_WORDS.search(s):
            continue
        prices = [m.group(1) or m.group(2) for m in PRICE.finditer(s)]
        if not prices:
            continue
        t = terms_in(case, s) or {}
        try:
            t["unit_price"] = number(prices[-1])  # "from EUR 126.53 to EUR 138.55": the new one
        except ValueError:
            continue
        out.append({"terms": t, "quote": s.strip()[:300], "source": "code"})
    return out
