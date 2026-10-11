"""R11.14–R11.15, R12.17 and R13.16: virtual time, no real sleeps."""

from types import SimpleNamespace

import pytest

from arena_kit.spec import runner
from arena_kit.spec.clock import CaseClock, model_budget


class Time:
    def __init__(self, value=0.0):
        self.value = value

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def test_r11_14_no_counter_at_360_and_force_close_at_420():
    time = Time(100)
    clock = CaseClock(now=time.monotonic).open()
    time.value = 459
    assert clock.can_counter()
    time.value = 460
    assert clock.late() and not clock.can_counter() and clock.round_fits()
    time.value = 520
    assert clock.must_close() and not clock.round_fits()
    clock.open()  # opening twice must not reset the case clock
    assert clock.elapsed() == 420


def test_r11_15_rounds_use_last_three_capped_at_90_plus_margin():
    time = Time()
    clock = CaseClock(now=time.monotonic).open()
    assert clock.round_time() == 55
    for seconds in (120, 10, 20, 30):
        clock.sent("supplier")
        time.sleep(seconds)
        clock.replied("supplier")
    assert clock.rounds == [90, 10, 20, 30]
    assert clock.round_time() == 40
    time.value = 350
    clock.rounds = [90]
    assert not clock.can_counter()  # round would end at 450, despite being before 360
    assert not clock.round_fits()


@pytest.mark.parametrize("seconds_left,no_counter,force", [(480, 360, 420), (240, 120, 180), (30, 0, 0), (0, 0, 0)])
def test_wave_clock_shortens_to_actual_remaining_time(seconds_left, no_counter, force):
    clock = CaseClock(seconds_left)
    assert (clock.no_counter_at, clock.force_close_at) == (no_counter, force)


@pytest.mark.parametrize("elapsed,others,expected", [(0, 0, 50), (10, 1, 25), (10, 2, 10), (10.1, 2, None)])
def test_r12_17_writer_budget(elapsed, others, expected):
    assert model_budget(elapsed, others) == expected


def test_r13_16_accept_wait_does_not_exceed_reply_clock(monkeypatch):
    time = Time(20)
    monkeypatch.setattr(runner, "time", time)
    runner.wait_for_rivals({"best": False, "slow": True}, "best", {"best": 0})
    assert time.value == 45  # 15 seconds remain to send the accept
    runner.wait_for_rivals({"slow": True}, "best", {"best": 0})
    assert time.value == 45  # expired budget must not restart on a second attempt


def test_r13_16_does_not_wait_for_own_call_or_completed_rivals(monkeypatch):
    time = Time(5)
    monkeypatch.setattr(runner, "time", time)
    runner.wait_for_rivals({"best": True, "other": False}, "best", {"best": 0})
    assert time.value == 5


def test_r13_16_slow_post_recovers_already_visible_reply(monkeypatch):
    from concurrent.futures import TimeoutError
    from importlib import import_module

    play = import_module("arena_kit.play")
    reply = {"from": "supplier", "status": "final_offer"}

    class Pending:
        def result(self, timeout):
            assert timeout <= 3  # polling must leave almost the entire 60-second reply budget
            raise TimeoutError

        def done(self):
            return False

    monkeypatch.setattr(play, "SENDER", SimpleNamespace(submit=lambda *args: Pending()))
    client = SimpleNamespace(send_message=lambda *args: None)
    table = play.RemoteTable(client)
    reads = iter([[{"from": "supplier"}], [{"from": "supplier"}, {"from": "buyer"}, reply]])
    monkeypatch.setattr(table, "refresh", lambda sid: next(reads))
    assert table.send("supplier", {"body": "offer"}) is reply
