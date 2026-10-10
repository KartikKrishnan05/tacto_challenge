"""The analyst of the dual agent: what one supplier thread tells us, kept current in the background.

On every supplier e-mail `Analyst.observe` updates a thread-safe `Intel` snapshot in two layers:

  structured  from the API, no model: standing term sheet and its move since the last round, status,
              signals, claim results, missing fields, who wrote (`from`) - plus code reading of the
              body: offers stated only in prose, cost drivers ("LCI-C rose 2.7 % ... 30 % of our
              cost"), questions, a rough tone. Ready at once.
  model       one extraction call per e-mail (JSON) in a worker thread: the same items read better,
              plus constraints, lever hints, tone, relationship style, withdraw language. Every
              figure it returns must appear in the e-mail, or it is dropped - the model reads, it
              never chooses a number.

Code then does the arithmetic: the C of every prose offer, the weighted cost impact of the stated
drivers against the requested increase, data claims for newly named indices, and `walk_risk` over
the whole thread. Between e-mails a worker mines the dossier for clues about the open gap.
"""

import copy
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field

from . import claims as cl
from . import writing
from .cost import complete_offer, must_haves_ok, total_cost
from .docs import standard_terms

log = logging.getLogger("arena")

BOSS_ROLES = {"boss", "owner", "md", "ceo", "management", "director", "head", "gf", "board", "managing"}
# walk_risk: points per signal, summed and clipped to 1
WALK_WEIGHTS = {
    "final_offer": 0.45,
    "price_unchanged_2_rounds": 0.25,
    "price_unchanged_1_round": 0.10,
    "reply_delay_rising": 0.10,
    "validity_shrinking": 0.10,
    "escalated_to_boss": 0.15,
    "tone_drop": 0.20,
    "withdraw_language": 0.30,
}
STALE_TOL = 0.001  # a move under 0.1 % of C is no move
WITHDRAW = re.compile(
    r"\b(withdraw\w*|end (?:our|the|these) (?:talks|negotiation|discussion)s?|"
    r"no further (?:concession|movement|room)s?|"
    r"cannot go (?:any )?(?:lower|further)|our last word|last offer|look elsewhere|terminate|break off|"
    r"not continue|step back from)\b",
    re.I,
)
POSITIVE = re.compile(
    r"\b(happy|pleased|glad|appreciate\w*|fair point|thank\w*|value|welcome|good cooperation)\b", re.I
)
NEGATIVE = re.compile(
    r"\b(disappoint\w*|unacceptable|cannot accept|regret\w*|insult\w*|surpris\w*|frustrat\w*|"
    r"not acceptable|no reciprocal|final)\b",
    re.I,
)
RELATIONSHIP = re.compile(
    r"\b(generations?|family|long-standing|partnership|our foundry|regional partners|loyal\w*|years of)\b", re.I
)
MONTHS = {
    m: i + 1
    for i, ms in enumerate(
        [
            ("january", "januar", "jan"),
            ("february", "februar", "feb"),
            ("march", "märz", "mar"),
            ("april", "apr"),
            ("may", "mai"),
            ("june", "juni", "jun"),
            ("july", "juli", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oktober", "oct", "okt"),
            ("november", "nov"),
            ("december", "dezember", "dec", "dez"),
        ]  # fmt: skip
    )
    for m in ms
}
PRICE = re.compile(r"(?:€|EUR)\s?(\d{1,6}(?:[.,]\d{1,2})?)|(\d{1,6}(?:[.,]\d{1,2})?)\s?(?:€|EUR\b|euros?\b)", re.I)
PAYMENT = re.compile(
    r"(?:zahlungsziel|payment(?: terms?)?(?: at| of| in)?)\s+(\d{1,3})\s+(?:days|tage)|(\d{1,3})\s+(?:days|tage)\s+net",
    re.I,
)
ISO = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
LONG_DATE = re.compile(r"\b(\d{1,2})\.?\s+([A-Za-zä]+)\s+(20\d\d)\b")
LEAD = re.compile(r"lead time (?:of )?(\d{1,3}) days", re.I)
MONTHS_TERM = re.compile(r"\b(\d{1,2})[- ]month", re.I)
VOLUME = re.compile(r"\b(\d{1,3}(?:[.,]\d{3})+|\d{3,6})\s+(?:pcs|pieces|units)\s+(?:per|a)\s+year", re.I)
NOT_THEIRS = re.compile(r"\byou (?:offered|proposed|suggest\w*)\b|\byour (?:offer|price|proposal) of\b", re.I)
OFFER_WORDS = re.compile(
    r"\b(alternatively|we (?:can|could|would|propose|offer)|our (?:price|offer|angebot)|beside this|in return|"
    r"price of|lower our price)\b",
    re.I,
)
DRIVER_VERB = re.compile(
    r"(?P<name>[\w-]+(?: [\w-]+){0,3}?)\s+(?:has |have )?(?P<verb>rose|risen|increased|went up|climbed|grew|"
    r"fell|dropped|decreased|went down|declined)\s+(?:by\s+)?(?P<chg>[+-]?\d+(?:[.,]\d+)?)\s?%",
    re.I,
)
SHARE = re.compile(
    r"(?P<share>\d+(?:[.,]\d+)?)\s?%\s+of\s+(?:our|the|its)\s+(?:total\s+|production\s+|unit\s+)?(?:cost|costs|price)",
    re.I,
)
CLUE_WORDS = (
    "cash", "liquidity", "payment", "skonto", "discount", "capacity", "volume", "contract", "index", "energy",
    "electricity", "gas", "wage", "labour", "steel", "scrap", "approval", "effective", "hold", "price",
    "short-time", "bank", "order book", "utilisation", "utilization", "lead time", "invest",
)  # fmt: skip

ANALYST_SYSTEM = """You are a procurement analyst reading one e-mail from an industrial supplier to our
purchasing team. Extract what it says - do not judge or advise. Copy every figure exactly as written.

Return ONE JSON object and nothing else:
{"prose_offers": [{"unit_price": 0.0, "payment_days": null, "effective_date": null, "other_terms": {},
                   "quote": "the sentence"}],
 "cost_drivers": [{"name": "LCI-C or 'energy'", "change_pct": 0.0, "cost_share_pct": null, "quote": "..."}],
 "named_indices": ["index series codes named, e.g. PPI-D351"],
 "constraints": ["limits they state, e.g. 'earliest effective date is 2027-01-01'"],
 "lever_hints": [{"field": "a negotiable field", "hint": "what they want or offer on it"}],
 "tone": 0,
 "relationship_style": "relationship | formal | transactional",
 "withdraw_language": false,
 "questions_to_us": ["questions they asked us"],
 "missing_info": ["what we still do not know that would help us, e.g. 'cost breakdown'"]}

- prose_offers: every price the supplier offers in the text with the terms it is tied to, including
  alternatives ("Alternatively, with 30 days net: EUR 137.00"). Not prices we offered, not old prices.
  effective_date as YYYY-MM-DD.
- tone: -2 (hostile) .. +2 (warm). withdraw_language: true only if they threaten to end the talks or
  withdraw the offer."""


@dataclass
class Mail:
    """One supplier e-mail, as walk_risk reads it."""

    status: str | None = None
    c: float | None = None  # C of its standing sheet
    price: float | None = None
    delay: float | None = None
    validity: float | None = None
    role: str | None = None
    tone: float | None = None
    withdraw: bool = False


@dataclass
class Intel:
    supplier_id: str
    mails: int = 0
    status: str | None = None
    sender: str | None = None
    standing: dict = field(default_factory=dict)  # {id, terms, c}
    delta: dict = field(default_factory=dict)  # vs the previous supplier mail: {price, c}
    offers: list = field(default_factory=list)  # every sheet of its latest mail: {id, terms, c}
    signals: dict = field(default_factory=dict)
    claim_results: list = field(default_factory=list)
    missing_fields: list = field(default_factory=list)
    unchanged_rounds: int = 0
    prose_offers: list = field(default_factory=list)  # {terms, c, beats_standing, in_sheet, quote, source}
    cost_drivers: list = field(default_factory=list)  # {name, series, change_pct, share_pct, data_change_pct}
    cost_impact_pct: float | None = None
    asked_pct: float | None = None
    cost_gap_pct: float | None = None
    new_claims: list = field(default_factory=list)  # claims.Claim for indices they named
    constraints: list = field(default_factory=list)
    lever_hints: list = field(default_factory=list)
    tone: float | None = None
    relationship_style: str | None = None
    withdraw_language: bool = False
    questions: list = field(default_factory=list)
    missing_info: list = field(default_factory=list)
    clues: list = field(default_factory=list)  # {source, text}
    walk_risk: float = 0.0
    walk_signals: dict = field(default_factory=dict)
    llm: str = "off"  # the model layer for the latest mail: off | pending | done | failed
    updated_at: float = 0.0

    def to_log(self):
        d = asdict(self)
        d["new_claims"] = [{"key": c.key, "sentence": c.sentence, "favourable": c.favourable} for c in self.new_claims]
        return d


# --- the arithmetic -------------------------------------------------------------------------------
def weighted_impact(drivers):
    """The price effect of cost drivers, in %: Σ change × cost share (drivers without a share skip)."""
    parts = [d["change_pct"] * d["share_pct"] / 100 for d in drivers if d.get("share_pct") is not None]
    return round(sum(parts), 2) if parts else None


def walk_risk(mails):
    """(0..1, {signal: points}) over the whole thread, oldest mail first."""
    if not mails:
        return 0.0, {}
    last, prev = mails[-1], mails[:-1]
    if last.status == "walked_away":
        return 1.0, {"walked_away": 1.0}
    sig = {}
    if last.status == "final_offer":
        sig["final_offer"] = WALK_WEIGHTS["final_offer"]
    if (n := unchanged(mails)) >= 2:
        sig["price_unchanged_2_rounds"] = WALK_WEIGHTS["price_unchanged_2_rounds"]
    elif n == 1:
        sig["price_unchanged_1_round"] = WALK_WEIGHTS["price_unchanged_1_round"]
    delays = [m.delay for m in prev if m.delay is not None]
    if last.delay is not None and delays and last.delay > min(delays):
        sig["reply_delay_rising"] = WALK_WEIGHTS["reply_delay_rising"]
    valid = [m.validity for m in prev if m.validity is not None]
    if last.validity is not None and valid and last.validity < max(valid):
        sig["validity_shrinking"] = WALK_WEIGHTS["validity_shrinking"]
    if _boss(last.role) and any(m.role and not _boss(m.role) for m in prev):
        sig["escalated_to_boss"] = WALK_WEIGHTS["escalated_to_boss"]
    tones = [m.tone for m in prev if m.tone is not None]
    if last.tone is not None and tones and tones[-1] - last.tone >= 2:
        sig["tone_drop"] = WALK_WEIGHTS["tone_drop"]
    if last.withdraw:
        sig["withdraw_language"] = WALK_WEIGHTS["withdraw_language"]
    return round(min(1.0, sum(sig.values())), 2), sig


def unchanged(mails):
    """How many replies in a row left the standing offer where it was (in C, else price)."""
    n = 0
    for a, b in zip(reversed(mails[:-1]), reversed(mails[1:]), strict=True):
        x, y = (a.c, b.c) if a.c and b.c else (a.price, b.price)
        if x is None or y is None or abs(x - y) > STALE_TOL * x:
            break
        n += 1
    return n


def _boss(role):
    return bool(role) and any(w in role.lower() for w in BOSS_ROLES)


# --- reading the text by code ----------------------------------------------------------------------
def _num(text):
    text = text.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", text):
        return float(re.sub(r"[.,]", "", text))
    return float(text.replace(",", "."))


def _date(sentence):
    if m := ISO.search(sentence):
        return m.group(1)
    for d, mon, y in LONG_DATE.findall(sentence):
        if (k := MONTHS.get(mon.lower())) and 1 <= int(d) <= 31:
            return f"{y}-{k:02d}-{int(d):02d}"
    return None


def prose_terms(sentence, negotiable):
    """The terms one sentence states: {field: value} for the negotiable fields it names."""
    t = {}
    prices = [m.group(1) or m.group(2) for m in PRICE.finditer(sentence)]
    if prices:
        t["unit_price"] = _num(prices[-1])  # "from EUR 126.53 to EUR 138.55": the new one
    if "payment_days" in negotiable and (m := PAYMENT.search(sentence)):
        t["payment_days"] = int(m.group(1) or m.group(2))
    if "effective_date" in negotiable and re.search(r"\b(from|ab|effective|valid|start)", sentence, re.I):
        if d := _date(sentence):
            t["effective_date"] = d
    if "lead_time_days" in negotiable and (m := LEAD.search(sentence)):
        t["lead_time_days"] = int(m.group(1))
    if "contract_months" in negotiable and (m := MONTHS_TERM.search(sentence)):
        t["contract_months"] = int(m.group(1))
    if "volume_per_year" in negotiable and (m := VOLUME.search(sentence)):
        t["volume_per_year"] = int(_num(m.group(1)))
    return t


def prose_offers_by_code(body, negotiable):
    out = []
    for s in writing.sentences(re.sub(r"\s*\n\s*", " ", body or "")):
        if NOT_THEIRS.search(s) or not OFFER_WORDS.search(s):
            continue
        t = prose_terms(s, negotiable)
        if t.get("unit_price"):
            out.append({"terms": t, "quote": s.strip(), "source": "code"})
    return out


def drivers_by_code(body, series_ids=()):
    out = []
    for s in writing.sentences(re.sub(r"\s*\n\s*", " ", body or "")):
        verb, share = DRIVER_VERB.search(s), SHARE.search(s)
        if not verb:
            continue
        chg = _num(verb.group("chg").lstrip("+"))
        if verb.group("verb").lower() in ("fell", "dropped", "decreased", "went down", "declined"):
            chg = -abs(chg)
        name = verb.group("name").split()[-1]
        series = next((i for i in series_ids if i.lower() == name.lower()), None)
        out.append(
            {
                "name": name,
                "series": series,
                "change_pct": chg,
                "share_pct": _num(share.group("share")) if share else None,
                "quote": s.strip(),
                "source": "code",
            }
        )
    return out


def tone_by_code(body):
    pos, neg = len(POSITIVE.findall(body or "")), len(NEGATIVE.findall(body or ""))
    return max(-2.0, min(2.0, round((pos - 2 * neg) / 2)))


def _appears(value, text):
    """A figure the model returned is real only if the e-mail has it."""
    if value is None:
        return False
    v = float(value)
    forms = {f"{v:.2f}", f"{v:.1f}", f"{v:g}", f"{v:.2f}".replace(".", ","), f"{v:.1f}".replace(".", ",")}
    if v == int(v):
        forms.add(str(int(v)))
    return any(re.search(rf"(?<![\d.,]){re.escape(f)}(?![\d])", text) for f in forms)


# --- the analyst -----------------------------------------------------------------------------------
class Analyst:
    """One supplier thread. `observe` is called with every new state of the thread; `snapshot` is
    safe to call from any thread at any time."""

    def __init__(self, case, sid, package=None, dossier=None, complete=None, pool=None, rec=None):
        self.case, self.sid, self.package, self.dossier = case, sid, package, dossier or {}
        self.complete, self.pool, self.rec = complete, pool, rec
        self._lock = threading.Lock()
        self._intel = Intel(supplier_id=sid, llm="off" if complete is None else "pending")
        self._llm = {}  # mail index -> parsed model extraction
        self._pending = {}  # mail index -> Future
        self._events = []
        self.series_ids = self._series_ids()
        self.known_claims = set()

    def _series_ids(self):
        ids = set()
        if self.package is not None and getattr(self.package, "db", None):
            try:
                ids |= {r["id"] for r in self.package.query("SELECT id FROM index_series")}
            except Exception:
                pass
        ids |= {k[4:].split("@")[0] for k in (self.case.get("facts") or {}) if k.startswith("idx:")}
        return sorted(ids, key=len, reverse=True)

    # -- the interface ---------------------------------------------------------------------------
    def observe(self, events):
        """A new state of the thread: structured layer now, the model layer in the background."""
        with self._lock:
            self._events = list(events)
        self._rebuild()
        mails = [i for i, m in enumerate(events) if m.get("from") != "buyer"]
        if self.complete is not None and self.pool is not None and mails:
            i = mails[-1]
            with self._lock:
                start = i not in self._llm and i not in self._pending
                if start:
                    self._pending[i] = self.pool.submit(self._extract, i, events[i])
                    self._intel.llm = "pending"
        if self.pool is not None:
            self.pool.submit(self._mine)
        return self

    def wait(self, timeout):
        """Wait up to `timeout` seconds for the model layer of the latest mail; True if it is in."""
        with self._lock:
            futures = list(self._pending.values())
        deadline = time.monotonic() + max(0.0, timeout)
        for f in futures:
            try:
                f.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                return False
        return True

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._intel)

    # -- the structured layer --------------------------------------------------------------------
    def _rebuild(self):
        with self._lock:
            events, llm = list(self._events), dict(self._llm)
            clues = self._intel.clues
        case, negotiable = self.case, self.case["negotiable"]
        std = standard_terms(events)
        sup = [(i, m) for i, m in enumerate(events) if m.get("from") != "buyer"]
        if not sup:
            return
        intel = Intel(supplier_id=self.sid, mails=len(sup), clues=clues)

        def c(terms):
            try:
                return total_cost(case, terms, std) if terms.get("unit_price") else None
            except Exception:
                return None

        history = []
        for i, m in sup:
            sheets = m.get("term_sheets") or []
            terms = sheets[0]["terms"] if sheets else {}
            x = llm.get(i) or {}
            sig = m.get("signals") or {}
            history.append(
                Mail(
                    status=m.get("status"),
                    c=c(terms),
                    price=terms.get("unit_price"),
                    delay=sig.get("reply_delay_days"),
                    validity=sig.get("validity_days"),
                    role=(m.get("from") or "").split("@")[0] or None,
                    tone=x.get("tone") if isinstance(x.get("tone"), int | float) else tone_by_code(m.get("body")),
                    withdraw=bool(x.get("withdraw_language")) or bool(WITHDRAW.search(m.get("body") or "")),
                )
            )
        i, last = sup[-1]
        x = llm.get(i) or {}
        sheets = last.get("term_sheets") or []
        intel.status, intel.sender = last.get("status"), last.get("from")
        intel.signals = dict(last.get("signals") or {})
        intel.claim_results = list(last.get("claim_results") or [])
        intel.missing_fields = list(last.get("missing_fields") or [])
        intel.offers = [{"id": s["term_sheet_id"], "terms": s["terms"], "c": c(s["terms"])} for s in sheets]
        intel.standing = intel.offers[0] if intel.offers else {}
        if len(history) > 1 and history[-1].price is not None and history[-2].price is not None:
            intel.delta = {
                "price": round(history[-1].price - history[-2].price, 2),
                "c": round((history[-1].c or 0) - (history[-2].c or 0), 2),
            }
        intel.unchanged_rounds = unchanged(history)
        intel.walk_risk, intel.walk_signals = walk_risk(history)
        intel.tone = history[-1].tone
        intel.withdraw_language = history[-1].withdraw
        body = last.get("body") or ""

        # (a) offers stated only in prose, priced by code
        found = prose_offers_by_code(body, negotiable)
        for o in x.get("prose_offers") or []:
            if not isinstance(o, dict) or not _appears(o.get("unit_price"), body):
                continue
            t = {"unit_price": float(o["unit_price"])}
            for f in ("payment_days", "effective_date"):
                if o.get(f) is not None and f in negotiable:
                    t[f] = o[f]
            for f, v in (o.get("other_terms") or {}).items():
                if f in negotiable and v is not None:
                    t[f] = v
            found.append({"terms": t, "quote": str(o.get("quote") or "")[:300], "source": "model"})
        base = intel.standing.get("terms") or {}
        c_standing = intel.standing.get("c")
        seen = []
        for o in found:
            stated = o["terms"]
            sheet = next((s for s in sheets if all(s["terms"].get(k) == v for k, v in stated.items())), None)
            match = sheet["term_sheet_id"] if sheet else None
            # a prose offer that is a sheet takes the sheet's terms; else what it left out stays as the standing offer
            terms = complete_offer(case, {**std, **(sheet["terms"] if sheet else {**base, **stated})})
            if any(terms == s for s in seen):
                continue
            seen.append(terms)
            cost_ = c(terms)
            intel.prose_offers.append(
                {
                    "terms": terms,
                    "stated": stated,
                    "c": cost_,
                    "in_sheet": match,
                    "beats_standing": bool(cost_ and c_standing and cost_ < c_standing - 0.01),
                    "must_haves_ok": must_haves_ok(case, terms, std),
                    "quote": o["quote"],
                    "source": o["source"],
                }
            )

        # (b) cost drivers -> the weighted impact against the increase asked
        drivers = {}
        for i2, m in sup:  # every mail: a driver named once still counts
            for d in drivers_by_code(m.get("body"), self.series_ids):
                drivers.setdefault((d["series"] or d["name"]).lower(), d)
            for d in (llm.get(i2) or {}).get("cost_drivers") or []:
                if not isinstance(d, dict) or not _appears(d.get("change_pct"), m.get("body") or ""):
                    continue
                share = d.get("cost_share_pct")
                share = float(share) if share is not None and _appears(share, m.get("body") or "") else None
                name = str(d.get("name") or "?")
                series = next((s for s in self.series_ids if s.lower() == name.lower()), None)
                key = (series or name).lower()
                if key not in drivers or (drivers[key]["share_pct"] is None and share is not None):
                    drivers[key] = {
                        "name": name, "series": series, "change_pct": float(d["change_pct"]),
                        "share_pct": share, "quote": str(d.get("quote") or "")[:300], "source": "model",
                    }  # fmt: skip
        for d in drivers.values():
            if d["series"] and (got := cl.index_change(self.package, case, d["series"])):
                d["data_change_pct"] = got[0]
        intel.cost_drivers = list(drivers.values())
        intel.cost_impact_pct = weighted_impact(intel.cost_drivers)
        intel.asked_pct = self._asked_pct(events)
        if intel.cost_impact_pct is not None and intel.asked_pct is not None:
            intel.cost_gap_pct = round(intel.asked_pct - intel.cost_impact_pct, 2)

        # (c) indices they named that the data can speak to
        named = {d["series"] for d in intel.cost_drivers if d["series"]}
        for _, m in sup:
            text = m.get("body") or ""
            named |= {s for s in self.series_ids if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text)}
        for i2, _ in sup:
            named |= {s for s in (llm.get(i2) or {}).get("named_indices") or [] if s in self.series_ids}
        for series in sorted(named):
            if got := cl.index_change(self.package, case, series):
                chg, a, b = got
                asked = intel.asked_pct
                intel.new_claims.append(
                    cl.Claim(
                        key=f"index:{series}",
                        kind="index",
                        ids=[a, b],
                        figure=chg,
                        favourable=chg <= 1.0 or (asked is not None and chg < asked / 2),
                        sentence=f"{cl.series_name(self.package, series)} changed {chg:+.1f}% "
                        f"from {a.split('@')[1]} to {b.split('@')[1]} ({a}, {b}).",
                        note="an index the supplier named",
                        strength=max(0.0, (asked or 0) - chg) / 4,
                    )
                )

        # (d)-(f) what the model read; code fallbacks where it has nothing
        intel.constraints = [str(s)[:200] for s in x.get("constraints") or []][:6]
        intel.lever_hints = [
            {"field": h.get("field"), "hint": str(h.get("hint") or "")[:200]}
            for h in x.get("lever_hints") or []
            if isinstance(h, dict) and h.get("field") in negotiable
        ][:6]
        style = x.get("relationship_style")
        intel.relationship_style = (
            style if style in ("relationship", "formal", "transactional")
            else "relationship" if RELATIONSHIP.search(" ".join(m.get("body") or "" for _, m in sup)) else None
        )  # fmt: skip
        intel.questions = [str(q)[:300] for q in x.get("questions_to_us") or []] or [
            s for s in writing.sentences(re.sub(r"\s*\n\s*", " ", body)) if s.endswith("?")
        ][:4]
        missing = [str(q)[:200] for q in x.get("missing_info") or []]
        names = " ".join(a.get("name", "") for _, m in sup for a in m.get("attachments") or []).lower()
        if "breakdown" not in names:
            missing.append("cost breakdown")
        missing += [f"{f} (closes on standard terms)" for f in intel.missing_fields]
        intel.missing_info = list(dict.fromkeys(missing))[:8]
        if self.complete is None:
            intel.llm = "off"
        else:
            intel.llm = "failed" if llm.get(i) == {} else "done" if i in llm else "pending"
        intel.updated_at = time.time()
        with self._lock:
            intel.clues = self._intel.clues  # the miner may have written meanwhile
            if i in self._pending and i not in self._llm:
                intel.llm = "pending"
            self._intel = intel

    def _asked_pct(self, events):
        trigger = cl._fact(self.package, self.case, self.case["trigger_doc"]) if self.case.get("trigger_doc") else None
        if trigger and trigger.get("asked_pct") is not None:
            return float(trigger["asked_pct"])
        current = self.case["buyer"].get("current_unit_price")
        prices = (
            s["terms"].get("unit_price") for m in events if m.get("from") != "buyer" for s in m.get("term_sheets") or []
        )
        opening = next(prices, None)
        if current and opening and self.case.get("type") in ("price_increase", "raw_material") and opening > current:
            return round((opening / current - 1) * 100, 2)
        return None

    # -- the model layer -------------------------------------------------------------------------
    def _extract(self, index, mail):
        t0 = time.monotonic()
        user = "\n\n".join(
            [
                f"NEGOTIABLE FIELDS: {json.dumps(self.case['negotiable'])}; options "
                f"{json.dumps(self.case.get('term_options') or {}, default=str)}",
                f"THEIR TERM SHEETS IN THIS E-MAIL: {json.dumps(mail.get('term_sheets') or [], default=str)}",
                f"E-MAIL from {mail.get('from')} (status {mail.get('status')}):\n{(mail.get('body') or '')[:6000]}",
            ]
        )
        reply = parsed = error = None
        try:
            reply = self.complete(ANALYST_SYSTEM, user)
            parsed = writing.parse(reply)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            parsed = {}
        finally:
            if self.rec:
                self.rec.llm(
                    kind="analyst", supplier_id=self.sid, mail=index, seconds=round(time.monotonic() - t0, 2),
                    prompt=user, reply=reply, parsed=parsed, error=error,
                )  # fmt: skip
        with self._lock:
            self._llm[index] = parsed if isinstance(parsed, dict) else {}
            self._pending.pop(index, None)
        self._rebuild()
        return parsed

    # -- between mails ---------------------------------------------------------------------------
    def _mine(self):
        """Clues in the dossier for the open gap: notes, letters, news, quote remarks that mention the
        drivers, the levers or the supplier's situation, best first."""
        intel = self.snapshot()
        focus = set(CLUE_WORDS) | {d["name"].lower() for d in intel.cost_drivers}
        focus |= {h["field"].replace("_", " ").split()[0] for h in intel.lever_hints if h.get("field")}
        rows = []
        for r in self.dossier.get("correspondence") or []:
            rows.append((r.get("id"), r.get("text") or ""))
        for r in self.dossier.get("news") or []:
            rows.append((r.get("id"), f"{r.get('headline') or ''}. {r.get('body') or ''}"))
        for r in self.dossier.get("quotes") or []:
            if r.get("remarks"):
                rows.append((r.get("id"), str(r["remarks"])))
        for r in self.dossier.get("history") or []:
            if r.get("note"):
                rows.append((f"history {r.get('date')}", str(r["note"])))
        found = []
        for src, text in rows:
            for s in writing.sentences(text):
                hits = sum(w in s.lower() for w in focus)
                if hits:
                    found.append((hits, src, s.strip()[:240]))
        found.sort(key=lambda h: -h[0])
        clues, seen = [], set()
        for _, src, s in found:
            if s not in seen:
                seen.add(s)
                clues.append({"source": src, "text": s})
            if len(clues) == 8:
                break
        with self._lock:
            self._intel.clues = clues
