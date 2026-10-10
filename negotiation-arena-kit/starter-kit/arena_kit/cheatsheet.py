"""Tips and tricks from past negotiations: a cheatsheet that grows with every logged session.

No agent reads it yet. It is built from `logs/` (session.json and rounds.jsonl of every hosted
session) in three steps, and only new sessions are processed on an update:

  1. facts (code)        per session: case type and goal, score, how much of the gap C_open -> C_best
                         we closed, each supplier's concession from its opening price, rounds, the
                         supplier's back-office notes (its internal reasoning, shown after the end).
  2. lessons (model)     per session, once: up to five general lessons from the e-mails, the result
                         and the back office - no case, supplier, person or euro figure in them.
  3. tips (model, code)  each new lesson is filed under an existing tip (supporting or contradicting
                         it) or opens a new one; code counts the evidence behind every tip.

Against overfitting: a tip is "established" only when sessions from at least MIN_CASES different
cases support it (MIN_SESSIONS in all) and support outweighs contradiction; until then it is a
"hypothesis". Lessons naming a case, an ID, a supplier or a euro amount are dropped. Statistics are
per case type, never per case, and back-office phrases count only once they recur across cases.

    uv run python -m arena_kit.cheatsheet             # update from all logs (new sessions only)
    uv run python -m arena_kit.cheatsheet --no-llm    # facts and statistics only
    uv run python -m arena_kit.cheatsheet --rebuild   # forget lessons and tips, file everything again

Files (in `knowledge/`, next to `arena_kit/`; `ARENA_KNOWLEDGE_DIR` moves it):
    sessions.jsonl   one line per session: facts and lessons (each with the tip it was filed under)
    cheatsheet.json  statistics, supplier mechanics and tips with their evidence (machine-readable)
    cheatsheet.md    the same for people
"""

import argparse
import datetime as dt
import json
import logging
import os
import re
import statistics
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import runlog

log = logging.getLogger("arena")

VERSION = 1
MIN_SESSIONS = 3  # an established tip: at least this many supporting sessions ...
MIN_CASES = 2  # ... from at least this many different cases
MIN_PHRASE_CASES = 2  # a back-office phrase counts once it recurs in this many cases
FILE_BATCH = 30  # lessons filed per model call
MAX_TIPS_SHOWN = 120  # the model sees at most this many tips when filing (the best supported first)
HOSTED = re.compile(r"^https://(?!localhost|127\.)")
LEAK = re.compile(
    r"\b(?:case|sup|part|offer|quote|doc|ctr|sc|idx|fc|po|inv|news|rfq|qn|cost|gr):\S+"  # data IDs
    r"|\bP\d{2}\b|\bD\d{2}\b"  # case numbers
    r"|€\s?\d|\d[\d.,]*\s?(?:€|EUR\b|euros?\b)",  # euro amounts
    re.I,
)
KINDS = ("do", "avoid", "supplier_behaviour")
ATTACHED = re.compile(r"\battached \([^)]*\)\s*$")  # "quote attached (x.pdf)": a file, not a reason

LESSON_SYSTEM = """You review one finished procurement negotiation (buyer side, by e-mail) to teach a
buyer agent for FUTURE, DIFFERENT cases. The score is 0-1: the share of the possible saving the buyer
captured (1 = the best deal the supplier would have accepted).

Write at most 5 lessons that would transfer to other cases of the same type or to any case:
- what worked (kind "do"), what cost value (kind "avoid"), or how suppliers decide (kind
  "supplier_behaviour", taken from the supplier's back-office notes).
- Each lesson is general: never name the case, a supplier, a person, a product, an ID, or a euro
  amount. Relative figures are fine ("half of the step", "after two rounds", "about 3 %").
- Ground every lesson in this session: quote the e-mail line or back-office note it rests on.
- scope: the case type it applies to (e.g. "rfq", "price_increase") or "all".
- No lesson rather than a vague one ("be polite", "use data").

Reply with ONE JSON object:
{"lessons": [{"scope": "...", "kind": "do|avoid|supplier_behaviour", "lesson": "...", "evidence": "..."}]}"""

FILE_SYSTEM = """You maintain a cheatsheet of negotiation tips for a buyer agent. File each NEW
LESSON under the existing TIP that states the same advice (relation "supports") or the opposite
advice ("contradicts"); if no tip matches, open a new tip. A new tip states the advice in one or two
general sentences (no case, supplier, ID or euro amount), with a scope (a case type or "all") and a
kind (do | avoid | supplier_behaviour). Prefer an existing tip when the advice is the same; do not
merge advice that differs in substance. When several new lessons share advice that has no tip yet,
open the new tip once and file the others with "same_as" the lesson that opened it.

Reply with ONE JSON object, one entry per new lesson:
{"filed": [{"lesson": "L1", "tip": "T3", "relation": "supports"},
           {"lesson": "L2", "new_tip": {"scope": "all", "kind": "do", "text": "..."}},
           {"lesson": "L3", "same_as": "L2", "relation": "supports"}]}"""


def knowledge_dir():
    return Path(os.environ.get("ARENA_KNOWLEDGE_DIR") or Path(__file__).resolve().parents[1] / "knowledge")


# --- 1. facts (code) ---------------------------------------------------------------------------------
def _sessions_in_logs(root):
    """Every hosted session that ended with a result, newest log wins per session ID."""
    found = {}
    for path in sorted(Path(root).glob("*/case-*/session.json")):
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        r = s.get("result") or {}
        if s.get("session_id") and r.get("score") is not None and HOSTED.match(s.get("replay_url") or ""):
            found[s["session_id"]] = path
    return found


def _pct(a, b):
    return round(100 * (a - b) / b, 2) if a is not None and b else None


def facts(path):
    """What happened in one session, in numbers and the supplier's own notes."""
    s = json.loads(Path(path).read_text(encoding="utf-8"))
    case, r, buyer = s.get("case") or {}, s.get("result") or {}, (s.get("case") or {}).get("buyer") or {}
    c_open, c_deal, c_best = r.get("c_open"), r.get("c_deal"), r.get("c_best")
    gap = (c_open - c_deal) / (c_open - c_best) if None not in (c_open, c_deal, c_best) and c_open > c_best else None
    suppliers = {}
    for sid, events in (s.get("threads") or {}).items():
        prices = [
            sh["terms"]["unit_price"]
            for m in events
            if m.get("from") != "buyer"
            for sh in m.get("term_sheets") or []
            if (sh.get("terms") or {}).get("unit_price")
        ]
        suppliers[sid] = {
            "mails_from_supplier": sum(1 for m in events if m.get("from") != "buyer"),
            "mails_from_us": sum(1 for m in events if m.get("from") == "buyer"),
            "opening_price": prices[0] if prices else None,
            "last_price": prices[-1] if prices else None,
            "concession_pct": -_pct(prices[-1], prices[0]) if len(prices) > 1 else 0.0 if prices else None,
            "price_steps": len(prices) - 1 if prices else 0,
        }
    closing, tactics = None, []
    rounds = Path(path).with_name("rounds.jsonl")
    if rounds.is_file():
        for line in rounds.read_text(encoding="utf-8").splitlines():
            with_round = json.loads(line)
            closing = with_round.get("close_reason") or closing
            tactics += [m.get("tactic") for m in with_round.get("moves") or [] if m.get("tactic")]
    names = {m.get("supplier") for m in s.get("backoffice") or [] if isinstance(m, dict)}
    notes = [
        {"round": m.get("round"), "from": m.get("from"), "to": m.get("to"), "text": m["text"]}
        for m in s.get("backoffice") or []
        if isinstance(m, dict) and m.get("text") and not ATTACHED.search(m["text"])
    ]
    return {
        "session_id": s["session_id"],
        "case_id": s.get("case_id"),
        "set": (s.get("state") or {}).get("set") or case.get("set"),
        "at": s.get("ended"),
        "agent": s.get("agent"),
        "log": str(Path(path).parent.relative_to(Path(path).parents[2])),
        "replay_url": s.get("replay_url"),
        "type": case.get("type"),
        "category_goal": buyer.get("category_goal"),
        "suppliers": len(case.get("suppliers") or []),
        "negotiable": case.get("negotiable"),
        "alternative": (buyer.get("alternative") or {}).get("kind"),
        "deal": r.get("deal"),
        "score": r.get("score"),
        "gap_closed": round(gap, 3) if gap is not None else None,
        "deal_above_best_pct": _pct(c_deal, c_best),
        "end_reason": r.get("end_reason") or (s.get("state") or {}).get("end_reason"),
        "close_reason": closing,
        "tactics": dict(Counter(tactics)),
        "cost_breakdown": {k: v for k, v in (r.get("cost_breakdown") or {}).items() if v},
        "winner": r.get("supplier_id"),
        "by_supplier": suppliers,
        "backoffice": notes,
        "_names": sorted(n for n in names if n),
    }


def _phrase(text):
    """A back-office note as a pattern: numbers, IDs and quotes made generic."""
    text = LEAK.sub("<x>", text)
    text = re.sub(r"\d+(?:[.,]\d+)?", "#", text)
    return re.sub(r"\s+", " ", text).strip()


# --- 2. lessons (model) ------------------------------------------------------------------------------
def _transcript(path, limit=9000):
    s = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for sid, events in (s.get("threads") or {}).items():
        for m in events:
            who = "BUYER" if m.get("from") == "buyer" else f"SUPPLIER {sid}"
            sheets = [sh.get("terms") for sh in m.get("term_sheets") or []]
            body = re.sub(r"\s+", " ", m.get("body") or "")[:900]
            out.append(f"[{who}] {body}" + (f"\n  term sheets: {json.dumps(sheets)}" if sheets else ""))
    text = "\n".join(out)
    return text if len(text) <= limit else text[: limit // 2] + "\n[...]\n" + text[-limit // 2 :]


def _clean(lesson, names):
    """A lesson fit for other cases, or None: known kind, no IDs, case numbers, euros or names."""
    text = str(lesson.get("lesson") or "").strip()
    if not text or lesson.get("kind") not in KINDS or LEAK.search(text):
        return None
    if any(n and n.split()[0].lower() in text.lower() for n in names):
        return None
    return {
        "scope": str(lesson.get("scope") or "all").strip().lower(),
        "kind": lesson["kind"],
        "lesson": text,
        "evidence": str(lesson.get("evidence") or "")[:400],
    }


def lessons(complete, path, f):
    from .agents.llm import parse

    user = (
        f"CASE TYPE {f['type']}, category goal {f['category_goal']}, {f['suppliers']} supplier(s), "
        f"negotiable {f['negotiable']}, alternative {f['alternative']}\n"
        f"RESULT deal {f['deal']}, score {f['score']:.2f}, gap closed {f['gap_closed']}, "
        f"C deal above the best possible by {f['deal_above_best_pct']} %, "
        f"ended by {f['end_reason'] or f['close_reason']}\n"
        f"COST BREAKDOWN OF THE DEAL {json.dumps(f['cost_breakdown'])}\n"
        f"SUPPLIERS (opening price -> last price, concession %) {json.dumps(f['by_supplier'])}\n\n"
        f"E-MAILS\n{_transcript(path)}\n\n"
        f"SUPPLIER BACK OFFICE (their internal reasoning, round by round)\n"
        + "\n".join(f"r{n['round']} {n['from']} -> {n['to']}: {n['text']}" for n in f["backoffice"])[:6000]
    )
    reply = parse(complete(LESSON_SYSTEM, user))
    kept = [c for c in (_clean(x, f["_names"]) for x in reply.get("lessons") or []) if c]
    return kept[:5]


# --- 3. tips (model + code) --------------------------------------------------------------------------
def _file(complete, tips, batch):
    """Ask the model where each lesson belongs; returns [(lesson_key, tip_id | new tip, relation)]."""
    from .agents.llm import parse

    shown = sorted(tips.values(), key=lambda t: -len(t["evidence"]))[:MAX_TIPS_SHOWN]
    labels = {f"L{i + 1}": key for i, (key, _) in enumerate(batch)}
    user = (
        "TIPS\n"
        + ("\n".join(f"{t['id']} [{t['scope']}/{t['kind']}] {t['text']}" for t in shown) or "(none yet)")
        + "\n\nNEW LESSONS\n"
        + "\n".join(
            f"{lab} [{les['scope']}/{les['kind']}] {les['lesson']}" for lab, (_, les) in zip(labels, batch, strict=True)
        )
    )
    out, targets, later = [], {}, []
    for item in parse(complete(FILE_SYSTEM, user)).get("filed") or []:
        key = labels.get(item.get("lesson"))
        rel = "contradicts" if item.get("relation") == "contradicts" else "supports"
        if not key:
            continue
        if item.get("tip") in tips:
            targets[item["lesson"]] = item["tip"]
            out.append((key, item["tip"], rel))
        elif isinstance(new := item.get("new_tip"), dict) and new.get("text") and not LEAK.search(new["text"]):
            targets[item["lesson"]] = new  # the same dict for every lesson filed with it: one new tip
            out.append((key, new, "supports"))
        elif item.get("same_as"):
            later.append((key, item["same_as"], rel))
    out += [(key, targets[ref], rel) for key, ref, rel in later if ref in targets]
    return out


def _status(t):
    sup = [e for e in t["evidence"] if e["relation"] == "supports"]
    against = len(t["evidence"]) - len(sup)
    cases = {e["case_id"] for e in sup}
    t["support"] = {
        "sessions": len(sup),
        "cases": len(cases),
        "case_types": sorted({e["type"] for e in sup if e.get("type")}),
        "against": against,
        "mean_score": round(statistics.mean(e["score"] for e in sup), 3) if sup else None,
    }
    established = len(sup) >= MIN_SESSIONS and len(cases) >= MIN_CASES and len(sup) > 2 * against
    contested = against and against >= len(sup)
    t["status"] = "established" if established else "contested" if contested else "hypothesis"
    return t


# --- statistics (code) -------------------------------------------------------------------------------
def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 3) if values else None


def stats(rows):
    by_type = defaultdict(list)
    for f in rows:
        by_type[f["type"] or "unknown"].append(f)
    # (scores are compared within a type only: the types differ in how much there is to win)
    out = {}
    for typ, fs in sorted(by_type.items()):
        winners = [f["by_supplier"].get(f["winner"]) or {} for f in fs if f.get("winner")]
        out[typ] = {
            "sessions": len(fs),
            "cases": len({f["case_id"] for f in fs}),
            "deal_rate": round(sum(bool(f["deal"]) for f in fs) / len(fs), 2),
            "score_mean": round(statistics.mean(f["score"] for f in fs), 3),
            "score_best": round(max(f["score"] for f in fs), 3),
            "gap_closed_median": _median(f["gap_closed"] for f in fs),
            "deal_above_best_pct_median": _median(f["deal_above_best_pct"] for f in fs),
            "winner_concession_pct_median": _median(w.get("concession_pct") for w in winners),
            "winner_price_steps_median": _median(w.get("price_steps") for w in winners),
            "our_mails_median": _median(sum(s["mails_from_us"] for s in f["by_supplier"].values()) for f in fs),
            "costs_left_in_deals": dict(
                Counter(k for f in fs for k in f["cost_breakdown"] if k != "price").most_common(5)
            ),
        }
    return out


def mechanics(rows):
    """Back-office phrases that recur across cases: how the suppliers decide."""
    seen = defaultdict(lambda: {"count": 0, "cases": set(), "types": set(), "example": None})
    for f in rows:
        for n in f["backoffice"]:
            p = _phrase(n["text"])
            if len(p) < 12:
                continue
            e = seen[p]
            e["count"] += 1
            e["cases"].add(f["case_id"])
            e["types"].add(f["type"])
            e["example"] = e["example"] or n["text"]
    keep = [
        {"pattern": p, "seen": e["count"], "cases": len(e["cases"]), "case_types": sorted(t for t in e["types"] if t)}
        for p, e in seen.items()
        if len(e["cases"]) >= MIN_PHRASE_CASES
    ]
    return sorted(keep, key=lambda x: (-x["cases"], -x["seen"]))[:60]


# --- update ------------------------------------------------------------------------------------------
def _read_jsonl(path):
    if not path.is_file():
        return {}
    rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {r["facts"]["session_id"]: r for r in rows}


def _write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def update(complete=None, log_root=None, rebuild=False, workers=6):
    """Bring the cheatsheet up to date with the logs. Returns the cheatsheet dict."""
    out_dir = knowledge_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    sessions_file, sheet_file = out_dir / "sessions.jsonl", out_dir / "cheatsheet.json"
    known = _read_jsonl(sessions_file)
    old = json.loads(sheet_file.read_text(encoding="utf-8")) if sheet_file.is_file() and not rebuild else {}
    tips = {t["id"]: t for t in old.get("tips", [])}
    if rebuild:
        for r in known.values():
            r["lessons"] = None
    root = Path(log_root or runlog.log_root())

    for sid, path in _sessions_in_logs(root).items():
        if sid not in known:
            known[sid] = {"facts": facts(path), "lessons": None}

    if complete:  # 2. lessons for every session that has none yet (and whose log is still here)
        todo = [r for r in known.values() if r["lessons"] is None and (root / r["facts"]["log"]).is_dir()]

        def one(r):
            try:
                r["lessons"] = lessons(complete, root / r["facts"]["log"] / "session.json", r["facts"])
            except Exception as e:  # one bad reply costs one session's lessons, not the update
                log.warning("cheatsheet: no lessons for %s (%s)", r["facts"]["session_id"], e)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, todo))

        # 3. file the new lessons under tips, a batch per model call, so new tips are seen by the next batch
        pending = [
            ((r["facts"]["session_id"], i), les, r)
            for r in known.values()
            for i, les in enumerate(r["lessons"] or [])
            if not les.get("tip")
        ]
        for start in range(0, len(pending), FILE_BATCH):
            chunk = pending[start : start + FILE_BATCH]
            try:
                filed = _file(complete, tips, [(k, les) for k, les, _ in chunk])
            except Exception as e:
                log.warning("cheatsheet: filing failed (%s) - these lessons wait for the next update", e)
                continue
            lookup = {k: (les, r) for k, les, r in chunk}
            opened = {}  # id(new tip) -> its tip ID, so lessons filed "same_as" join it
            for key, target, relation in filed:
                les, r = lookup[key]
                if isinstance(target, dict) and id(target) in opened:
                    target = opened[id(target)]
                elif isinstance(target, dict):
                    tid = f"T{1 + max((int(t[1:]) for t in tips), default=0)}"
                    opened[id(target)] = tid
                    tips[tid] = {
                        "id": tid,
                        "scope": str(target.get("scope") or les["scope"]).lower(),
                        "kind": target.get("kind") if target.get("kind") in KINDS else les["kind"],
                        "text": target["text"].strip(),
                        "evidence": [],
                    }
                    target = tid
                les["tip"] = target
                f = r["facts"]
                tips[target]["evidence"].append(
                    {
                        "session_id": f["session_id"],
                        "case_id": f["case_id"],
                        "type": f["type"],
                        "score": f["score"],
                        "relation": relation,
                        "lesson": les["lesson"],
                        "evidence": les["evidence"],
                    }
                )
            for _, les, _ in chunk:  # the model saw it and filed it nowhere: not asked again
                les.setdefault("tip", "unfiled")

    rows = [r["facts"] for r in known.values()]
    order = {"established": 0, "hypothesis": 1, "contested": 2}
    ranked = sorted(
        (_status(t) for t in tips.values()), key=lambda t: (order[t["status"]], -t["support"]["sessions"], t["id"])
    )
    sheet = {
        "version": VERSION,
        "updated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "used_by_agents": False,
        "sessions": len(rows),
        "cases": len({f["case_id"] for f in rows}),
        "rules": {
            "established": f">= {MIN_SESSIONS} supporting sessions from >= {MIN_CASES} cases, support > 2x against"
        },
        "stats_by_case_type": stats(rows) if rows else {},
        "supplier_mechanics": mechanics(rows),
        "tips": ranked,
    }
    _write(
        sessions_file,
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in known.values()),
    )
    _write(sheet_file, json.dumps(sheet, ensure_ascii=False, indent=1, default=str) + "\n")
    _write(out_dir / "cheatsheet.md", render(sheet))
    return sheet


def render(sheet):
    out = [
        "# Negotiation cheatsheet",
        "",
        f"Learned from {sheet['sessions']} hosted sessions of {sheet['cases']} cases; updated {sheet['updated']}. "
        "**Not used by the agent yet.** Built by `python -m arena_kit.cheatsheet`; do not edit by hand.",
        f"A tip is *established* with {sheet['rules']['established']}; otherwise a *hypothesis*.",
        "",
        "## Results by case type",
        "",
        "| type | sessions | cases | deal rate | score mean | best | gap closed (median) "
        "| winner concession % | our mails |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for typ, s in sheet["stats_by_case_type"].items():
        out.append(
            f"| {typ} | {s['sessions']} | {s['cases']} | {s['deal_rate']} | {s['score_mean']} | {s['score_best']} | "
            f"{s['gap_closed_median']} | {s['winner_concession_pct_median']} | {s['our_mails_median']} |"
        )
    for status in ("established", "hypothesis", "contested"):
        tips = [t for t in sheet["tips"] if t["status"] == status]
        if not tips:
            continue
        out += ["", f"## Tips: {status} ({len(tips)})", ""]
        for t in tips:
            s = t["support"]
            out.append(
                f"- **{t['id']}** [{t['scope']} · {t['kind']}] {t['text']}  \n"
                f"  _{s['sessions']} sessions / {s['cases']} cases, {s['against']} against, "
                f"mean score of supporters {s['mean_score']}_"
            )
    if sheet["supplier_mechanics"]:
        out += ["", "## Supplier mechanics (back-office notes recurring across cases)", ""]
        for m in sheet["supplier_mechanics"][:30]:
            out.append(f"- {m['pattern']} — _{m['seen']}× in {m['cases']} cases ({', '.join(m['case_types'])})_")
    return "\n".join(out) + "\n"


def after_run():
    """Called by `arena run` / `arena play` once the cases are over: learn from the new sessions.
    ARENA_CHEATSHEET=0 skips it; it never fails the run."""
    if os.environ.get("ARENA_CHEATSHEET", "1").lower() in ("0", "false", "no", "off"):
        return
    from .agents.llm import llm_from_env

    try:
        model = os.environ.get("LLM_CHEATSHEET_MODEL") or os.environ.get("LLM_MODEL")
        complete = llm_from_env(model, timeout=180.0) if model else None
        sheet = update(complete)
        counts = Counter(t["status"] for t in sheet["tips"])
        print(f"cheatsheet: {sheet['sessions']} sessions, tips {dict(counts) or 'none'} ({knowledge_dir()})")
    except Exception as e:
        log.warning("cheatsheet update failed (%s) - run `python -m arena_kit.cheatsheet` later", e)


def main(argv=None):
    from .agents.llm import llm_from_env
    from .cli import load_env

    ap = argparse.ArgumentParser(prog="python -m arena_kit.cheatsheet", description=__doc__.split("\n")[0])
    ap.add_argument("--no-llm", action="store_true", help="facts and statistics only, no lessons or tips")
    ap.add_argument("--rebuild", action="store_true", help="forget lessons and tips, learn everything again")
    ap.add_argument("--model", help="the model for lessons and filing (default: LLM_CHEATSHEET_MODEL or LLM_MODEL)")
    args = ap.parse_args(argv)
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    model = args.model or os.environ.get("LLM_CHEATSHEET_MODEL") or os.environ.get("LLM_MODEL")
    complete = None if args.no_llm or not model else llm_from_env(model, timeout=180.0)
    sheet = update(complete, rebuild=args.rebuild)
    counts = Counter(t["status"] for t in sheet["tips"])
    print(
        f"{sheet['sessions']} sessions, {sheet['cases']} cases; tips: {dict(counts) or 'none'}; "
        f"written to {knowledge_dir()}"
    )


if __name__ == "__main__":
    main()
