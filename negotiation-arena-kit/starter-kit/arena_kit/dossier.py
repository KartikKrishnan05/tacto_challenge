"""What the data says about one supplier: the dossier code gathers before a case opens.

The case file points to a handful of facts; the supplier's real situation sits in the rest of the
package - call and meeting notes, letters, past negotiations, its financials, our payment record,
its scorecards, the cost structure of the part. `gather` collects all of it for one supplier and
the case's parts, `render` turns it into text a language model reads once, before the clock runs.
Built on `DataPackage`, so the Sunday release is included.
"""

import json
import re
import threading

CASH_WORDS = re.compile(
    r"\b(cash|liquidity|pay(?:ing)? our invoices|within \d+ days|cash discount|skonto|prepay|advance payment|"
    r"short[- ]time work|insolven|bank|credit line|working capital)\b",
    re.I,
)
_cache, _lock = {}, threading.Lock()


def _q(package, sql, params=()):
    try:
        return package.query(sql, params)
    except Exception:  # a package without SQLite, or a table this release does not have
        return []


def _text(package, ref, limit):
    try:
        text = package.document(ref)
    except (FileNotFoundError, ValueError, KeyError):
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("# ")]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()[:limit]


def gather(package, case, sid):
    """Everything about `sid` and the case's parts, as {section: rows}. Cached per supplier and parts."""
    parts = tuple(case.get("parts", []))
    key = (sid, parts, case.get("trigger_doc"))
    with _lock:
        if key in _cache:
            return _cache[key]
    d = {"supplier_id": sid}
    if package is not None:
        d.update(_from_package(package, case, sid, parts))
    # Hidden cases carry facts that exist only in the case (e.g. a new letter).
    d["case_facts"] = {k: v for k, v in (case.get("facts") or {}).items() if sid in json.dumps(v)}
    text = " ".join(str(r.get("text", "")) for r in d.get("correspondence", []))
    intel = (d.get("intel") or [{}])[0]
    eq, ci, ci0 = intel.get("equity_ratio_pct"), intel.get("credit_index"), intel.get("credit_index_12m_ago")
    d["cash_signals"] = sorted(
        {m.group(0).lower() for m in CASH_WORDS.finditer(text)}
        | ({f"equity ratio {eq}%"} if isinstance(eq, int | float) and eq < 20 else set())
        | (
            {f"credit index worsened {ci0}->{ci}"}
            if isinstance(ci, int | float) and isinstance(ci0, int | float) and ci - ci0 >= 15
            else set()
        )
    )
    with _lock:
        _cache[key] = d
    return d


def _from_package(package, case, sid, parts):
    marks = ",".join("?" * len(parts)) or "''"
    d = {
        "supplier": _q(
            package,
            "SELECT id, name, country, payment_days, skonto_days, skonto_pct, incoterm, "
            "category, standard_lead_time_days, abc_class, contract_id FROM suppliers WHERE id = ?",
            (sid,),
        ),
        "intel": _q(package, "SELECT * FROM supplier_intel WHERE supplier_id = ?", (sid,)),
        "sourcing": _q(package, f"SELECT * FROM sourcing WHERE part_id IN ({marks})", parts),
        "parts": _q(
            package,
            f"SELECT id, description, category, material_group, net_weight_kg, price_unit, "
            f"standard_price, abc_class FROM parts WHERE id IN ({marks})",
            parts,
        ),
        "scorecards": _q(
            package,
            "SELECT id, quarter, otd_pct, ppm, qn_count, open_8d_overdue, score, colour "
            "FROM scorecards WHERE supplier_id = ? ORDER BY quarter DESC LIMIT 4",
            (sid,),
        ),
        "history": _q(
            package,
            "SELECT date, part_id, type, asked_pct, settled_pct, note FROM negotiation_history "
            "WHERE supplier_id = ? ORDER BY date DESC LIMIT 12",
            (sid,),
        ),
        "price_conditions": _q(
            package,
            f"SELECT part_id, valid_from, valid_to, price, basis FROM price_conditions "
            f"WHERE supplier_id = ? AND part_id IN ({marks}) ORDER BY valid_from DESC LIMIT 6",
            (sid, *parts),
        ),
        "quotes": _q(
            package,
            f"SELECT id, supplier_id, part_id, quote_date, unit_price, lead_time_days, incoterm, "
            f"payment_days, tooling_eur, awarded, remarks FROM quotes WHERE part_id IN ({marks}) "
            f"ORDER BY quote_date DESC LIMIT 10",
            parts,
        ),
        "should_cost": _q(package, f"SELECT * FROM should_cost WHERE part_id IN ({marks})", parts),
        "cost_drivers": _q(
            package, f"SELECT part_id, series_id, weight FROM cost_drivers WHERE part_id IN ({marks})", parts
        ),
        "news": _q(
            package,
            "SELECT id, date, topic, headline, body FROM news WHERE supplier_id = ? ORDER BY date DESC LIMIT 6",
            (sid,),
        ),
        "quality": _q(
            package,
            f"SELECT count(*) AS notifications, sum(complaint_qty) AS complaints, "
            f"sum(cost_recharged_eur) AS recharged_eur, max(qn_date) AS last FROM quality_notifications "
            f"WHERE supplier_id = ? AND part_id IN ({marks}) AND qn_date >= '2025-10-01'",
            (sid, *parts),
        ),
        "payments": _q(
            package,
            "SELECT count(*) AS invoices, round(avg(julianday(paid_date) - julianday(invoice_date)), 1) "
            "AS days_to_pay, round(avg(julianday(paid_date) - julianday(due_date)), 1) AS days_late, "
            "round(avg(discount_taken), 2) AS discount_share FROM invoices WHERE supplier_id = ? "
            "AND invoice_date >= '2025-10-01'",
            (sid,),
        ),
        "forecast": _q(
            package,
            f"SELECT id, part_id, snapshot_month, qty_next_12m FROM forecasts WHERE part_id IN "
            f"({marks}) ORDER BY snapshot_month DESC LIMIT 2",
            parts,
        ),
    }
    ctr = next((r["contract_id"] for r in d["supplier"] if r.get("contract_id")), None)
    d["contract"] = (
        _q(
            package,
            "SELECT id, type, valid_from, valid_to, payment_days, incoterm, notice_days, price_basis "
            "FROM contracts WHERE id = ?",
            (ctr,),
        )
        if ctr
        else []
    )
    d["clauses"] = _q(package, "SELECT id, text FROM contract_clauses WHERE contract_id = ?", (ctr,)) if ctr else []
    docs = _q(
        package,
        "SELECT id, date, kind, subject, asked_pct, asked_price, effective_date, notice_days "
        "FROM documents WHERE supplier_id = ? ORDER BY date DESC LIMIT 18",
        (sid,),
    )
    seen = {r["id"] for r in docs}
    # The Sunday release is CSV only: its letters and news are found by ID, not by SQL.
    for ref, row in getattr(package, "_ids", {}).items() if package.release else []:
        if row.get("root") and row.get("supplier_id") == sid and ref not in seen:
            docs.append({k: row.get(k) for k in ("id", "date", "kind", "subject", "topic", "headline")})
    trigger = case.get("trigger_doc")
    for r in docs:
        r["text"] = _text(package, r["id"], 2500 if r["id"] == trigger else 420)
    d["correspondence"] = sorted(docs, key=lambda r: str(r.get("date")), reverse=True)
    return d


def _rows(rows, skip=()):
    return "\n".join(
        "  "
        + json.dumps(
            {k: v for k, v in r.items() if v not in (None, "") and k not in skip}, ensure_ascii=False, default=str
        )
        for r in rows
    )


def render(d):
    """The dossier as text, newest first, for one language-model read."""
    out = [f"SUPPLIER {d['supplier_id']}"]
    for title, key in (
        ("Master data", "supplier"),
        ("Company (filed accounts, credit index: higher is worse)", "intel"),
        ("Contract", "contract"),
        ("Contract clauses", "clauses"),
        ("Scorecards, newest first", "scorecards"),
        ("Past negotiations with us (asked vs settled %)", "history"),
        ("Our price conditions", "price_conditions"),
        ("Quotes for these parts (all suppliers)", "quotes"),
        ("Should-cost estimates (no margin)", "should_cost"),
        ("Cost drivers of the part (index series, weight)", "cost_drivers"),
        ("Who else can make the part", "sourcing"),
        ("Parts", "parts"),
        ("News", "news"),
        ("Quality last 12 months", "quality"),
        ("How we paid them last 12 months", "payments"),
        ("Demand forecast", "forecast"),
    ):
        if d.get(key):
            out.append(f"{title}:\n{_rows(d[key])}")
    if d.get("correspondence"):
        out.append("Correspondence, call and meeting notes (newest first):")
        for r in d["correspondence"]:
            out.append(
                f"  [{r.get('date')} {r.get('kind') or ''} {r['id']}] {r.get('subject') or r.get('headline') or ''}: "
                f"{r.get('text', '')}"
            )
    if d.get("case_facts"):
        out.append(
            f"Facts only this case has:\n  {json.dumps(d['case_facts'], ensure_ascii=False, default=str)[:3000]}"
        )
    if d.get("cash_signals"):
        out.append(f"Cash signals found: {', '.join(d['cash_signals'])}")
    return "\n".join(out)
