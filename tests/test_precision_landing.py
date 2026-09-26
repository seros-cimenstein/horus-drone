from __future__ import annotations

import math
import time
from types import SimpleNamespace

import numpy as np
import pytest

from horus.config import ExecutorSettings
from horus.executor import HorusExecutor
from horus.executor.precision_landing import (
    AprilTag36h11Detector,
    AprilTagPrecisionLandingController,
    centering_velocity_ned,
)
from horus.flight import Pose
from scripts.landing_error import _messages, parse_pose_info


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


class _FakeTelemetry:
    def __init__(self, pose: Pose):
        self.pose = pose
        self.pose_updated_monotonic = time.monotonic()
        self.heading_deg = 0.0
        self.armed = True


class _FakeFlight:
    def __init__(self, pose: Pose):
        self._telemetry = _FakeTelemetry(pose)
        self.commands: list[tuple] = []

    def telemetry(self):
        return self._telemetry

    def rth(self):
        self.commands.append(("rth",))

    def land(self):
        self.commands.append(("land",))

    def set_gimbal_pitch_yaw(self, pitch_deg, yaw_deg=0.0):
        self.commands.append(("gimbal", pitch_deg, yaw_deg))

    def set_guided(self):
        self.commands.append(("guided",))

    def set_velocity_ned(self, north, east, down, yaw_deg=None):
        self.commands.append(("velocity", north, east, down) if yaw_deg is None else ("velocity", north, east, down, yaw_deg))

    def hold(self):
        self.commands.append(("hold",))


class _FakePerception:
    def __init__(self, image):
        self.frame = SimpleNamespace(image=image, timestamp=time.monotonic())

    def get_frame(self):
        return self.frame


def _executor_settings():
    return ExecutorSettings(
        control_hz=15.0,
        command_ttl_s=1.0,
        max_speed_m_s=1.0,
        max_vertical_speed_m_s=0.5,
        max_distance_m=5.0,
        min_altitude_m=0.5,
        max_altitude_m=4.0,
        geofence_radius_m=3.0,
        arrival_tolerance_m=0.25,
        follow_distance_m=2.0,
        target_width_m=None,
    )


@pytest.mark.parametrize(("action", "fallback"), [("rth", ("rth",)), ("land", ("land",))])
def test_emergency_landing_keeps_autopilot_until_near_home_and_tag_acquisition(action, fallback):
    flight = _FakeFlight(Pose(4.0, 0.0, 2.0))
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

        time.sleep(executor.PRECISION_GIMBAL_SETTLE_S + 0.02)
        perception.frame.timestamp = time.monotonic()
        executor.tick()
        assert flight.commands[-2] == ("guided",)
        assert flight.commands[-1][0] == "velocity"
        assert flight.commands[-1][2] > 0.0
        assert flight.commands[-1][3] == 0.0

        perception.frame.image = np.full((480, 640), 255, dtype=np.uint8)
        perception.frame.timestamp = time.monotonic()
        executor.tick()
        assert flight.commands[-1] == ("velocity", 0.0, 0.0, 0.0)
    finally:
        executor.close()


def test_landing_error_parser_uses_vehicle_and_pad_world_positions():
    message = '''
header {
  stamp { sec: 1 nsec: 2 }
}
pose {
  name: "horus_iris"
  position { x: 1.25 y: -2.0 z: 0.4 }
  orientation { w: 1 }
}
pose {
  name: "home_apriltag_pad"
  position { x: -0.5 y: 0.25 z: 0.0 }
  orientation { w: 1 }
}
'''

    error = parse_pose_info(message)

    assert error is not None
    assert error.dx_m == pytest.approx(1.75)
    assert error.dy_m == pytest.approx(-2.25)
    assert error.distance_m == pytest.approx(math.hypot(1.75, -2.25))
    assert parse_pose_info('pose { name: "horus_iris" position { x: 1 y: 2 } }') is None


def test_landing_error_parser_treats_omitted_coordinates_as_zero():
    message = '''
pose {
  name: "horus_iris"
  position { x: 0.45 y: -0.25 z: 0.3 }
}
pose {
  name: "home_apriltag_pad"
  position {
  }
}
'''

    error = parse_pose_info(message)

    assert error is not None
    assert error.pad_x_m == pytest.approx(0.0)
    assert error.pad_y_m == pytest.approx(0.0)
    assert error.dx_m == pytest.approx(0.45)
    assert error.dy_m == pytest.approx(-0.25)
    assert error.distance_m == pytest.approx(math.hypot(0.45, -0.25))


def test_landing_error_stream_frames_pose_info_samples():
    sample = '''header {
  stamp { sec: 1 nsec: 0 }
}
pose {
  name: "horus_iris"
  position { x: 2.0 y: 3.0 z: 1.0 }
}
pose {
  name: "home_apriltag_pad"
  position { x: 0.0 y: 0.0 z: 0.0 }
}
'''
    messages = list(_messages((sample + sample.replace("x: 2.0", "x: 4.0")).splitlines(True)))

    assert len(messages) == 2
    assert parse_pose_info(messages[0]).distance_m == pytest.approx(math.sqrt(13.0))
    assert parse_pose_info(messages[1]).distance_m == pytest.approx(5.0)


def test_rth_does_not_start_precision_control_without_required_inputs():
    flight = _FakeFlight(Pose(0.0, 0.0, 2.0))
    executor = HorusExecutor(flight, _executor_settings(), perception=None, autostart=False)
    try:
        executor.rth()
        executor.tick()
        flight._telemetry = None
        executor.tick()
        assert flight.commands == [("rth",)]
    finally:
        executor.close()
