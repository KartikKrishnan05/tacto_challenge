"""Tips and tricks from past negotiations: general principles that grow with every logged session.

Kept apart from negotiating on purpose: no agent reads it, and playing a case never builds it -
only this command does (tests/test_cheatsheet.py fails if negotiation code imports it). It is built
from `logs/` (session.json and rounds.jsonl of every hosted session); only new sessions are read by
the model on an update.

  1. facts (code)        per session: score, how much of the gap C_open -> C_best we closed, each
                         supplier's concession, the supplier's back-office notes (its reasoning).
  2. lessons (model)     per session, once: up to five lessons, each a principle of the form
                         WHEN <a situation recognisable in any case> DO <action> BECAUSE <mechanism>.
  3. tips (model, code)  each lesson is filed under an existing tip (supporting or contradicting it)
                         or opens a new one; then near-duplicate tips are merged and wording that
                         still points at one case is generalised. Code counts the evidence.

Against overfitting - the cheatsheet must help in cases we have not seen:
  - A tip is a situation and a principle: no case, case type label, supplier, product, ID, price,
    euro amount or term value. The situation must be recognisable from a case file or an e-mail.
  - "established" needs MIN_SESSIONS sessions from MIN_CASES different cases of MIN_TYPES different
    case types, and support > 2x contradiction. Anything less is a "hypothesis".
  - Effect is measured against the same case: a tip's lift is how much better its supporting
    sessions scored than other sessions of the same case. A "do" tip whose supporters scored worse
    is "doubtful", however often the model wrote it down.
  - Numbers per case type and the supplier's literal phrases are evidence about the current cases,
    not advice: they go to diagnostics.md, never into the cheatsheet.

    uv run python -m arena_kit.cheatsheet             # update from all logs (new sessions only)
    uv run python -m arena_kit.cheatsheet --no-llm    # facts and diagnostics only
    uv run python -m arena_kit.cheatsheet --rebuild   # forget lessons and tips, learn everything again

Files (in `knowledge/`, next to `arena_kit/`; `ARENA_KNOWLEDGE_DIR` moves it):
    cheatsheet.md / .json   the principles with their evidence
    diagnostics.md / .json  results by general case features, recurring supplier reasoning (raw evidence)
    sessions.jsonl          one line per session: facts and lessons (each with the tip it was filed under)
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

VERSION = 2
MIN_SESSIONS = 3  # an established tip: at least this many supporting sessions ...
MIN_CASES = 3  # ... from at least this many different cases ...
MIN_TYPES = 2  # ... of at least this many case types
DOUBTFUL_LIFT = -0.03  # a "do" tip whose supporters score this much below their case's sessions
MIN_PHRASE_CASES = 2  # a back-office phrase counts in the diagnostics once it recurs in this many cases
FILE_BATCH = 30  # lessons filed per model call
MAX_TIPS_SHOWN = 120  # the model sees at most this many tips when filing (the best supported first)
HOSTED = re.compile(r"^https://(?!localhost|127\.)")
LEAK = re.compile(
    r"\b(?:case|sup|part|offer|quote|doc|ctr|sc|idx|fc|po|inv|news|rfq|qn|cost|gr):\S+"  # data IDs
    r"|\bP\d{2}\b|\bD\d{2}\b"  # case numbers
    r"|€\s?\d|\d[\d.,]*\s?(?:€|EUR\b|euros?\b)"  # euro amounts
    r"|\b\d+\s?(?:days?|ppm|pc|pcs|units?|months?)\b"  # term values: the next case has other ones
    r"|\b[a-z]+_[a-z_]+\b",  # field and case-type labels (price_increase, payment_days): say it in words
    re.I,
)
KINDS = ("do", "avoid", "supplier_behaviour")
ATTACHED = re.compile(r"\battached \([^)]*\)\s*$")  # "quote attached (x.pdf)": a file, not a reason

PRINCIPLE = """A lesson is a principle for a buyer agent that will negotiate DIFFERENT cases later:
other products, suppliers, prices, terms and case types. Write it as
  when:   a situation the agent can recognise from a case file or a supplier e-mail, in general words
          ("the supplier justifies its price with a cost index", "two or more suppliers quote",
          "the supplier repeats an offer unchanged") - never a case type label or a named case;
  action: what to do (or avoid), in general words;
  why:    the mechanism that makes it work - how the supplier decides, or how the score counts.
No supplier, person, product, ID, price, euro amount or term value (no "60 days", "500 ppm"); name
terms in words ("payment terms", "lead time"), not as field names.
Relative amounts that scale to any case are fine ("half of their last step", "a third of the gap").
Test each lesson: would it still be right with a different product, supplier, price level and case
type? If not, generalise the situation, or leave the lesson out. No platitudes ("be polite")."""

LESSON_SYSTEM = f"""You review one finished procurement negotiation (buyer side, by e-mail). The score is
0-1: the share of the possible saving the buyer captured (1 = the best deal the supplier would have
accepted). The supplier's back-office notes show its real reasoning.

{PRINCIPLE}

Write at most 5 lessons: what worked (kind "do"), what cost value (kind "avoid"), or how suppliers
decide (kind "supplier_behaviour", from the back-office notes). Ground each in this session: quote
the e-mail line or back-office note it rests on.

Reply with ONE JSON object:
{{"lessons": [{{"kind": "do|avoid|supplier_behaviour", "when": "...", "action": "...", "why": "...",
               "evidence": "..."}}]}}"""

FILE_SYSTEM = f"""You maintain a cheatsheet of negotiation principles. File each NEW LESSON under the
existing TIP that gives the same advice in the same situation (relation "supports") or the opposite
advice ("contradicts"); if none matches, open a new tip. When several new lessons share advice that
has no tip yet, open it once and file the others with "same_as" the lesson that opened it. Prefer an
existing tip when the advice is the same; do not merge advice that differs in substance.

{PRINCIPLE}

Reply with ONE JSON object, one entry per new lesson:
{{"filed": [{{"lesson": "L1", "tip": "T3", "relation": "supports"}},
           {{"lesson": "L2", "new_tip": {{"kind": "do", "when": "...", "action": "...", "why": "..."}}}},
           {{"lesson": "L3", "same_as": "L2", "relation": "supports"}}]}}"""

CONSOLIDATE_SYSTEM = f"""You keep a cheatsheet of negotiation principles short and general.
1. merge: tips that give the same advice in the same situation become one (keep the better
   supported ID, write the merged wording).
2. rewrite: a tip whose wording still points at one case - a product, a figure, a term value, a
   case type used as the situation - gets general wording with the same meaning.
Leave everything else alone; never merge advice that differs in substance.

{PRINCIPLE}

Reply with ONE JSON object (empty lists when nothing needs doing):
{{"merge": [{{"into": "T1", "from": ["T7", "T9"], "when": "...", "action": "...", "why": "..."}}],
  "rewrite": [{{"tip": "T4", "when": "...", "action": "...", "why": "..."}}]}}"""


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


def _general(item, names=()):
    """`when`, `action` and `why` of a lesson or tip if they are general, else None."""
    parts = {k: re.sub(r"\s+", " ", str(item.get(k) or "")).strip() for k in ("when", "action", "why")}
    text = " ".join(parts.values()).lower()
    if not parts["when"] or not parts["action"] or LEAK.search(" ".join(parts.values())):
        return None
    if any(n and n.split()[0].lower() in text for n in names):
        return None
    return parts


def _clean(lesson, names):
    """A lesson fit for other cases, or None."""
    parts = _general(lesson, names)
    if not parts or lesson.get("kind") not in KINDS:
        return None
    return {"kind": lesson["kind"], **parts, "evidence": str(lesson.get("evidence") or "")[:400]}


def lessons(complete, path, f):
    from .agents.llm import parse

    user = (
        f"CASE: {f['suppliers']} supplier(s), category goal {f['category_goal']}, negotiable {f['negotiable']}, "
        f"alternative without a deal: {f['alternative']}\n"
        f"RESULT deal {f['deal']}, score {f['score']:.2f}, share of the gap closed {f['gap_closed']}, "
        f"ended by {f['end_reason'] or f['close_reason']}\n"
        f"COSTS LEFT IN THE DEAL BESIDES PRICE {sorted(k for k in f['cost_breakdown'] if k != 'price')}\n"
        f"SUPPLIERS (opening price -> last price, concession %) {json.dumps(f['by_supplier'])}\n\n"
        f"E-MAILS\n{_transcript(path)}\n\n"
        f"SUPPLIER BACK OFFICE (their internal reasoning, round by round)\n"
        + "\n".join(f"r{n['round']} {n['from']} -> {n['to']}: {n['text']}" for n in f["backoffice"])[:6000]
    )
    reply = parse(complete(LESSON_SYSTEM, user))
    kept = [c for c in (_clean(x, f["_names"]) for x in reply.get("lessons") or []) if c]
    return kept[:5]


def _line(t):
    return f"[{t['kind']}] WHEN {t['when']} DO {t['action']} BECAUSE {t.get('why') or '-'}"


# --- 3. tips (model + code) --------------------------------------------------------------------------
def _file(complete, tips, batch):
    """Ask the model where each lesson belongs; returns [(lesson_key, tip_id | new tip, relation)]."""
    from .agents.llm import parse

    shown = sorted(tips.values(), key=lambda t: -len(t["evidence"]))[:MAX_TIPS_SHOWN]
    labels = {f"L{i + 1}": key for i, (key, _) in enumerate(batch)}
    user = (
        "TIPS\n"
        + ("\n".join(f"{t['id']} {_line(t)}" for t in shown) or "(none yet)")
        + "\n\nNEW LESSONS\n"
        + "\n".join(f"{lab} {_line(les)}" for lab, (_, les) in zip(labels, batch, strict=True))
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
        elif isinstance(new := item.get("new_tip"), dict) and _general(new):
            targets[item["lesson"]] = new  # the same dict for every lesson filed with it: one new tip
            out.append((key, new, "supports"))
        elif item.get("same_as"):
            later.append((key, item["same_as"], rel))
    out += [(key, targets[ref], rel) for key, ref, rel in later if ref in targets]
    return out


def _consolidate(complete, tips, known):
    """Merge tips that say the same, generalise wording that still points at one case."""
    from .agents.llm import parse

    if len(tips) < 2:
        return
    user = "TIPS (id, evidence count, text)\n" + "\n".join(
        f"{t['id']} ({len(t['evidence'])}) {_line(t)}" for t in sorted(tips.values(), key=lambda t: t["id"])
    )
    reply = parse(complete(CONSOLIDATE_SYSTEM, user))
    moved = {}
    for m in reply.get("merge") or []:
        into, wording = m.get("into"), _general(m)
        sources = [t for t in m.get("from") or [] if t in tips and t != into and t not in moved]
        if into not in tips or into in moved or not sources:
            continue
        for src in sources:
            tips[into]["evidence"] += tips.pop(src)["evidence"]
            moved[src] = into
        if wording:
            tips[into].update(wording)
    for rw in reply.get("rewrite") or []:
        if rw.get("tip") in tips and (wording := _general(rw)):
            tips[rw["tip"]].update(wording)
    for r in known.values():  # lessons follow their tip
        for les in r["lessons"] or []:
            les["tip"] = moved.get(les.get("tip"), les.get("tip"))
    if moved:
        log.info("cheatsheet: merged %s", ", ".join(f"{a}->{b}" for a, b in moved.items()))


def _case_means(rows):
    by_case = defaultdict(list)
    for f in rows:
        by_case[f["case_id"]].append(f["score"])
    return {c: (statistics.mean(v), len(v)) for c, v in by_case.items()}


def _status(t, case_means):
    sup = [e for e in t["evidence"] if e["relation"] == "supports"]
    against = len(t["evidence"]) - len(sup)
    cases, types = {e["case_id"] for e in sup}, {e["type"] for e in sup if e.get("type")}
    # lift: supporters vs all sessions of the same case (only cases played more than once say anything)
    lifts = [e["score"] - case_means[e["case_id"]][0] for e in sup if case_means.get(e["case_id"], (0, 0))[1] > 1]
    lift = round(statistics.mean(lifts), 3) if lifts else None
    t["support"] = {
        "sessions": len(sup),
        "cases": len(cases),
        "case_types": len(types),
        "against": against,
        "lift_vs_same_case": lift,
    }
    general = len(sup) >= MIN_SESSIONS and len(cases) >= MIN_CASES and len(types) >= MIN_TYPES
    if t["kind"] == "do" and lift is not None and lift <= DOUBTFUL_LIFT and len(lifts) >= MIN_SESSIONS:
        t["status"] = "doubtful"
    elif against and against >= len(sup):
        t["status"] = "contested"
    else:
        t["status"] = "established" if general and len(sup) > 2 * against else "hypothesis"
    return t


# --- diagnostics (code): about the cases played so far, not advice --------------------------------
def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def _group(fs):
    winners = [f["by_supplier"].get(f["winner"]) or {} for f in fs if f.get("winner")]
    return {
        "sessions": len(fs),
        "cases": len({f["case_id"] for f in fs}),
        "deal_rate": round(sum(bool(f["deal"]) for f in fs) / len(fs), 2),
        "score_mean": round(statistics.mean(f["score"] for f in fs), 2),
        "gap_closed_median": _median(f["gap_closed"] for f in fs),
        "winner_concession_pct_median": _median(w.get("concession_pct") for w in winners),
    }


def diagnostics(rows):
    """Results by general features of a case (competition, an alternative, the goal), and the
    supplier reasoning that recurs across cases. Groups with fewer than MIN_CASES cases are left out:
    with one or two cases they describe those cases, not the feature."""
    features = {
        "all": lambda f: "all",
        "competition": lambda f: "several suppliers" if f["suppliers"] > 1 else "one supplier",
        "alternative without a deal": lambda f: "none" if f["alternative"] in (None, "none") else "yes",
        "category goal": lambda f: f["category_goal"] or "unknown",
    }
    out = {}
    for name, key in features.items():
        groups = defaultdict(list)
        for f in rows:
            groups[key(f)].append(f)
        out[name] = {g: _group(fs) for g, fs in sorted(groups.items()) if len({f["case_id"] for f in fs}) >= MIN_CASES}
    seen = defaultdict(lambda: {"count": 0, "cases": set()})
    for f in rows:
        for n in f["backoffice"]:
            p = re.sub(r"\s+", " ", re.sub(r"\d+(?:[.,]\d+)?", "#", LEAK.sub("<x>", n["text"]))).strip()
            if len(p) >= 12:
                seen[p]["count"] += 1
                seen[p]["cases"].add(f["case_id"])
    phrases = [
        {"pattern": p, "seen": e["count"], "cases": len(e["cases"])}
        for p, e in seen.items()
        if len(e["cases"]) >= MIN_PHRASE_CASES
    ]
    return {"results": out, "supplier_reasoning": sorted(phrases, key=lambda x: (-x["cases"], -x["seen"]))[:40]}


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


def _learn(complete, known, tips, root, workers):
    """Steps 2 and 3: lessons for sessions without, filed under tips; True when a tip was opened."""

    def one(r):
        try:
            r["lessons"] = lessons(complete, root / r["facts"]["log"] / "session.json", r["facts"])
        except Exception as e:  # one bad reply costs one session's lessons, not the update
            log.warning("cheatsheet: no lessons for %s (%s)", r["facts"]["session_id"], e)

    todo = [r for r in known.values() if r["lessons"] is None and (root / r["facts"]["log"]).is_dir()]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))

    pending = [
        ((r["facts"]["session_id"], i), les, r)
        for r in known.values()
        for i, les in enumerate(r["lessons"] or [])
        if not les.get("tip")
    ]
    opened_any = False
    for start in range(0, len(pending), FILE_BATCH):  # a batch per call: the next batch sees the new tips
        chunk = pending[start : start + FILE_BATCH]
        try:
            filed = _file(complete, tips, [(k, les) for k, les, _ in chunk])
        except Exception as e:
            log.warning("cheatsheet: filing failed (%s) - these lessons wait for the next update", e)
            continue
        lookup, opened = {k: (les, r) for k, les, r in chunk}, {}
        for key, target, relation in filed:
            les, r = lookup[key]
            if isinstance(target, dict) and id(target) in opened:
                target = opened[id(target)]
            elif isinstance(target, dict):
                tid = f"T{1 + max((int(t[1:]) for t in tips), default=0)}"
                opened[id(target)] = tid
                kind = target.get("kind") if target.get("kind") in KINDS else les["kind"]
                tips[tid] = {"id": tid, "kind": kind, **_general(target), "evidence": []}
                target, opened_any = tid, True
            les["tip"] = target
            f = r["facts"]
            tips[target]["evidence"].append(
                {
                    "session_id": f["session_id"],
                    "case_id": f["case_id"],
                    "type": f["type"],
                    "score": f["score"],
                    "relation": relation,
                    "lesson": _line(les),
                    "evidence": les["evidence"],
                }
            )
        for _, les, _ in chunk:  # the model saw it and filed it nowhere: not asked again
            les.setdefault("tip", "unfiled")
    return opened_any


def update(complete=None, log_root=None, rebuild=False, workers=6):
    """Bring the cheatsheet up to date with the logs. Returns the cheatsheet dict."""
    out_dir = knowledge_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    sessions_file, sheet_file = out_dir / "sessions.jsonl", out_dir / "cheatsheet.json"
    known = _read_jsonl(sessions_file)
    old = json.loads(sheet_file.read_text(encoding="utf-8")) if sheet_file.is_file() else {}
    if rebuild or old.get("version") != VERSION:  # a new format learns again from the logs
        old = {}
        for r in known.values():
            r["lessons"] = None
    tips = {t["id"]: t for t in old.get("tips", [])}
    root = Path(log_root or runlog.log_root())

    for sid, path in _sessions_in_logs(root).items():  # facts are code: recomputed, lessons are kept
        known.setdefault(sid, {"lessons": None})["facts"] = facts(path)

    if complete and _learn(complete, known, tips, root, workers):
        try:
            _consolidate(complete, tips, known)
        except Exception as e:
            log.warning("cheatsheet: consolidation failed (%s) - tips stay as filed", e)

    rows = [r["facts"] for r in known.values()]
    means = _case_means(rows)
    order = {"established": 0, "hypothesis": 1, "contested": 2, "doubtful": 3}
    ranked = sorted(
        (_status(t, means) for t in tips.values()),
        key=lambda t: (order[t["status"]], -t["support"]["cases"], -t["support"]["sessions"], t["id"]),
    )
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    sheet = {
        "version": VERSION,
        "updated": now,
        "used_by_agents": False,
        "sessions": len(rows),
        "cases": len({f["case_id"] for f in rows}),
        "rules": {
            "established": f">= {MIN_SESSIONS} supporting sessions from >= {MIN_CASES} cases of >= {MIN_TYPES} "
            "case types, support > 2x against",
            "doubtful": f"a 'do' tip whose supporters scored {abs(DOUBTFUL_LIFT)} or more below other sessions "
            "of the same case",
        },
        "tips": ranked,
    }
    diag = {"updated": now, "sessions": len(rows), **diagnostics(rows)}
    _write(sessions_file, "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in known.values()))
    _write(sheet_file, json.dumps(sheet, ensure_ascii=False, indent=1, default=str) + "\n")
    _write(out_dir / "cheatsheet.md", render(sheet))
    _write(out_dir / "diagnostics.json", json.dumps(diag, ensure_ascii=False, indent=1) + "\n")
    _write(out_dir / "diagnostics.md", render_diagnostics(diag))
    return sheet


def _lower(text):
    """'A supplier ...' reads on after 'When'; an acronym ('RFQ ...') keeps its capitals."""
    first = text.split(" ", 1)[0]
    return text if len(first) > 1 and first.isupper() else text[:1].lower() + text[1:]


def render(sheet):
    out = [
        "# Negotiation cheatsheet",
        "",
        f"General principles learned from {sheet['sessions']} hosted sessions of {sheet['cases']} cases "
        f"(updated {sheet['updated']}). **Not used by the agent yet.** Generated by "
        "`python -m arena_kit.cheatsheet`; do not edit by hand.",
        "",
        f"*Established*: {sheet['rules']['established']}. *Doubtful*: {sheet['rules']['doubtful']}. "
        "*Lift*: how much better the supporting sessions scored than other sessions of the same case.",
    ]
    titles = {
        "do": "Do",
        "avoid": "Avoid",
        "supplier_behaviour": "How suppliers decide",
    }
    for status in ("established", "hypothesis", "contested", "doubtful"):
        tips = [t for t in sheet["tips"] if t["status"] == status]
        if not tips:
            continue
        out += ["", f"## {status.capitalize()} ({len(tips)})"]
        for kind, title in titles.items():
            group = [t for t in tips if t["kind"] == kind]
            if not group:
                continue
            out += ["", f"### {title}", ""]
            for t in group:
                s = t["support"]
                lift = f", lift {s['lift_vs_same_case']:+.2f}" if s["lift_vs_same_case"] is not None else ""
                out.append(
                    f"- **{t['id']}** When {_lower(t['when'])}: {t['action']}"
                    + (f" — *{t['why']}*" if t.get("why") else "")
                    + f"  \n  _{s['sessions']} sessions, {s['cases']} cases, {s['case_types']} case types, "
                    f"{s['against']} against{lift}_"
                )
    if not sheet["tips"]:
        out += ["", "_No tips yet: run `python -m arena_kit.cheatsheet` with a model configured._"]
    return "\n".join(out) + "\n"


def render_diagnostics(diag):
    out = [
        "# Diagnostics",
        "",
        f"About the {diag['sessions']} sessions played so far - evidence, not advice (updated {diag['updated']}). "
        f"Groups with fewer than {MIN_CASES} different cases are left out.",
    ]
    for feature, groups in diag["results"].items():
        if not groups:
            continue
        out += [
            "",
            f"## By {feature}",
            "",
            "| group | sessions | cases | deal rate | score mean | gap closed | winner concession % |",
            "|---|---|---|---|---|---|---|",
        ]
        for g, s in groups.items():
            out.append(
                f"| {g} | {s['sessions']} | {s['cases']} | {s['deal_rate']} | {s['score_mean']} | "
                f"{s['gap_closed_median']} | {s['winner_concession_pct_median']} |"
            )
    if diag["supplier_reasoning"]:
        out += ["", "## Supplier reasoning recurring across cases (back-office notes, literal)", ""]
        out += [f"- {p['pattern']} — _{p['seen']}× in {p['cases']} cases_" for p in diag["supplier_reasoning"]]
    return "\n".join(out) + "\n"


def main(argv=None):
    from .agents.llm import llm_from_env
    from .cli import load_env

    ap = argparse.ArgumentParser(prog="python -m arena_kit.cheatsheet", description=__doc__.split("\n")[0])
    ap.add_argument("--no-llm", action="store_true", help="facts and diagnostics only, no lessons or tips")
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
