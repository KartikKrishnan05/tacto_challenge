"""Data briefing (step 1, "prepare"): what the data package says about each supplier and part of a
case, plus ready-made arguments whose figures come straight from facts.json - so the arena checks
them as `verified`. Pure SQL and arithmetic, no LLM: it runs in well under a second.

    uv run python -m team.briefing case:P01        # print the briefing of a practice case
"""

import argparse
import json
import logging
import threading
from datetime import date

log = logging.getLogger("arena")
MAX_DOCS = 8
PACKAGE_AS_OF = "2026-09-30"  # the data package README: everything is as of this date


def _months_back(month, n):
    y, m = map(int, month.split("-"))
    m -= n
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


_DB_LOCK = threading.Lock()  # one SQLite connection, shared by the cases of a wave


def _q(p, sql, *args):
    try:
        with _DB_LOCK:
            return p.query(sql, args)
    except Exception as e:  # a missing table in a later package must not stop the case
        log.warning("briefing query failed: %s", e)
        return []


def index_change(p, facts, series):
    """12-month change of an index, from the facts the arena checks: (pct, id_from, id_to) or None."""
    rows = _q(p, "SELECT max(month) AS m FROM index_values WHERE series_id = ?", series)
    if not rows or not rows[0]["m"]:
        return None
    last = rows[0]["m"]
    for back in (12, 11, 6):  # a pair at most 12 months apart, both in the facts
        first = _months_back(last, back)
        a, b = facts.get(f"idx:{series}@{first}"), facts.get(f"idx:{series}@{last}")
        if a and b and a.get("value"):
            return round((b["value"] / a["value"] - 1) * 100, 1), f"idx:{series}@{first}", f"idx:{series}@{last}"
    return None


class Briefing:
    """`text` for the prompt, `claims` as [{kind, ids, sentence, helps, supplier, value}]."""

    def __init__(self, case, package):
        self.case, self.p = case, package
        self.facts = {**(package.facts if package else {}), **(case.get("facts") or {})}
        self.claims, self.sections = [], []
        self.index_impact_pct = None
        self.index_impacts = []
        self.supplier_text = {}  # sid -> its news and correspondence, raw (for signals such as cash needs)
        if package is None:
            self.sections.append("No data package loaded: argue from the case file only.")
            return
        for part in case.get("parts", []):
            self._part(part)
        for sid in case.get("suppliers", []):
            self._supplier(sid)
        self._notice()
        self._memos()

    def _memos(self):
        """Internal memos (the Sunday release adds one): company-wide guidance, not tied to a supplier."""
        out, ours = [], set(self.case.get("parts", [])) | set(self.case.get("suppliers", []))
        for d in _q(self.p, "SELECT id, date, subject FROM documents WHERE kind = 'memo' ORDER BY date DESC LIMIT 20"):
            try:
                with _DB_LOCK:
                    text = self.p.document(d["id"])
            except (FileNotFoundError, ValueError):
                continue
            # only memos about this case's parts or suppliers, or newer than the package (the Sunday release)
            if not any(i in text for i in ours) and str(d["date"]) <= PACKAGE_AS_OF:
                continue
            out.append(f"- {d['id']} {d['date']} '{d['subject']}': " + " ".join(text.split())[:700])
        if out:
            self.sections.append("BUYER MEMOS (internal guidance)\n" + "\n".join(out))

    # ---- parts: cost drivers, should-cost, forecast -------------------------------------------
    def _part(self, part):
        p, out = self.p, []
        row = (_q(p, "SELECT * FROM parts WHERE id = ?", part) or [{}])[0]
        out.append(f"PART {part}: {row.get('description', '')} ({row.get('category', '')}, "
                   f"{row.get('grade', '')}, price per {row.get('price_unit', 1)} {row.get('uom', 'pc')})")
        weighted, weights = 0.0, 0.0
        for d in _q(p, "SELECT series_id, weight FROM cost_drivers WHERE part_id = ? ORDER BY weight DESC", part):
            ch = index_change(p, self.facts, d["series_id"])
            name = (_q(p, "SELECT name FROM index_series WHERE id = ?", d["series_id"]) or [{}])[0].get("name", "")
            if not ch:
                out.append(f"- cost driver {d['series_id']} ({name}), weight {d['weight']:.0%}: no recent data")
                continue
            pct, a, b = ch
            weighted += d["weight"] * pct
            weights += d["weight"]
            self.index_impacts.append({"series": d["series_id"], "weight": d["weight"], "change_pct": pct})
            out.append(f"- cost driver {d['series_id']} ({name}), weight {d['weight']:.0%}: {pct:+.1f} % in 12 months")
            self.claims.append({
                "kind": "index", "ids": [a, b], "value": pct, "supplier": None,
                "helps": "buyer" if pct < 1.0 else "supplier",
                "sentence": f"{name or d['series_id']} changed {pct:+.1f}% from {a.split('@')[1]} to "
                            f"{b.split('@')[1]} ({a}, {b}).",
            })
        if weights:
            if len(self.case.get("parts", [])) == 1:
                self.index_impact_pct = round(weighted, 2)
            out.append(f"- => cost drivers justify about {weighted:+.1f} % on the price "
                       f"({weights:.0%} of cost is index-linked; the rest is fixed or margin)")
        for s in _q(p, "SELECT * FROM should_cost WHERE part_id = ? ORDER BY estimate_date DESC", part)[:1]:
            fact = self.facts.get(s["id"]) or {}
            cost = fact.get("unit_cost", s["unit_cost"])
            out.append(f"- should-cost {s['id']}: {cost:.2f} EUR (material {s['material_eur']}, conversion "
                       f"{s['conversion_eur']}, overhead {s['overhead_eur']}; +-{s['confidence_pct']} %)")
            self.claims.append({
                "kind": "should_cost", "ids": [s["id"]], "value": cost, "supplier": s.get("supplier_id"),
                "helps": "buyer",
                "sentence": f"Our bottom-up should-cost estimate for this part is EUR {cost:.2f} per unit ({s['id']}).",
            })
        fc = _q(p, "SELECT id FROM forecasts WHERE part_id = ? ORDER BY snapshot_month DESC LIMIT 1", part)
        if fc and (fact := self.facts.get(fc[0]["id"])) and fact.get("change_pct") is not None:
            pct = fact["change_pct"]
            out.append(f"- forecast {fc[0]['id']}: volume {fact.get('volume_per_year')} a year, {pct:+.1f} %")
            if pct > 0:
                self.claims.append({
                    "kind": "forecast", "ids": [fc[0]["id"]], "value": pct, "supplier": None, "helps": "buyer",
                    "sentence": f"Our demand forecast for this part is up {pct:.1f}% ({fc[0]['id']}).",
                })
        self.sections.append("\n".join(out))

    # ---- suppliers: who they are and what moves them ---------------------------------------------
    def _supplier(self, sid):
        p, out = self.p, []
        s = (_q(p, "SELECT * FROM suppliers WHERE id = ?", sid) or [{}])[0]
        out.append(f"SUPPLIER {sid}: {s.get('name', '')} ({s.get('country', '')}), standard {s.get('payment_days')} "
                   f"days net, skonto {s.get('skonto_pct')} % in {s.get('skonto_days')} days, {s.get('incoterm')}")
        for i in _q(p, "SELECT * FROM supplier_intel WHERE supplier_id = ?", sid):
            out.append(f"- company: {i.get('owner_type')} owned, {i.get('staff')} staff, turnover "
                       f"{i.get('turnover_eur')} EUR, equity ratio {i.get('equity_ratio_pct')} %, credit index "
                       f"{i.get('credit_index_12m_ago')} -> {i.get('credit_index')} (higher = riskier), "
                       f"{i.get('certifications')}")
        spend = _q(p, "SELECT fiscal_year, spend_eur FROM spend_cube WHERE supplier_id = ? ORDER BY fiscal_year DESC "
                      "LIMIT 1", sid)
        if spend:
            out.append(f"- our spend {spend[0]['fiscal_year']}: {spend[0]['spend_eur']:,.0f} EUR")
        for c in _q(p, "SELECT * FROM contracts WHERE supplier_id = ? ORDER BY valid_to DESC LIMIT 1", sid):
            out.append(f"- contract {c['id']}: {c['valid_from']} to {c['valid_to']}, {c['payment_days']} days, "
                       f"notice {c['notice_days']} days, price basis {c['price_basis']}")
            for cl in _q(p, "SELECT id, type, text FROM contract_clauses WHERE contract_id = ? AND "
                            "(type LIKE '%index%' OR type LIKE '%price%' OR type LIKE '%notice%')", c["id"])[:3]:
                out.append(f"  - {cl['id']} ({cl['type']}): {str(cl['text'])[:220]}")
        hist = _q(p, "SELECT date, part_id, type, asked_pct, settled_pct, note FROM negotiation_history WHERE "
                     "supplier_id = ? ORDER BY date DESC LIMIT 4", sid)
        for h in hist:
            out.append(f"- past {h['type']} {h['date']} {h['part_id']}: asked {h['asked_pct']} %, settled "
                       f"{h['settled_pct']} % {('- ' + h['note']) if h['note'] else ''}")
        scs = _q(p, "SELECT id, quarter, otd_pct, ppm, colour, open_8d_overdue FROM scorecards WHERE supplier_id = ? "
                    "ORDER BY quarter DESC LIMIT 4", sid)
        if scs:
            out.append("- scorecards: " + "; ".join(
                f"{r['quarter']} OTD {r['otd_pct']} % ppm {r['ppm']} {r['colour']}" for r in scs))
            last = scs[0]
            fact = self.facts.get(last["id"]) or {}
            otd, ppm = fact.get("otd_pct", last["otd_pct"]), fact.get("ppm", last["ppm"])
            if otd is not None and otd < 95:
                self.claims.append({
                    "kind": "delivery", "ids": [last["id"]], "value": otd, "supplier": sid, "helps": "buyer",
                    "sentence": f"Your on-time delivery was only {otd:.1f}% in {last['quarter']} ({last['id']}).",
                })
            if ppm:
                self.claims.append({
                    "kind": "quality", "ids": [last["id"]], "value": ppm, "supplier": sid, "helps": "buyer",
                    "sentence": f"Your quality was {ppm:.0f} ppm in {last['quarter']} ({last['id']}).",
                })
        qns = _q(p, "SELECT id, qn_date, defect, complaint_qty FROM quality_notifications WHERE supplier_id = ? AND "
                    "supplier_caused = 1 ORDER BY qn_date DESC LIMIT 3", sid)
        if qns:
            out.append("- recent complaints: " + "; ".join(f"{q['id']} {q['qn_date']} {q['defect']}" for q in qns))
        late = _q(p, "SELECT AVG(julianday(paid_date) - julianday(due_date)) AS d FROM invoices WHERE supplier_id = ? "
                     "AND paid_date IS NOT NULL", sid)
        if late and late[0]["d"] is not None:
            out.append(f"- we pay on average {late[0]['d']:+.0f} days vs. due date")
        for part in self.case.get("parts", []):
            pos = _q(p, "SELECT id, order_date, price FROM po_lines WHERE supplier_id = ? AND part_id = ? "
                        "ORDER BY order_date DESC LIMIT 3", sid, part)
            ok = [r for r in pos if r["id"] in self.facts]
            if len(ok) >= 2:  # context only: price history 'holds, but does not change our calculation' (41/46)
                out.append("- recent orders: " + ", ".join(f"{r['order_date']} {r['price']}" for r in ok))
        for n in _q(p, "SELECT id, date, topic, headline FROM news WHERE supplier_id = ? ORDER BY date DESC LIMIT 3",
                    sid):
            out.append(f"- news {n['id']} {n['date']}: {n['headline']} ({n['topic']})")
            self.supplier_text[sid] = self.supplier_text.get(sid, "") + f" {n['headline']} {n['topic']}"
        out.append("- correspondence, newest first (read for mood, cash needs, capacity, what they want):")
        for d in _q(p, "SELECT id, date, kind, subject FROM documents WHERE supplier_id = ? ORDER BY date DESC LIMIT ?",
                    sid, MAX_DOCS):
            try:
                with _DB_LOCK:
                    text = self.p.document(d["id"])
                text = " ".join(line for line in text.splitlines()[2:] if line.strip())[:500]
            except (FileNotFoundError, ValueError):
                text = ""
            out.append(f"  - {d['id']} {d['kind']} '{d['subject']}': {text}")
            self.supplier_text[sid] = self.supplier_text.get(sid, "") + f" {d['subject']} {text}"
        self.sections.append("\n".join(out))

    # ---- notice period: a letter that gives less notice than the contract ------------------------
    def _notice(self):
        doc = self.facts.get(self.case.get("trigger_doc") or "")
        if not doc or not doc.get("days"):
            return
        for ref in self.case.get("refs", []):
            ctr = self.facts.get(ref) if ref.startswith("ctr:") else None
            if ctr and ctr.get("days") and doc["days"] < ctr["days"]:
                self.claims.append({
                    "kind": "notice", "ids": [self.case["trigger_doc"], ref], "value": doc["days"],
                    "supplier": doc.get("supplier"), "helps": "buyer",
                    "sentence": f"Your letter gives only {doc['days']} days of notice ({self.case['trigger_doc']}), "
                                f"our contract requires {ctr['days']} days ({ref}).",
                })

    @property
    def text(self):
        lines = ["DATA BRIEFING (from the data package; as of " + str(date.today()) + ")", *self.sections]
        good = [c for c in self.claims if c["helps"] == "buyer"]
        bad = [c for c in self.claims if c["helps"] == "supplier"]
        if good:
            lines.append("ARGUMENTS THAT CHECK OUT (copy the sentence with its IDs; max 3 per e-mail, each ID once "
                         "per case):")
            lines += [f"[A{i}] {c['sentence']}" for i, c in enumerate(good)]
        if bad:
            lines.append("WHAT THE SUPPLIER WILL ARGUE (rising cost drivers - be ready, do not cite them):")
            lines += [f"- {c['sentence']}" for c in bad]
        return "\n".join(lines)


def main(argv=None):
    import os

    from arena_kit.cli import load_env
    from arena_kit.client import ArenaClient
    from arena_kit.data import DataPackage

    load_env()
    ap = argparse.ArgumentParser(prog="briefing")
    ap.add_argument("case_id")
    ap.add_argument("--set", default="practice")
    a = ap.parse_args(argv)
    case = next(c for c in ArenaClient().cases(a.set) if c["case_id"] == a.case_id)
    b = Briefing(case, DataPackage(os.environ.get("ARENA_DATA", "data/package")))
    print(b.text)
    print(f"\n({len(b.text)} chars, {len(b.claims)} claims)")
    if os.environ.get("BRIEFING_JSON"):
        print(json.dumps(b.claims, indent=1))


if __name__ == "__main__":
    main()
