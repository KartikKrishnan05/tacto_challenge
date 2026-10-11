"""A background analyst per supplier thread (R11.1-R11.8), ported from DualAgent's `intel.py`.

On every supplier e-mail `Analyst.observe` updates a thread-safe `Intel` snapshot in two layers:
  code    at once, no model: the standing sheet and its move, status, signals, claim results,
          missing fields, who wrote, offers stated only in prose (`prose`), cost drivers the supplier
          names ("LCI-C rose 2.7 % ... 30 % of our cost"), questions, a rough tone
  model   one extraction call per e-mail in a worker thread: the same items read better, plus
          constraints, lever hints, relationship style, withdraw language. Every figure it returns
          must appear in the e-mail, or it is dropped (R11.2) - the model reads, it never sets a number.
Code does the arithmetic: the weighted effect of the named drivers against the increase asked for
(`cost_gap_pct`, R11.4), checked arguments for indices the supplier names, and `walk_risk` (R11.5) -
a signal for the tone and the prose lock-in, never a close rule.
"""

import copy
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field

from .. import claims as cl
from ..docs import standard_terms
from . import prose
from .terms import complete

log = logging.getLogger("arena")

GRACE_S = 12.0  # R11.3: the negotiator waits at most this long for the model layer
BOSS_ROLES = {"boss", "owner", "md", "ceo", "management", "director", "head", "gf", "board", "managing"}
WALK_WEIGHTS = {
    "final_offer": 0.45, "price_unchanged_2_rounds": 0.25, "price_unchanged_1_round": 0.10,
    "reply_delay_rising": 0.10, "validity_shrinking": 0.10, "escalated_to_boss": 0.15, "tone_drop": 0.20,
    "withdraw_language": 0.30,
}  # fmt: skip
STALE_TOL = 0.001
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
    r"\b(disappoint\w*|unacceptable|cannot accept|regret\w*|insult\w*|surpris\w*|frustrat\w*|not acceptable|"
    r"no reciprocal|final)\b",
    re.I,
)
RELATIONSHIP = re.compile(
    r"\b(generations?|family|long-standing|partnership|our foundry|regional partners|loyal\w*|years of)\b", re.I
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
# A supplier asking for faster payment, from its own words (R11.7, R12.19)
CASH_ASK = re.compile(
    r"faster payment|quick(?:er)? payment|early payment|shorter payment|cash ?flow|liquidit|"
    r"pay(?:ment)?\s+(?:of\s+)?(?:our\s+)?invoices\s+(?:with)?in\s+\d+\s+days|schnellere zahlung",
    re.I,
)

SYSTEM = """You are a procurement analyst reading one e-mail from an industrial supplier to our
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
 "missing_info": ["what we still do not know that would help us"]}

- prose_offers: every price the supplier offers in the text with the terms it is tied to, including
  alternatives ("Alternatively, with 30 days net: EUR 137.00"). Not prices we offered, not old prices.
  effective_date as YYYY-MM-DD.
- tone: -2 (hostile) .. +2 (warm). withdraw_language: true only if they threaten to end the talks."""


@dataclass
class Mail:
    status: str | None = None
    c: float | None = None
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
    standing: dict = field(default_factory=dict)
    signals: dict = field(default_factory=dict)
    claim_results: list = field(default_factory=list)
    missing_fields: list = field(default_factory=list)
    unchanged_rounds: int = 0
    prose_offers: list = field(default_factory=list)  # {terms, stated, quote, source, in_sheet}
    alternatives: list = field(default_factory=list)  # R13.6 lines of the latest mail
    cost_drivers: list = field(default_factory=list)
    cost_impact_pct: float | None = None
    asked_pct: float | None = None
    cost_gap_pct: float | None = None
    new_claims: list = field(default_factory=list)
    constraints: list = field(default_factory=list)
    lever_hints: list = field(default_factory=list)
    cash_ask: bool = False
    tone: float | None = None
    relationship_style: str | None = None
    withdraw_language: bool = False
    questions: list = field(default_factory=list)
    walk_risk: float = 0.0
    walk_signals: dict = field(default_factory=dict)
    llm: str = "off"

    def to_log(self):
        d = asdict(self)
        d["new_claims"] = [{"key": c.key, "sentence": c.sentence, "favourable": c.favourable} for c in self.new_claims]
        return d


def weighted_impact(drivers):
    """The price effect of the drivers the supplier named, in %: sum of change x cost share (R11.4)."""
    parts = [d["change_pct"] * d["share_pct"] / 100 for d in drivers if d.get("share_pct") is not None]
    return round(sum(parts), 2) if parts else None


def unchanged(mails):
    """How many replies in a row left the standing offer where it was."""
    n = 0
    for a, b in zip(reversed(mails[:-1]), reversed(mails[1:]), strict=True):
        x, y = (a.c, b.c) if a.c and b.c else (a.price, b.price)
        if x is None or y is None or abs(x - y) > STALE_TOL * x:
            break
        n += 1
    return n


def _boss(role):
    return bool(role) and any(w in role.lower() for w in BOSS_ROLES)


def walk_risk(mails):
    """(0..1, {signal: points}) over the whole thread, oldest mail first (R11.5)."""
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


def tone_by_code(body):
    pos, neg = len(POSITIVE.findall(body or "")), len(NEGATIVE.findall(body or ""))
    return max(-2.0, min(2.0, round((pos - 2 * neg) / 2)))


def appears(value, text):
    """R11.2: a figure the model returned is real only if the e-mail has it."""
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    forms = {f"{v:.2f}", f"{v:.1f}", f"{v:g}", f"{v:.2f}".replace(".", ","), f"{v:.1f}".replace(".", ",")}
    if v == int(v):
        forms.add(str(int(v)))
    return any(re.search(rf"(?<![\d.,]){re.escape(f)}(?![\d])", text or "") for f in forms)


def drivers_by_code(body, series_ids=()):
    out = []
    for s in prose.sentences(re.sub(r"\s*\n\s*", " ", body or "")):
        verb, share = DRIVER_VERB.search(s), SHARE.search(s)
        if not verb:
            continue
        chg = prose.number(verb.group("chg").lstrip("+"))
        if verb.group("verb").lower() in ("fell", "dropped", "decreased", "went down", "declined"):
            chg = -abs(chg)
        name = verb.group("name").split()[-1]
        series = next((i for i in series_ids if i.lower() == name.lower()), None)
        out.append({"name": name, "series": series, "change_pct": chg,
                    "share_pct": prose.number(share.group("share")) if share else None,
                    "quote": s.strip(), "source": "code"})  # fmt: skip
    return out


def clean_extraction(parsed, body, negotiable):
    """R11.2: keep only what the e-mail itself says - every figure must appear in it."""
    if not isinstance(parsed, dict):
        return {}
    out = dict(parsed)
    offers = []
    for o in parsed.get("prose_offers") or []:
        if not isinstance(o, dict) or not appears(o.get("unit_price"), body):
            continue
        t = {"unit_price": float(o["unit_price"])}
        if o.get("payment_days") is not None and "payment_days" in negotiable and appears(o["payment_days"], body):
            t["payment_days"] = int(float(o["payment_days"]))
        if o.get("effective_date") and "effective_date" in negotiable:
            t["effective_date"] = str(o["effective_date"])[:10]
        for f, v in (o.get("other_terms") or {}).items():
            if f in negotiable and v is not None and (not isinstance(v, int | float) or appears(v, body)):
                t[f] = v
        offers.append({"terms": t, "quote": str(o.get("quote") or "")[:300], "source": "model"})
    out["prose_offers"] = offers
    drivers = []
    for d in parsed.get("cost_drivers") or []:
        if not isinstance(d, dict) or not appears(d.get("change_pct"), body):
            continue
        share = d.get("cost_share_pct")
        drivers.append({
            "name": str(d.get("name") or "?"), "change_pct": float(d["change_pct"]),
            "share_pct": float(share) if share is not None and appears(share, body) else None,
            "quote": str(d.get("quote") or "")[:300], "source": "model",
        })  # fmt: skip
    out["cost_drivers"] = drivers
    return out


class Analyst:
    """One supplier thread. `observe` with every new state of the thread; `snapshot` is safe from any
    thread at any time."""

    def __init__(self, case, sid, package=None, complete_fn=None, pool=None, cost_fn=None):
        self.case, self.sid, self.package = case, sid, package
        self.complete, self.pool = complete_fn, pool
        self.cost_fn = cost_fn  # (terms, std) -> C, for the unchanged-offer test
        self._lock = threading.Lock()
        self._intel = Intel(supplier_id=sid, llm="off" if complete_fn is None else "pending")
        self._llm, self._pending, self._events = {}, {}, []
        self.series_ids = self._series_ids()

    def _series_ids(self):
        ids = set()
        if self.package is not None:
            try:
                ids |= {r["id"] for r in self.package.query("SELECT id FROM index_series")}
            except Exception:
                pass
        ids |= {k[4:].split("@")[0] for k in (self.case.get("facts") or {}) if k.startswith("idx:")}
        return sorted(ids, key=len, reverse=True)

    def observe(self, events):
        with self._lock:
            self._events = list(events)
        self._rebuild()
        mails = [i for i, m in enumerate(events) if m.get("from") != "buyer"]
        if self.complete is not None and self.pool is not None and mails:
            i = mails[-1]
            with self._lock:
                if i not in self._llm and i not in self._pending:
                    self._pending[i] = self.pool.submit(self._extract, i, events[i])
        return self

    def wait(self, timeout=GRACE_S):
        """Wait up to `timeout` seconds for the model layer of the latest mail (R11.3)."""
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

    def _c(self, terms, std):
        if not self.cost_fn or not terms.get("unit_price"):
            return None
        try:
            return self.cost_fn(terms, std)
        except Exception:
            return None

    def _rebuild(self):
        with self._lock:
            events, llm = list(self._events), dict(self._llm)
        case, negotiable = self.case, self.case.get("negotiable", [])
        std = standard_terms(events)
        sup = [(i, m) for i, m in enumerate(events) if m.get("from") != "buyer"]
        if not sup:
            return
        intel = Intel(supplier_id=self.sid, mails=len(sup))
        history = []
        for i, m in sup:
            sheets = m.get("term_sheets") or []
            terms = sheets[0]["terms"] if sheets else {}
            x = llm.get(i) or {}
            sig = m.get("signals") or {}
            history.append(Mail(
                status=m.get("status"), c=self._c(terms, std), price=terms.get("unit_price"),
                delay=sig.get("reply_delay_days"), validity=sig.get("validity_days"),
                role=(m.get("from") or "").split("@")[0] or None,
                tone=x.get("tone") if isinstance(x.get("tone"), int | float) else tone_by_code(m.get("body")),
                withdraw=bool(x.get("withdraw_language")) or bool(WITHDRAW.search(m.get("body") or "")),
            ))  # fmt: skip
        i, last = sup[-1]
        x = llm.get(i) or {}
        sheets = last.get("term_sheets") or []
        body = last.get("body") or ""
        intel.status, intel.sender = last.get("status"), last.get("from")
        intel.signals = dict(last.get("signals") or {})
        intel.claim_results = list(last.get("claim_results") or [])
        intel.missing_fields = list(last.get("missing_fields") or [])
        intel.standing = {"id": sheets[0]["term_sheet_id"], "terms": sheets[0]["terms"]} if sheets else {}
        intel.unchanged_rounds = unchanged(history)
        intel.walk_risk, intel.walk_signals = walk_risk(history)
        intel.tone, intel.withdraw_language = history[-1].tone, history[-1].withdraw
        intel.cash_ask = bool(CASH_ASK.search(body))

        # offers stated only in prose (R11.8) and the "Alternatively" lines (R13.6)
        intel.alternatives = prose.prose_alternatives(case, last)
        found = [*({"terms": t, "quote": "", "source": "alternative"} for t in intel.alternatives),
                 *prose.prose_offers(case, body), *(x.get("prose_offers") or [])]  # fmt: skip
        base, seen = intel.standing.get("terms") or {}, []
        for o in found:
            stated = o["terms"]
            sheet = next((s for s in sheets if all(s["terms"].get(k) == v for k, v in stated.items())), None)
            terms = complete(case, {**(sheet["terms"] if sheet else {**base, **stated})}, std)
            if terms.get("unit_price") is None or any(terms == s for s in seen):
                continue
            seen.append(terms)
            intel.prose_offers.append(
                {
                    "terms": terms,
                    "stated": stated,
                    "quote": o.get("quote", ""),
                    "source": o.get("source"),
                    "in_sheet": sheet["term_sheet_id"] if sheet else None,
                }
            )

        # cost drivers -> the weighted effect against the increase asked for (R11.4)
        drivers = {}
        for i2, m in sup:
            for d in drivers_by_code(m.get("body"), self.series_ids):
                drivers.setdefault((d["series"] or d["name"]).lower(), d)
            for d in (llm.get(i2) or {}).get("cost_drivers") or []:
                series = next((s for s in self.series_ids if s.lower() == d["name"].lower()), None)
                k = (series or d["name"]).lower()
                if k not in drivers or (drivers[k]["share_pct"] is None and d["share_pct"] is not None):
                    drivers[k] = {**d, "series": series}
        intel.cost_drivers = list(drivers.values())
        intel.cost_impact_pct = weighted_impact(intel.cost_drivers)
        intel.asked_pct = self._asked_pct(events)
        if intel.cost_impact_pct is not None and intel.asked_pct is not None:
            intel.cost_gap_pct = round(intel.asked_pct - intel.cost_impact_pct, 2)

        # indices the supplier named that the data covers -> checked arguments. A rising index argues
        # its side and is kept out unless it rose less than half the increase asked for (R13.9).
        named = {d["series"] for d in intel.cost_drivers if d.get("series")}
        for _, m in sup:
            text = m.get("body") or ""
            named |= {s for s in self.series_ids if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text)}
        for i2, _ in sup:
            named |= {s for s in (llm.get(i2) or {}).get("named_indices") or [] if s in self.series_ids}
        for series in sorted(named):
            got = cl.index_change(self.package, case, series) if self.package else None
            if not got:
                continue
            chg, a, b = got
            asked = intel.asked_pct
            intel.new_claims.append(cl.Claim(
                key=f"index:{series}", kind="index", ids=[a, b], figure=chg,
                favourable=chg <= 0 or (asked is not None and chg < asked / 2),
                sentence=f"{cl.series_name(self.package, series)} changed {chg:+.1f}% "
                f"from {a.split('@')[1]} to {b.split('@')[1]} ({a}, {b}).",
                note="an index the supplier named", strength=max(0.0, (asked or 0) - chg) / 4,
            ))  # fmt: skip

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
            s for s in prose.sentences(re.sub(r"\s*\n\s*", " ", body)) if s.endswith("?")
        ][:4]
        if self.complete is None:
            intel.llm = "off"
        else:
            intel.llm = "failed" if llm.get(i) == {} else "done" if i in llm else "pending"
        with self._lock:
            self._intel = intel

    def _asked_pct(self, events):
        trigger = cl._fact(self.package, self.case, self.case["trigger_doc"]) if self.case.get("trigger_doc") else None
        if trigger and trigger.get("asked_pct") is not None:
            return float(trigger["asked_pct"])
        current = self.case["buyer"].get("current_unit_price")
        prices = (s["terms"].get("unit_price") for m in events if m.get("from") != "buyer"
                  for s in m.get("term_sheets") or [])  # fmt: skip
        opening = next(prices, None)
        if current and opening and self.case.get("type") in ("price_increase", "raw_material") and opening > current:
            return round((opening / current - 1) * 100, 2)
        return None

    def _extract(self, index, mail):
        user = "\n\n".join([
            f"NEGOTIABLE FIELDS: {json.dumps(self.case['negotiable'])}; options "
            f"{json.dumps(self.case.get('term_options') or {}, default=str)}",
            f"THEIR TERM SHEETS IN THIS E-MAIL: {json.dumps(mail.get('term_sheets') or [], default=str)}",
            f"E-MAIL from {mail.get('from')} (status {mail.get('status')}):\n{(mail.get('body') or '')[:6000]}",
        ])  # fmt: skip
        parsed = {}
        try:
            from .llm import parse_json

            parsed = clean_extraction(parse_json(self.complete(SYSTEM, user)), mail.get("body") or "",
                                      self.case.get("negotiable", []))  # fmt: skip
        except Exception as e:
            log.info("%s analyst extraction failed: %s", self.sid, e)
            parsed = {}
        with self._lock:
            self._llm[index] = parsed
            self._pending.pop(index, None)
        self._rebuild()
        return parsed
