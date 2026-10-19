from __future__ import annotations

import asyncio
import math
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from horus.events import EventBus
from horus.executor import HorusExecutor
from horus.executor.precision_landing import (
    AprilTag36h11Detector,
    AprilTagPrecisionLandingController,
    PrecisionLanding,
    centering_velocity_ned,
)
from horus.flight import Pose

from tests.fakes import FakeFlight, PerceptionStub, executor_settings as _executor_settings


def _cv2_aruco():
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "aruco"):
        pytest.skip("OpenCV aruco module is unavailable")
    return cv2, cv2.aruco


def _tag_image(center_x: int = 400, center_y: int = 220, marker_id: int = 0) -> np.ndarray:
    cv2, aruco = _cv2_aruco()
    dictionary = aruco.getPredefinedDictionary(aruco.DICT_APRILTAG_36h11)
    if hasattr(aruco, "drawMarker"):
        marker = aruco.drawMarker(dictionary, marker_id, 160)
    else:
        marker = aruco.generateImageMarker(dictionary, marker_id, 160)
    image = np.full((480, 640), 255, dtype=np.uint8)
    left, top = center_x - marker.shape[1] // 2, center_y - marker.shape[0] // 2
    image[top : top + marker.shape[0], left : left + marker.shape[1]] = marker
    return image


def test_synthetic_id0_detection_and_bounded_centering_deadband():
    image = _tag_image()
    controller = AprilTagPrecisionLandingController(AprilTag36h11Detector())

    detection, velocity = controller.observe(image, heading_deg=0.0)

    assert detection is not None
    assert detection.center_x == pytest.approx(400.0, abs=2.0)
    assert detection.center_y == pytest.approx(220.0, abs=2.0)
    assert velocity[0] > 0.0  # Marker is image-up: move vehicle forward/north.
    assert velocity[1] > 0.0  # Marker is image-right: move vehicle right/east.
    assert velocity[2] == 0.0
    assert math.hypot(velocity[0], velocity[1]) <= 0.35
    rotated = centering_velocity_ned(320.0, 200.0, 640, 480, 90.0)
    assert rotated[0] == pytest.approx(0.0, abs=1e-12)
    assert rotated[1] > 0.0  # Image-up follows vehicle-forward after a 90-degree heading.

    extreme = centering_velocity_ned(640.0, 0.0, 640, 480, 0.0)
    assert math.hypot(extreme[0], extreme[1]) == pytest.approx(0.35)
    assert extreme[2] == 0.0

    aligned = centering_velocity_ned(331.0, 245.0, 640, 480, 0.0)
    assert aligned == (0.0, 0.0, 0.15)
    # Inside the descent cone but outside the deadband: correct right while descending.
    near_center = centering_velocity_ned(340.0, 240.0, 640, 480, 0.0)
    assert near_center[0] == 0.0
    assert near_center[1] > 0.0
    assert near_center[2] == 0.15
    # A residual 8% offset must still command a usable correction (regression: the old
    # deadband-subtracting law produced ~1 cm/s here and the vehicle hovered indefinitely).
    residual = centering_velocity_ned(320.0 + 0.08 * 320.0, 240.0, 640, 480, 0.0)
    assert residual[1] >= 0.05
    # Far off-center: correct only, no descent.
    far = centering_velocity_ned(320.0 + 0.3 * 320.0, 240.0, 640, 480, 0.0)
    assert far[2] == 0.0

    missing_detection, missing_velocity = controller.observe(
        np.full((480, 640), 255, dtype=np.uint8), heading_deg=0.0
    )
    assert missing_detection is None
    assert missing_velocity == (0.0, 0.0, 0.0)
    wrong_id_detection, _ = controller.observe(_tag_image(marker_id=1), heading_deg=0.0)
    assert wrong_id_detection is None


class _FakePerception(PerceptionStub):
    def __init__(self, image):
        self.frame = SimpleNamespace(image=image, timestamp=time.monotonic())

    def get_frame(self):
        return self.frame


@pytest.mark.parametrize(("action", "fallback"), [("rth", ("rth",)), ("land", ("land",))])
def test_emergency_landing_keeps_autopilot_until_near_home_and_tag_acquisition(action, fallback):
    flight = FakeFlight(Pose(4.0, 0.0, 2.0), armed=True)
    perception = _FakePerception(_tag_image())
    executor = HorusExecutor(flight, _executor_settings(), perception, autostart=False)
    try:
        getattr(executor, action)()
        executor.tick()
        assert flight.commands == [fallback]

        flight._telemetry.pose = Pose(0.0, 0.0, 2.0)
        flight._telemetry.pose_updated_monotonic = time.monotonic()
        executor.tick()
        assert flight.commands[-1] == ("gimbal", -90.0, 0.0)
        assert not any(command[0] == "guided" for command in flight.commands)

        time.sleep(PrecisionLanding.GIMBAL_SETTLE_S + 0.02)
        perception.frame.timestamp = time.monotonic()
        executor.tick()
        assert flight.commands[-2] == ("guided",)
        assert flight.commands[-1][0] == "velocity"
        assert flight.commands[-1][2] > 0.0
        assert flight.commands[-1][3] == 0.0

        # A sustained tag loss (past the setpoint grace) returns to the zero-velocity hold.
        perception.frame.image = np.full((480, 640), 255, dtype=np.uint8)
        time.sleep(executor.settings.precision_landing_target_grace_s + 0.02)
        _refresh_precision_inputs(flight, perception)
        executor.tick()
        assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0)
    finally:
        executor.close()


def test_precision_landing_coasts_the_tag_setpoint_between_detections():
    # The detector only sees the tag on a fraction of frames; re-deriving the setpoint every
    # tick would drop the descent to zero on every miss, which the autopilot averages into a
    # near-hover (0.017-0.03 m/s instead of 0.15 m/s).
    flight = FakeFlight(Pose(0.0, 0.0, 1.5), armed=True)
    perception = _FakePerception(_centered_tag_image())
    executor = HorusExecutor(
        flight, _executor_settings(precision_landing_target_grace_s=0.2), perception, autostart=False
    )
    try:
        _settle_on_visible_tag(executor, flight, perception)
        executor.tick()  # acquire the tag and command the centring descent
        assert flight.commands[-1][0] == "velocity"
        assert flight.commands[-1][3] == pytest.approx(0.15)

        # Frames without a detection keep the last validated setpoint inside the grace: the
        # descent is not pulsed to zero every time the detector misses the tag.
        perception.frame.image = np.full((480, 640), 255, dtype=np.uint8)
        _refresh_precision_inputs(flight, perception)
        executor.tick()
        assert flight.commands[-1][0] == "velocity"
        assert flight.commands[-1][3] == pytest.approx(0.15)
        executor.tick()
        assert flight.commands[-1][3] == pytest.approx(0.15)

        # Past the grace the vehicle holds instead of descending blind.
        time.sleep(executor.settings.precision_landing_target_grace_s + 0.02)
        _refresh_precision_inputs(flight, perception)
        executor.tick()
        assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0)
    finally:
        executor.close()


def test_rth_does_not_start_precision_control_without_required_inputs():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True)
    executor = HorusExecutor(flight, _executor_settings(), perception=None, autostart=False)
    try:
        executor.rth()
        executor.tick()
        flight._telemetry.pose = None  # no local pose yet (EKF origin unset)
        executor.tick()
        assert flight.commands == [("rth",)]
    finally:
        executor.close()


class _BlockingGuidedFlight(FakeFlight):
    """Blocks the controller thread while it sends GUIDED to start precision control."""

    def __init__(self, pose: Pose):
        super().__init__(pose, armed=True)
        self.entered = threading.Event()
        self.release = threading.Event()

    def set_guided(self):
        if threading.current_thread().name == "tick":
            self.entered.set()
            assert self.release.wait(5.0)
        super().set_guided()


def _settle_on_visible_tag(executor, flight, perception):
    executor.rth()
    executor.tick()  # gimbal down
    time.sleep(PrecisionLanding.GIMBAL_SETTLE_S + 0.02)
    now = time.monotonic()
    perception.frame.timestamp = now
    flight._telemetry.pose_updated_monotonic = now
    flight._telemetry.heading_updated_monotonic = now


def test_operator_hold_during_precision_tag_acquisition_is_not_overridden():
    flight = _BlockingGuidedFlight(Pose(0.0, 0.0, 2.0))
    perception = _FakePerception(_tag_image())
    executor = HorusExecutor(flight, _executor_settings(), perception, autostart=False)
    try:
        _settle_on_visible_tag(executor, flight, perception)
        worker = threading.Thread(target=executor.tick, name="tick")
        worker.start()
        assert flight.entered.wait(2.0)
        operator = threading.Thread(target=executor.hold, name="operator")
        operator.start()
        flight.release.set()
        worker.join(2.0)
        operator.join(2.0)
        assert not worker.is_alive() and not operator.is_alive()

        flight_commands = [c for c in flight.commands if c[0] != "gimbal"]
        assert flight_commands[-1] == ("hold",)
        mark = len(flight.commands)
        for _ in range(3):
            perception.frame.timestamp = time.monotonic()
            executor.tick()
        assert all(c == ("velocity", 0.0, 0.0, 0.0) for c in flight.commands[mark:])
        state = executor.get_state()
        assert state["state"] == "HOLDING"
        assert state["precision_landing"]["active"] is False
    finally:
        executor.close()


def test_precision_landing_does_not_steer_on_a_stale_heading():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True)
    perception = _FakePerception(_tag_image())
    executor = HorusExecutor(flight, _executor_settings(), perception, autostart=False)
    try:
        _settle_on_visible_tag(executor, flight, perception)
        flight._telemetry.heading_updated_monotonic = time.monotonic() - 2.0
        executor.tick()
        assert flight.commands == [("rth",), ("gimbal", -90.0, 0.0)]

        flight._telemetry.heading_updated_monotonic = time.monotonic()
        executor.tick()
        assert flight.commands[-2] == ("guided",)
        assert flight.commands[-1][0] == "velocity"
    finally:
        executor.close()


def _centered_tag_image():
    # Within the 0.05 deadband and 0.15 descent cone of a 640x480 frame: the controller commands
    # a pure descent, as it does while it has the tag and the autopilot is supposed to follow.
    return _tag_image(center_x=328, center_y=232)


def _refresh_precision_inputs(flight, perception):
    now = time.monotonic()
    perception.frame.timestamp = now
    flight._telemetry.pose_updated_monotonic = now
    flight._telemetry.heading_updated_monotonic = now


def test_precision_control_failure_hands_off_to_land_instead_of_hovering():
    # A pose sample going stale while precision control owns the vehicle must hand off to LAND;
    # holding in GUIDED, armed, high above the pad leaves no automatic way out.
    flight = FakeFlight(Pose(0.0, 0.0, 1.5), armed=True)
    perception = _FakePerception(_centered_tag_image())
    executor = HorusExecutor(flight, _executor_settings(), perception, autostart=False)
    try:
        _settle_on_visible_tag(executor, flight, perception)
        executor.tick()
        assert executor.get_state()["precision_landing"]["active"] is True

        flight._telemetry.pose_updated_monotonic = time.monotonic() - 5.0  # stale pose sample
        perception.frame.timestamp = time.monotonic()
        flight._telemetry.heading_updated_monotonic = time.monotonic()
        executor.tick()

        assert flight.commands[-2] == ("velocity", 0.0, 0.0, 0.0)
        assert flight.commands[-1] == ("land",)
        state = executor.get_state()
        assert state["precision_landing"]["active"] is False
        assert state["state"] == "IDLE"
        assert "controller failed" in state["precision_landing"]["last_fallback"]
    finally:
        executor.close()


def test_precision_watchdog_scales_the_descent_rate_to_autopilot_time():
    # A low sim RTF shrinks a legitimate descent in wall time: 0.05 s of autopilot time in 0.25 s
    # of wall time (RTF 0.2) with 0.005 m of descent is 0.1 m/s on the autopilot clock but only
    # 0.02 m/s of wall time; the stall check must use the autopilot clock.
    executor, flight, perception = _watchdog_executor(12.0)
    try:
        time.sleep(0.25)
        flight._telemetry.time_boot_ms += 50  # +0.05 s autopilot
        flight._telemetry.pose = Pose(0.0, 0.0, 11.995)
        _refresh_precision_inputs(flight, perception)
        flight._telemetry.time_boot_updated_monotonic = time.monotonic()
        executor.tick()

        assert ("land",) not in flight.commands
        assert executor.get_state()["precision_landing"]["active"] is True
    finally:
        executor.close()


def test_precision_watchdog_fires_on_a_stall_at_low_autopilot_rates():
    executor, flight, perception = _watchdog_executor(12.0)
    try:
        time.sleep(0.25)
        flight._telemetry.time_boot_ms += 50
        _refresh_precision_inputs(flight, perception)  # altitude unchanged: a real stall
        flight._telemetry.time_boot_updated_monotonic = time.monotonic()
        executor.tick()

        assert flight.commands[-1] == ("land",)
        state = executor.get_state()
        assert state["precision_landing"]["active"] is False
        assert "no descent progress" in state["precision_landing"]["last_fallback"]
    finally:
        executor.close()


def _watchdog_executor(up_m: float):
    flight = FakeFlight(Pose(0.0, 0.0, up_m), armed=True)
    flight._telemetry.time_boot_ms = 1_000_000
    flight._telemetry.time_boot_updated_monotonic = time.monotonic()
    perception = _FakePerception(_centered_tag_image())
    executor = HorusExecutor(
        flight,
        _executor_settings(precision_landing_stall_s=0.25, precision_landing_min_descent_m_s=0.05),
        perception,
        autostart=False,
    )
    _settle_on_visible_tag(executor, flight, perception)
    executor.tick()  # acquire the tag
    executor.tick()  # start the descent-progress window
    return executor, flight, perception


def test_precision_landing_stall_falls_back_to_land_and_reports():
    flight = FakeFlight(Pose(0.0, 0.0, 1.5), armed=True)
    perception = _FakePerception(_centered_tag_image())
    event_bus = EventBus()
    executor = HorusExecutor(
        flight,
        _executor_settings(precision_landing_stall_s=0.05, precision_landing_min_descent_m_s=0.05),
        perception,
        event_bus=event_bus,
        autostart=False,
    )
    try:
        _settle_on_visible_tag(executor, flight, perception)
        executor.tick()  # acquire the tag; the controller commands a 0.15 m/s descent
        assert any(
            command[0] == "velocity" and command[3] == pytest.approx(0.15)
            for command in flight.commands
        )
        executor.tick()  # start the descent-progress window
        assert executor.get_state()["precision_landing"]["active"] is True

        time.sleep(0.08)  # the vehicle never descends (e.g. a masked autopilot failsafe)
        _refresh_precision_inputs(flight, perception)
        executor.tick()

        assert flight.commands[-1] == ("land",)
        state = executor.get_state()
        assert state["precision_landing"]["active"] is False
        assert "no descent progress" in state["precision_landing"]["last_fallback"]
        assert state["state"] == "IDLE"
        events = asyncio.run(event_bus.wait(after_id=0, timeout_s=0.0))
        stalled = [event for event in events if event["kind"] == "precision_landing_stalled"]
        assert len(stalled) == 1
        assert stalled[0]["data"]["descent_rate_m_s"] == pytest.approx(0.0)
        assert stalled[0]["data"]["elapsed_s"] >= 0.05
    finally:
        executor.close()


def test_precision_landing_keeps_control_while_altitude_progresses():
    flight = FakeFlight(Pose(0.0, 0.0, 1.5), armed=True)
    perception = _FakePerception(_centered_tag_image())
    executor = HorusExecutor(
        flight,
        _executor_settings(precision_landing_stall_s=0.05, precision_landing_min_descent_m_s=0.05),
        perception,
        autostart=False,
    )
    try:
        _settle_on_visible_tag(executor, flight, perception)
        executor.tick()
        executor.tick()

        time.sleep(0.08)
        flight._telemetry.pose = Pose(0.0, 0.0, 1.4)  # 0.1 m descent over the window
        _refresh_precision_inputs(flight, perception)
        executor.tick()

        assert not any(command == ("land",) for command in flight.commands)
        state = executor.get_state()
        assert state["precision_landing"]["active"] is True
        assert state["precision_landing"]["last_fallback"] is None
    finally:
        executor.close()


def test_operator_hold_restores_the_camera_turned_nadir_for_precision_landing():
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True)
    perception = _FakePerception(_tag_image())
    executor = HorusExecutor(flight, _executor_settings(), perception, autostart=False)
    try:
        executor.rth()
        executor.tick()
        assert flight.commands[-1] == ("gimbal", -90.0, 0.0)

        executor.hold()

        assert flight.commands[-2:] == [("hold",), ("gimbal", 0.0, 0.0)]
    finally:
        executor.close()


def test_next_skill_after_a_pilot_override_restores_the_nadir_camera():
    # The override ends precision control but keeps the nadir-camera record for the next skill.
    flight = FakeFlight(Pose(0.0, 0.0, 2.0), armed=True)
    perception = _FakePerception(_tag_image())
    executor = HorusExecutor(flight, _executor_settings(), perception, autostart=False)
    try:
        executor.rth()
        executor.tick()
        assert flight.commands[-1] == ("gimbal", -90.0, 0.0)

        flight._telemetry.mode = "LOITER"
        executor.tick()
        assert executor.get_state()["state"] == "PILOT"
        flight._telemetry.mode = "GUIDED"
        executor.tick()
        assert executor.get_state()["state"] == "HOLDING"

        mark = len(flight.commands)
        executor.submit_skill("goto", {"pose": {"north_m": 0.0, "east_m": 0.0, "up_m": 2.0}}, ttl_s=5.0)
        _refresh_precision_inputs(flight, perception)
        executor.tick()
        assert flight.commands[mark] == ("gimbal", 0.0, 0.0)
    finally:
        executor.close()
