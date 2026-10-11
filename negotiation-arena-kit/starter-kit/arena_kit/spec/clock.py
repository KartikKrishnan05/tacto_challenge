"""The case clock (R11.14-R11.16) and the model's time budget (R12.17).

From case open: no new counter-offer after NO_NEW_COUNTER_AT, closed by FORCE_CLOSE_AT, so every
case ends inside the 8-minute wave cap. A round (our send to the supplier's reply) is measured per
thread - the last three, each capped at MAX_ROUND_S, DEFAULT_ROUND_S before any is measured - plus
ROUND_MARGIN_S, and no round starts that cannot finish before FORCE_CLOSE_AT. In the final the case
may open with less than 8 minutes left on its wave (`seconds_left`): the two marks move earlier.
"""

import threading
import time

NO_NEW_COUNTER_AT = 360.0
FORCE_CLOSE_AT = 420.0
DEFAULT_ROUND_S = 45.0
MAX_ROUND_S = 90.0
ROUND_MARGIN_S = 10.0
CAP_MARGIN_S = 60.0  # final: closed this long before the wave's cap at the latest
# R12.17: the model gets MODEL_WINDOW_S minus the time since the supplier's reply minus PER_OTHER_S for
# every other thread with a call in flight; under MIN_MODEL_S the planned move goes out without it.
MODEL_WINDOW_S, PER_OTHER_S, MIN_MODEL_S = 50.0, 15.0, 10.0


def model_budget(since_reply_s, others_in_flight):
    """Seconds the writer model may take, or None when the planned move must go without it."""
    budget = MODEL_WINDOW_S - max(0.0, since_reply_s) - PER_OTHER_S * max(0, others_in_flight)
    return budget if budget >= MIN_MODEL_S else None


class CaseClock:
    def __init__(self, seconds_left=None, now=time.monotonic):
        self.now = now
        self.t_open = None
        self.force_close_at, self.no_counter_at = FORCE_CLOSE_AT, NO_NEW_COUNTER_AT
        if isinstance(seconds_left, int | float):
            cap = seconds_left - CAP_MARGIN_S
            if cap < FORCE_CLOSE_AT:
                self.force_close_at = max(0.0, cap)
                self.no_counter_at = max(0.0, cap - (FORCE_CLOSE_AT - NO_NEW_COUNTER_AT))
        self.rounds = []  # measured round times, oldest first
        self._sent = {}  # sid -> when our last message went out
        self._lock = threading.Lock()

    def open(self):
        if self.t_open is None:
            self.t_open = self.now()
        return self

    def elapsed(self):
        return 0.0 if self.t_open is None else self.now() - self.t_open

    def sent(self, sid):
        with self._lock:
            self._sent[sid] = self.now()

    def replied(self, sid):
        """The supplier answered: one round measured (capped at MAX_ROUND_S)."""
        with self._lock:
            t = self._sent.pop(sid, None)
            if t is not None:
                self.rounds.append(min(self.now() - t, MAX_ROUND_S))

    def round_time(self):
        with self._lock:
            seen = self.rounds[-3:]
        return (max(seen) if seen else DEFAULT_ROUND_S) + ROUND_MARGIN_S

    def can_counter(self):
        """A new counter-offer may still go out: before NO_NEW_COUNTER_AT, and its round fits."""
        e = self.elapsed()
        return e < self.no_counter_at and e + self.round_time() <= self.force_close_at

    def round_fits(self):
        """One more round (e.g. a lock-in of a prose offer) finishes before FORCE_CLOSE_AT."""
        return self.elapsed() + self.round_time() <= self.force_close_at

    def late(self):
        return self.elapsed() >= self.no_counter_at

    def must_close(self):
        return self.elapsed() >= self.force_close_at

    def near_end(self, rounds=2):
        """Fewer than `rounds` rounds left before NO_NEW_COUNTER_AT (R11.8 pressure)."""
        return self.elapsed() + rounds * self.round_time() > self.no_counter_at
