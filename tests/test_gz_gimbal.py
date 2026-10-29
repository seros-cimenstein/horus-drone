"""Math of the Gazebo gimbal device (horus/gimbal/gazebo.py): IMU lean, stabilisation, joints."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from horus.gimbal import frames
from horus.gimbal.gazebo import (
    PAN_LIMIT_DEG,
    ROLL_LIMIT_DEG,
    TILT_MAX_DEG,
    TILT_MIN_DEG,
    Mount,
    camera_attitude,
    clamp_mount,
    joint_commands,
    mount_from_joints,
    mount_quaternion,
    solve_mount,
    vehicle_lean,
)

MODELS = Path(__file__).resolve().parents[1] / "sim" / "models"
FLU_TO_FRD = np.diag([1.0, -1.0, -1.0])


def _matrix(q: frames.Quaternion) -> np.ndarray:
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def _quaternion(m: np.ndarray) -> frames.Quaternion:
    w = math.sqrt(max(0.0, 1.0 + m[0, 0] + m[1, 1] + m[2, 2])) / 2.0
    x = math.copysign(math.sqrt(max(0.0, 1.0 + m[0, 0] - m[1, 1] - m[2, 2])) / 2.0, m[2, 1] - m[1, 2])
    y = math.copysign(math.sqrt(max(0.0, 1.0 - m[0, 0] + m[1, 1] - m[2, 2])) / 2.0, m[0, 2] - m[2, 0])
    z = math.copysign(math.sqrt(max(0.0, 1.0 - m[0, 0] - m[1, 1] + m[2, 2])) / 2.0, m[1, 0] - m[0, 1])
    return (w, x, y, z)


def _axis_angle(axis: np.ndarray, angle_rad: float) -> np.ndarray:
    axis = axis / np.linalg.norm(axis)
    k = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + math.sin(angle_rad) * k + (1 - math.cos(angle_rad)) * k @ k


def _sdf_rpy(pose: ET.Element) -> np.ndarray:
    """Rotation of an SDF ``<pose>`` (x y z roll pitch yaw, fixed axes: Rz * Ry * Rx)."""
    values = [float(value) for value in pose.text.split()]
    roll, pitch, yaw = (math.radians(v) for v in values[3:]) if pose.get("degrees") == "true" else values[3:]
    return (_axis_angle(np.array([0, 0, 1.0]), yaw) @ _axis_angle(np.array([0, 1.0, 0]), pitch)
            @ _axis_angle(np.array([1.0, 0, 0]), roll))


def _gimbal_include_pose(airframe: str) -> np.ndarray:
    model = ET.parse(MODELS / airframe / "model.sdf").getroot().find("model")
    includes = [include for include in model.findall("include") if include.findtext("name") == "gimbal"]
    assert len(includes) == 1
    return _sdf_rpy(includes[0].find("pose"))


def _same_rotation(a: frames.Quaternion, b: frames.Quaternion) -> bool:
    return abs(abs(sum(x * y for x, y in zip(a, b))) - 1.0) < 1e-9


LEANS = [(0.0, 0.0), (10.0, -5.0), (-20.0, 15.0), (25.0, 25.0), (-8.0, -22.0), (3.0, 30.0)]
TARGETS = [(-90.0, 0.0), (-90.0, 75.0), (-60.0, -120.0), (-45.0, 0.0), (-10.0, 45.0), (0.0, -30.0),
           (20.0, 140.0), (30.0, 0.0)]


@pytest.mark.parametrize("lean", LEANS)
@pytest.mark.parametrize(("pitch", "yaw"), TARGETS)
def test_solved_mount_holds_the_target_against_the_lean(lean, pitch, yaw):
    mount = solve_mount(lean, pitch, yaw)

    lean_q = frames.quaternion_from_euler(lean[0], lean[1], 0.0)
    camera = frames.quaternion_multiply(lean_q, mount_quaternion(mount))
    assert _same_rotation(camera, frames.quaternion_from_euler(0.0, pitch, yaw))
    reported = camera_attitude(lean, mount)
    assert reported == pytest.approx((pitch, 0.0, yaw), abs=1e-6)


def test_level_body_maps_the_target_straight_onto_pan_and_tilt():
    assert solve_mount((0.0, 0.0), -45.0, 30.0) == pytest.approx(Mount(30.0, 0.0, -45.0))


def test_lean_is_countered_by_roll_and_tilt():
    # Right side down 10 deg: the roll axis turns left 10 deg; nose down 5 deg: 5 deg less tilt.
    assert solve_mount((10.0, 0.0), -45.0, 0.0) == pytest.approx(Mount(0.0, -10.0, -45.0), abs=1e-9)
    assert solve_mount((0.0, -5.0), -45.0, 0.0) == pytest.approx(Mount(0.0, 0.0, -40.0), abs=1e-9)


def test_joint_commands_follow_the_model_axes_and_invert_the_joint_readback():
    mount = Mount(30.0, 10.0, -45.0)
    commands = joint_commands(mount)

    assert commands == pytest.approx({"roll": math.radians(10.0), "pitch": math.radians(45.0),
                                      "yaw": math.radians(-30.0)})
    assert mount_from_joints(commands["roll"], commands["pitch"], commands["yaw"]) == pytest.approx(mount)


@pytest.mark.parametrize("variant", ["outdoor", "procedural"])
@pytest.mark.parametrize("joints", [(0.0, 0.0, 0.0), (0.3, 0.0, 0.0), (0.0, 0.7, 0.0), (0.0, 0.0, -1.1),
                                    (0.2, 0.9, 0.5), (-0.4, -0.3, 2.0)])
def test_mount_from_joints_matches_the_sdf_kinematics(variant, joints):
    """Camera axes from the gimbal SDF (mount pose, joint axes, sensor pose) agree with the mount."""
    gimbal = ET.parse(MODELS / f"horus_gimbal_small_3d_{variant}" / "model.sdf").getroot().find("model")
    for element in (*gimbal.findall("link"), *gimbal.findall("joint")):
        pose = element.find("pose")
        assert pose is None or [float(v) for v in pose.text.split()[3:]] == [0.0, 0.0, 0.0]
    axes = {joint.get("name"): np.array([float(v) for v in joint.findtext("axis/xyz").split()])
            for joint in gimbal.findall("joint")}
    sensor = _sdf_rpy(gimbal.find("link[@name='pitch_link']/sensor[@name='camera']/pose"))
    roll_joint, pitch_joint, yaw_joint = joints

    body_flu = (_gimbal_include_pose(f"horus_iris_with_gimbal_{variant}")
                @ _axis_angle(axes["yaw_joint"], yaw_joint) @ _axis_angle(axes["roll_joint"], roll_joint)
                @ _axis_angle(axes["pitch_joint"], pitch_joint) @ sensor)
    expected = _matrix(mount_quaternion(mount_from_joints(roll_joint, pitch_joint, yaw_joint)))

    # Camera sensor x looks along the optical axis and z is image up; the HORUS camera frame is
    # forward-right-down, so image up is its -z.
    np.testing.assert_allclose(FLU_TO_FRD @ body_flu[:, 0], expected[:, 0], atol=2e-3)
    np.testing.assert_allclose(FLU_TO_FRD @ body_flu[:, 2], -expected[:, 2], atol=2e-3)


def test_indoor_airframe_mounts_the_gimbal_like_the_outdoor_one():
    np.testing.assert_allclose(_gimbal_include_pose("horus_iris_with_gimbal"),
                               _gimbal_include_pose("horus_iris_with_gimbal_outdoor"))


def test_clamped_mount_reports_the_attitude_the_gimbal_can_reach():
    # 60 deg nose-up needs -150 deg tilt for nadir: the stop at -135 leaves the camera at -75.
    mount = clamp_mount(solve_mount((0.0, 60.0), -90.0, 0.0))
    assert mount.tilt_deg == TILT_MIN_DEG
    assert camera_attitude((0.0, 60.0), mount) == pytest.approx((-75.0, 0.0, 0.0), abs=1e-6)

    mount = clamp_mount(solve_mount((40.0, 0.0), 0.0, 0.0))
    assert mount.roll_deg == -ROLL_LIMIT_DEG
    assert camera_attitude((40.0, 0.0), mount)[1] == pytest.approx(10.0, abs=1e-6)

    mount = clamp_mount(solve_mount((0.0, 0.0), 60.0, 170.0))
    assert (mount.pan_deg, mount.tilt_deg) == (PAN_LIMIT_DEG, TILT_MAX_DEG)
    assert camera_attitude((0.0, 0.0), mount) == pytest.approx((TILT_MAX_DEG, 0.0, PAN_LIMIT_DEG), abs=1e-6)

    assert clamp_mount(Mount(-170.0, 31.0, -140.0)) == Mount(-PAN_LIMIT_DEG, ROLL_LIMIT_DEG, TILT_MIN_DEG)


# NED -> ENU world coordinates, and the Iris imu_sensor's pose on its FLU imu_link
# (ardupilot_gazebo models/iris_with_standoffs/model.sdf: <pose degrees="true">0 0 0 180 0 0</pose>).
NED_TO_ENU = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]])
IMU_SENSOR_ON_LINK = _axis_angle(np.array([1.0, 0.0, 0.0]), math.pi)


def _gz_imu_orientation(spawn_yaw_deg: float, roll: float, pitch: float, yaw: float) -> frames.Quaternion:
    """What gz-sim's IMU reports: the sensor's world rotation relative to its spawn rotation."""
    def sensor_in_world(r: float, p: float, y: float) -> np.ndarray:
        body_frd_in_ned = _matrix(frames.quaternion_from_euler(r, p, y))
        return NED_TO_ENU @ body_frd_in_ned @ FLU_TO_FRD @ IMU_SENSOR_ON_LINK

    reference = sensor_in_world(0.0, 0.0, spawn_yaw_deg)
    return _quaternion(reference.T @ sensor_in_world(roll, pitch, yaw))


@pytest.mark.parametrize("spawn_yaw", [0.0, 90.0, -137.0])
@pytest.mark.parametrize(("roll", "pitch", "yaw_change"), [
    (0.0, 0.0, 0.0), (12.0, 0.0, 0.0), (0.0, -18.9, 0.0), (-7.0, 9.0, 0.0), (15.0, -20.0, 63.0),
    (-25.0, 30.0, -170.0)])
def test_vehicle_lean_reads_ned_roll_and_pitch_from_the_gz_imu(spawn_yaw, roll, pitch, yaw_change):
    q = _gz_imu_orientation(spawn_yaw, roll, pitch, spawn_yaw + yaw_change)

    assert vehicle_lean(q) == pytest.approx((roll, pitch), abs=1e-6)
