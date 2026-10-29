"""Gimbal attitude frames of the MAVLink gimbal-device protocol (Gimbal Protocol v2).

HORUS's gimbal contract: pitch and roll relative to the horizon (pitch negative down), yaw
relative to the vehicle nose (positive right), all in degrees. MAVLink's vehicle frame is the earth
frame rotated by the vehicle yaw, so a quaternion in it decomposes (yaw-pitch-roll, Z-Y-X) straight
into that contract.
"""

from __future__ import annotations

import math
from typing import Any

Quaternion = tuple[float, float, float, float]

# GIMBAL_DEVICE_FLAGS bits.
FLAG_ROLL_LOCK = 4
FLAG_PITCH_LOCK = 8
FLAG_YAW_LOCK = 16
FLAG_YAW_IN_VEHICLE_FRAME = 32
FLAG_YAW_IN_EARTH_FRAME = 64
# Setpoints and reports HORUS uses: roll and pitch held to the horizon, yaw relative to the nose.
HORIZON_LOCKED_VEHICLE_YAW = FLAG_ROLL_LOCK | FLAG_PITCH_LOCK | FLAG_YAW_IN_VEHICLE_FRAME


# Within this many degrees of pitch ±90 roll and yaw turn the image about nearly the same axis, so
# their split amplifies a small attitude error by 1/cos(pitch); the decomposition reports roll 0.
NEAR_VERTICAL_DEG = 5.0


def quaternion_from_euler(roll_deg: float, pitch_deg: float, yaw_deg: float) -> Quaternion:
    """(w, x, y, z) of the yaw-pitch-roll (Z-Y-X) rotation, as MAVLink and ArduPilot use."""
    cr, sr = math.cos(math.radians(roll_deg) / 2.0), math.sin(math.radians(roll_deg) / 2.0)
    cp, sp = math.cos(math.radians(pitch_deg) / 2.0), math.sin(math.radians(pitch_deg) / 2.0)
    cy, sy = math.cos(math.radians(yaw_deg) / 2.0), math.sin(math.radians(yaw_deg) / 2.0)
    return (
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    )


def quaternion_to_euler(q: Quaternion) -> tuple[float, float, float]:
    """Roll, pitch and yaw (degrees) of a (w, x, y, z) quaternion, Z-Y-X order.

    Within ``NEAR_VERTICAL_DEG`` of pitch ±90 (a camera looking nearly straight down or up) roll
    and yaw rotate the image about almost the same axis and their split is ill-conditioned; the
    rotation is then returned with roll 0 and the combined rotation in yaw (yaw + roll looking
    down, yaw - roll looking up), which stays well-conditioned.
    """
    w, x, y, z = q
    # atan2 of R[2][0] against the norm of R[2][1], R[2][2] stays exact near ±90, unlike asin.
    pitch = math.degrees(
        math.atan2(2.0 * (w * y - z * x), math.hypot(2.0 * (y * z + w * x), 1.0 - 2.0 * (x * x + y * y)))
    )
    if abs(pitch) >= 90.0 - NEAR_VERTICAL_DEG:
        # Rotation-matrix elements R[0][1] and R[1][1]: -sin and cos of the combined yaw.
        yaw = math.degrees(math.atan2(-2.0 * (x * y - w * z), 1.0 - 2.0 * (x * x + z * z)))
        return 0.0, pitch, wrap_deg(yaw)
    roll = math.degrees(math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y)))
    yaw = math.degrees(math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)))
    return roll, pitch, yaw


def quaternion_multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def wrap_deg(angle_deg: float) -> float:
    """``angle_deg`` wrapped to [-180, 180)."""
    return (angle_deg + 180.0) % 360.0 - 180.0


def setpoint_quaternion(pitch_deg: float, yaw_deg: float) -> Quaternion:
    """GIMBAL_DEVICE_SET_ATTITUDE quaternion for ``HORIZON_LOCKED_VEHICLE_YAW``: level roll."""
    return quaternion_from_euler(0.0, pitch_deg, yaw_deg)


def status_angles(
    message: Any, vehicle_yaw_deg: float | None, lean_deg: tuple[float, float]
) -> tuple[float, float, float] | None:
    """Horizon-relative pitch and roll and nose-relative yaw (degrees) of a
    GIMBAL_DEVICE_ATTITUDE_STATUS, or None when the sample cannot be placed in that frame.

    The quaternion (w, x, y, z) decomposes as yaw-pitch-roll. The yaw frame comes from the flags:
    YAW_IN_VEHICLE_FRAME or YAW_IN_EARTH_FRAME, else (pre-flag devices) YAW_LOCK means earth frame.
    Earth-frame yaw is made nose-relative with the message's own ``delta_yaw`` (earth yaw =
    vehicle-frame yaw + delta_yaw) or, when NaN, ``vehicle_yaw_deg``; with neither the sample is
    unusable, never read as nose-relative. The resulting attitude is then rotated by the vehicle
    ``lean_deg`` (roll, pitch): the actual lean for a device that reports body-frame roll/pitch,
    (0, 0) for one whose roll/pitch are horizon-relative as the protocol states.
    """
    try:
        q = tuple(float(value) for value in message.q)
        flags = int(message.flags)
    except (AttributeError, TypeError, ValueError):
        return None
    if len(q) != 4 or not all(math.isfinite(value) for value in q):
        return None
    roll, pitch, yaw = quaternion_to_euler(q)  # type: ignore[arg-type]
    if flags & FLAG_YAW_IN_VEHICLE_FRAME:
        earth_frame = False
    elif flags & FLAG_YAW_IN_EARTH_FRAME:
        earth_frame = True
        delta_yaw = float(getattr(message, "delta_yaw", math.nan))
        if math.isfinite(delta_yaw):
            vehicle_yaw_deg = math.degrees(delta_yaw)
    else:
        earth_frame = bool(flags & FLAG_YAW_LOCK)
    if earth_frame:
        if vehicle_yaw_deg is None:
            return None
        yaw -= vehicle_yaw_deg
    lean = quaternion_from_euler(lean_deg[0], lean_deg[1], 0.0)
    roll, pitch, yaw = quaternion_to_euler(quaternion_multiply(lean, quaternion_from_euler(roll, pitch, yaw)))
    return pitch, roll, wrap_deg(yaw)


def setpoint_angles(message: Any) -> tuple[float, float] | None:
    """(pitch_deg, yaw_deg) of a GIMBAL_DEVICE_SET_ATTITUDE with horizon pitch and nose-relative
    yaw, or None for a setpoint in another frame (earth-frame yaw), a NaN quaternion (rate-only
    control) or a retract/neutral request."""
    try:
        q = tuple(float(value) for value in message.q)
        flags = int(message.flags)
    except (AttributeError, TypeError, ValueError):
        return None
    if len(q) != 4 or not all(math.isfinite(value) for value in q):
        return None
    if flags & FLAG_YAW_IN_EARTH_FRAME or (flags & FLAG_YAW_LOCK and not flags & FLAG_YAW_IN_VEHICLE_FRAME):
        return None
    if flags & 3:  # RETRACT or NEUTRAL
        return None
    _, pitch, yaw = quaternion_to_euler(q)  # type: ignore[arg-type]
    return pitch, wrap_deg(yaw)
