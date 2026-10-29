"""The Gazebo gimbal as a MAVLink gimbal device: a self-levelling three-axis gimbal.

Like a brushless gimbal with its own IMU, it reads the vehicle's lean from the airframe IMU, holds
HORUS's setpoint (horizon pitch, nose-relative yaw, level roll) against it with the model's
yaw/roll/pitch joints, and reports the attitude its joint encoders and IMU measure through
``GimbalDeviceEndpoint``. ArduPilot takes no part in the gimbal.

Kinematics of ``gimbal_small_3d`` as the HORUS airframes mount it (include pose
``0 -0.01 -0.124923 90 0 90`` deg on the Iris base link, x forward, y left, z up): the gimbal
model's x/y/z axes are the body's left/up/forward. The chain runs base -> ``yaw_joint`` (axis y,
body up) -> ``roll_joint`` (axis z, body forward) -> ``pitch_joint`` (axis x, body left), and the
camera sensor looks along the body's forward axis, image up, at zero joint angles. In the NED body
frame (x forward, y right, z down) the camera-in-body rotation is therefore
Rz(pan) * Rx(roll) * Ry(tilt) with pan = -yaw_joint, roll = +roll_joint, tilt = -pitch_joint
(+pan right, +roll right side down, +tilt up).

Gazebo bindings are imported only by ``main`` so the math stays importable and testable without them.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import threading
import time
from typing import Any, NamedTuple

from horus.gimbal import frames
from horus.gimbal.device import GimbalDeviceEndpoint

logger = logging.getLogger(__name__)

Lean = tuple[float, float]

# Mechanical travel (degrees), the ranges the model's servo mount used to command.
PAN_LIMIT_DEG = 160.0
ROLL_LIMIT_DEG = 30.0
TILT_MIN_DEG = -135.0
TILT_MAX_DEG = 45.0

IMU_TOPIC_SUFFIX = "/model/iris_with_standoffs/link/imu_link/sensor/imu_sensor/imu"
JOINT_STATE_TOPIC = "/gimbal/joint_state"
COMMAND_TOPIC_PREFIX = "/gimbal/cmd_"
TOPIC_WAIT_S = 300.0


class Mount(NamedTuple):
    """Gimbal angles relative to the body (degrees): +pan right, +roll right side down, +tilt up."""

    pan_deg: float
    roll_deg: float
    tilt_deg: float


def _conjugate(q: frames.Quaternion) -> frames.Quaternion:
    w, x, y, z = q
    return (w, -x, -y, -z)


def _matrix(q: frames.Quaternion) -> tuple[tuple[float, float, float], ...]:
    """Rotation matrix of a unit (w, x, y, z) quaternion."""
    w, x, y, z = q
    return (
        (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z), 2.0 * (x * z + w * y)),
        (2.0 * (x * y + w * z), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x)),
        (2.0 * (x * z - w * y), 2.0 * (y * z + w * x), 1.0 - 2.0 * (x * x + y * y)),
    )


def _lean_quaternion(lean: Lean) -> frames.Quaternion:
    return frames.quaternion_from_euler(lean[0], lean[1], 0.0)


def mount_quaternion(mount: Mount) -> frames.Quaternion:
    """Camera-in-body rotation Rz(pan) * Rx(roll) * Ry(tilt) of the yaw -> roll -> pitch chain."""
    pan = frames.quaternion_from_euler(0.0, 0.0, mount.pan_deg)
    roll = frames.quaternion_from_euler(mount.roll_deg, 0.0, 0.0)
    tilt = frames.quaternion_from_euler(0.0, mount.tilt_deg, 0.0)
    return frames.quaternion_multiply(frames.quaternion_multiply(pan, roll), tilt)


def vehicle_lean(imu_orientation: frames.Quaternion) -> Lean:
    """Vehicle (roll, pitch) in degrees, NED convention, from the airframe IMU's (w, x, y, z)
    orientation.

    Without ``<orientation_reference_frame>`` gz-sim reports the sensor's orientation relative to
    its own orientation at spawn: q = R_sensor(0)^-1 * R_sensor(t), whatever the world frame. The
    Iris ``imu_sensor`` is posed ``0 0 0 180 0 0`` on its FLU ``imu_link``, so the sensor frame is
    forward-right-down, and the spawn attitude is level. q is then the FRD body's rotation in a
    level NED-like frame whose x is the spawn heading: its Z-Y-X angles are the NED roll and
    pitch (and the yaw change since spawn, which the nose-relative gimbal does not need).
    """
    roll, pitch, _ = frames.quaternion_to_euler(imu_orientation)
    return roll, pitch


def solve_mount(lean: Lean, pitch_deg: float, yaw_deg: float) -> Mount:
    """Unclamped mount angles that put the camera at horizon ``pitch_deg``, nose-relative
    ``yaw_deg`` and level roll on a body leaning by ``lean`` (roll, pitch).

    R_body_cam = R_lean^T * Rz(yaw) * Ry(pitch), decomposed Z-X-Y as Rz(pan) * Rx(roll) * Ry(tilt);
    the roll axis never reaches +/-90 degrees, so the decomposition is unique.
    """
    target = frames.quaternion_from_euler(0.0, pitch_deg, yaw_deg)
    m = _matrix(frames.quaternion_multiply(_conjugate(_lean_quaternion(lean)), target))
    roll = math.degrees(math.asin(max(-1.0, min(1.0, m[2][1]))))
    pan = math.degrees(math.atan2(-m[0][1], m[1][1]))
    tilt = math.degrees(math.atan2(-m[2][0], m[2][2]))
    return Mount(pan, roll, tilt)


def clamp_mount(mount: Mount) -> Mount:
    """``mount`` limited to the gimbal's mechanical travel."""
    return Mount(
        max(-PAN_LIMIT_DEG, min(PAN_LIMIT_DEG, mount.pan_deg)),
        max(-ROLL_LIMIT_DEG, min(ROLL_LIMIT_DEG, mount.roll_deg)),
        max(TILT_MIN_DEG, min(TILT_MAX_DEG, mount.tilt_deg)),
    )


def joint_commands(mount: Mount) -> dict[str, float]:
    """Joint position commands (radians) for ``/gimbal/cmd_<axis>``."""
    return {
        "roll": math.radians(mount.roll_deg),
        "pitch": -math.radians(mount.tilt_deg),
        "yaw": -math.radians(mount.pan_deg),
    }


def mount_from_joints(roll_joint_rad: float, pitch_joint_rad: float, yaw_joint_rad: float) -> Mount:
    """Mount angles of measured joint positions (radians)."""
    return Mount(-math.degrees(yaw_joint_rad), math.degrees(roll_joint_rad), -math.degrees(pitch_joint_rad))


def camera_attitude(lean: Lean, mount: Mount) -> tuple[float, float, float]:
    """(pitch, roll, yaw) of the camera in degrees: pitch and roll relative to the horizon, yaw
    relative to the nose, for ``GimbalDeviceEndpoint.publish``."""
    q = frames.quaternion_multiply(_lean_quaternion(lean), mount_quaternion(mount))
    roll, pitch, yaw = frames.quaternion_to_euler(q)
    return pitch, roll, frames.wrap_deg(yaw)


def gimbal_joint_positions(joints: Any) -> tuple[float, float, float] | None:
    """(roll, pitch, yaw) joint positions (radians) from a gz.msgs.Model joint list, or None when
    one is missing."""
    positions: dict[str, float] = {}
    for joint in joints:
        positions[joint.name.rsplit("::", 1)[-1]] = joint.axis1.position
    try:
        return positions["roll_joint"], positions["pitch_joint"], positions["yaw_joint"]
    except KeyError:
        return None


class _Sensors:
    """Latest vehicle lean and gimbal joint positions, written by Gazebo transport callbacks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lean: Lean | None = None
        self._joints: tuple[float, float, float] | None = None

    def on_imu(self, message: Any) -> None:
        o = message.orientation
        lean = vehicle_lean((o.w, o.x, o.y, o.z))
        with self._lock:
            self._lean = lean

    def on_joint_state(self, message: Any) -> None:
        joints = gimbal_joint_positions(message.joint)
        if joints is None:
            return
        with self._lock:
            self._joints = joints

    def latest(self) -> tuple[Lean | None, tuple[float, float, float] | None]:
        with self._lock:
            return self._lean, self._joints


def _find_topic(node: Any, prefix: str, suffix: str, wait_s: float) -> str:
    deadline = time.monotonic() + wait_s
    while True:
        topics = [topic for topic in node.topic_list() if topic.startswith(prefix) and topic.endswith(suffix)]
        if topics:
            return topics[0]
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out locating a Gazebo topic {prefix}...{suffix}")
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    """Run the simulated gimbal device as ``horus-gz-gimbal`` or ``python -m horus.gimbal.gazebo``."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--world", required=True, help="Gazebo world name (the IMU topic is scoped to it)")
    parser.add_argument("--mavlink", default="udpout:127.0.0.1:14600",
                        help="MAVLink connection to HORUS's gimbal.connection (default: %(default)s)")
    parser.add_argument("--rate-hz", type=float, default=50.0,
                        help="command/report rate (default: %(default)s)")
    parser.add_argument("--default-pitch-deg", type=float, default=-45.0,
                        help="horizon pitch held until HORUS sends a setpoint (default: %(default)s)")
    parser.add_argument("--imu-topic",
                        help=f"airframe IMU topic (default: /world/<world>/...{IMU_TOPIC_SUFFIX})")
    parser.add_argument("--joint-state-topic", default=JOINT_STATE_TOPIC,
                        help="gimbal JointStatePublisher topic (default: %(default)s)")
    parser.add_argument("--command-topic-prefix", default=COMMAND_TOPIC_PREFIX,
                        help="joint command topics <prefix>roll|pitch|yaw (default: %(default)s)")
    args = parser.parse_args(argv)
    if args.rate_hz <= 0:
        parser.error("--rate-hz must be positive")
    logging.basicConfig(level=os.environ.get("HORUS_LOG_LEVEL", "INFO"))
    try:
        from gz.msgs10.double_pb2 import Double
        from gz.msgs10.imu_pb2 import IMU
        from gz.msgs10.model_pb2 import Model
        from gz.transport13 import Node
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Gazebo Python bindings are unavailable; install python3-gz-transport13 and python3-gz-msgs10"
        ) from exc

    node = Node()
    imu_topic = args.imu_topic or _find_topic(node, f"/world/{args.world}/", IMU_TOPIC_SUFFIX, TOPIC_WAIT_S)
    sensors = _Sensors()
    for message_type, topic, callback in ((IMU, imu_topic, sensors.on_imu),
                                          (Model, args.joint_state_topic, sensors.on_joint_state)):
        if not node.subscribe(message_type, topic, callback):
            raise RuntimeError(f"Gazebo Transport rejected topic {topic!r}")
    publishers = {}
    for axis in ("roll", "pitch", "yaw"):
        publisher = node.advertise(f"{args.command_topic_prefix}{axis}", Double)
        if not publisher:
            raise RuntimeError(f"Gazebo could not advertise {args.command_topic_prefix}{axis}")
        publishers[axis] = publisher
    logger.info("gimbal device: IMU %s, joints %s, commands %s*, MAVLink %s",
                imu_topic, args.joint_state_topic, args.command_topic_prefix, args.mavlink)

    endpoint = GimbalDeviceEndpoint(args.mavlink)
    endpoint.start()
    period = 1.0 / args.rate_hz
    command = Double()
    try:
        while True:
            lean, joints = sensors.latest()
            if lean is not None:
                pitch, yaw = endpoint.target() or (args.default_pitch_deg, 0.0)
                for axis, value in joint_commands(clamp_mount(solve_mount(lean, pitch, yaw))).items():
                    command.data = value
                    publishers[axis].publish(command)
                if joints is not None:
                    endpoint.publish(*camera_attitude(lean, mount_from_joints(*joints)))
            time.sleep(period)
    except KeyboardInterrupt:
        return 0
    finally:
        endpoint.close()


if __name__ == "__main__":
    raise SystemExit(main())
