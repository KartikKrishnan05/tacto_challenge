"""Prepare before the clock (section 1): a dossier per supplier and one profile call per supplier.

`gather` (ported from BuyerAgent's `dossier.py`) collects everything the data package says about one
supplier and the case's parts - notes, letters, past negotiations, financials, our payment record,
scorecards, should-cost, cost drivers, contract clauses - cached per supplier and parts (R1.1). Cash
signals (R1.2) are read from the supplier's OWN words only - its letters and e-mails - plus its filed
accounts, never from our call and meeting notes (R12.19). `profiles` runs one model call per supplier
in parallel (R1.3), validates what comes back (R1.4) and falls back to a default profile (R1.5).
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .. import claims as cl
from .llm import parse_json
from .playbook import GOAL_WORDS, type_levers

log = logging.getLogger("arena")
CASH_WORDS = re.compile(
    r"\b(cash|liquidity|pay(?:ing)? our invoices|within \d+ days|cash discount|skonto|prepay|advance payment|"
    r"short[- ]time work|insolven\w*|credit line|working capital)\b",
    re.I,
)
OWN_WORDS = ("email", "letter")  # documents the supplier wrote; notes are ours
PROFILE_TIMEOUT_S = 90.0
_cache, _lock = {}, threading.Lock()

PROFILE_SYSTEM = """You are a senior procurement analyst at Hollerau Pumpenwerke, a German pump maker.
Before a negotiation by e-mail, read everything our data says about one supplier and write a short
profile for the buyer. Read between the lines: call and meeting notes, letters, past negotiations
(what it asked vs what it settled), its finances, how we pay it, its scorecards, the cost drivers.
Some notes are just noise.

Return ONE JSON object and nothing else:
{"persona": "who is at the desk and how they think (1-2 sentences)",
 "tone": "how to write to them (e.g. personal and appreciative; crisp and factual; ...)",
 "pressures": ["what presses the supplier right now"],
 "wants": ["what it values beyond price"],
 "levers": ["negotiable field names, most valuable to the SUPPLIER first"],
 "avoid": ["arguments or wording that would backfire with this supplier"],
 "cash_short": true or false,
 "claim_ranking": ["keys from ARGUMENTS, most convincing for this supplier first; leave out what would backfire"],
 "questions": ["1-3 questions worth asking it"],
 "angle": "the line of argument for the first e-mail, in one or two sentences"}"""


def safe_query(package, sql, params=()):
    try:
        return package.query(sql, params)
    except Exception:
        return []


def _text(package, ref, limit):
    try:
        text = package.document(ref)
    except (FileNotFoundError, ValueError, KeyError):
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("# ")]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()[:limit]


def gather(package, case, sid):
    """Everything about `sid` and the case's parts, as {section: rows}; cached per supplier and parts."""
    parts = tuple(case.get("parts") or [])
    cache_key = (sid, parts, case.get("trigger_doc"), id(package))
    with _lock:
        if cache_key in _cache:
            return _cache[cache_key]
    d = {"supplier_id": sid}
    if package is not None:
        try:
            d.update(_from_package(package, case, sid, parts))
        except Exception as e:  # data trouble never costs the case
            log.warning("dossier for %s failed: %s", sid, e)
    d["case_facts"] = {k: v for k, v in (case.get("facts") or {}).items() if sid in json.dumps(v, default=str)}
    d["cash_signals"] = cash_signals(d)
    with _lock:
        _cache[cache_key] = d
    return d


def cash_signals(d):
    """R1.2 + R12.19: cash-pressure words in the supplier's own letters and e-mails, and its accounts."""
    own = " ".join(str(r.get("text", "")) for r in d.get("correspondence", []) if r.get("kind") in OWN_WORDS)
    intel = (d.get("intel") or [{}])[0]
    eq, ci, ci0 = intel.get("equity_ratio_pct"), intel.get("credit_index"), intel.get("credit_index_12m_ago")
    hits = {m.group(0).lower() for m in CASH_WORDS.finditer(own)}
    if isinstance(eq, int | float) and eq < 20:
        hits.add(f"equity ratio {eq}%")
    if isinstance(ci, int | float) and isinstance(ci0, int | float) and ci - ci0 >= 15:
        hits.add(f"credit index worsened {ci0}->{ci}")
    return sorted(hits)


def _from_package(package, case, sid, parts):
    marks = ",".join("?" * len(parts)) or "''"
    q = lambda sql, params=(): safe_query(package, sql, params)  # noqa: E731
    d = {
        "supplier": q("SELECT id, name, country, payment_days, skonto_days, skonto_pct, incoterm, category, "
                      "standard_lead_time_days, abc_class, contract_id FROM suppliers WHERE id = ?", (sid,)),
        "intel": q("SELECT * FROM supplier_intel WHERE supplier_id = ?", (sid,)),
        "parts": q(f"SELECT id, description, category, material_group, price_unit, standard_price FROM parts "
                   f"WHERE id IN ({marks})", parts),
        "scorecards": q("SELECT id, quarter, otd_pct, ppm, qn_count, open_8d_overdue, colour FROM scorecards "
                        "WHERE supplier_id = ? ORDER BY quarter DESC LIMIT 4", (sid,)),
        "history": q("SELECT date, part_id, type, asked_pct, settled_pct, note FROM negotiation_history "
                     "WHERE supplier_id = ? ORDER BY date DESC LIMIT 12", (sid,)),
        "quotes": q(f"SELECT id, supplier_id, part_id, quote_date, unit_price, lead_time_days, payment_days, remarks "
                    f"FROM quotes WHERE part_id IN ({marks}) ORDER BY quote_date DESC LIMIT 10", parts),
        "should_cost": q(f"SELECT * FROM should_cost WHERE part_id IN ({marks})", parts),
        "cost_drivers": q(f"SELECT part_id, series_id, weight FROM cost_drivers WHERE part_id IN ({marks})", parts),
        "news": q("SELECT id, date, topic, headline, body FROM news WHERE supplier_id = ? ORDER BY date DESC LIMIT 6",
                  (sid,)),
        "payments": q("SELECT count(*) AS invoices, round(avg(julianday(paid_date) - julianday(due_date)), 1) AS "
                      "days_late FROM invoices WHERE supplier_id = ? AND invoice_date >= '2025-10-01'", (sid,)),
    }  # fmt: skip
    ctr = next((r["contract_id"] for r in d["supplier"] if r.get("contract_id")), None)
    d["contract"] = q("SELECT id, valid_from, valid_to, payment_days, notice_days, price_basis FROM contracts "
                      "WHERE id = ?", (ctr,)) if ctr else []  # fmt: skip
    d["clauses"] = q("SELECT id, text FROM contract_clauses WHERE contract_id = ?", (ctr,)) if ctr else []
    docs = q("SELECT id, date, kind, subject FROM documents WHERE supplier_id = ? ORDER BY date DESC LIMIT 18", (sid,))
    trigger = case.get("trigger_doc")
    for r in docs:
        r["text"] = _text(package, r["id"], 2500 if r["id"] == trigger else 420)
    d["correspondence"] = docs
    return d


def render(d):
    """The dossier as text for one model read."""
    out = [f"SUPPLIER {d['supplier_id']}"]
    for title, k in (
        ("Master data", "supplier"), ("Company (filed accounts, credit index: higher is worse)", "intel"),
        ("Contract", "contract"), ("Contract clauses", "clauses"), ("Scorecards, newest first", "scorecards"),
        ("Past negotiations (asked vs settled %)", "history"), ("Quotes for these parts", "quotes"),
        ("Should-cost (no margin)", "should_cost"), ("Cost drivers (series, weight)", "cost_drivers"),
        ("Parts", "parts"), ("News", "news"), ("How we paid them", "payments"),
    ):  # fmt: skip
        if d.get(k):
            rows = "\n".join("  " + json.dumps({a: b for a, b in r.items() if b not in (None, "")}, ensure_ascii=False,
                                               default=str) for r in d[k])  # fmt: skip
            out.append(f"{title}:\n{rows}")
    if d.get("correspondence"):
        out.append("Correspondence, call and meeting notes (newest first):")
        out += [f"  [{r.get('date')} {r.get('kind')} {r['id']}] {r.get('subject') or ''}: {r.get('text', '')}"
                for r in d["correspondence"]]  # fmt: skip
    if d.get("case_facts"):
        out.append(f"Facts only this case has: {json.dumps(d['case_facts'], ensure_ascii=False, default=str)[:3000]}")
    if d.get("cash_signals"):
        out.append(f"Cash signals in its own words or accounts: {', '.join(d['cash_signals'])}")
    return "\n".join(out)


def company_name(d):
    return ((d or {}).get("supplier") or [{}])[0].get("name")


# --- the profile (R1.3-R1.5) ------------------------------------------------------------------------
def default_profile(case, d):
    """R1.5: polite, factual, constructive; payment days first when cash signals exist, then the case
    type's levers. A missing profile never costs the case."""
    cash = bool((d or {}).get("cash_signals"))
    return {
        "persona": "", "tone": "polite, factual, constructive", "pressures": [], "wants": [],
        "levers": list(dict.fromkeys((["payment_days"] if cash and "payment_days" in case["negotiable"] else [])
                                     + type_levers(case))),
        "avoid": [], "cash_short": False, "claim_ranking": [], "questions": [], "angle": "", "source": "default",
    }  # fmt: skip


def validate(case, d, parsed, arg_keys):
    """R1.4: levers mapped onto the case's negotiable fields and claim keys onto real argument keys;
    anything that does not match is dropped. cash_short: the model says so or the dossier has cash
    signals and payment days is the top lever - in both cases only with a signal in the supplier's own
    words or accounts (R12.19)."""
    from ..writing import match_key

    profile = {**default_profile(case, d), **{k: v for k, v in (parsed or {}).items() if v not in (None, "")}}
    fields = {f: f for f in case["negotiable"] if f != "unit_price"}
    profile["levers"] = list(dict.fromkeys(f for t in profile.get("levers") or [] if (f := match_key(t, fields))))
    keys = {k: k for k in arg_keys}
    profile["claim_ranking"] = [k for t in profile.get("claim_ranking") or [] if (k := match_key(t, keys))]
    signals = bool((d or {}).get("cash_signals"))
    said = bool(profile.get("cash_short")) and not isinstance(profile.get("cash_short"), str)
    top = profile["levers"][:1] == ["payment_days"]
    profile["cash_short"] = signals and (said or top)
    for k in ("pressures", "wants", "avoid", "questions"):
        profile[k] = [str(x)[:300] for x in profile.get(k) or [] if x][:4]
    for k in ("persona", "tone", "angle"):
        profile[k] = str(profile.get(k) or "")[:400]
    profile["source"] = "model"
    return profile


def profiles(case, package, dossiers, complete, timeout=PROFILE_TIMEOUT_S):
    """One profile per supplier, the calls in parallel, all within `timeout` seconds together (R1.3)."""
    out = {sid: default_profile(case, dossiers.get(sid)) for sid in case.get("suppliers") or []}
    if complete is None or timeout <= 1:
        return out

    def one(sid):
        args = cl.candidates(package, case, sid)
        brief = {"title": case.get("title"), "type": case.get("type"), "goal": GOAL_WORDS.get(
            case["buyer"].get("category_goal")), "parts": case.get("parts"), "negotiable": case["negotiable"],
            "term_options": case.get("term_options"), "need_per_year": case["buyer"].get("need_per_year")}  # fmt: skip
        user = (
            f"CASE\n{json.dumps(brief, ensure_ascii=False, default=str)}\n\n{render(dossiers[sid])[:14000]}\n\n"
            "ARGUMENTS ([key] sentence - note)\n" + "\n".join(f"[{a.key}] {a.sentence} - {a.note}" for a in args)
        )
        parsed = parse_json(complete(PROFILE_SYSTEM, user, timeout=timeout))
        return validate(case, dossiers.get(sid), parsed, [a.key for a in args])

    pool = ThreadPoolExecutor(max_workers=max(1, len(out)), thread_name_prefix="profile")
    futures = {sid: pool.submit(one, sid) for sid in out}
    deadline = time.monotonic() + timeout
    for sid, f in futures.items():
        try:
            out[sid] = f.result(timeout=max(0.1, deadline - time.monotonic()))
        except Exception as e:
            log.warning("profile for %s failed (%s) - default profile", sid, e)
    pool.shutdown(wait=False)  # a hung model call must not hold the case
    return out
