from __future__ import annotations

import math
import time

import pytest

from horus.config import ExecutorSettings
from horus.executor import ExecutorState, HorusExecutor
from horus.flight import Pose


class FakeTelemetry:
    def __init__(self, pose: Pose):
        self.pose = pose
        self.observed_at = "2026-09-24T00:00:00+00:00"
        self.received_monotonic = time.monotonic()
        self.pose_updated_monotonic = self.received_monotonic
        self.heading_deg = 0.0
        self.armed = False
        self.mode = "STABILIZE"

    def as_dict(self):
        return {"pose": self.pose.as_dict(), "observed_at": self.observed_at}


class FakeFlight:
    def __init__(self, pose: Pose = Pose(0.0, 0.0, 2.0)):
        self._telemetry = FakeTelemetry(pose)
        self.commands = []
        self.follow_yaw_commands = False

    def telemetry(self):
        return self._telemetry

    def set_guided(self):
        self.commands.append(("guided",))

    def arm(self):
        self.commands.append(("arm",))

    def takeoff(self, altitude):
        self.commands.append(("takeoff", altitude))

    def set_velocity_ned(self, north, east, down, yaw_deg=None):
        self.commands.append(("velocity", north, east, down) if yaw_deg is None else ("velocity", north, east, down, yaw_deg))
        if yaw_deg is not None and self.follow_yaw_commands:
            self._telemetry.heading_deg = yaw_deg

    def hold(self):
        self.commands.append(("hold",))

    def rth(self):
        self.commands.append(("rth",))

    def land(self):
        self.commands.append(("land",))

    def set_yaw(self, heading):
        self.commands.append(("yaw", heading))


def _settings(**overrides):
    values = {
        "control_hz": 15.0,
        "command_ttl_s": 1.0,
        "max_speed_m_s": 1.0,
        "max_vertical_speed_m_s": 0.5,
        "max_distance_m": 5.0,
        "min_altitude_m": 0.5,
        "max_altitude_m": 4.0,
        "geofence_radius_m": 3.0,
        "arrival_tolerance_m": 0.25,
        "follow_distance_m": 2.0,
        "target_width_m": None,
    }
    values.update(overrides)
    return ExecutorSettings(**values)


def _scan_area():
    return {
        "north_min_m": 0.0,
        "north_max_m": 0.5,
        "east_min_m": 0.0,
        "east_max_m": 0.5,
        "altitude_m": 2.0,
        "spacing_m": 0.5,
    }


def _set_flight_pose(flight, pose):
    flight._telemetry.pose = pose
    flight._telemetry.pose_updated_monotonic = time.monotonic()


def _armed_scan_flight(pose=Pose(0.0, 0.0, 2.0), *, follow_yaw=True):
    flight = FakeFlight(pose)
    flight._telemetry.armed = True
    flight.follow_yaw_commands = follow_yaw
    return flight


def test_scan_reaches_every_waypoint_without_skips():
    flight = _armed_scan_flight()
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task = executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=300.0)
    task_id = task["task_id"]

    assert task["skipped_waypoints"] == []
    executor.tick()
    for waypoint in task["executed_target"][1:]:
        _set_flight_pose(
            flight,
            Pose(waypoint["north_m"], waypoint["east_m"], waypoint["up_m"]),
        )
        executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "succeeded"
    assert status["skipped_waypoints"] == []
    assert status["result"]["skipped_waypoints"] == []
    executor.close()


def test_scan_skips_stuck_waypoint_and_continues(monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    flight = _armed_scan_flight()
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task = executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=300.0)
    task_id = task["task_id"]
    waypoints = task["executed_target"]

    executor.tick()  # Reach waypoint zero and begin moving toward waypoint one.
    clock[0] += 1.0
    _set_flight_pose(flight, Pose(0.0, 0.0, 2.0))
    executor.tick()  # Start the no-progress timer for waypoint one.
    assert executor.get_task_status(task_id)["skipped_waypoints"] == []

    clock[0] += 15.0
    _set_flight_pose(flight, Pose(0.0, 0.0, 2.0))
    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "running"
    assert len(status["skipped_waypoints"]) == 1
    skipped = status["skipped_waypoints"][0]
    assert skipped["index"] == 1
    assert skipped["waypoint"] == waypoints[1]
    assert skipped["vehicle_pose"] == {"north_m": 0.0, "east_m": 0.0, "up_m": 2.0}
    assert skipped["distance_m"] == 0.5
    assert skipped["stuck_s"] == 15.0
    assert "no progress" in skipped["reason"]
    assert "obstacle avoidance" in skipped["reason"]
    assert task_id in caplog.text
    assert "waypoint 1" in caplog.text
    assert any(command == ("velocity", 0.0, 0.0, 0.0) for command in flight.commands)

    for waypoint in waypoints[2:]:
        _set_flight_pose(
            flight,
            Pose(waypoint["north_m"], waypoint["east_m"], waypoint["up_m"]),
        )
        executor.tick()

    final = executor.get_task_status(task_id)
    assert final["status"] == "succeeded"
    assert final["result"]["skipped_waypoints"] == final["skipped_waypoints"]
    executor.close()


def test_scan_slow_progress_resets_timeout_without_skipping(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    flight = _armed_scan_flight()
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task = executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=300.0)
    task_id = task["task_id"]
    executor.tick()

    for _ in range(100):
        internal_task = executor._tasks[task_id]
        if internal_task.status != "running":
            break
        target = internal_task._waypoints[internal_task._waypoint_index]
        current = flight._telemetry.pose
        north_delta = target.north_m - current.north_m
        east_delta = target.east_m - current.east_m
        horizontal = math.hypot(north_delta, east_delta)
        step = min(0.1, horizontal)
        if horizontal > 0.0:
            pose = Pose(
                current.north_m + north_delta * step / horizontal,
                current.east_m + east_delta * step / horizontal,
                target.up_m,
            )
        else:
            pose = target
        clock[0] += 5.0
        _set_flight_pose(flight, pose)
        executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "succeeded"
    assert status["skipped_waypoints"] == []
    assert clock[0] - 1000.0 > 15.0
    executor.close()


def test_scan_fails_early_when_every_waypoint_is_unreachable(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    flight = _armed_scan_flight(Pose(0.0, -1.0, 2.0))
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task = executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=600.0)
    task_id = task["task_id"]

    for _ in range(2 * len(task["executed_target"]) + 1):
        executor.tick()
        if executor.get_task_status(task_id)["status"] != "running":
            break
        clock[0] += 15.0
        _set_flight_pose(flight, flight._telemetry.pose)

    status = executor.get_task_status(task_id)
    assert status["status"] == "failed"
    assert "all 4 waypoints" in status["error"]
    assert "no progress toward any waypoint" in status["error"]
    assert "obstacle avoidance" in status["error"]
    assert len(status["skipped_waypoints"]) == 4
    assert status["result"]["skipped_waypoints"] == status["skipped_waypoints"]
    assert clock[0] - 1000.0 < 600.0
    executor.close()


def test_scan_fails_disarmed_and_preserves_earlier_skips(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    flight = _armed_scan_flight()
    executor = HorusExecutor(flight, _settings(scan_progress_timeout_s=2.0), autostart=False)
    task_id = executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=300.0)["task_id"]

    executor.tick()
    clock[0] += 0.1
    _set_flight_pose(flight, flight._telemetry.pose)
    executor.tick()
    clock[0] += 2.0
    _set_flight_pose(flight, flight._telemetry.pose)
    executor.tick()
    running = executor.get_task_status(task_id)
    assert running["status"] == "running"
    assert len(running["skipped_waypoints"]) == 1

    flight._telemetry.armed = False
    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "failed"
    assert "disarmed" in status["error"]
    assert "waypoint 3/4" in status["error"]
    assert "index 2" in status["error"]
    assert "north=0.000, east=0.000, up=2.000" in status["error"]
    assert "crash" in status["error"] and "never took off" in status["error"]
    assert len(status["skipped_waypoints"]) == 1
    assert status["result"]["skipped_waypoints"] == status["skipped_waypoints"]
    executor.close()


def test_scan_turns_before_translation_and_keeps_yaw_on_translation():
    flight = _armed_scan_flight(follow_yaw=False)
    executor = HorusExecutor(
        flight, _settings(scan_progress_timeout_s=2.0), autostart=False
    )
    task = executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=300.0)

    executor.tick()
    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0, 90.0)
    assert not any(
        command[0] == "velocity" and any(abs(value) > 0.0 for value in command[1:4])
        for command in flight.commands
    )

    flight._telemetry.heading_deg = 86.0
    executor.tick()
    translation = flight.commands[-1]
    assert translation[0] == "velocity"
    assert translation[1:4] == (0.0, 0.5, 0.0)
    assert translation[4] == 90.0

    _set_flight_pose(flight, Pose(0.0, 0.2, 2.0))
    executor.tick()
    assert flight.commands[-1] == ("velocity", 0.0, 0.3, 0.0, 90.0)
    executor.close()


def test_scan_without_usable_heading_translates_with_leg_bearing():
    flight = _armed_scan_flight()
    flight._telemetry.heading_deg = None
    executor = HorusExecutor(flight, _settings(), autostart=False)
    executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=300.0)

    executor.tick()

    assert flight.commands[-1] == ("velocity", 0.0, 0.5, 0.0, 90.0)
    executor.close()


def test_scan_turn_time_does_not_count_as_no_progress(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    flight = _armed_scan_flight(follow_yaw=False)
    executor = HorusExecutor(
        flight, _settings(scan_progress_timeout_s=2.0), autostart=False
    )
    task_id = executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=300.0)["task_id"]

    executor.tick()
    clock[0] += 1.5
    _set_flight_pose(flight, flight._telemetry.pose)
    executor.tick()
    assert executor.get_task_status(task_id)["skipped_waypoints"] == []

    flight._telemetry.heading_deg = 90.0
    executor.tick()
    clock[0] += 1.5
    _set_flight_pose(flight, flight._telemetry.pose)
    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "running"
    assert status["skipped_waypoints"] == []
    executor.close()


def test_scan_turn_timeout_skips_waypoint_with_turn_reason(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    flight = _armed_scan_flight(follow_yaw=False)
    executor = HorusExecutor(
        flight, _settings(scan_progress_timeout_s=2.0), autostart=False
    )
    task_id = executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=300.0)["task_id"]

    executor.tick()
    clock[0] += 2.0
    _set_flight_pose(flight, flight._telemetry.pose)
    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "running"
    assert len(status["skipped_waypoints"]) == 1
    assert status["skipped_waypoints"][0]["index"] == 1
    assert "could not turn toward waypoint" in status["skipped_waypoints"][0]["reason"]
    executor.close()


def test_scan_turn_skips_count_toward_all_waypoints_failure(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    flight = _armed_scan_flight(Pose(0.0, -1.0, 2.0), follow_yaw=False)
    executor = HorusExecutor(
        flight, _settings(scan_progress_timeout_s=2.0), autostart=False
    )
    task_id = executor.submit_skill("scan", {"area": _scan_area()}, ttl_s=300.0)["task_id"]

    for _ in range(2 * len(executor.get_task_status(task_id)["executed_target"]) + 1):
        executor.tick()
        if executor.get_task_status(task_id)["status"] != "running":
            break
        clock[0] += 2.0
        _set_flight_pose(flight, flight._telemetry.pose)

    status = executor.get_task_status(task_id)
    assert status["status"] == "failed"
    assert len(status["skipped_waypoints"]) == 4
    assert all("could not turn toward waypoint" in skip["reason"] for skip in status["skipped_waypoints"])
    assert "skipped all 4 waypoints" in status["error"]
    assert status["result"]["skipped_waypoints"] == status["skipped_waypoints"]
    executor.close()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("scan_progress_threshold_m", 0.0),
        ("scan_progress_threshold_m", -0.1),
        ("scan_progress_timeout_s", 0.0),
        ("scan_progress_timeout_s", -0.1),
    ],
)
def test_scan_progress_settings_must_be_positive(setting, value):
    with pytest.raises(ValueError, match="scan progress"):
        HorusExecutor(FakeFlight(), _settings(**{setting: value}), autostart=False)


def test_goto_completes_only_at_measured_target_and_enters_guided_hold():
    flight = FakeFlight()
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


def test_goto_only_passes_yaw_when_heading_was_requested():
    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(), autostart=False)
    executor.submit_skill(
        "goto", {"pose": {"north_m": 1.0, "east_m": 0.0, "up_m": 2.0}}
    )
    executor.tick()
    assert flight.commands[-1] == ("velocity", 1.0, 0.0, 0.0)
    assert not any(len(command) == 5 for command in flight.commands if command[0] == "velocity")
    executor.close()

    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(), autostart=False)
    executor.submit_skill(
        "goto",
        {"pose": {"north_m": 0.0, "east_m": 0.0, "up_m": 2.0}, "heading_deg": 45.0},
    )
    executor.tick()
    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0, 45.0)
    executor.close()


def test_take_off_waits_for_guided_mode_and_arming_before_native_takeoff():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    executor = HorusExecutor(flight, _settings(), autostart=False)
    task = executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)

    executor.tick()
    task_id = task["task_id"]
    assert executor.get_task_status(task_id)["status"] == "running"
    assert flight.commands == [("guided",)]

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


def test_clamped_goto_reports_executed_target_and_geofence_reasons():
    executor = HorusExecutor(FakeFlight(), _settings(), autostart=False)
    task = executor.submit_skill(
        "goto", {"pose": {"north_m": 10, "east_m": 0, "up_m": 9}}, ttl_s=5
    )

    assert task["requested_target"] == {"north_m": 10.0, "east_m": 0.0, "up_m": 9.0}
    assert task["executed_target"] == {"north_m": 3.0, "east_m": 0.0, "up_m": 4.0}
    assert "travel distance clamped" in task["clamp_reason"]
    assert "geofence radius" in task["clamp_reason"]
    assert "altitude clamped" in task["clamp_reason"]
    executor.close()


def _indoor_settings():
    return _settings(
        max_distance_m=20.0,
        geofence_radius_m=8.0,
        geofence_polygon_ned=(
            (-4.0, -5.0),
            (-4.0, 5.0),
            (4.0, 5.0),
            (4.0, 1.4),
            (8.0, 1.4),
            (8.0, -1.4),
            (4.0, -1.4),
            (4.0, -5.0),
        ),
        geofence_margin_m=0.25,
    )


def test_polygon_geofence_projects_outside_target_and_reports_clamp():
    executor = HorusExecutor(FakeFlight(), _indoor_settings(), autostart=False)
    task = executor.submit_skill(
        "goto", {"pose": {"north_m": 0, "east_m": 6, "up_m": 2}}, ttl_s=5
    )

    assert task["requested_target"] == {"north_m": 0.0, "east_m": 6.0, "up_m": 2.0}
    assert task["executed_target"] == {"north_m": 0.0, "east_m": 4.75, "up_m": 2.0}
    assert "polygon geofence" in task["clamp_reason"]
    executor.close()


def test_polygon_geofence_projects_target_from_concave_corner():
    executor = HorusExecutor(FakeFlight(), _indoor_settings(), autostart=False)
    task = executor.submit_skill(
        "goto", {"pose": {"north_m": 5, "east_m": 2, "up_m": 2}}, ttl_s=5
    )

    assert task["executed_target"] == {"north_m": 5.0, "east_m": 1.15, "up_m": 2.0}
    assert "polygon geofence" in task["clamp_reason"]
    executor.close()


def test_polygon_geofence_keeps_corridor_target_inside_margin():
    executor = HorusExecutor(FakeFlight(), _indoor_settings(), autostart=False)
    task = executor.submit_skill(
        "goto", {"pose": {"north_m": 7, "east_m": 0, "up_m": 2}}, ttl_s=5
    )

    assert task["executed_target"] == {"north_m": 7.0, "east_m": 0.0, "up_m": 2.0}
    assert task["clamp_reason"] is None
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


class _LowConfidenceTarget:
    def validate_id(self, candidate_id):
        return candidate_id == 7

    def get_candidate(self, candidate_id):
        return {
            "bearing_rad": 0.0,
            "distance_m": 3.0,
            "confidence": 0.95,
            "target_match_confidence": 0.2,
        }


def test_approach_fails_safe_when_target_profile_confidence_is_below_threshold():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(),
        perception=_LowConfidenceTarget(),
        autostart=False,
    )
    task = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0})

    executor.tick()

    result = executor.get_task_status(task["task_id"])
    assert result["status"] == "failed"
    assert "profile-match confidence" in result["error"]
    assert not any(command[0] == "velocity" and command[1:] != (0.0, 0.0, 0.0) for command in flight.commands)
    executor.close()

class _ReidentifyingTarget:
    def __init__(self):
        self.current = {
            "id": 7,
            "bbox": [10, 10, 40, 60],
            "bearing_rad": 0.0,
            "distance_m": 3.0,
            "confidence": 0.95,
            "attributes": {"shirt_colour": "blue"},
        }

    def get_candidate(self, candidate_id):
        if self.current is not None and self.current["id"] == candidate_id:
            return self.current
        return None

    def validate_id(self, candidate_id):
        return self.get_candidate(candidate_id) is not None

    def reidentify_candidate(self, reference_id):
        if self.current is None:
            return None
        return {
            "reference_id": reference_id,
            "candidate": self.current,
            "similarity": 0.91,
            "similarity_threshold": 0.72,
        }


def _follow_through_target_loss(matched_id):
    target = _ReidentifyingTarget()
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(),
        perception=target,
        autostart=False,
    )
    task = executor.submit_skill("follow", {"id": 7}, ttl_s=5.0)
    executor.tick()

    target.current = None
    executor.tick()
    lost = executor.get_task_status(task["task_id"])
    assert lost["status"] == "running"
    assert not lost["tracking"]["reacquired"]
    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0)

    target.current = {
        "id": matched_id,
        "bbox": [10, 10, 40, 60],
        "bearing_rad": 0.0,
        "distance_m": 3.0,
        "confidence": 0.95,
        "attributes": {"shirt_colour": "blue"},
    }
    executor.tick()
    status = executor.get_task_status(task["task_id"])
    executor.close()
    return status


def test_follow_revalidates_same_id_after_temporary_occlusion():
    status = _follow_through_target_loss(7)

    tracking = status["tracking"]
    assert tracking["reacquired"]
    assert tracking["embedding_validated"]
    assert tracking["id_preserved"]
    assert not tracking["id_rematched"]
    assert tracking["embedding_similarity"] >= tracking["embedding_similarity_threshold"]
    assert tracking["time_to_reacquire_s"] >= 0.0


def test_follow_rematches_appearance_to_new_tracker_id_after_occlusion():
    status = _follow_through_target_loss(9)

    tracking = status["tracking"]
    assert tracking["reacquired"]
    assert tracking["embedding_validated"]
    assert not tracking["id_preserved"]
    assert tracking["id_rematched"]
    assert tracking["matched_id"] == 9



class _ValidatedSnapshotTarget:
    def get_candidate(self, candidate_id):
        return {
            "id": candidate_id,
            "bearing_rad": -0.5,
            "distance_m": 3.0,
            "confidence": 0.95,
        }

    def validate_candidate(self, candidate_id):
        return {
            "id": candidate_id,
            "bearing_rad": 0.25,
            "distance_m": None,
            "confidence": 0.95,
        }

    def validate_id(self, candidate_id):
        return False


def test_follow_uses_atomic_validated_candidate_snapshot():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(),
        perception=_ValidatedSnapshotTarget(),
        autostart=False,
    )
    task = executor.submit_skill("follow", {"id": 7}, ttl_s=5.0)

    executor.tick()

    assert executor.get_task_status(task["task_id"])["status"] == "running"
    # Yaw travels in the velocity setpoint; a separate yaw command would be overridden.
    assert any(
        command[0] == "velocity" and len(command) == 5
        and math.isclose(command[4], math.degrees(0.25), abs_tol=1e-9)
        for command in flight.commands
    )
    executor.close()


class _TransientlyInvalidTarget:
    def __init__(self):
        self.current = {
            "id": 7,
            "bearing_rad": 0.0,
            "distance_m": 3.0,
            "confidence": 0.95,
            "attributes": {"shirt_colour": "blue"},
        }
        self.validation_calls = 0

    def get_candidate(self, candidate_id):
        return self.current if self.current["id"] == candidate_id else None

    def validate_id(self, candidate_id):
        self.validation_calls += 1
        return self.validation_calls > 1

    def reidentify_candidate(self, reference_id):
        if self.current["id"] == reference_id:
            return None
        return {
            "reference_id": reference_id,
            "candidate": self.current,
            "similarity": 0.91,
            "similarity_threshold": 0.72,
        }


def test_follow_retries_reidentification_after_transient_validation_failure():
    target = _TransientlyInvalidTarget()
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(),
        perception=target,
        autostart=False,
    )
    task = executor.submit_skill("follow", {"id": 7}, ttl_s=5.0)

    executor.tick()

    lost = executor.get_task_status(task["task_id"])
    assert lost["status"] == "running"
    assert not lost["tracking"]["reacquired"]
    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0)

    target.current = {
        "id": 9,
        "bearing_rad": 0.0,
        "distance_m": 3.0,
        "confidence": 0.95,
        "attributes": {"shirt_colour": "blue"},
    }
    executor.tick()

    status = executor.get_task_status(task["task_id"])
    assert status["status"] == "running"
    assert status["tracking"]["reacquired"]
    assert status["tracking"]["embedding_validated"]
    assert status["tracking"]["id_rematched"]
    assert status["tracking"]["matched_id"] == 9
    assert status["tracking"]["embedding_similarity"] == 0.91
    executor.close()


class _BoxOnlyTarget:
    def validate_id(self, candidate_id):
        return candidate_id == 7

    def get_candidate(self, candidate_id):
        if candidate_id != 7:
            return None
        return {
            "bbox": [420, 100, 460, 300],
            "frame_width": 640,
            "confidence": 0.95,
        }


def test_follow_derives_bearing_from_candidate_box():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0),
        perception=_BoxOnlyTarget(),
        autostart=False,
    )
    task = executor.submit_skill("follow", {"id": 7}, ttl_s=5.0)

    executor.tick()

    expected_bearing_deg = math.degrees(math.atan2(120.0, 320.0))
    assert executor.get_task_status(task["task_id"])["status"] == "running"
    assert any(
        command[0] == "velocity" and len(command) == 5
        and math.isclose(command[4], expected_bearing_deg, abs_tol=1e-9)
        for command in flight.commands
    )
    executor.close()


class _PersonBoxTarget:
    """A 1.7 m person whose box height puts them ~4 m ahead, slightly right of center."""

    def validate_candidate(self, candidate_id):
        # 90 deg HFOV on 640 px -> focal 320 px; 1.7 m * 320 / 136 px = 4.0 m.
        return {"id": candidate_id, "bbox": [330, 100, 370, 236], "frame_width": 640, "confidence": 0.9}


def test_follow_closes_range_from_person_box_height_while_facing_target():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=_PersonBoxTarget(),
        autostart=False,
    )
    executor.submit_skill("follow", {"id": 3}, ttl_s=5.0)
    executor.tick()
    command = next(c for c in flight.commands if c[0] == "velocity" and len(c) == 5)
    _, north, east, _, yaw_deg = command
    bearing = math.atan2(30.0, 320.0)  # box centre 350 px vs 320 px image centre
    assert math.isclose(yaw_deg, math.degrees(bearing), abs_tol=1e-9)
    # 4.0 m range vs 2.0 m follow distance: move toward the target along its bearing.
    assert north > 0.0 and east > 0.0
    assert math.isclose(math.atan2(east, north), bearing, abs_tol=1e-9)
    executor.close()


def test_follow_reports_longest_occlusion_in_autopilot_time():
    target = _ReidentifyingTarget()
    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(), perception=target, autostart=False)
    task_id = executor.submit_skill("follow", {"id": 7}, ttl_s=5.0)["task_id"]
    visible = dict(target.current)

    def occlude(boot_ms_lost, boot_ms_back, matched_id):
        flight._telemetry.time_boot_ms = boot_ms_lost
        target.current = None
        executor.tick()
        flight._telemetry.time_boot_ms = boot_ms_back
        target.current = dict(visible, id=matched_id)
        executor.tick()

    executor.tick()
    occlude(10_000, 10_500, 7)  # brief 0.5 s loss (e.g. passing behind another person)
    occlude(20_000, 22_100, 9)  # the designed ~2 s screen occlusion, re-matched to a new ID
    occlude(30_000, 30_300, 9)
    tracking = executor.get_task_status(task_id)["tracking"]
    executor.close()

    assert [round(e["time_to_reacquire_s"], 3) for e in tracking["reacquisitions"]] == [0.5, 2.1, 0.3]
    assert math.isclose(tracking["time_to_reacquire_s"], 2.1)
    assert tracking["time_basis"] == "autopilot_time_boot"
    assert tracking["matched_id"] == 9 and tracking["id_rematched"]


def test_take_off_levels_a_gimbal_left_pointing_down():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    gimbal = []
    flight.set_gimbal_pitch_yaw = lambda pitch, yaw: gimbal.append((pitch, yaw))
    executor = HorusExecutor(flight, _settings(), autostart=False)
    executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)
    executor.tick()
    assert gimbal == [(0.0, 0.0)]
    executor.close()


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
