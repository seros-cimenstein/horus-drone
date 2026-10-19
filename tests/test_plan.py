from __future__ import annotations

import asyncio
import threading
import time

import pytest

from horus.events import EventBus
from horus.executor import ExecutorState, HorusExecutor
from horus.executor.plan import PlanError, PlanRunner, validate_plan
from horus.flight import Pose

from tests.fakes import FakeFlight, PerceptionStub, executor_settings as _settings


class _Loop:
    """Drive the executor's controller tick from a thread, like the real 15 Hz worker."""

    def __init__(self, executor: HorusExecutor, on_tick=None):
        self.executor = executor
        self.on_tick = on_tick
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop.is_set():
            if self.on_tick is not None:
                self.on_tick()
            self.executor.tick()
            self.stop.wait(0.005)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5.0)
        assert not self.thread.is_alive(), "executor tick loop did not stop"


def _arrive_at_goto_target(flight: FakeFlight, executor: HorusExecutor):
    """Teleport the fake vehicle onto the executed target of the running goto."""

    def on_tick() -> None:
        state = executor.get_state()
        task_id = state["active_task_id"]
        if state["active_skill"] == "goto" and task_id is not None:
            target = executor.get_task_status(task_id)["executed_target"]
            flight._telemetry.pose = Pose(target["north_m"], target["east_m"], target["up_m"])
        now = time.monotonic()
        flight._telemetry.pose_updated_monotonic = now
        flight._telemetry.heading_updated_monotonic = now

    return on_tick


def _finish(runner: PlanRunner, plan_id: str, timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = runner.status(plan_id)
        if status["status"] != "running":
            return status
        time.sleep(0.01)
    raise AssertionError(f"plan still running: {runner.status(plan_id)}")


@pytest.mark.parametrize(
    "source, message",
    [
        ("import os", "Import"),
        ("goto(1, 2, 3).real", "Attribute"),
        ("x = [1, 2]", "List"),
        ("while True:\n    pass", "While"),
        ("for i in range(1000):\n    wait(0)", "at most 100"),
        ("for i in range(50):\n    for j in range(50):\n        wait(0)", "nested loops"),
        ("for i in range(n):\n    wait(0)", "integer literals"),
        ("x = 2 ** 64", "arithmetic is allowed"),
        ("open('f')", "not a plan function"),
        ("goto(1, 2)", "missing up_m"),
        ("goto(1, 2, 3, speed=9)", "no argument 'speed'"),
        ("goto = 1", "cannot assign"),
        ("x = goto", "must be called"),
        ("log(f'{1:1000000000}')", "format specs"),
    ],
)
def test_plans_outside_the_grammar_are_rejected_before_anything_runs(source, message):
    with pytest.raises(PlanError, match=message):
        validate_plan(source)


def test_plan_runs_skills_in_order_and_branches_on_their_results():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0))
    flight._telemetry.armed = True
    flight._telemetry.mode = "GUIDED"
    events = EventBus()
    executor = HorusExecutor(flight, _settings(command_ttl_s=5.0), event_bus=events, autostart=False)
    loop = _Loop(executor, _arrive_at_goto_target(flight, executor))
    runner = PlanRunner(executor)
    try:
        submitted = runner.submit(
            "for i in range(2):\n"
            "    ok = goto(1 + i, 0, 2)\n"
            "    if not ok:\n"
            "        break\n"
            "far = goto(30, 0, 2)\n"  # clamped by the executor, still arrives
            "log(f'legs ok={ok} far={far}')\n"
        )
        status = _finish(runner, submitted["plan_id"])
    finally:
        loop.close()
        executor.close()

    assert status["status"] == "succeeded", status
    assert [step["call"] for step in status["steps"]] == ["goto", "goto", "goto"]
    assert all(step["status"] == "succeeded" for step in status["steps"])
    assert status["logs"] == ["legs ok=True far=True"]
    third = executor.get_task_status(status["steps"][2]["task_id"])
    assert third["executed_target"]["north_m"] == pytest.approx(3.0)  # geofence radius clamp
    kinds = [event["kind"] for event in asyncio.run(events.wait(0, 0.0))]
    assert kinds.count("plan_finished") == 1


def test_emergency_hold_aborts_the_plan_and_no_later_step_is_queued():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0))
    flight._telemetry.armed = True
    flight._telemetry.mode = "GUIDED"
    executor = HorusExecutor(flight, _settings(command_ttl_s=5.0), autostart=False)
    loop = _Loop(executor)  # never arrives: the goto keeps running
    runner = PlanRunner(executor)
    try:
        plan_id = runner.submit("goto(2, 0, 2)\ngoto(-2, 0, 2)\nlog('unreachable')")["plan_id"]
        deadline = time.monotonic() + 2.0
        while runner.status(plan_id)["current_task_id"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        executor.hold()
        status = _finish(runner, plan_id)
    finally:
        loop.close()
        executor.close()

    assert status["status"] == "aborted"
    assert [step["call"] for step in status["steps"]] == ["goto"]
    assert status["steps"][0]["status"] == "cancelled"
    assert status["logs"] == []
    assert executor.state == ExecutorState.HOLDING


def test_cancel_plan_stops_the_running_step_and_the_plan():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0))
    flight._telemetry.armed = True
    flight._telemetry.mode = "GUIDED"
    executor = HorusExecutor(flight, _settings(command_ttl_s=5.0), autostart=False)
    loop = _Loop(executor)
    runner = PlanRunner(executor)
    try:
        plan_id = runner.submit("goto(2, 0, 2)\nlog('unreachable')")["plan_id"]
        deadline = time.monotonic() + 2.0
        while runner.status(plan_id)["current_task_id"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        task_id = runner.status(plan_id)["current_task_id"]
        with pytest.raises(RuntimeError, match="owns the skill queue"):
            runner.require_idle()
        runner.cancel(plan_id)
        status = _finish(runner, plan_id)
    finally:
        loop.close()
        executor.close()

    assert status["status"] == "cancelled"
    assert executor.get_task_status(task_id)["status"] == "cancelled"
    assert status["logs"] == []
    runner.require_idle()


def test_pilot_override_aborts_the_plan():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0))
    flight._telemetry.armed = True
    flight._telemetry.mode = "GUIDED"
    executor = HorusExecutor(flight, _settings(command_ttl_s=5.0), autostart=False)
    loop = _Loop(executor)
    runner = PlanRunner(executor)
    try:
        plan_id = runner.submit("goto(2, 0, 2)\ngoto(0, 0, 2)")["plan_id"]
        deadline = time.monotonic() + 2.0
        while runner.status(plan_id)["current_task_id"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        flight._telemetry.mode = "LOITER"
        status = _finish(runner, plan_id)
    finally:
        loop.close()
        executor.close()

    assert status["status"] == "aborted"
    assert len(status["steps"]) == 1
    assert executor.state == ExecutorState.PILOT


def test_runtime_errors_fail_the_plan_without_moving_the_vehicle():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0))
    executor = HorusExecutor(flight, _settings(), autostart=False)
    runner = PlanRunner(executor)
    try:
        undefined = _finish(runner, runner.submit("goto(x, 0, 2)")["plan_id"])
        divide = _finish(runner, runner.submit("x = 1 / 0")["plan_id"])
        bad_type = _finish(runner, runner.submit("goto('north', 0, 2)")["plan_id"])
        zero_id = _finish(runner, runner.submit("approach(0)")["plan_id"])
    finally:
        executor.close()

    assert undefined["status"] == "failed" and "'x' is not defined" in undefined["error"]
    assert divide["status"] == "failed" and "division by zero" in divide["error"]
    assert bad_type["status"] == "failed" and "finite number" in bad_type["error"]
    assert zero_id["status"] == "failed" and "tracker id must be a positive integer" in zero_id["error"]
    assert flight.commands == []


def test_plan_ttl_bounds_waiting_and_only_one_plan_runs_at_a_time():
    executor = HorusExecutor(FakeFlight(), _settings(), autostart=False)
    runner = PlanRunner(executor)
    try:
        plan_id = runner.submit("wait(30)", ttl_s=0.2)["plan_id"]
        with pytest.raises(RuntimeError, match="still running"):
            runner.submit("log('second')")
        status = _finish(runner, plan_id)
    finally:
        executor.close()

    assert status["status"] == "failed"
    assert status["error"] == "plan TTL expired"


class _TargetNeverVisible(PerceptionStub):
    """Every validation fails: a follow holds and re-identifies until its TTL."""


def test_follow_cut_short_by_the_plan_deadline_never_reports_the_full_duration():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0))
    flight._telemetry.armed = True
    flight._telemetry.mode = "GUIDED"
    executor = HorusExecutor(flight, _settings(), _TargetNeverVisible(), autostart=False)
    loop = _Loop(executor)
    runner = PlanRunner(executor)
    try:
        statuses = [
            _finish(runner, runner.submit("ok = follow(7, 5)", ttl_s=0.4)["plan_id"])
            for _ in range(3)
        ]
    finally:
        loop.close()
        executor.close()

    for status in statuses:
        # Either the plan deadline wins the race (failed) or the truncated follow returns False.
        assert status["variables"].get("ok") is not True, status
        assert status["status"] in {"failed", "succeeded"}
