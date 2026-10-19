from __future__ import annotations

import threading
import time

import pytest

from scripts.gz_sim import _SimClock


class _Process:
    def __init__(self, returncode: int | None):
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def _sim_clock(topic: str = "/world/horus_urban/stats", process: object = None) -> _SimClock:
    """A clock with no subscriber thread: tests feed samples and the process state directly."""
    clock = _SimClock.__new__(_SimClock)
    clock._lock = threading.Lock()
    clock._sample = None
    clock.topic = topic
    clock._process = process
    return clock


def test_sim_clock_extrapolates_at_the_measured_rate_not_the_reported_factor():
    clock = _sim_clock()
    clock._parse("sim_time {\n  sec: 12\n  nsec: 500000000\n}\nreal_time_factor: 0.65\n", wall_seconds=100.0)
    assert clock._sample == (12.5, 100.0, 0.0)  # one message: no rate yet
    # 0.5 s of simulation over 2 s of wall time is a measured 0.25, whatever the field reports.
    clock._parse("sim_time {\n  sec: 13\n}\nreal_time_factor: 0.9\n", wall_seconds=102.0)
    assert clock._sample == (13.0, 102.0, 0.25)
    # A statistics message without a sim_time block must not overwrite the last sample.
    clock._parse("real_time_factor: 0.9\n", wall_seconds=103.0)
    assert clock._sample[0] == 13.0


def test_sim_clock_startup_failure_names_the_statistics_topic():
    clock = _sim_clock(process=_Process(1))

    with pytest.raises(RuntimeError, match="/world/horus_urban/stats"):
        clock.wait_ready(timeout_s=120.0)


def test_sim_clock_startup_wait_can_be_cancelled():
    stop = threading.Event()
    stop.set()

    _sim_clock(process=object()).wait_ready(timeout_s=120.0, stop_event=stop)


def test_sim_clock_startup_wait_can_keep_waiting_for_a_flight_client():
    clock = _sim_clock("/world/horus_forest/stats", _Process(None))
    consulted: list[float] = []

    def keep_waiting(elapsed_s: float) -> bool:
        consulted.append(elapsed_s)
        clock._sample = (0.0, time.monotonic(), 1.0)  # statistics finally arrive
        return True

    clock.wait_ready(timeout_s=0.0, keep_waiting=keep_waiting)

    assert consulted  # the timeout consulted the caller instead of raising

    with pytest.raises(RuntimeError, match="/world/horus_urban/stats"):
        _sim_clock(process=_Process(None)).wait_ready(timeout_s=0.0, keep_waiting=lambda elapsed_s: False)
