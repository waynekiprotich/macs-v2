import pytest

from core.schedule import next_cycle, wait_for_cycle

# A 15-minute boundary: 2026-09-15 15:30:00 UTC.
B = 1_789_486_200


class FakeClock:
    def __init__(self, t, factor=1.0, jumps=()):
        self.t, self.factor, self.jumps, self.sleeps = t, factor, list(jumps), []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += self.jumps.pop(0) if self.jumps else seconds * self.factor


def test_reference_time_is_a_boundary():
    assert B % 900 == 0


@pytest.mark.parametrize("now, last, wait, boundary", [
    (B - 1, None, 11, B),
    (B + 1, None, 9, B),
    (B + 10, None, 0, B),
    (B + 60, None, 0, B),
    (B + 120, None, 0, B),
    (B + 121, None, 900 + 10 - 121, B + 900),
    (B + 25, B, 900 + 10 - 25, B + 900),
    (B + 950, B, 0, B + 900),
    (B + 1100, B, 1800 + 10 - 1100, B + 1800),
], ids=[
    "immediately before boundary", "immediately after boundary", "at start delay",
    "restart inside window", "last second of window", "delayed past window",
    "after a quick cycle", "long analysis into next window", "long analysis past next window",
])
def test_next_cycle(now, last, wait, boundary):
    assert next_cycle(now, last) == (pytest.approx(wait), boundary)


def test_clock_stepping_backwards_never_reruns_a_boundary():
    wait, boundary = next_cycle(B - 2000, last_boundary=B)
    assert boundary == B + 900
    assert wait == pytest.approx(B + 910 - (B - 2000))


def test_restart_inside_the_window_runs_immediately():
    clock = FakeClock(B + 60)
    assert wait_for_cycle(None, clock=clock.now, sleep=clock.sleep) == B
    assert clock.sleeps == []


@pytest.mark.parametrize("factor", [0.5, 1.0, 1.5], ids=["timer runs slow", "exact", "timer oversleeps"])
def test_waits_on_the_clock_so_start_times_do_not_drift(factor):
    clock = FakeClock(B + 25, factor=factor)

    boundary = wait_for_cycle(B, clock=clock.now, sleep=clock.sleep)

    assert boundary == B + 900
    assert B + 910 <= clock.t <= B + 910 + 15
    assert all(s <= 30 for s in clock.sleeps)


def test_consecutive_cycles_stay_on_boundaries_whatever_the_cycle_length():
    clock = FakeClock(B + 10)
    last = None
    starts = []
    for cycle_seconds in (8, 45, 110, 3):
        last = wait_for_cycle(last, clock=clock.now, sleep=clock.sleep)
        starts.append(clock.t - last)
        clock.t += cycle_seconds
    assert starts == [pytest.approx(10)] * 4
    assert last == B + 3 * 900


def test_paused_process_skips_the_missed_candle_instead_of_running_it_late():
    clock = FakeClock(B + 25, jumps=[2000])

    boundary = wait_for_cycle(B, clock=clock.now, sleep=clock.sleep)

    assert boundary == B + 2700
    assert clock.t - boundary == pytest.approx(10)
