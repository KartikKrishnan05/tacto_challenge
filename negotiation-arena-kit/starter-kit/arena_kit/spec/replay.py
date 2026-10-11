"""Replaying recorded games: a free screen before a practice session is spent.

The hosted suppliers answer the same e-mail the same way (sweep-1: ten variants that sent identical
e-mails on P10 got identical replies and scores in ten separate sessions). So a code-only game whose
e-mails match a recorded game's, message for message, IS that game - its replies and its result are
already known. `ReplayTable` stands in for the arena: it serves the recorded opening, answers every
e-mail that some recorded game sent in exactly the same thread position (same term sheets, same
action, same cited refs) with the reply that game got, and returns the recorded result for an accept
of the same terms. The first e-mail no recorded game sent makes the game `diverged`: only then is a
real session needed - and its reply joins the book for the next screen.

Attachments (quote PDFs, cost breakdowns) are fetched once from the recorded session and cached in
`runs/lab/attachments/`; reading a finished session's files does not use a practice session.
"""

import copy
import json
import logging
from pathlib import Path

log = logging.getLogger("arena")
KIT = Path(__file__).resolve().parents[2]
CACHE = KIT / "runs" / "lab" / "attachments"


def msg_key(msg):
    """What the supplier reacts to in one of our e-mails: the term sheets, the action and the refs."""
    action = msg.get("action") or {}
    return json.dumps({"sheets": [ts.get("terms") or {} for ts in msg.get("term_sheets") or []],
                       "action": action.get("type"), "id": action.get("term_sheet_id"),
                       "refs": sorted(msg.get("refs") or [])}, sort_keys=True, default=str)  # fmt: skip


def terms_key(terms):
    return json.dumps(terms or {}, sort_keys=True, default=str)


def sheet_terms(messages, offer_id):
    """The terms of the supplier's sheet `offer_id` in a thread, or None."""
    for m in messages:
        if m.get("from") == "buyer":
            continue
        for ts in m.get("term_sheets") or []:
            if ts.get("term_sheet_id") == offer_id:
                return ts.get("terms") or {}
    return None


class CaseBook:
    """Everything the recorded games of one case say: openings, replies by position, deals."""

    def __init__(self):
        self.opening = None  # {sid: [supplier messages before our first e-mail]}
        self.replies = {}  # (sid, (key, key, ...)) -> the supplier's reply to the last of those e-mails
        self.deals = {}  # (sid, accepted terms) -> the session's result
        self.att_session = {}  # attachment id -> a session that has it
        self.games = 0

    def add(self, record):
        threads, result, session = record.get("threads") or {}, record.get("result") or {}, record.get("session_id")
        if not threads or not result or any(t.get("timeouts") for t in threads.values()):
            return False  # a timeout depends on the clock, not on the e-mails: not replayable
        opening = {}
        for sid, t in threads.items():
            msgs = t.get("messages") or []
            first = next((i for i, m in enumerate(msgs) if m.get("from") == "buyer"), len(msgs))
            opening[sid] = msgs[:first]
            prefix = []
            for i, m in enumerate(msgs):
                for att in m.get("attachments") or []:
                    if att.get("attachment_id") and session:
                        self.att_session.setdefault(att["attachment_id"], session)
                if m.get("from") != "buyer":
                    continue
                prefix.append(msg_key(m))
                reply = next((r for r in msgs[i + 1 :] if r.get("from") != "buyer"), None)
                if reply is not None and not (m.get("action") or {}).get("type"):
                    self.replies.setdefault((sid, tuple(prefix)), reply)
        if self.opening is None:
            self.opening = opening
        sid, oid = result.get("supplier_id"), result.get("offer_id")
        if result.get("deal") and sid in threads and oid:
            sheet = sheet_terms(threads[sid].get("messages") or [], oid)
            if sheet is not None:
                self.deals.setdefault((sid, oid, terms_key(sheet)), result)
        self.games += 1
        return True


class Book:
    """The recorded code-only games of every case, from the experiment sheet's `record` files."""

    def __init__(self):
        self.cases = {}

    @classmethod
    def from_rows(cls, rows):
        book = cls()
        for r in rows:
            if r.get("mode") != "code" or r.get("error") or r.get("replay") or not r.get("record"):
                continue
            path = KIT / r["record"]
            if not path.exists():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            book.add(r["case_id"], record)
        return book

    def add(self, case_id, record):
        return self.cases.setdefault(case_id, CaseBook()).add(record)

    def get(self, case_id):
        b = self.cases.get(case_id)
        return b if b and b.opening else None


class _Client:
    """The parts of ArenaClient the spec runner calls, answered from the replay."""

    def __init__(self, table, real):
        self.table, self.real = table, real

    def thread(self, session_id, supplier_id):
        return {"messages": copy.deepcopy(self.table.threads[supplier_id])}

    def session(self, session_id):
        t = self.table
        return {"status": "ended" if t.final else "open", "result": t.final}

    def attachment(self, session_id, attachment_id):
        return self.table.attachment(attachment_id)


class ReplayTable:
    """A stand-in for RemoteTable that plays a case from its recorded games."""

    def __init__(self, book, case, real_client=None):
        self.book, self.case, self.real = book, case, real_client
        self.session_id, self.done, self.final = "replay", None, None
        self.threads, self.diverged = {}, None
        self.client = _Client(self, real_client)

    def open(self, case_id):
        self.threads = copy.deepcopy(self.book.opening)
        return copy.deepcopy(self.case), copy.deepcopy(self.threads)

    def refresh(self, supplier_id):
        return copy.deepcopy(self.threads[supplier_id])

    def _stop(self, why):
        if self.diverged is None:
            self.diverged = why
            log.info("replay %s diverged: %s", self.case.get("case_id"), why)
        self.final = {"deal": None, "diverged": why}
        return {"result": self.final}

    def send(self, supplier_id, message):
        if self.final:
            return {"result": self.final}
        mine = [msg_key(m) for m in self.threads[supplier_id] if m.get("from") == "buyer"]
        reply = self.book.replies.get((supplier_id, (*mine, msg_key(message))))
        if reply is None:
            return self._stop(f"{supplier_id} e-mail {len(mine) + 1} not in the book")
        self.threads[supplier_id] += [{**copy.deepcopy(message), "from": "buyer"}, copy.deepcopy(reply)]
        return copy.deepcopy(reply)

    def close(self, supplier_id, message):
        if self.final:
            return self.final
        action = message.get("action") or {}
        if action.get("type") != "accept":
            return self._stop(f"{action.get('type')} on {supplier_id} not in the book")["result"]
        oid = action.get("term_sheet_id")
        terms = sheet_terms(self.threads[supplier_id], oid)
        result = self.book.deals.get((supplier_id, oid, terms_key(terms)))
        if result is None:
            return self._stop(f"accept of {action.get('term_sheet_id')} ({terms}) not in the book")["result"]
        self.final = copy.deepcopy(result)
        return self.final

    def missed(self, supplier_id, error):
        return self._stop(f"{supplier_id}: {error}")

    def result(self):
        return self.final or self._stop("the game ended without a close")["result"]

    def attachment(self, attachment_id):
        path = CACHE / attachment_id.replace(":", "_")
        if path.exists():
            return path.read_bytes()
        session = self.book.att_session.get(attachment_id)
        if self.real is None or session is None:
            raise FileNotFoundError(attachment_id)
        data = self.real.attachment(session, attachment_id)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data
