from __future__ import annotations

import asyncio
import math
import threading
import time

import pytest

from horus.events import EventBus
from horus.executor import ExecutorState, HorusExecutor
from horus.flight import Pose

from tests.fakes import FakeFlight, FakeGimbal, executor_settings as _settings


def test_goto_completes_only_at_measured_target_and_enters_guided_hold():
    flight = FakeFlight()
    flight._telemetry.armed = True
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task = executor.submit_skill("goto", {"pose": {"north_m": 0, "east_m": 0, "up_m": 2}})

    executor.tick()

    status = executor.get_task_status(task["task_id"])
    assert status["status"] == "succeeded"
    assert status["result"]["success"] is True
    assert executor.state == ExecutorState.HOLDING
    assert ("guided",) in flight.commands
    assert ("hold",) in flight.commands
    executor.close()


def test_take_off_waits_for_guided_mode_and_arming_before_native_takeoff():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    gimbal = FakeGimbal()
    executor = HorusExecutor(flight, _settings(), gimbal=gimbal, autostart=False)
    task = executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)

    executor.tick()
    task_id = task["task_id"]
    assert executor.get_task_status(task_id)["status"] == "running"
    assert flight.commands == [("guided",)]
    assert gimbal.points == [(0.0, 0.0)]  # take-off restores the configured search pitch

    flight._telemetry.mode = "GUIDED"
    executor.tick()
    assert flight.commands == [("guided",), ("arm",)]

    flight._telemetry.armed = True
    executor.tick()
    assert flight.commands[-1] == ("takeoff", 2.0)
    assert not any(command[0] == "velocity" for command in flight.commands)

    executor.tick()
    assert executor.get_task_status(task_id)["status"] == "running"
    assert not any(command[0] == "velocity" for command in flight.commands)

    flight._telemetry.pose = Pose(0.0, 0.0, 2.0)
    flight._telemetry.pose_updated_monotonic = time.monotonic()
    executor.tick()

    final = executor.get_task_status(task_id)
    assert final["status"] == "succeeded"
    assert final["result"]["success"] is True
    executor.close()


def test_take_off_retries_arming_after_prearm_rejection(monkeypatch):
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task_id = executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=30.0)["task_id"]
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    flight._telemetry.mode = "GUIDED"
    executor.tick()
    executor.tick()
    assert flight.commands.count(("arm",)) == 1

    # Arming was rejected by a pre-arm check: no retry before the interval, one retry after it.
    clock[0] += HorusExecutor.ARM_RETRY_INTERVAL_S / 2
    executor.tick()
    assert flight.commands.count(("arm",)) == 1
    clock[0] += HorusExecutor.ARM_RETRY_INTERVAL_S
    executor.tick()
    assert flight.commands.count(("arm",)) == 2
    assert executor.get_task_status(task_id)["status"] == "running"

    flight._telemetry.armed = True
    executor.tick()
    assert flight.commands[-1] == ("takeoff", 2.0)
    executor.close()


def test_ttl_expiry_stops_active_motion_and_enters_holding():
    flight = FakeFlight()
    flight._telemetry.armed = True
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task = executor.submit_skill(
        "goto", {"pose": {"north_m": 2, "east_m": 0, "up_m": 2}}, ttl_s=0.01
    )
    executor.tick()
    assert executor.state == ExecutorState.EXECUTING
    assert any(command[0] == "velocity" and command[1] > 0 for command in flight.commands)

    time.sleep(0.02)
    executor.tick()

    status = executor.get_task_status(task["task_id"])
    assert status["status"] == "expired"
    assert executor.state == ExecutorState.HOLDING
    assert flight.commands[-1] == ("hold",)
    executor.close()


def test_goto_and_take_off_run_their_targets_unclamped_and_reject_targets_at_or_below_home():
    executor = HorusExecutor(FakeFlight(), _settings(), autostart=False)
    task = executor.submit_skill(
        "goto", {"pose": {"north_m": 100, "east_m": -40, "up_m": 60}}, ttl_s=5
    )
    assert task["requested_target"] == {"north_m": 100.0, "east_m": -40.0, "up_m": 60.0}
    assert task["executed_target"] == task["requested_target"]
    assert task["clamp_reason"] is None
    take_off = executor.submit_skill("take_off", {"altitude_m": 60.0}, ttl_s=5)
    assert take_off["executed_target"] == {"altitude_m": 60.0}
    assert take_off["clamp_reason"] is None

    for up_m in (0.0, -1.0):
        with pytest.raises(ValueError, match="above home"):
            executor.submit_skill("goto", {"pose": {"north_m": 1, "east_m": 0, "up_m": up_m}})
        with pytest.raises(ValueError, match="altitude_m must be above 0"):
            executor.submit_skill("take_off", {"altitude_m": up_m})
    executor.close()


def test_emergency_hold_preempts_queued_skill_without_waiting_for_tick():
    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task = executor.submit_skill("goto", {"pose": {"north_m": 2, "east_m": 0, "up_m": 2}})

    result = executor.hold()

    assert result["state"] == "HOLDING"
    assert executor.get_task_status(task["task_id"])["status"] == "cancelled"
    assert flight.commands[-1] == ("hold",)
    executor.close()


def test_hold_transitions_to_rth_after_link_loss_grace():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight, _settings(link_loss_rth_delay_s=0.01), autostart=False
    )
    executor.hold()
    executor.tick()
    assert executor.state == ExecutorState.HOLDING
    time.sleep(0.02)

    executor.tick()

    assert executor.state == ExecutorState.RTH
    assert flight.commands[-1] == ("rth",)
    executor.close()


def test_supervisor_contact_keeps_hold_and_its_loss_triggers_rth():
    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(link_loss_rth_delay_s=0.05), autostart=False)
    executor.hold()
    for _ in range(4):  # a commander long-polling events keeps the link alive past the delay
        time.sleep(0.02)
        executor.note_supervisor_contact()
        executor.tick()
    assert executor.state == ExecutorState.HOLDING

    time.sleep(0.07)
    executor.tick()

    assert executor.state == ExecutorState.RTH
    executor.close()


def test_operator_emergency_refuses_the_ai_until_released_but_not_the_operator():
    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(), autostart=False)
    executor.submit_skill("goto", {"pose": {"north_m": 2, "east_m": 0, "up_m": 2}})

    assert executor.rth(source="operator")["state"] == "RTH"
    assert executor.get_state()["operator_lock"] == "rth"

    # The AI's own emergencies and moves would undo the operator's RTH: refused, RTH kept.
    for undo in (
        lambda: executor.hold(source="mcp"),
        lambda: executor.land(source="mcp"),
        lambda: executor.submit_skill("goto", {"pose": {"north_m": 1, "east_m": 0, "up_m": 2}}),
    ):
        with pytest.raises(RuntimeError, match="operator emergency rth in effect"):
            undo()
    assert executor.state == ExecutorState.RTH
    assert flight.commands[-1] == ("rth",)

    # The operator can change its mind at any time; the lock follows the latest action.
    assert executor.hold(source="operator")["state"] == "HOLDING"
    assert executor.get_state()["operator_lock"] == "hold"

    assert executor.release_operator_lock() == {"released": "hold"}
    assert executor.get_state()["operator_lock"] is None
    assert executor.hold(source="mcp")["state"] == "HOLDING"
    executor.submit_skill("goto", {"pose": {"north_m": 1, "east_m": 0, "up_m": 2}})
    executor.close()


def test_link_loss_rth_still_fires_during_an_operator_hold():
    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(link_loss_rth_delay_s=0.01), autostart=False)
    executor.hold(source="operator")
    executor.tick()
    time.sleep(0.02)

    executor.tick()

    assert executor.state == ExecutorState.RTH
    assert flight.commands[-1] == ("rth",)
    executor.close()


def test_task_that_ends_on_a_disarmed_vehicle_never_arms_link_loss_rth():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))  # on the ground, never armed
    executor = HorusExecutor(flight, _settings(link_loss_rth_delay_s=0.01), autostart=False)
    task = executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=0.02)
    executor.tick()
    time.sleep(0.03)
    executor.tick()
    assert executor.get_task_status(task["task_id"])["status"] == "expired"
    mark = len(flight.commands)

    time.sleep(0.03)
    for _ in range(3):
        executor.tick()

    assert executor.state == ExecutorState.IDLE
    assert flight.commands[mark:] == []
    executor.close()


def _flying_goto(flight, executor, ttl_s=5.0):
    """Start a goto in the air and let the autopilot confirm HORUS's own GUIDED request."""
    flight._telemetry.armed = True
    flight._telemetry.mode = "LOITER"
    executor.tick()
    task = executor.submit_skill(
        "goto", {"pose": {"north_m": 2, "east_m": 0, "up_m": 2}}, ttl_s=ttl_s
    )
    executor.tick()
    assert ("guided",) in flight.commands
    flight._telemetry.mode = "GUIDED"
    executor.tick()
    assert executor.state == ExecutorState.EXECUTING
    return task


@pytest.mark.parametrize("taken_mode", ["LOITER", "RTL", "LAND"])
def test_uncommanded_mode_change_cancels_work_and_horus_never_retakes_the_vehicle(taken_mode):
    flight = FakeFlight()
    event_bus = EventBus()
    executor = HorusExecutor(
        flight, _settings(link_loss_rth_delay_s=0.01), event_bus=event_bus, autostart=False
    )
    task = _flying_goto(flight, executor, ttl_s=0.05)

    flight._telemetry.mode = taken_mode  # pilot RC switch or autopilot failsafe
    mark = len(flight.commands)
    executor.tick()

    status = executor.get_task_status(task["task_id"])
    assert status["status"] == "cancelled"
    assert "pilot override" in status["error"]
    assert executor.state == ExecutorState.PILOT
    assert executor.get_state()["pilot_override"] == taken_mode
    time.sleep(0.06)  # past both the task TTL and the link-loss RTH delay
    for _ in range(3):
        executor.tick()
    for refused in (
        lambda: executor.submit_skill("goto", {"pose": {"north_m": 1, "east_m": 0, "up_m": 2}}),
        executor.hold,
        executor.rth,
        executor.land,
    ):
        with pytest.raises(RuntimeError, match="pilot override"):
            refused()
    assert flight.commands[mark:] == []
    kinds = [event["kind"] for event in asyncio.run(event_bus.wait(0, 0.0))]
    assert kinds.count("pilot_override") == 1
    executor.close()
    assert flight.commands[mark:] == []


def test_switching_back_to_guided_returns_control_without_resending_the_mode():
    flight = FakeFlight()
    event_bus = EventBus()
    executor = HorusExecutor(flight, _settings(), event_bus=event_bus, autostart=False)
    _flying_goto(flight, executor)
    flight._telemetry.mode = "LOITER"
    executor.tick()
    flight._telemetry.mode = "ALT_HOLD"
    executor.tick()
    assert executor.get_state()["pilot_override"] == "ALT_HOLD"

    flight._telemetry.mode = "GUIDED"
    executor.tick()

    assert executor.state == ExecutorState.HOLDING
    assert executor.get_state()["pilot_override"] is None
    assert "pilot_released" in [event["kind"] for event in asyncio.run(event_bus.wait(0, 0.0))]
    mark = len(flight.commands)
    task = executor.submit_skill("goto", {"pose": {"north_m": 2, "east_m": 0, "up_m": 2}}, ttl_s=5)
    executor.tick()
    assert executor.get_task_status(task["task_id"])["status"] == "running"
    # GUIDED is confirmed, so a new task only streams setpoints; re-sending the mode on a report
    # that predates a fresh pilot switch would undo that switch.
    assert ("guided",) not in flight.commands[mark:]
    assert any(command[0] == "velocity" and command[1] > 0 for command in flight.commands[mark:])
    executor.close()


def test_horus_commanded_rth_is_not_a_pilot_override():
    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(), autostart=False)
    _flying_goto(flight, executor)

    executor.rth()
    flight._telemetry.mode = "RTL"
    executor.tick()

    assert executor.state == ExecutorState.RTH
    assert executor.get_state()["pilot_override"] is None
    executor.close()


def test_disarm_ends_pilot_override_and_accepts_a_new_flight():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    executor = HorusExecutor(flight, _settings(), autostart=False)
    _flying_goto(flight, executor)
    flight._telemetry.mode = "LAND"
    executor.tick()
    assert executor.state == ExecutorState.PILOT

    flight._telemetry.armed = False
    executor.tick()

    assert executor.state == ExecutorState.IDLE
    task = executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)
    assert task["status"] == "queued"
    executor.close()


def test_take_off_rejects_a_missing_altitude_value():
    executor = HorusExecutor(FakeFlight(), _settings(), autostart=False)
    with pytest.raises(ValueError, match="altitude_m must be a finite number"):
        executor.submit_skill("take_off", {"altitude_m": None})
    executor.close()


def test_take_off_uses_configured_outdoor_gimbal_pitch():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    gimbal = FakeGimbal()
    settings = _settings(default_gimbal_pitch_deg=-45.0)
    executor = HorusExecutor(flight, settings, gimbal=gimbal, autostart=False)
    executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)
    executor.tick()
    assert gimbal.points == [(-45.0, 0.0)]
    executor.close()


def test_take_off_with_a_fixed_camera_flies_without_aiming():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    executor = HorusExecutor(flight, _settings(default_gimbal_pitch_deg=-45.0), autostart=False)
    task_id = executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)["task_id"]
    executor.tick()
    assert executor.get_task_status(task_id)["status"] == "running"
    assert flight.commands == [("guided",)]
    assert executor.get_state()["gimbal"] is None
    executor.close()


def test_executor_rejects_out_of_range_default_gimbal_pitch():
    with pytest.raises(ValueError):
        _settings(default_gimbal_pitch_deg=-90.01)
    with pytest.raises(ValueError):
        _settings(default_gimbal_pitch_deg=45.01)


def test_take_off_turns_to_requested_heading_before_succeeding():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task_id = executor.submit_skill("take_off", {"altitude_m": 2.0, "heading_deg": 0.0}, ttl_s=5.0)["task_id"]
    flight._telemetry.mode = "GUIDED"
    flight._telemetry.armed = True
    flight._telemetry.heading_deg = 40.0  # left over from a previous flight
    for _ in range(3):
        executor.tick()
    flight._telemetry.pose = Pose(0.0, 0.0, 2.0)
    flight._telemetry.pose_updated_monotonic = time.monotonic()
    executor.tick()
    assert executor.get_task_status(task_id)["status"] == "running"
    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0, 0.0)

    flight._telemetry.heading_deg = 358.0  # within tolerance across the 0/360 wrap
    executor.tick()
    assert executor.get_task_status(task_id)["status"] == "succeeded"
    executor.close()

def _advance_autopilot_clock(flight: FakeFlight, seconds: float) -> None:
    telemetry = flight._telemetry
    telemetry.time_boot_ms = (telemetry.time_boot_ms or 10_000) + int(round(seconds * 1000.0))
    telemetry.time_boot_updated_monotonic = time.monotonic()


def _set_heading(flight: FakeFlight, heading_deg: float) -> None:
    flight._telemetry.heading_deg = heading_deg
    flight._telemetry.heading_updated_monotonic = time.monotonic()


def test_turn_by_sweeps_the_yaw_setpoint_at_its_rate_in_autopilot_time_through_north():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True, mode="GUIDED")
    _set_heading(flight, 350.0)
    _advance_autopilot_clock(flight, 0.0)
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task_id = executor.submit_skill("turn", {"by_deg": 90.0, "rate_deg_s": 30.0}, ttl_s=30.0)["task_id"]

    executor.tick()
    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0, pytest.approx(350.0))
    # 0.5 s of autopilot time per tick at 30 deg/s: 15 deg per tick, hovering, wrapping past north;
    # a longer autopilot gap still advances at most 0.5 s, and the setpoint stops at the sweep's end.
    for gap_s, expected in ((0.5, 5.0), (0.5, 20.0), (5.0, 35.0), (0.5, 50.0), (0.5, 65.0), (0.5, 80.0), (0.5, 80.0)):
        _advance_autopilot_clock(flight, gap_s)
        executor.tick()
        assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0, pytest.approx(expected))
    assert executor.get_task_status(task_id)["status"] == "running"

    _set_heading(flight, 77.0)
    _advance_autopilot_clock(flight, 0.5)
    executor.tick()
    status = executor.get_task_status(task_id)
    assert (status["status"], status["reason"]) == ("succeeded", "succeeded")
    assert status["result"]["executed_target"] == {
        "by_deg": 90.0, "rate_deg_s": 30.0, "heading_deg": pytest.approx(80.0),
        "from_heading_deg": 350.0, "sweep_deg": 90.0,
    }
    executor.close()


def test_turn_to_a_heading_goes_the_shorter_way_at_an_unclamped_rate_and_limits_the_sweep():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True, mode="GUIDED")
    _set_heading(flight, 10.0)
    _advance_autopilot_clock(flight, 0.0)
    executor = HorusExecutor(flight, _settings(turn_rate_deg_s=20.0), autostart=False)
    default = executor.submit_skill("turn", {"by_deg": 30.0}, ttl_s=30.0)
    assert default["executed_target"]["rate_deg_s"] == 20.0  # the configured default
    executor.cancel(default["task_id"])
    # 100 deg/s is above the 20 deg/s default (and above the old 30 deg/s HORUS maximum): it is
    # flown as asked, since ArduPilot's yaw-rate limits bound the real turn.
    task_id = executor.submit_skill("turn", {"heading_deg": 300.0, "rate_deg_s": 100.0}, ttl_s=30.0)["task_id"]

    executor.tick()
    _advance_autopilot_clock(flight, 0.5)
    executor.tick()
    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0, pytest.approx(320.0))  # 10 - 100 * 0.5
    status = executor.get_task_status(task_id)
    assert status["executed_target"]["sweep_deg"] == pytest.approx(-70.0)
    assert status["executed_target"]["rate_deg_s"] == 100.0
    assert status["clamp_reason"] is None

    _advance_autopilot_clock(flight, 0.5)
    executor.tick()
    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0, pytest.approx(300.0))
    _set_heading(flight, 301.0)
    executor.tick()
    assert executor.get_task_status(task_id)["reason"] == "succeeded"

    whole_turn = executor.submit_skill("turn", {"by_deg": -720.0})
    assert whole_turn["executed_target"]["by_deg"] == -360.0
    assert "revolution" in whole_turn["clamp_reason"]
    for arguments in (
        {}, {"heading_deg": 90.0, "by_deg": 90.0}, {"by_deg": 90.0, "rate_deg_s": 0.0},
        {"by_deg": 90.0, "rate_deg_s": float("inf")},
    ):
        with pytest.raises(ValueError):
            executor.submit_skill("turn", arguments)
    executor.close()


def test_move_resolves_its_body_frame_target_when_it_starts_and_flies_it_unclamped():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True, mode="GUIDED")
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task_id = executor.submit_skill("move", {"forward_m": 2.0, "right_m": 1.0, "up_m": 0.5})["task_id"]
    _set_heading(flight, 90.0)  # the vehicle turned east while the move was queued

    executor.tick()
    status = executor.get_task_status(task_id)
    target = {"north_m": pytest.approx(-1.0), "east_m": pytest.approx(2.0), "up_m": pytest.approx(2.5)}
    assert status["requested_target"] == {
        "forward_m": 2.0, "right_m": 1.0, "up_m": 0.5, "from_heading_deg": 90.0, "pose": target,
    }
    assert status["executed_target"] == {"from_heading_deg": 90.0, "pose": target}
    assert status["clamp_reason"] is None
    # Velocity only, toward the target at speed_m_s, climbing at vertical_speed_m_s; no yaw.
    assert flight.commands[-1] == (
        "velocity", pytest.approx(-1.0 / math.sqrt(5.0)), pytest.approx(2.0 / math.sqrt(5.0)), pytest.approx(-0.5)
    )
    flight._telemetry.pose = Pose(-1.0, 2.0, 2.5)
    flight._telemetry.pose_updated_monotonic = time.monotonic()
    executor.tick()
    assert executor.get_task_status(task_id)["reason"] == "succeeded"

    far = executor.submit_skill("move", {"forward_m": 10.0})["task_id"]
    executor.tick()
    status = executor.get_task_status(far)
    assert status["requested_target"]["pose"]["east_m"] == pytest.approx(12.0)
    assert status["executed_target"]["pose"]["east_m"] == pytest.approx(12.0)
    assert status["clamp_reason"] is None
    executor.close()


def test_move_that_would_end_at_or_below_home_fails_when_it_starts():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True, mode="GUIDED")
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task_id = executor.submit_skill("move", {"up_m": -2.0})["task_id"]

    executor.tick()
    status = executor.get_task_status(task_id)
    assert status["status"] == "failed"
    assert "above home" in status["error"]
    # It never moved: the failed start leaves a zero-velocity hold.
    assert [command for command in flight.commands if command[0] == "velocity"] == [("velocity", 0.0, 0.0, 0.0)]
    executor.close()


def test_look_tilts_the_gimbal_and_the_pitch_survives_take_off_and_a_released_landing_camera():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    fixed = HorusExecutor(flight, _settings(default_gimbal_pitch_deg=-45.0), autostart=False)
    with pytest.raises(RuntimeError, match="no gimbal is configured"):
        fixed.look(-20.0)
    fixed.close()

    gimbal = FakeGimbal()
    executor = HorusExecutor(flight, _settings(default_gimbal_pitch_deg=-45.0), gimbal=gimbal, autostart=False)
    with pytest.raises(RuntimeError, match="fixed"):
        executor.look(-20.0)  # the gimbal has reported no attitude yet: treated as a fixed camera
    assert gimbal.points == []

    gimbal.report(-45.0)
    assert executor.look(-20.0)["clamp_reason"] is None
    assert gimbal.points[-1] == (-20.0, 0.0)
    clamped = executor.look(-120.0)
    assert (clamped["pitch_deg"], bool(clamped["clamp_reason"])) == (-90.0, True)
    assert gimbal.points[-1] == (-90.0, 0.0)
    executor.look(-20.0)
    gimbal.report(-20.0, 0.5, 1.0)
    state = executor.get_state()
    assert state["camera_pitch_deg"] == -20.0
    assert (state["gimbal"]["pitch_deg"], state["gimbal"]["roll_deg"], state["gimbal"]["yaw_deg"]) == (-20.0, 0.5, 1.0)
    assert state["gimbal"]["age_s"] >= 0.0

    executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)
    executor.tick()  # take-off aims the camera: at the commanded pitch, not the configured default
    assert gimbal.points[-1] == (-20.0, 0.0)

    # Precision landing turned the camera straight down: look waits, and a hold gives it back.
    executor._precision.gimbal_requested_at = time.monotonic()
    with pytest.raises(RuntimeError, match="precision landing"):
        executor.look(0.0)
    executor.hold(source="mcp")
    assert gimbal.points[-1] == (-20.0, 0.0)
    assert executor.look(0.0)["pitch_deg"] == 0.0
    executor.close()


def test_look_pans_the_gimbal_clamps_yaw_and_keeps_the_direction_for_later_skills():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    gimbal = FakeGimbal(pitch_deg=-45.0)
    executor = HorusExecutor(flight, _settings(default_gimbal_pitch_deg=-45.0), gimbal=gimbal, autostart=False)
    assert executor.get_state()["camera_yaw_deg"] == 0.0

    result = executor.look(-30.0, 60.0)
    assert (result["pitch_deg"], result["yaw_deg"], result["clamp_reason"]) == (-30.0, 60.0, None)
    assert gimbal.points[-1] == (-30.0, 60.0)
    state = executor.get_state()
    assert (state["camera_pitch_deg"], state["camera_yaw_deg"]) == (-30.0, 60.0)

    # Beyond the gimbal's yaw travel (gimbal.yaw_limit_deg, 160 by default) it stops at the limit.
    clamped = executor.look(-30.0, -200.0)
    assert (clamped["requested_yaw_deg"], clamped["yaw_deg"]) == (-200.0, -160.0)
    assert "yaw limited to ±160" in clamped["clamp_reason"] and "pitch" not in clamped["clamp_reason"]
    assert gimbal.points[-1] == (-30.0, -160.0)
    with pytest.raises(ValueError, match="yaw_deg"):
        executor.look(-30.0, float("nan"))
    assert gimbal.points[-1] == (-30.0, -160.0)

    executor.look(-30.0, 75.0)
    executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)
    executor.tick()  # take-off re-aims the camera: the commanded pitch and yaw, not the defaults
    assert gimbal.points[-1] == (-30.0, 75.0)
    # A pitch-only call (as before pan existed) faces the nose again.
    assert executor.look(-10.0)["yaw_deg"] == 0.0
    assert gimbal.points[-1] == (-10.0, 0.0)
    executor.close()



def test_executor_publishes_task_finished_once_on_terminal_transition():
    event_bus = EventBus()
    executor = HorusExecutor(FakeFlight(), _settings(), event_bus=event_bus, autostart=False)
    task_id = executor.submit_skill(
        "goto", {"pose": {"north_m": 0.0, "east_m": 0.0, "up_m": 2.0}}
    )["task_id"]

    executor.tick()
    executor.tick()
    events = asyncio.run(event_bus.wait(after_id=0, timeout_s=0.0))

    finished = [event for event in events if event["kind"] == "task_finished"]
    assert len(finished) == 1
    assert finished[0]["data"] == {
        "task_id": task_id,
        "skill": "goto",
        "status": "succeeded",
        "error": None,
    }
    executor.close()


def test_pose_staleness_limit_is_configurable():
    flight = FakeFlight()
    flight._telemetry.pose_updated_monotonic = time.monotonic() - 0.8
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task = executor.submit_skill("goto", {"pose": {"north_m": 1.0, "east_m": 0.0, "up_m": 2.0}}, ttl_s=5.0)
    executor.tick()
    assert executor.get_task_status(task["task_id"])["status"] == "running"
    executor.close()

    strict_flight = FakeFlight()
    strict_flight._telemetry.pose_updated_monotonic = time.monotonic() - 0.8
    strict = HorusExecutor(strict_flight, _settings(telemetry_max_age_s=0.5), autostart=False)
    task_id = strict.submit_skill(
        "goto", {"pose": {"north_m": 1.0, "east_m": 0.0, "up_m": 2.0}}, ttl_s=5.0
    )["task_id"]
    strict.tick()
    status = strict.get_task_status(task_id)
    assert status["status"] == "failed"
    assert "stale" in status["error"]
    strict.close()


def test_heartbeat_flapping_is_not_an_event_but_a_persistent_loss_is(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    flight = FakeFlight()
    beats = {"ok": True}
    flight.heartbeat_ok = lambda: beats["ok"]
    event_bus = EventBus()
    executor = HorusExecutor(flight, _settings(), event_bus=event_bus, autostart=False)

    def tick_at(offset, ok):
        clock[0] = 1000.0 + offset
        beats["ok"] = ok
        executor.tick()

    tick_at(0.0, True)
    for step in range(10):  # a slow sim: heartbeat drops ~2 s out of every 4 s
        tick_at(1.0 + 4 * step, False)
        tick_at(3.0 + 4 * step, True)
    assert not [e for e in asyncio.run(event_bus.wait(0, 0.0)) if e["kind"].startswith("link_")]

    tick_at(50.0, False)
    tick_at(56.0, False)

    kinds = [e["kind"] for e in asyncio.run(event_bus.wait(0, 0.0))]
    assert kinds.count("link_lost") == 1
    executor.close()


_EMERGENCY_COMMAND = {"hold": ("hold",), "rth": ("rth",), "land": ("land",)}
_EMERGENCY_STATE = {"hold": ExecutorState.HOLDING, "rth": ExecutorState.RTH, "land": ExecutorState.HOLDING}


def _finished_events(event_bus, task_id):
    events = asyncio.run(event_bus.wait(after_id=0, timeout_s=0.0))
    return [e for e in events if e["kind"] == "task_finished" and e["data"]["task_id"] == task_id]


def _assert_emergency_stays_in_control(executor, flight, action):
    """The emergency command is the vehicle's last; later ticks never re-enter GUIDED or move."""
    assert flight.commands[-1] == _EMERGENCY_COMMAND[action]
    flight._telemetry.armed = True  # still airborne, so RTH/LAND stays in progress
    mark = len(flight.commands)
    for _ in range(5):
        executor.tick()
    later = flight.commands[mark:]
    assert all(command == ("velocity", 0.0, 0.0, 0.0) for command in later), later
    if action != "hold":
        assert later == []
    assert executor.state == _EMERGENCY_STATE[action]


class _BlockingFlight(FakeFlight):
    """Blocks the controller thread inside one autopilot send, as a slow MAVLink write would."""

    def __init__(self, blocked: str, pose: Pose = Pose(0.0, 0.0, 2.0)):
        super().__init__(pose)
        self.blocked = blocked
        self.entered = threading.Event()
        self.release = threading.Event()

    def _block(self, name):
        if name == self.blocked and threading.current_thread().name == "tick" and not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(5.0)

    def set_velocity_ned(self, north, east, down, yaw_deg=None):
        self._block("velocity")
        super().set_velocity_ned(north, east, down, yaw_deg)

    def hold(self):
        self._block("hold")
        super().hold()


@pytest.mark.parametrize("action", ["hold", "rth", "land"])
@pytest.mark.parametrize(
    ("scenario", "blocked", "statuses"),
    [
        ("motion", "velocity", {"cancelled"}),
        ("arrived", "hold", {"succeeded", "cancelled"}),
        ("cancel", "hold", {"cancelled"}),
        ("ttl", "hold", {"expired", "cancelled"}),
    ],
)
def test_emergency_arriving_during_a_step_command_is_sent_after_it(action, scenario, blocked, statuses):
    flight = _BlockingFlight(blocked)
    event_bus = EventBus()
    executor = HorusExecutor(flight, _settings(), event_bus=event_bus, autostart=False)
    target = {"north_m": 0.0 if scenario == "arrived" else 2.0, "east_m": 0.0, "up_m": 2.0}
    task_id = executor.submit_skill(
        "goto", {"pose": target}, ttl_s=0.2 if scenario == "ttl" else 5.0
    )["task_id"]
    if scenario in ("cancel", "ttl"):
        executor.tick()  # running, commanding velocity
        if scenario == "cancel":
            executor.cancel(task_id)
        else:
            time.sleep(0.25)
    worker = threading.Thread(target=executor.tick, name="tick")
    worker.start()
    assert flight.entered.wait(2.0)

    operator = threading.Thread(target=getattr(executor, action), name="operator")
    operator.start()
    flight.release.set()
    worker.join(2.0)
    operator.join(2.0)
    assert not worker.is_alive() and not operator.is_alive()

    # Whichever finished first, the in-flight step command precedes the emergency, never follows.
    status = executor.get_task_status(task_id)["status"]
    assert status in statuses
    assert len(_finished_events(event_bus, task_id)) == 1
    _assert_emergency_stays_in_control(executor, flight, action)
    executor.close()


def _last_velocity(flight):
    velocities = [command for command in flight.commands if command[0] == "velocity"]
    assert velocities, "no velocity command was issued"
    return velocities[-1][1:]


def _cruise_settings(**overrides):
    values = {"cruise_speed_m_s": 2.0, "cruise_min_altitude_m": 3.0}
    values.update(overrides)
    return _settings(**values)


@pytest.mark.parametrize(("start_up", "target_up", "settings"), [
    pytest.param(2.5, 3.5, _cruise_settings(), id="starts-below-the-gate"),
    pytest.param(3.5, 2.0, _cruise_settings(), id="target-below-the-gate"),
    pytest.param(3.5, 3.5, _settings(), id="cruise-disabled"),
])
def test_goto_keeps_speed_m_s_unless_start_and_target_are_above_the_cruise_gate(
    start_up, target_up, settings
):
    flight = FakeFlight(Pose(0.0, 0.0, start_up))
    executor = HorusExecutor(flight, settings, autostart=False)
    executor.submit_skill("goto", {"pose": {"north_m": 5.0, "east_m": 0.0, "up_m": target_up}}, ttl_s=5.0)
    executor.tick()
    executor.tick()
    assert _last_velocity(flight)[:2] == (1.0, 0.0)
    executor.close()


def test_goto_cruises_above_the_gate_and_drops_back_to_speed_m_s_when_descending_below_it():
    flight = FakeFlight(Pose(0.0, 0.0, 3.5))
    executor = HorusExecutor(flight, _cruise_settings(), autostart=False)
    executor.submit_skill("goto", {"pose": {"north_m": 5.0, "east_m": 0.0, "up_m": 3.5}}, ttl_s=5.0)
    executor.tick()
    executor.tick()
    assert _last_velocity(flight)[:2] == (2.0, 0.0)
    flight._telemetry.pose = Pose(1.0, 0.0, 2.5)
    executor.tick()
    # Under the obstacle band the proven max speed applies again.
    assert _last_velocity(flight)[0] == pytest.approx(1.0)
    executor.close()
