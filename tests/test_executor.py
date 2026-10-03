from __future__ import annotations

import asyncio
import math
import threading
import time
from types import SimpleNamespace

import pytest

from horus.config import ExecutorSettings
from horus.events import EventBus
from horus.executor import ExecutorState, HorusExecutor, flight_envelope
from horus.flight import Pose


class FakeTelemetry:
    def __init__(self, pose: Pose):
        self.pose = pose
        self.observed_at = "2026-09-24T00:00:00+00:00"
        self.received_monotonic = time.monotonic()
        self.pose_updated_monotonic = self.received_monotonic
        self.heading_deg = 0.0
        self.heading_updated_monotonic = self.received_monotonic
        self.armed = False
        self.mode = "STABILIZE"

    def as_dict(self):
        return {"pose": self.pose.as_dict(), "observed_at": self.observed_at}


class FakeFlight:
    def __init__(self, pose: Pose = Pose(0.0, 0.0, 2.0)):
        self._telemetry = FakeTelemetry(pose)
        self.commands = []

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


def test_take_off_uses_default_gimbal_pitch():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    gimbal = []
    flight.set_gimbal_pitch_yaw = lambda pitch, yaw: gimbal.append((pitch, yaw))
    executor = HorusExecutor(flight, _settings(), autostart=False)
    executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)
    executor.tick()
    assert gimbal == [(0.0, 0.0)]
    executor.close()


def test_take_off_uses_configured_outdoor_gimbal_pitch():
    flight = FakeFlight(Pose(0.0, 0.0, 0.2))
    gimbal = []
    flight.set_gimbal_pitch_yaw = lambda pitch, yaw: gimbal.append((pitch, yaw))
    settings = _settings(default_gimbal_pitch_deg=-45.0)
    executor = HorusExecutor(flight, settings, autostart=False)
    executor.submit_skill("take_off", {"altitude_m": 2.0}, ttl_s=5.0)
    executor.tick()
    assert gimbal == [(-45.0, 0.0)]
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


def test_follow_publishes_target_lost_and_reacquired_only_on_transitions():
    event_bus = EventBus()
    target = _ReidentifyingTarget()
    executor = HorusExecutor(
        FakeFlight(), _settings(), perception=target, event_bus=event_bus, autostart=False
    )
    task_id = executor.submit_skill("follow", {"id": 7}, ttl_s=5.0)["task_id"]
    executor.tick()

    target.current = None
    executor.tick()
    executor.tick()
    target.current = {
        "id": 9,
        "bbox": [10, 10, 40, 60],
        "bearing_rad": 0.0,
        "distance_m": 3.0,
        "confidence": 0.95,
    }
    executor.tick()
    events = asyncio.run(event_bus.wait(after_id=0, timeout_s=0.0))
    lost = [event for event in events if event["kind"] == "target_lost"]
    reacquired = [event for event in events if event["kind"] == "target_reacquired"]

    assert len(lost) == 1
    assert lost[0]["data"] == {"task_id": task_id, "reference_id": 7}
    assert len(reacquired) == 1
    assert reacquired[0]["data"] == {
        "task_id": task_id,
        "reference_id": 7,
        "matched_id": 9,
        "similarity": 0.91,
    }
    executor.close()


class _GenericTarget:
    def __init__(self, bearing_rad: float = 0.0):
        self.bearing_rad = bearing_rad

    def validate_candidate(self, candidate_id):
        return {
            "id": candidate_id,
            "bbox": [320, 50, 360, 150],
            "frame_width": 640,
            "class_name": "chair",
            "class_id": None,
            "bearing_rad": self.bearing_rad,
            "confidence": 0.95,
        }


def _front_tof(distance_m: float = 3.0):
    return SimpleNamespace(
        orientation=0,
        distance_m=distance_m,
        min_distance_m=0.1,
        max_distance_m=6.0,
        updated_monotonic=time.monotonic(),
    )


def test_approach_ranges_non_person_from_forward_tof_when_centered():
    flight = FakeFlight()
    flight._telemetry.proximity = (_front_tof(),)
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0),
        perception=_GenericTarget(),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    motion = next(command for command in flight.commands if command[0] == "velocity")
    assert tracking["range_source"] == "forward_tof"
    assert tracking["range_m"] == 3.24
    assert motion[1] > 0.0 and motion[2] == 0.0
    executor.close()


def test_forward_tof_is_skipped_when_the_beam_misses_the_target_outdoors():
    # Outdoor geometry: -45 deg gimbal, 6.15 m up, the target ~5 m ahead and far below the beam.
    # A fresh forward reading (a trunk on the flight plane) must not be used for the target.
    box = _logged_approach_box(horizontal_m=4.97, altitude_m=6.15)
    flight = FakeFlight(Pose(0.0, 0.0, 6.15))
    flight._telemetry.proximity = (_front_tof(3.0),)
    executor = HorusExecutor(
        flight,
        _settings(
            camera_horizontal_fov_deg=84.0,
            target_height_m=1.9,
            default_gimbal_pitch_deg=-45.0,
            max_distance_m=20.0,
        ),
        perception=_LoggedGeometryTarget(box),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 2.0}, ttl_s=5.0)["task_id"]

    for _ in range(4):
        executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "ground_plane"
    assert tracking["range_m"] > 5.0  # not the 3.24 m beam reading
    executor.close()


def test_forward_tof_is_used_for_a_level_target_indoors():
    # Indoors the gimbal is level and the vehicle flies near person height, so the single-ray
    # beam and the target ray coincide and the measured range stays the first choice.
    flight = FakeFlight(Pose(0.0, 0.0, 2.0))
    flight._telemetry.proximity = (_front_tof(3.0),)
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=84.0, target_height_m=1.9, default_gimbal_pitch_deg=0.0),
        perception=_LoggedGeometryTarget([300, 200, 330, 280], class_name="chair", class_id=None),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=5.0)["task_id"]

    for _ in range(4):
        executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "forward_tof"
    assert tracking["range_m"] == 3.24
    executor.close()


def test_approach_yaws_before_translating_when_target_is_off_center():
    flight = FakeFlight()
    flight._telemetry.proximity = (_front_tof(),)
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0),
        perception=_GenericTarget(math.radians(20.0)),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7}, ttl_s=5.0)["task_id"]

    executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    command = next(command for command in flight.commands if command[0] == "velocity")
    assert tracking["range_m"] is None
    assert tracking["range_source"] is None
    assert command[1:4] == (0.0, 0.0, 0.0)
    assert math.isclose(command[4], 20.0, abs_tol=1e-9)
    executor.close()


def test_approach_uses_explicit_target_size_for_non_person_range():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0),
        perception=_GenericTarget(),
        autostart=False,
    )
    task_id = executor.submit_skill(
        "approach", {"id": 7, "dist_m": 1.0, "target_size_m": 1.0}, ttl_s=5.0
    )["task_id"]

    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["tracking"]["range_source"] == "target_size"
    assert math.isclose(status["tracking"]["range_m"], 3.2)
    assert status["executed_target"]["target_size_m"] == 1.0
    executor.close()


_INTERMITTENT_CANDIDATE = {
    "id": 7,
    "bbox": [10, 10, 40, 60],
    "bearing_rad": 0.0,
    "distance_m": 3.0,
    "confidence": 0.95,
}


class _IntermittentTarget:
    """Perception double: a validated target that can vanish and return under a new ID."""

    def __init__(self):
        self.candidate = dict(_INTERMITTENT_CANDIDATE)
        self.reject = False  # present, but failing appearance revalidation
        self.failure = None  # reason reported for the rejected candidate

    def validate_candidate(self, candidate_id):
        if self.reject or self.candidate is None or self.candidate["id"] != candidate_id:
            return None
        return dict(self.candidate)

    def validate_candidate_failure(self, candidate_id):
        return self.failure

    def reidentify_candidate(self, reference_id):
        if self.reject or self.candidate is None:
            return None
        return {
            "reference_id": reference_id,
            "candidate": dict(self.candidate),
            "similarity": 0.91,
            "similarity_threshold": 0.72,
        }


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


@pytest.mark.parametrize(
    ("skill", "arguments"),
    [("approach", {"id": 7, "dist_m": 1.0}), ("orbit", {"id": 7, "radius_m": 2.0})],
)
def test_target_skills_hold_and_reidentify_across_a_missing_candidate(skill, arguments):
    target = _IntermittentTarget()
    flight = FakeFlight()
    event_bus = EventBus()
    executor = HorusExecutor(
        flight, _settings(), perception=target, event_bus=event_bus, autostart=False
    )
    task_id = executor.submit_skill(skill, arguments, ttl_s=5.0)["task_id"]

    executor.tick()
    assert executor.get_task_status(task_id)["status"] == "running"
    assert any(command[0] == "velocity" and (command[1], command[2]) != (0.0, 0.0) for command in flight.commands)

    # A single missing candidate frame must not end the task: hold at zero velocity and retry.
    target.candidate = None
    target.failure = "not_visible"
    executor.tick()
    executor.tick()
    dropped = executor.get_task_status(task_id)
    assert dropped["status"] == "running"
    assert dropped["tracking"]["reacquired"] is False
    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0)

    # The target returns under a new tracker ID; appearance re-identification resumes the skill.
    target.candidate = {**_INTERMITTENT_CANDIDATE, "id": 9, "bbox": [12, 10, 42, 60]}
    target.failure = None
    executor.tick()

    status = executor.get_task_status(task_id)
    tracking = status["tracking"]
    assert status["status"] == "running"
    assert tracking["reacquired"] and tracking["embedding_validated"]
    assert tracking["id_rematched"] and tracking["matched_id"] == 9
    assert tracking["reference_id"] == 7
    assert tracking["embedding_similarity"] == 0.91
    events = asyncio.run(event_bus.wait(after_id=0, timeout_s=0.0))
    lost = [event for event in events if event["kind"] == "target_lost"]
    reacquired = [event for event in events if event["kind"] == "target_reacquired"]
    assert len(lost) == 1
    assert lost[0]["data"] == {"task_id": task_id, "reference_id": 7}
    assert "not visible in the current frame" in lost[0]["summary"]
    assert len(reacquired) == 1
    assert reacquired[0]["data"] == {
        "task_id": task_id,
        "reference_id": 7,
        "matched_id": 9,
        "similarity": 0.91,
    }
    executor.close()


def test_target_skill_reports_an_appearance_mismatch_as_its_own_cause():
    target = _IntermittentTarget()
    target.reject = True
    target.failure = "appearance_mismatch"
    flight = FakeFlight()
    event_bus = EventBus()
    executor = HorusExecutor(
        flight, _settings(), perception=target, event_bus=event_bus, autostart=False
    )
    executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=5.0)

    executor.tick()

    assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0)
    events = asyncio.run(event_bus.wait(after_id=0, timeout_s=0.0))
    lost = [event for event in events if event["kind"] == "target_lost"]
    assert len(lost) == 1
    assert "reference appearance mismatch" in lost[0]["summary"]
    executor.close()


def test_approach_completes_at_the_standoff_and_does_not_chase():
    # One-shot by design: the skill closes to the commanded standoff and then holds; it never
    # keeps station on a target that walks away afterwards (the campaign must assert the minimum
    # range reached plus task completion, not the final range).
    target = _IntermittentTarget()
    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(), perception=target, autostart=False)
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 5.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "succeeded"
    assert flight.commands[-1] == ("hold",)

    # The target keeps moving after the standoff: no new motion commands are issued.
    target.candidate = {**_INTERMITTENT_CANDIDATE, "id": 9, "bbox": [200, 10, 230, 60]}
    executor.tick()

    assert executor.get_task_status(task_id)["status"] == "succeeded"
    assert not [
        command for command in flight.commands
        if command[0] == "velocity" and (command[1], command[2]) != (0.0, 0.0)
    ]
    executor.close()


class _OrbitTarget:
    """Perception double: a validated person 8 m out and 20 deg right of the heading."""

    def __init__(self, bearing_rad: float = math.radians(20.0), distance_m: float = 8.0):
        self.bearing_rad = bearing_rad
        self.distance_m = distance_m

    def validate_candidate(self, candidate_id):
        return {
            "id": candidate_id,
            "bbox": [400, 100, 440, 220],
            "frame_width": 640,
            "class_name": "person",
            "class_id": 0,
            "bearing_rad": self.bearing_rad,
            "distance_m": self.distance_m,
            "confidence": 0.9,
        }


def test_orbit_faces_the_target_while_circling():
    # Koko orbit trials: the vehicle translated without yawing, so the target left the camera's
    # field of view, was lost, and the orbit held one circle point (a -0.5 deg heading sweep).
    # The orbit must keep the person framed like follow and approach do.
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=84.0),
        perception=_OrbitTarget(),
        autostart=False,
    )
    task_id = executor.submit_skill("orbit", {"id": 7, "radius_m": 6.0}, ttl_s=5.0)["task_id"]

    for _ in range(4):
        executor.tick()

    motion = [
        command for command in flight.commands
        if command[0] == "velocity" and (command[1], command[2]) != (0.0, 0.0)
    ]
    assert motion
    north, east, _down, yaw = motion[-1][1:5]
    assert math.isclose(yaw, 20.0, abs_tol=1e-6)
    # Radial component toward the target (8 m out vs a 6 m radius) plus a tangential component.
    bearing = math.radians(20.0)
    radial = north * math.cos(bearing) + east * math.sin(bearing)
    tangent = -north * math.sin(bearing) + east * math.cos(bearing)
    assert radial > 0.0
    assert abs(tangent) > 0.2
    assert executor.get_task_status(task_id)["status"] == "running"
    executor.close()


def test_target_skill_hold_is_bounded_by_the_task_ttl_when_the_target_never_returns():
    target = _IntermittentTarget()
    flight = FakeFlight()
    executor = HorusExecutor(flight, _settings(), perception=target, autostart=False)
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=0.05)["task_id"]

    target.candidate = None
    target.failure = "not_visible"
    executor.tick()
    executor.tick()
    assert executor.get_task_status(task_id)["status"] == "running"

    time.sleep(0.06)
    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "expired"
    assert flight.commands[-1] == ("hold",)
    executor.close()


class _MislabeledPersonTarget:
    """A person the detector currently calls a kite; 90 deg HFOV puts it ~4 m ahead."""

    def __init__(self, *, shirt_colour: str | None = None, person_history: bool = False):
        self.shirt_colour = shirt_colour
        self.person_history = person_history

    def validate_candidate(self, candidate_id):
        candidate = {
            "id": candidate_id,
            "bbox": [330, 100, 370, 236],
            "frame_width": 640,
            "class_name": "kite",
            "class_id": 33,
            "confidence": 0.9,
        }
        if self.person_history:
            candidate["person_evidence"] = True
        if self.shirt_colour is not None:
            candidate["attributes"] = {"shirt_colour": self.shirt_colour}
        return candidate


@pytest.mark.parametrize("evidence", ["person_history", "shirt_colour"])
def test_approach_uses_the_configured_target_height_for_an_evidenced_mislabeled_person(evidence):
    kwargs = {"person_history": True} if evidence == "person_history" else {"shirt_colour": "red"}
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=_MislabeledPersonTarget(**kwargs),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    status = executor.get_task_status(task_id)
    tracking = status["tracking"]
    assert status["status"] == "running"
    assert tracking["range_source"] == "person_height"
    assert math.isclose(tracking["range_m"], 4.0, rel_tol=1e-6)
    # The configured height is used even though the current label disagrees; that is reported.
    assert "kite" in tracking["range_source_note"]
    assert "person evidence" in tracking["range_source_note"]
    motion = next(
        command for command in flight.commands
        if command[0] == "velocity" and (command[1], command[2]) != (0.0, 0.0)
    )
    assert motion[1] > 0.0  # 4.0 m range vs 1.0 m desired: close the range along its bearing.
    executor.close()


def test_explicit_target_size_outranks_the_configured_target_height():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=_MislabeledPersonTarget(person_history=True),
        autostart=False,
    )
    task_id = executor.submit_skill(
        "approach", {"id": 7, "dist_m": 1.0, "target_size_m": 1.0}, ttl_s=5.0
    )["task_id"]

    executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "target_size"
    assert math.isclose(tracking["range_m"], 1.0 * 320.0 / 136.0, rel_tol=1e-9)
    assert "range_source_note" not in tracking
    executor.close()


def test_measured_forward_tof_outranks_the_configured_person_height():
    flight = FakeFlight()
    flight._telemetry.proximity = (_front_tof(),)
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=_MislabeledPersonTarget(person_history=True),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "forward_tof"
    assert tracking["range_m"] == 3.24
    assert "range_source_note" not in tracking
    executor.close()


def test_approach_refuses_a_mislabeled_target_without_person_evidence():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=_MislabeledPersonTarget(),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "failed"
    # The refusal names the label so the commander can pass the object's real height.
    assert "calibrated target range" in status["error"]
    assert "'kite'" in status["error"]
    assert "target_size_m" in status["error"]
    assert not [
        command for command in flight.commands
        if command[0] == "velocity" and (command[1], command[2]) != (0.0, 0.0)
    ]
    executor.close()


class _GrounderPersonSeed:
    """An open-vocabulary person seed; the detector has not labelled it person."""

    def __init__(self, *, detector_evidence: bool = False):
        self.detector_evidence = detector_evidence

    def validate_candidate(self, candidate_id):
        candidate = {
            "id": candidate_id,
            "bbox": [330, 100, 370, 236],
            "frame_width": 640,
            "class_name": "person" if self.detector_evidence else "person lying on the ground",
            "class_id": None,
            "source": "open_vocab",
            "confidence": 0.42,
        }
        if self.detector_evidence:
            candidate["person_evidence"] = True
        return candidate


def test_grounder_seed_ranges_from_the_ground_plane_without_a_declared_size():
    # The round-2 rule still holds for the *person-height* source (see the test below), but the
    # ground-plane estimate is a geometric measurement of where the object is, so it does not
    # need to know the target is a person: a grounder-only person seed with the camera geometry
    # available ranges normally instead of refusing.
    box = _logged_approach_box(horizontal_m=4.97, altitude_m=6.15)
    flight = FakeFlight(Pose(0.0, 0.0, 6.15))
    executor = HorusExecutor(
        flight,
        _settings(
            camera_horizontal_fov_deg=84.0,
            target_height_m=1.9,
            default_gimbal_pitch_deg=-45.0,
            max_distance_m=20.0,
        ),
        perception=_LoggedGeometryTarget(box, class_name="person lying on the ground", class_id=None),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 2.0}, ttl_s=5.0)["task_id"]

    for _ in range(4):
        executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "ground_plane"
    executor.close()


def test_approach_refuses_person_height_for_a_grounder_only_person_seed():
    # A text-grounding seed is a guess: ranged from the 1.7 m person height, a false seed would
    # fly closer than the commanded standoff, and a prone body's box is not 1.7 m tall anyway.
    # (This fixture carries no frame size, so the ground-plane source is unavailable and the
    # person-height refusal is what is asserted; see the test above for the geometric source.)
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=_GrounderPersonSeed(),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "failed"
    assert "calibrated target range" in status["error"]
    assert "person lying on the ground" in status["error"]
    assert "target_size_m" in status["error"]
    assert not [
        command for command in flight.commands
        if command[0] == "velocity" and (command[1], command[2]) != (0.0, 0.0)
    ]
    executor.close()


def test_approach_uses_the_declared_target_size_for_a_grounder_person_seed():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=_GrounderPersonSeed(),
        autostart=False,
    )
    task_id = executor.submit_skill(
        "approach", {"id": 7, "dist_m": 1.0, "target_size_m": 0.4}, ttl_s=5.0
    )["task_id"]

    executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "target_size"
    assert math.isclose(tracking["range_m"], 0.4 * 320.0 / 136.0, rel_tol=1e-6)
    executor.close()


def test_approach_uses_person_height_once_the_detector_labels_a_grounder_seed():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=_GrounderPersonSeed(detector_evidence=True),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "person_height"
    assert math.isclose(tracking["range_m"], 4.0, rel_tol=1e-6)
    assert "person evidence" in tracking["range_source_note"]
    executor.close()


class _LoggedGeometryTarget:
    """A person box from the verify-forest-15178 approach geometry, with frame size for ranging."""

    def __init__(self, box, *, frame=(640, 480), class_name="person", class_id=0):
        self.box = list(box)
        self.frame = frame
        self.class_name = class_name
        self.class_id = class_id

    def validate_candidate(self, candidate_id):
        return {
            "id": candidate_id,
            "bbox": list(self.box),
            "frame_width": self.frame[0],
            "frame_height": self.frame[1],
            "class_name": self.class_name,
            "class_id": self.class_id,
            "confidence": 0.8,
        }


def _logged_approach_box(*, horizontal_m, altitude_m, pitch_deg=-45.0, person_height_m=1.9,
                         width=640, height=480, hfov_deg=84.0):
    """Project a standing person (feet on the ground, head at ``person_height_m``)."""
    f = width / (2.0 * math.tan(math.radians(hfov_deg) / 2.0))
    theta = math.radians(-pitch_deg)  # optical-axis depression, positive downwards

    def row(drop_m: float) -> float:
        angle = math.atan2(drop_m, horizontal_m) - theta
        return height / 2.0 + f * math.tan(angle)

    return [280.0, row(altitude_m - person_height_m), 310.0, row(altitude_m)]


def test_approach_ranges_from_the_ground_plane_at_the_logged_forest_geometry():
    # verify-forest-15178: altitude 6.15 m, camera pitch -45 deg, the target 4.97 m ahead.
    # The configured person height read 12.36 m there (2.5x high, foreshortened box); the
    # ground-plane ray must land on the true camera-to-torso distance so arrival is detected.
    box = _logged_approach_box(horizontal_m=4.97, altitude_m=6.15)
    flight = FakeFlight(Pose(0.0, 0.0, 6.15))
    executor = HorusExecutor(
        flight,
        _settings(
            camera_horizontal_fov_deg=84.0,
            target_height_m=1.9,
            default_gimbal_pitch_deg=-45.0,
            max_distance_m=20.0,
        ),
        perception=_LoggedGeometryTarget(box),
        autostart=False,
    )
    true_slant = math.hypot(4.97, 6.15 - 1.12)
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": true_slant + 0.4}, ttl_s=5.0)["task_id"]

    for _ in range(4):  # guided/arm precede the first ranging tick
        executor.tick()

    status = executor.get_task_status(task_id)
    tracking = status["tracking"]
    assert tracking["range_source"] == "ground_plane"
    assert abs(tracking["range_m"] - true_slant) / true_slant < 0.1
    horizontal = float(tracking["range_source_note"].split(":")[-1].split(" m horizontal")[0])
    assert abs(horizontal - 4.97) / 4.97 < 0.05
    # Arrival is now detectable: the estimate is inside the standoff+tolerance on the first tick.
    assert status["status"] == "succeeded"
    executor.close()


def test_ground_plane_range_holds_close_in_where_the_person_height_estimate_fails():
    # The same camera nearly overhead: 1.5 m horizontal, 6.15 m up. The true camera-to-torso is
    # 5.27 m; the configured person height would read 14.4 m here (a 2.7x over-estimate), so the
    # approach could never detect arrival.
    box = _logged_approach_box(horizontal_m=1.5, altitude_m=6.15)
    flight = FakeFlight(Pose(0.0, 0.0, 6.15))
    executor = HorusExecutor(
        flight,
        _settings(
            camera_horizontal_fov_deg=84.0,
            target_height_m=1.9,
            default_gimbal_pitch_deg=-45.0,
            max_distance_m=20.0,
        ),
        perception=_LoggedGeometryTarget(box),
        autostart=False,
    )
    true_slant = math.hypot(1.5, 6.15 - 1.12)
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 5.5}, ttl_s=5.0)["task_id"]

    for _ in range(4):
        executor.tick()

    status = executor.get_task_status(task_id)
    tracking = status["tracking"]
    assert tracking["range_source"] == "ground_plane"
    assert abs(tracking["range_m"] - true_slant) / true_slant < 0.05
    assert status["status"] == "succeeded"
    executor.close()


def test_orbit_uses_the_ground_plane_range_at_the_logged_geometry():
    box = _logged_approach_box(horizontal_m=8.0, altitude_m=9.5)
    flight = FakeFlight(Pose(0.0, 0.0, 9.5))
    executor = HorusExecutor(
        flight,
        _settings(
            camera_horizontal_fov_deg=84.0,
            target_height_m=1.9,
            default_gimbal_pitch_deg=-45.0,
            max_distance_m=20.0,
        ),
        perception=_LoggedGeometryTarget(box),
        autostart=False,
    )
    task_id = executor.submit_skill("orbit", {"id": 7, "radius_m": 6.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "ground_plane"
    true_slant = math.hypot(8.0, 9.5 - 1.12)
    assert abs(tracking["range_m"] - true_slant) / true_slant < 0.1
    executor.close()


def test_ground_plane_bearing_compensates_for_the_camera_pitch():
    # A 10 deg image bearing is ~15 deg on the ground at a -45 deg pitch (sim/README's own
    # example): the translation and yaw must use the ground bearing, not the image one.
    box = _logged_approach_box(horizontal_m=4.97, altitude_m=6.15)
    box[0], box[2] = 368.0, 397.0  # centre column 10 deg right of the optical axis
    flight = FakeFlight(Pose(0.0, 0.0, 6.15))
    executor = HorusExecutor(
        flight,
        _settings(
            camera_horizontal_fov_deg=84.0,
            target_height_m=1.9,
            default_gimbal_pitch_deg=-45.0,
            max_distance_m=20.0,
        ),
        perception=_LoggedGeometryTarget(box),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 2.0}, ttl_s=5.0)["task_id"]

    for _ in range(4):
        executor.tick()

    motion = [
        command for command in flight.commands
        if command[0] == "velocity" and (command[1], command[2]) != (0.0, 0.0)
    ]
    assert motion
    north, east = motion[-1][1], motion[-1][2]
    ground_bearing_deg = math.degrees(math.atan2(east, north))
    assert 13.0 < ground_bearing_deg < 18.0  # image bearing is 10 deg; ground is ~15.6 deg
    assert executor.get_task_status(task_id)["status"] == "running"
    executor.close()


def test_ground_plane_range_is_refused_for_a_shallow_ray():
    # A level camera looking at a person 40 m out: the ray depression is ~3 deg, where a small
    # pitch error dominates, so the ground-plane estimate is refused and the person height stays
    # the fallback (which is accurate at a level camera).
    box = _logged_approach_box(horizontal_m=40.0, altitude_m=2.0, pitch_deg=0.0)
    flight = FakeFlight(Pose(0.0, 0.0, 2.0))
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=84.0, target_height_m=1.9, default_gimbal_pitch_deg=0.0),
        perception=_LoggedGeometryTarget(box),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 5.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "person_height"
    assert abs(tracking["range_m"] - 40.0) / 40.0 < 0.3
    executor.close()


def test_fresh_downward_rangefinder_height_overrides_the_pose_altitude():
    from types import SimpleNamespace

    box = _logged_approach_box(horizontal_m=4.97, altitude_m=6.15)
    flight = FakeFlight(Pose(0.0, 0.0, 6.15))
    flight._telemetry.ground_distance = SimpleNamespace(
        orientation=25, distance_m=9.0, min_distance_m=0.1, max_distance_m=40.0,
        updated_monotonic=time.monotonic(),
    )
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=84.0, target_height_m=1.9, default_gimbal_pitch_deg=-45.0),
        perception=_LoggedGeometryTarget(box),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 5.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "ground_plane"
    assert "9.00 m above ground" in tracking["range_source_note"]
    executor.close()


def test_saturated_downward_rangefinder_falls_back_to_the_pose_altitude():
    # Koko runs koko-verify-forest/urban-15178: the payload's downward lidar saturates at its 4 m
    # maximum, and that saturation was used as the height, so every ground-plane estimate read
    # short (the forest approach "succeeded" 5.39 m from a target 37.7 m away). A reading at the
    # bound must fall back to the pose altitude.
    from types import SimpleNamespace

    box = _logged_approach_box(horizontal_m=20.0, altitude_m=9.5)
    flight = FakeFlight(Pose(0.0, 0.0, 9.5))
    flight._telemetry.ground_distance = SimpleNamespace(
        orientation=25, distance_m=4.0, min_distance_m=0.1, max_distance_m=4.0,
        updated_monotonic=time.monotonic(),
    )
    executor = HorusExecutor(
        flight,
        _settings(
            camera_horizontal_fov_deg=84.0,
            target_height_m=1.9,
            default_gimbal_pitch_deg=-45.0,
            max_distance_m=40.0,
        ),
        perception=_LoggedGeometryTarget(box),
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 2.0}, ttl_s=5.0)["task_id"]

    for _ in range(4):
        executor.tick()

    tracking = executor.get_task_status(task_id)["tracking"]
    assert tracking["range_source"] == "ground_plane"
    assert "9.50 m above ground" in tracking["range_source_note"]
    true_slant = math.hypot(20.0, 9.5 - 1.12)
    assert abs(tracking["range_m"] - true_slant) / true_slant < 0.1
    executor.close()


def test_camera_pitch_is_earth_frame_and_ignores_the_vehicle_pitch():
    # ArduPilot reports mount/gimbal pitch in the earth frame (MOUNT_ORIENTATION.pitch is
    # "Pitch in global frame"; GIMBAL_DEVICE_ATTITUDE_STATUS carries an earth-frame quaternion)
    # and FlightLink.set_gimbal_pitch_yaw documents its pitch as earth-frame, so a 10 deg
    # nose-down cruise attitude must not be added: that would shift the ground-plane range.
    from types import SimpleNamespace

    box = _logged_approach_box(horizontal_m=10.0, altitude_m=6.0)
    true_slant = math.hypot(10.0, 6.0 - 1.12)
    settings = _settings(
        camera_horizontal_fov_deg=84.0,
        target_height_m=1.9,
        default_gimbal_pitch_deg=-45.0,
        max_distance_m=40.0,
    )

    # Fresh mount telemetry: the reported earth-frame pitch is used as-is.
    flight = FakeFlight(Pose(0.0, 0.0, 6.0))
    flight._telemetry.mount_status = SimpleNamespace(pitch_deg=-45.0, updated_monotonic=time.monotonic())
    flight._telemetry.vehicle_pitch_deg = -10.0
    executor = HorusExecutor(
        flight, settings, perception=_LoggedGeometryTarget(box), autostart=False
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 2.0}, ttl_s=5.0)["task_id"]
    for _ in range(4):
        executor.tick()
    tracking = executor.get_task_status(task_id)["tracking"]
    assert "pitch -45.0 deg" in tracking["range_source_note"]
    assert abs(tracking["range_m"] - true_slant) / true_slant < 0.1
    executor.close()

    # Configured fallback: also earth-frame, so the vehicle pitch is still not added.
    fallback_flight = FakeFlight(Pose(0.0, 0.0, 6.0))
    fallback_flight._telemetry.vehicle_pitch_deg = -10.0
    fallback = HorusExecutor(
        fallback_flight, settings, perception=_LoggedGeometryTarget(box), autostart=False
    )
    fallback_id = fallback.submit_skill("approach", {"id": 7, "dist_m": 2.0}, ttl_s=5.0)["task_id"]
    for _ in range(4):
        fallback.tick()
    fallback_tracking = fallback.get_task_status(fallback_id)["tracking"]
    assert "pitch -45.0 deg" in fallback_tracking["range_source_note"]
    assert abs(fallback_tracking["range_m"] - true_slant) / true_slant < 0.1
    fallback.close()


def test_approach_still_refuses_when_no_target_height_is_configured():
    flight = FakeFlight()
    executor = HorusExecutor(
        flight, _settings(camera_horizontal_fov_deg=90.0), perception=_MislabeledPersonTarget(), autostart=False
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": 1.0}, ttl_s=5.0)["task_id"]

    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "failed"
    assert "calibrated target range" in status["error"]
    assert not [
        command for command in flight.commands
        if command[0] == "velocity" and (command[1], command[2]) != (0.0, 0.0)
    ]
    executor.close()


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


class _GatedPersonTarget:
    """A person ~4 m ahead whose identity revalidation blocks until the test releases it."""

    def __init__(self, confidence: float = 0.9):
        self.confidence = confidence
        self.entered = threading.Event()
        self.release = threading.Event()

    def validate_candidate(self, candidate_id):
        self.entered.set()
        assert self.release.wait(5.0)
        return {
            "id": candidate_id,
            "bbox": [330, 100, 370, 236],
            "frame_width": 640,
            "class_name": "person",
            "confidence": self.confidence,
        }


@pytest.mark.parametrize("action", ["hold", "rth", "land"])
@pytest.mark.parametrize(
    ("outcome", "dist_m", "confidence"),
    [("moving", 1.0, 0.9), ("arrived", 4.0, 0.9), ("failed", 1.0, 0.05)],
)
def test_emergency_during_candidate_revalidation_is_not_undone_by_the_step(
    action, outcome, dist_m, confidence
):
    flight = FakeFlight()
    target = _GatedPersonTarget(confidence)
    event_bus = EventBus()
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=target,
        event_bus=event_bus,
        autostart=False,
    )
    task_id = executor.submit_skill("approach", {"id": 7, "dist_m": dist_m}, ttl_s=5.0)["task_id"]
    worker = threading.Thread(target=executor.tick)
    worker.start()
    assert target.entered.wait(2.0)

    getattr(executor, action)()
    replacement = executor.submit_skill("goto", {"pose": {"north_m": 1.0, "east_m": 0.0, "up_m": 2.0}})
    target.release.set()
    worker.join(2.0)
    assert not worker.is_alive()

    # The emergency cancelled the task mid-step; neither success, failure nor motion follows it.
    assert flight.commands[-1] == _EMERGENCY_COMMAND[action]
    status = executor.get_task_status(task_id)
    assert status["status"] == "cancelled"
    assert status["error"] == f"preempted by emergency {action}"
    assert status["result"]["status"] == "cancelled"
    assert [e["data"]["status"] for e in _finished_events(event_bus, task_id)] == ["cancelled"]
    assert executor.get_task_status(replacement["task_id"])["status"] == "queued"
    executor.cancel(replacement["task_id"])
    _assert_emergency_stays_in_control(executor, flight, action)
    executor.close()


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


@pytest.mark.parametrize("skill", ["approach", "follow", "orbit"])
@pytest.mark.parametrize("stale", ["pose", "heading"])
def test_target_skills_refuse_motion_on_stale_pose_or_heading(skill, stale):
    flight = FakeFlight()
    # Other telemetry (heartbeat, mode) stays current; only this field stopped updating.
    setattr(flight._telemetry, f"{stale}_updated_monotonic", time.monotonic() - 2.0)
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0, target_height_m=1.7),
        perception=_PersonBoxTarget(),
        autostart=False,
    )
    arguments = {"id": 7, "radius_m": 2.0} if skill == "orbit" else {"id": 7, "dist_m": 1.0}
    task_id = executor.submit_skill(skill, arguments, ttl_s=5.0)["task_id"]

    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "failed"
    assert status["error"].startswith(f"vehicle {stale} is stale")
    assert not [command for command in flight.commands if command[0] == "velocity"]
    assert flight.commands[-1] == ("hold",)
    executor.close()


@pytest.mark.parametrize(
    ("target_size_m", "range_source"), [(None, None), (1.0, "target_size")]
)
def test_stalled_forward_tof_range_is_discarded_but_visual_range_still_works(target_size_m, range_source):
    flight = FakeFlight()
    stalled = _front_tof()
    stalled.updated_monotonic = time.monotonic() - 2.0
    flight._telemetry.proximity = (stalled,)
    executor = HorusExecutor(
        flight,
        _settings(camera_horizontal_fov_deg=90.0),
        perception=_GenericTarget(),
        autostart=False,
    )
    arguments = {"id": 7, "dist_m": 1.0}
    if target_size_m is not None:
        arguments["target_size_m"] = target_size_m
    task_id = executor.submit_skill("approach", arguments, ttl_s=5.0)["task_id"]

    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["tracking"]["range_source"] == range_source
    moving = [c for c in flight.commands if c[0] == "velocity" and (c[1], c[2]) != (0.0, 0.0)]
    if range_source is None:
        assert status["status"] == "failed"
        assert "calibrated target range" in status["error"]
        assert not moving
    else:
        assert status["status"] == "running"
        assert moving and moving[0][1] > 0.0
    executor.close()


class _SearchScene:
    """Perception stand-in: a processed-frame counter, its candidates and validation results."""

    def __init__(self):
        self.frame_id = 1
        self.rows = []
        self.validated = set()
        self.reidentified = None
        self.frame_age_s = 0.0
        self.validated_frame_offset = 0

    def next_frame(self, rows=(), validated=()):
        self.frame_id += 1
        self.rows, self.validated = list(rows), set(validated)

    def latest_observation(self):
        return SimpleNamespace(
            frame_id=self.frame_id,
            timestamp=time.monotonic() - self.frame_age_s,
            pose={"time_boot_ms": 1000 * self.frame_id, "time_boot_age_s": 0.05},
            candidates=tuple(self.rows),
        )

    def list_candidates(self):
        return self.rows

    def validate_candidate(self, candidate_id):
        if candidate_id not in self.validated:
            return None
        row = next((row for row in self.rows if row["id"] == candidate_id), None)
        # Validation reads the tracker's current state, which may already belong to a newer frame.
        return None if row is None else {**row, "frame_id": self.frame_id + self.validated_frame_offset}

    def reidentify_candidate(self, reference_id):
        return self.reidentified


_SEARCH_AREA = {"north_min_m": 0.0, "north_max_m": 2.0, "east_min_m": -1.0, "east_max_m": 1.0, "altitude_m": 2.0}


def _search_executor(scene, flight=None):
    event_bus = EventBus()
    flight = flight or FakeFlight()
    executor = HorusExecutor(flight, _settings(), perception=scene, event_bus=event_bus, autostart=False)
    return executor, flight, event_bus


def test_search_scans_until_a_validated_label_match_then_holds_and_reports_the_id():
    scene = _SearchScene()
    executor, flight, event_bus = _search_executor(scene)
    task_id = executor.submit_skill("search", {"area": _SEARCH_AREA, "class_name": "person"}, ttl_s=10.0)["task_id"]

    executor.tick()  # enter GUIDED
    executor.tick()  # nothing seen: keep flying the raster
    executor.tick()  # the same frame is not re-checked
    scene.next_frame(
        [{"id": 5, "class_name": "Person", "bbox": [0, 0, 20, 40]}, {"id": 6, "class_name": "car", "bbox": [0, 0, 90, 90]}],
        validated={6},
    )
    executor.tick()  # a person-labelled box that fails appearance validation is not a match
    status = executor.get_task_status(task_id)
    assert status["status"] == "running" and status["tracking"]["frames_checked"] == 2
    assert any(c[0] == "velocity" and (c[1], c[2]) != (0.0, 0.0) for c in flight.commands)

    scene.next_frame(scene.rows, validated={5, 6})
    mark = len(flight.commands)
    executor.tick()

    status = executor.get_task_status(task_id)
    assert status["status"] == "succeeded"
    assert status["tracking"]["found_id"] == 5 and status["tracking"]["found"] is True
    assert flight.commands[mark:] == [("velocity", 0.0, 0.0, 0.0), ("hold",)]
    events = asyncio.run(event_bus.wait(after_id=0, timeout_s=0.0))
    [found] = [event for event in events if event["kind"] == "target_found"]
    assert found["data"]["id"] == 5 and found["data"]["matching_ids"] == [5] and found["data"]["ambiguous"] is False
    assert found["data"]["matches"] == [{"id": 5, "untrusted_detector_label": '"Person"', "bbox": [0.0, 0.0, 20.0, 40.0]}]
    assert (found["data"]["frame_time_boot_ms"], found["data"]["frame_time_boot_age_s"]) == (3000, 0.05)
    executor.close()


def test_search_fails_after_covering_the_area_without_a_match():
    scene = _SearchScene()
    executor, flight, _ = _search_executor(scene)
    task = executor.submit_skill("search", {"area": _SEARCH_AREA, "class_name": "person"}, ttl_s=10.0)
    for waypoint in task["executed_target"]:
        flight._telemetry.pose = Pose(waypoint["north_m"], waypoint["east_m"], waypoint["up_m"])
        flight._telemetry.pose_updated_monotonic = time.monotonic()
        scene.next_frame([{"id": 1, "class_name": "car", "bbox": [0, 0, 5, 5]}], validated={1})
        for _ in range(3):
            executor.tick()
    status = executor.get_task_status(task["task_id"])
    assert status["status"] == "failed"
    assert status["error"] == "search covered the area without a validated match"
    assert status["tracking"]["found"] is False
    executor.close()


def test_search_with_an_attribute_cue_ignores_partial_matches_and_reports_visible_ambiguity():
    scene = _SearchScene()
    executor, _, event_bus = _search_executor(scene)
    cue = {"class_name": "person", "attribute": "shirt_colour", "equals": "red"}
    task_id = executor.submit_skill("search", {"area": _SEARCH_AREA, **cue}, ttl_s=10.0)["task_id"]
    executor.tick()
    person = lambda i, shirt: {"id": i, "class_name": "person", "bbox": [0, 0, 10, 20], "attributes": {"shirt_colour": shirt}}
    # An unrelated person (blue shirt) and a person without a colour reading never stop the raster.
    scene.next_frame([person(4, "blue"), {"id": 7, "class_name": "person", "bbox": [0, 0, 9, 9], "attributes": {}}], validated={4, 7})
    executor.tick()
    assert executor.get_task_status(task_id)["status"] == "running"
    # Two validated red-shirt persons are visible together: stop, but never pick one.
    scene.next_frame([person(4, "blue"), person(8, "red"), person(9, "red")], validated={4, 8, 9})
    executor.tick()
    status = executor.get_task_status(task_id)
    assert status["status"] == "succeeded"
    tracking = status["tracking"]
    assert (tracking["found_id"], tracking["matching_ids"], tracking["ambiguous"]) == (None, [8, 9], True)
    [found] = [e for e in asyncio.run(event_bus.wait(0, 0.0)) if e["kind"] == "target_found"]
    assert found["data"]["id"] is None and found["data"]["ambiguous"] is True
    executor.close()


def test_search_by_reference_id_reacquires_a_lost_track_by_appearance():
    scene = _SearchScene()
    executor, _, event_bus = _search_executor(scene)
    task_id = executor.submit_skill("search", {"area": _SEARCH_AREA, "reference_id": 3}, ttl_s=10.0)["task_id"]
    executor.tick()
    nine = {"id": 9, "class_name": "person", "bbox": [0, 0, 10, 20]}
    scene.next_frame([nine])
    scene.reidentified = {"reference_id": 3, "candidate": {"id": 9}, "similarity": 0.5, "similarity_threshold": 0.72}
    executor.tick()  # below threshold: keep searching
    assert executor.get_task_status(task_id)["status"] == "running"
    scene.next_frame([nine])
    scene.reidentified = {"reference_id": 3, "candidate": {"id": 9}, "similarity": 0.91, "similarity_threshold": 0.72}
    executor.tick()
    tracking = executor.get_task_status(task_id)["tracking"]
    assert (tracking["found_id"], tracking["similarity"], tracking["mode"], tracking["matching_ids"]) == (9, 0.91, "reidentify", [9])
    [found] = [e for e in asyncio.run(event_bus.wait(0, 0.0)) if e["kind"] == "target_found"]
    assert found["summary"] == "Search re-identified lost ID 3 as tracker ID 9 (similarity 0.91)"
    executor.close()


def test_search_hovers_instead_of_flying_the_raster_while_camera_frames_are_stale():
    scene = _SearchScene()
    executor, flight, _ = _search_executor(scene)
    task_id = executor.submit_skill("search", {"area": _SEARCH_AREA, "class_name": "person"}, ttl_s=10.0)["task_id"]
    executor.tick()  # GUIDED and a first check of the then-fresh frame
    scene.frame_age_s = 2.0  # camera stalled: newest frame older than the telemetry age limit
    scene.next_frame()  # even a frame not yet checked is too old to count
    mark = len(flight.commands)
    for _ in range(5):
        executor.tick()
    status = executor.get_task_status(task_id)
    assert status["status"] == "running" and status["tracking"]["frames_checked"] == 1
    assert flight.commands[mark:] == [("velocity", 0.0, 0.0, 0.0)] * 5
    scene.frame_age_s = 0.0
    scene.next_frame()
    executor.tick()
    assert any(c[0] == "velocity" and (c[1], c[2]) != (0.0, 0.0) for c in flight.commands[mark + 5:])
    executor.close()


def test_search_never_attributes_a_newer_frame_validation_to_the_checked_frame():
    scene = _SearchScene()
    executor, _, _ = _search_executor(scene)
    task_id = executor.submit_skill("search", {"area": _SEARCH_AREA, "class_name": "person"}, ttl_s=10.0)["task_id"]
    executor.tick()
    scene.next_frame([{"id": 1, "class_name": "person", "bbox": [0, 0, 10, 20]}], validated={1})
    scene.validated_frame_offset = 1  # a newer frame landed between the snapshot and validation
    executor.tick()
    assert executor.get_task_status(task_id)["status"] == "running"
    scene.validated_frame_offset = 0
    scene.next_frame(scene.rows, validated={1})
    executor.tick()
    status = executor.get_task_status(task_id)
    assert status["status"] == "succeeded" and status["tracking"]["found_frame_id"] == scene.frame_id
    executor.close()


def test_emergency_during_search_validation_publishes_no_find_after_it():
    scene = _SearchScene()
    executor, flight, event_bus = _search_executor(scene)
    task_id = executor.submit_skill("search", {"area": _SEARCH_AREA, "class_name": "person"}, ttl_s=10.0)["task_id"]
    executor.tick()
    scene.next_frame([{"id": 1, "class_name": "person", "bbox": [0, 0, 10, 20]}], validated={1})
    validate = scene.validate_candidate

    def validate_then_emergency(candidate_id):
        result = validate(candidate_id)
        executor.hold()  # the operator's hold lands while the step is still validating
        return result

    scene.validate_candidate = validate_then_emergency
    executor.tick()
    kinds = [e["kind"] for e in asyncio.run(event_bus.wait(0, 0.0))]
    assert "target_found" not in kinds
    status = executor.get_task_status(task_id)
    assert status["status"] == "cancelled" and status["tracking"]["found"] is False
    assert flight.commands[-1] == ("hold",)
    executor.close()


@pytest.mark.parametrize(
    "arguments",
    [{}, {"class_name": "person", "reference_id": 3}, {"class_name": "  "}, {"reference_id": True},
     {"class_name": "person", "attribute": "shirt_colour"}, {"reference_id": 3, "attribute": "shirt_colour", "equals": "red"}],
)
def test_search_needs_exactly_one_valid_target_description(arguments):
    executor, _, _ = _search_executor(_SearchScene())
    with pytest.raises(ValueError):
        executor.submit_skill("search", {"area": _SEARCH_AREA, **arguments})
    executor.close()


@pytest.mark.parametrize("action", ["hold", "rth", "land"])
def test_emergency_preempts_a_running_search(action):
    scene = _SearchScene()
    executor, flight, event_bus = _search_executor(scene)
    task_id = executor.submit_skill("search", {"area": _SEARCH_AREA, "class_name": "person"}, ttl_s=10.0)["task_id"]
    executor.tick()
    executor.tick()
    getattr(executor, action)()
    # A match arriving after the emergency must not restart motion or flip the outcome.
    scene.next_frame([{"id": 5, "class_name": "person", "bbox": [0, 0, 5, 5]}], validated={5})
    assert executor.get_task_status(task_id)["status"] == "cancelled"
    assert [e["data"]["status"] for e in _finished_events(event_bus, task_id)] == ["cancelled"]
    _assert_emergency_stays_in_control(executor, flight, action)
    assert not [e for e in asyncio.run(event_bus.wait(0, 0.0)) if e["kind"] == "target_found"]
    executor.close()


def _last_velocity(flight):
    velocities = [command for command in flight.commands if command[0] == "velocity"]
    assert velocities, "no velocity command was issued"
    return velocities[-1][1:]


def _cruise_settings(**overrides):
    values = {"cruise_speed_m_s": 2.0, "cruise_min_altitude_m": 3.0}
    values.update(overrides)
    return _settings(**values)


def test_goto_uses_the_gated_cruise_speed_above_the_minimum_altitude():
    flight = FakeFlight(Pose(0.0, 0.0, 3.5))
    executor = HorusExecutor(flight, _cruise_settings(), autostart=False)
    executor.submit_skill("goto", {"pose": {"north_m": 5.0, "east_m": 0.0, "up_m": 3.5}}, ttl_s=5.0)
    executor.tick()
    executor.tick()
    assert _last_velocity(flight)[:2] == (2.0, 0.0)
    executor.close()


def test_goto_keeps_max_speed_below_the_cruise_minimum_altitude():
    flight = FakeFlight(Pose(0.0, 0.0, 2.5))
    executor = HorusExecutor(flight, _cruise_settings(), autostart=False)
    executor.submit_skill("goto", {"pose": {"north_m": 5.0, "east_m": 0.0, "up_m": 3.5}}, ttl_s=5.0)
    executor.tick()
    executor.tick()
    assert _last_velocity(flight)[:2] == (1.0, 0.0)
    executor.close()


def test_goto_keeps_max_speed_when_the_target_altitude_is_below_the_gate():
    flight = FakeFlight(Pose(0.0, 0.0, 3.5))
    executor = HorusExecutor(flight, _cruise_settings(), autostart=False)
    executor.submit_skill("goto", {"pose": {"north_m": 5.0, "east_m": 0.0, "up_m": 2.0}}, ttl_s=5.0)
    executor.tick()
    executor.tick()
    assert _last_velocity(flight)[:2] == (1.0, 0.0)
    executor.close()


def test_goto_drops_back_to_max_speed_when_descending_below_the_gate():
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


def test_disabled_cruise_speed_keeps_max_speed_everywhere():
    flight = FakeFlight(Pose(0.0, 0.0, 3.5))
    executor = HorusExecutor(flight, _settings(), autostart=False)
    executor.submit_skill("goto", {"pose": {"north_m": 5.0, "east_m": 0.0, "up_m": 3.5}}, ttl_s=5.0)
    executor.tick()
    executor.tick()
    assert _last_velocity(flight)[:2] == (1.0, 0.0)
    executor.close()


def test_flight_envelope_reports_cruise_settings():
    enabled = flight_envelope(_cruise_settings())
    assert enabled["cruise_speed_m_s"] == 2.0
    assert enabled["cruise_min_altitude_m"] == 3.0
    disabled = flight_envelope(_settings())
    assert disabled["cruise_speed_m_s"] is None
    assert disabled["cruise_min_altitude_m"] is None
