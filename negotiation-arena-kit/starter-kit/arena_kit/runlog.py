"""Run log: everything a run does, on disk, to read again later.

Every `arena` command (and every `play()` from your own script) writes a run folder under
`logs/` (next to `arena_kit/`, or `ARENA_LOG_DIR`). `ARENA_LOG=0` switches it off.

    logs/
      index.jsonl                      one line per case played: when, case, session, deal, score, folder
      2026-10-10_143205_play_case-P01/ one folder per command
        run.json                       command, git commit, model, start/end, how it ended
        run.log                        the full log at DEBUG level, whatever -v says
        output.txt                     what the command printed
        http.jsonl                     API calls outside a case (case list, board, me, ...)
        case-P01/                      one folder per case played
          transcript.md                the e-mail discussion, readable
          session.json                 case file, every thread, result, back office
          decisions.jsonl              every move the agent chose, and how long it took
          llm.jsonl                    every model call: prompt, raw reply, what was sent
          rounds.jsonl                 agents that log their rounds (DualAgent): tactic, intel, C, close
          http.jsonl                   every API call of this case: request, response, status, ms
          attachments/                 the supplier documents as received (quote PDFs, XLSX)

The API key is never written. Cases run in parallel threads; each thread logs to its own case.
"""

import atexit
import contextlib
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger("arena")

_lock = threading.Lock()
_local = threading.local()
_run = None
# Shown in run.json: never a key.
ENV_KEYS = (
    "ARENA_URL", "ARENA_DATA", "LLM_PROVIDER", "LLM_MODEL", "LLM_EFFORT", "LLM_BASE_URL", "LLM_ANALYST_MODEL",
    "LLM_PREP_MODEL",
)  # fmt: skip


def enabled():
    return os.environ.get("ARENA_LOG", "1").lower() not in ("0", "false", "no", "off")


def log_root():
    return Path(os.environ.get("ARENA_LOG_DIR") or Path(__file__).resolve().parents[1] / "logs")


def _now():
    return dt.datetime.now().astimezone()


def _slug(text, limit=60):
    return re.sub(r"[^\w.-]+", "-", str(text)).strip("-")[:limit] or "x"


def _json(obj, **kw):
    return json.dumps(obj, ensure_ascii=False, default=str, **kw)


def _append(path, record):
    with _lock, open(path, "a", encoding="utf-8") as f:
        f.write(_json(record) + "\n")


def _unique(path):
    n, out = 1, path
    while out.exists():
        n += 1
        out = path.with_name(f"{path.name}_{n}")
    return out


def _git_commit():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5
        )
        commit = out.stdout.strip()
        return commit + ("-dirty" if dirty.stdout.strip() else "") if commit else None
    except (OSError, subprocess.SubprocessError):
        return None


class _Tee:
    """Write to the terminal and to output.txt."""

    def __init__(self, stream, path):
        self.stream, self.file = stream, open(path, "a", encoding="utf-8")

    def write(self, text):
        self.stream.write(text)
        with contextlib.suppress(ValueError):
            self.file.write(text)
            self.file.flush()
        return len(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class Run:
    def __init__(self, name, argv, meta=None):
        self.started = _now()
        base = log_root() / f"{self.started:%Y-%m-%d_%H%M%S}_{_slug(name)}"
        while True:  # two commands started in the same second must not share (or crash on) a folder
            self.dir = _unique(base)
            try:
                self.dir.mkdir(parents=True)
                break
            except FileExistsError:
                continue
        self.meta = {
            "run": self.dir.name,
            "argv": argv,
            "cwd": os.getcwd(),
            "git_commit": _git_commit(),
            "env": {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)},
            "started": self.started.isoformat(timespec="seconds"),
            **(meta or {}),
            "cases": [],
        }
        self._write_meta()
        self.handler = logging.FileHandler(self.dir / "run.log", encoding="utf-8")
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(self.handler)
        for h in root.handlers:  # the terminal keeps the level it had; the file gets everything
            if h is not self.handler and h.level == logging.NOTSET:
                h.setLevel(root.level)
        root.setLevel(logging.DEBUG)
        for noisy in ("httpx", "httpcore", "anthropic", "urllib3"):  # http.jsonl has the calls
            logging.getLogger(noisy).setLevel(logging.WARNING)
        self.stdout = sys.stdout
        sys.stdout = _Tee(sys.stdout, self.dir / "output.txt")
        self.closed = False

    def _write_meta(self):
        (self.dir / "run.json").write_text(_json(self.meta, indent=2) + "\n", encoding="utf-8")

    def end(self, status="ok", error=None):
        if self.closed:
            return
        self.closed = True
        ended = _now()
        self.meta.update(
            ended=ended.isoformat(timespec="seconds"),
            seconds=round((ended - self.started).total_seconds(), 1),
            status=status,
        )
        if error:
            self.meta["error"] = error
        with _lock:
            self._write_meta()
        logging.getLogger().removeHandler(self.handler)
        self.handler.close()
        if isinstance(sys.stdout, _Tee):
            sys.stdout.file.close()
            sys.stdout = self.stdout


def start_run(name, argv=None, meta=None):
    """Open the run folder (the CLI calls this once per command). Returns the Run, or None if off."""
    global _run
    if not enabled():
        return None
    with _lock:
        if _run and not _run.closed:
            return _run
        _run = Run(name, list(sys.argv if argv is None else argv), meta)
    log.info("run log: %s", _run.dir)
    return _run


def end_run(status="ok", error=None):
    if _run:
        _run.end(status, error)


def _ensure_run():
    """A run for `play()` called from a script, not from the CLI: named after the script."""
    if _run is None or _run.closed:
        start_run(Path(sys.argv[0] or "python").stem)
        atexit.register(end_run)
    return _run


def current():
    """The case this thread is playing, or None."""
    return getattr(_local, "case", None)


class CaseLog:
    def __init__(self, run, case_id, agent):
        self.run, self.case_id = run, case_id
        self.dir = _unique(run.dir / _slug(case_id))
        (self.dir / "attachments").mkdir(parents=True)
        self.transcript = self.dir / "transcript.md"
        self.started = _now()
        self.agent = type(agent).__name__ if agent is not None else None
        self.session_id = self.case = None
        self.threads, self.note, self.flag, self.rounds = {}, None, None, 0
        self.saved_attachments = {}
        self._md(f"# {case_id}\n\n- Started: {self.started:%Y-%m-%d %H:%M:%S %Z}\n- Agent: {self.agent}\n")
        if self.agent in ("LLMAgent", "BuyerAgent", "DualAgent") and (model := os.environ.get("LLM_MODEL")):
            self._md(f"- Model: {os.environ.get('LLM_PROVIDER', '')} {model}\n")
        self._md(f"- Run: `{run.dir.name}`\n")

    def _md(self, text):
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write(text)

    def _jsonl(self, name, record):
        _append(self.dir / name, {"at": _now().isoformat(timespec="milliseconds"), **record})

    # --- the negotiation -------------------------------------------------------------------
    def opened(self, session_id, case, threads, resumed=False):
        self.session_id, self.case = session_id, case
        self.threads = {sid: list(events) for sid, events in threads.items()}
        c = case or {}
        lines = [f"- Session: `{session_id}`" + (" (resumed)" if resumed else "")]
        for key in ("title", "type", "set", "category_goal", "wave", "round_limit"):
            if c.get(key) not in (None, ""):
                lines.append(f"- {key}: {c[key]}")
        if c.get("suppliers"):
            lines.append(f"- Suppliers: {', '.join(c['suppliers'])}")
        if c.get("parts"):
            lines.append(f"- Parts: {', '.join(c['parts'])}")
        if alt := (c.get("buyer") or {}).get("alternative"):
            lines.append(f"- Alternative: `{_json(alt)}`")
        self._md("\n".join(lines) + "\n")
        if c:
            self._md(f"\n<details><summary>Case file</summary>\n\n```json\n{_json(c, indent=2)}\n```\n</details>\n")
        self._md("\n## E-mails\n")
        for sid, events in threads.items():
            for m in events:
                self._message(sid, m)

    def decision(self, decision, seconds, source):
        """The move the agent (or the closing logic) chose, before it is sent."""
        self.note = f"{source}, {seconds:.1f}s" + (f", {self.flag}" if self.flag else "")
        self.flag = None
        self._jsonl("decisions.jsonl", {"source": source, "seconds": round(seconds, 2), "decision": decision})

    def sent(self, supplier_id, message):
        self.rounds += 1
        m = {**message, "from": "buyer"}
        self.threads.setdefault(supplier_id, []).append(m)
        self._message(supplier_id, m)

    def received(self, supplier_id, message):
        self.threads.setdefault(supplier_id, []).append(message)
        self._message(supplier_id, message)

    def _message(self, sid, m):
        mine = m.get("from") == "buyer"
        head = f"us → {sid}" if mine else f"{m.get('from') or sid} → us"
        meta = [x for x in (m.get("status") and f"status **{m['status']}**", mine and self.note) if x]
        if mine:
            self.note = None
        out = [f"\n### {_now():%H:%M:%S} · {head}" + (f" · {' · '.join(meta)}" if meta else "")]
        if action := m.get("action"):
            out.append(f"**Action:** `{_json(action)}`")
        if m.get("subject"):
            out.append(f"**Subject:** {m['subject']}")
        if m.get("body"):
            out.append("\n" + "\n".join("> " + line for line in str(m["body"]).splitlines()) + "\n")
        for ts in m.get("term_sheets") or []:
            tid = ts.get("term_sheet_id")
            out.append(f"- Term sheet{f' `{tid}`' if tid else ''}: `{_json(ts.get('terms', ts))}`")
        for key in ("claim_results", "missing_fields", "signals", "refs"):
            if m.get(key):
                out.append(f"- {key}: `{_json(m[key])}`")
        for att in m.get("attachments") or []:
            saved = self.saved_attachments.get(att.get("attachment_id"))
            out.append(f"- Attachment: {att.get('name')}" + (f" → `attachments/{saved}`" if saved else ""))
        self._md("\n".join(out) + "\n")

    def attachment(self, att, data):
        name = f"{_slug(att.get('attachment_id'))}__{_slug(att.get('name') or 'file', 80)}"
        (self.dir / "attachments" / name).write_bytes(data)
        self.saved_attachments[att.get("attachment_id")] = name

    # --- what went into the decision -------------------------------------------------------
    def llm(self, **record):
        self._jsonl("llm.jsonl", record)

    def http(self, record):
        self._jsonl("http.jsonl", record)

    def agent_round(self, **record):
        """One round as the agent saw it: its reasons, its view of each thread, what it sent."""
        self._jsonl("rounds.jsonl", record)

    def agent_note(self, text):
        """A line in the transcript, before the e-mails it explains."""
        self._md(f"\n*{text}*\n")

    # --- the end ---------------------------------------------------------------------------
    def finish(self, result=None, table=None, error=None):
        extra = {}
        client, sid = getattr(table, "client", None), getattr(table, "session_id", None)
        if client and sid:
            with contextlib.suppress(Exception):
                extra["state"] = client.session(sid)
            with contextlib.suppress(Exception):  # practice: the supplier's internal talk, after the end
                extra["backoffice"] = client.backoffice(sid)
        replay = (extra.get("state") or {}).get("replay_url")
        if replay and os.environ.get("ARENA_URL"):
            replay = os.environ["ARENA_URL"].rstrip("/") + replay
        ended = _now()
        r = result if isinstance(result, dict) else {}
        self._md("\n## Result\n\n")
        if error:
            self._md(f"**Error:** `{error}`\n\n")
        for key in ("deal", "supplier_id", "end_reason", "c_open", "c_deal", "c_best", "score", "saved_eur"):
            if key in r:
                self._md(f"- {key}: {r[key]}\n")
        if r.get("terms"):
            self._md(f"- terms: `{_json(r['terms'])}`\n")
        if replay:
            self._md(f"- Replay: {replay}\n")
        self._md(f"- Rounds sent: {self.rounds}, took {(ended - self.started).total_seconds():.0f}s\n")
        if r:
            self._md(f"\n<details><summary>Full result</summary>\n\n```json\n{_json(r, indent=2)}\n```\n</details>\n")
        if bo := extra.get("backoffice"):
            self._md("\n## Back office (the supplier's internal talk)\n\n")
            for item in bo if isinstance(bo, list) else [bo]:
                if isinstance(item, dict) and item.get("text"):
                    who = " → ".join(str(item[k]) for k in ("from", "to") if item.get(k))
                    self._md(f"- **{who}**: {item['text']}\n")
                else:
                    self._md(f"- `{_json(item)}`\n")
        session = {
            "case_id": self.case_id,
            "session_id": self.session_id,
            "agent": self.agent,
            "started": self.started.isoformat(timespec="seconds"),
            "ended": ended.isoformat(timespec="seconds"),
            "error": error,
            "result": result if isinstance(result, dict) else None,
            "replay_url": replay,
            "case": self.case,
            "threads": self.threads,
            **extra,
        }
        (self.dir / "session.json").write_text(_json(session, indent=2) + "\n", encoding="utf-8")
        entry = {
            "at": ended.isoformat(timespec="seconds"),
            "run": self.run.dir.name,
            "case_id": self.case_id,
            "session_id": self.session_id,
            "agent": self.agent,
            "model": os.environ.get("LLM_MODEL") if self.agent == "LLMAgent" else None,
            **{k: r.get(k) for k in ("deal", "supplier_id", "score", "c_deal", "c_open", "c_best", "end_reason")},
            "rounds": self.rounds,
            "error": error,
            "path": str(self.dir.relative_to(log_root())),
        }
        _append(log_root() / "index.jsonl", entry)
        with _lock:
            self.run.meta["cases"].append({k: entry[k] for k in ("case_id", "session_id", "deal", "score", "path")})
            self.run._write_meta()


@contextlib.contextmanager
def case(case_id, agent=None, table=None):
    """Log one case played in this thread; yields its CaseLog (or None when logging is off)."""
    if not enabled():
        yield None
        return
    rec = CaseLog(_ensure_run(), case_id, agent)
    _local.case = rec
    try:
        yield rec
    except BaseException as e:
        rec.finish(table=table, error=f"{type(e).__name__}: {e}")
        raise
    finally:
        _local.case = None


@contextlib.contextmanager
def bind(rec):
    """Log what this thread does to `rec`'s case - for work a case hands to other threads (e.g. its
    e-mails to several suppliers at once)."""
    old = getattr(_local, "case", None)
    _local.case = rec
    try:
        yield
    finally:
        _local.case = old


def _redact(obj):
    """Keys never reach the log (e.g. the simulator's `register` answer)."""
    if isinstance(obj, dict):
        return {k: "<redacted>" if "api_key" in k.lower() or k.lower() == "key" else _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def http(method, path, params, body, status, response, seconds, attempt, move_id=None):
    """One API call. Called by ArenaClient for every request; goes to the case this thread plays."""
    if not enabled():
        return
    body, response = _redact(body), _redact(response)
    record = {
        "method": method,
        "path": path,
        "params": params,
        "move_id": move_id,
        "attempt": attempt,
        "status": status,
        "ms": round(seconds * 1000),
        "request": body,
        "response": response,
    }
    if rec := current():
        rec.http(record)
    elif _run and not _run.closed:
        _append(_run.dir / "http.jsonl", {"at": _now().isoformat(timespec="milliseconds"), **record})
