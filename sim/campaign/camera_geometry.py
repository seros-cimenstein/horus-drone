"""Gimbal camera-chain geometry: SDF poses, the composed Gazebo camera pose and projection.

Pure functions over Gazebo Pose_V model poses (``[x, y, z, yaw, roll, pitch]``, parent-relative)
and SDF poses (``(x, y, z, roll, pitch, yaw)``).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

Pose6 = tuple[float, float, float, float, float, float]
Quaternion = tuple[float, float, float, float]

CAMERA_POSE_CHAIN = ("horus_iris", "iris_with_gimbal", "gimbal", "pitch_link")
CAMERA_POSE_HISTORY = frozenset(CAMERA_POSE_CHAIN)
POSE_SOURCE = "Gazebo Pose_V hierarchy + SDF camera sensor pose"


def quat_from_rpy(roll: float, pitch: float, yaw: float) -> Quaternion:
    cr, sr = math.cos(roll / 2.0), math.sin(roll / 2.0)
    cp, sp = math.cos(pitch / 2.0), math.sin(pitch / 2.0)
    cy, sy = math.cos(yaw / 2.0), math.sin(yaw / 2.0)
    return (
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
        cr * cp * cy + sr * sp * sy,
    )


def _cross(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def quat_multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    av, bv = a[:3], b[:3]
    cross = _cross(av, bv)
    q = tuple(a[3] * bv[i] + b[3] * av[i] + cross[i] for i in range(3)) + (
        a[3] * b[3] - sum(av[i] * bv[i] for i in range(3)),
    )
    norm = math.sqrt(sum(value * value for value in q))
    return tuple(value / norm for value in q)


def quat_rotate(q: Quaternion, vector: tuple[float, float, float]) -> tuple[float, float, float]:
    first = _cross(q[:3], vector)
    second = _cross(q[:3], first)
    return tuple(vector[i] + 2.0 * (q[3] * first[i] + second[i]) for i in range(3))


def sdf_pose(element: ET.Element | None) -> Pose6:
    if element is None or not element.text:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    values = [float(value) for value in element.text.split()]
    if len(values) != 6:
        raise ValueError(f"SDF pose requires six values: {element.text!r}")
    if element.attrib.get("degrees", "false").lower() == "true":
        values[3:] = [math.radians(value) for value in values[3:]]
    return tuple(values)


def model_sdf_for_uri(uri: str, roots: Iterable[Path]) -> Path | None:
    name = (uri or "").removeprefix("model://")
    for root in roots:
        candidate = root / name / "model.sdf"
        if candidate.is_file():
            return candidate
    return None


def include_pose_and_sdf(parent_sdf: Path, child_name: str, roots: Iterable[Path]) -> tuple[Pose6, Path | None]:
    """Static pose of an included child model and its own SDF (include pose wins)."""
    for include in ET.parse(parent_sdf).getroot().iter("include"):
        if include.findtext("name") != child_name:
            continue
        child_sdf = model_sdf_for_uri(include.findtext("uri") or "", roots)
        pose_element = include.find("pose")
        if pose_element is not None and pose_element.text:
            return sdf_pose(pose_element), child_sdf
        if child_sdf is not None:
            model = ET.parse(child_sdf).getroot().find("./model")
            if model is not None:
                return sdf_pose(model.find("./pose")), child_sdf
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0), child_sdf
    raise KeyError(f"{parent_sdf} does not include model {child_name}")


def camera_sensor_pose(gimbal_sdf: Path) -> Pose6:
    """SDF pose of the gimbal model's ``camera`` sensor."""
    gimbal_model = ET.parse(gimbal_sdf).getroot().find("./model")
    if gimbal_model is None:
        raise RuntimeError(f"outdoor gimbal SDF has no model: {gimbal_sdf}")
    sensor = gimbal_model.find(".//sensor[@name='camera']")
    if sensor is None:
        raise RuntimeError(f"outdoor gimbal SDF has no camera sensor: {gimbal_sdf}")
    return sdf_pose(sensor.find("./pose"))


def camera_chain_relative_poses(world_sdf: Path, roots: Iterable[Path]) -> dict[str, Pose6]:
    """Static relative poses for the rigid camera-chain steps (joint links excluded)."""
    roots = tuple(roots)
    world_root = ET.parse(world_sdf).getroot()
    root_include = next(
        (include for include in world_root.iter("include")
         if include.findtext("name") == CAMERA_POSE_CHAIN[0]), None)
    if root_include is None:
        raise KeyError(f"world does not include {CAMERA_POSE_CHAIN[0]}")
    parent_sdf = model_sdf_for_uri(root_include.findtext("uri") or "", roots)
    if parent_sdf is None:
        raise KeyError(f"cannot resolve {root_include.findtext('uri')}")
    relative: dict[str, Pose6] = {}
    for child in CAMERA_POSE_CHAIN[1:-1]:
        pose, child_sdf = include_pose_and_sdf(parent_sdf, child, roots)
        relative[child] = pose
        if child_sdf is None:
            break
        parent_sdf = child_sdf
    return relative


def with_camera_chain_fallbacks(models: Mapping[str, Any], chain_relative: Mapping[str, Pose6]) -> dict[str, Any]:
    """Fill rigid chain models the sparse pose topic has not republished yet.

    Pose_V entries are parent-relative, so the SDF include pose is exactly the value to
    restore when a nested model that never moved (e.g. ``iris_with_gimbal`` on a parked
    drone) is missing while its parent and the joint-driven ``pitch_link`` are present.
    """
    resolved = dict(models)
    for child, relative in chain_relative.items():
        if child not in resolved:
            # SDF poses are (x, y, z, roll, pitch, yaw); pose-topic entries are
            # (x, y, z, yaw, roll, pitch).
            resolved[child] = [float(relative[0]), float(relative[1]), float(relative[2]),
                               float(relative[5]), float(relative[3]), float(relative[4])]
    return resolved


def compose_camera_pose(models: Mapping[str, Any], sensor_pose: Pose6) -> dict[str, Any] | None:
    """Compose the outdoor camera transform from sparse Gazebo model poses."""
    transforms: list[tuple[str, Pose6]] = []
    for name in CAMERA_POSE_CHAIN:
        raw = models.get(name)
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            return None
        transforms.append((name, tuple(float(value) for value in raw[:6])))
    root = transforms[0][1]
    position = list(root[:3])
    orientation = quat_from_rpy(root[4], root[5], root[3])
    for _name, pose in transforms[1:]:
        offset = quat_rotate(orientation, pose[:3])
        position = [position[index] + offset[index] for index in range(3)]
        orientation = quat_multiply(orientation, quat_from_rpy(pose[4], pose[5], pose[3]))
    offset = quat_rotate(orientation, sensor_pose[:3])
    position = [position[index] + offset[index] for index in range(3)]
    orientation = quat_multiply(orientation, quat_from_rpy(sensor_pose[3], sensor_pose[4], sensor_pose[5]))
    forward = quat_rotate(orientation, (1.0, 0.0, 0.0))
    up = quat_rotate(orientation, (0.0, 0.0, 1.0))
    yaw = math.atan2(forward[1], forward[0])
    pitch = math.atan2(forward[2], math.hypot(forward[0], forward[1]))
    base_right = (math.sin(yaw), -math.cos(yaw), 0.0)
    base_up = (-math.sin(pitch) * math.cos(yaw), -math.sin(pitch) * math.sin(yaw), math.cos(pitch))
    roll = math.atan2(sum(up[i] * base_right[i] for i in range(3)),
                      sum(up[i] * base_up[i] for i in range(3)))
    yaw_offset = math.degrees((yaw - root[3] + math.pi) % (2.0 * math.pi) - math.pi)
    return {
        "position": tuple(position), "quaternion_xyzw": orientation,
        "pitch_deg": math.degrees(pitch), "yaw_rad": yaw, "yaw_deg": math.degrees(yaw),
        "roll_deg": math.degrees(roll), "yaw_offset_deg": yaw_offset,
        "pose_source": POSE_SOURCE,
        "pose_chain": [name for name, _pose in transforms] + ["camera_sensor"],
    }


def camera_pose(sample: Mapping[str, Any] | None, sensor_pose: Pose6,
                chain_relative: Mapping[str, Pose6]) -> dict[str, Any] | None:
    """Camera pose of one ground-truth sample, restoring rigid chain models it omits."""
    models = sample.get("models", {}) if isinstance(sample, Mapping) else {}
    if not isinstance(models, Mapping):
        return None
    return compose_camera_pose(with_camera_chain_fallbacks(models, chain_relative), sensor_pose)


def project(camera_pose: Mapping[str, Any], point: tuple[float, float, float], camera: Any,
            camera_info: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """Pixel of a world point; runtime CameraInfo intrinsics win over the planner camera."""
    if camera is None:
        return None
    eye = camera_pose["position"]
    orientation = camera_pose["quaternion_xyzw"]
    delta = tuple(point[i] - eye[i] for i in range(3))
    forward = quat_rotate(orientation, (1.0, 0.0, 0.0))
    right = quat_rotate(orientation, (0.0, -1.0, 0.0))
    up = quat_rotate(orientation, (0.0, 0.0, 1.0))
    depth = sum(delta[i] * forward[i] for i in range(3))
    if depth <= 1e-6:
        return None
    # The rendered camera follows the SDF optics; CameraInfo publishes those intrinsics.
    intrinsics = camera_info or {}
    default_fx = camera.focal_px
    fx = float(intrinsics.get("fx_px", default_fx))
    fy = float(intrinsics.get("fy_px", default_fx))
    cx = float(intrinsics.get("cx_px", camera.width / 2.0))
    cy = float(intrinsics.get("cy_px", camera.height / 2.0))
    return (
        cx + fx * sum(delta[i] * right[i] for i in range(3)) / depth,
        cy - fy * sum(delta[i] * up[i] for i in range(3)) / depth,
    )


def torso(person: Mapping[str, Any], position: tuple[float, float, float]) -> tuple[float, float, float]:
    """Torso point of a person standing, sitting or lying at ``position``."""
    from sim.worldgen.plan import POSTURE_TORSO_M

    posture = str(person.get("posture", "standing"))
    if posture not in POSTURE_TORSO_M:
        posture = "standing"
    return position[0], position[1], position[2] + POSTURE_TORSO_M[posture]


def project_people(
    camera_pose: Mapping[str, Any] | None, people: Iterable[Mapping[str, Any]],
    positions: Mapping[str, tuple[float, float, float]], *, world: Any, camera: Any,
    camera_info: Mapping[str, Any] | None,
) -> tuple[dict[str, tuple[float, float] | None], dict[str, Any]]:
    """Projected torso pixel per person model plus its line-of-sight/visibility details."""
    people_by_name = {str(person.get("model")): person for person in people}
    if camera_pose is None:
        return {name: None for name in people_by_name}, {}
    projected: dict[str, tuple[float, float] | None] = {}
    details: dict[str, Any] = {}
    eye = camera_pose["position"]
    for name, person in people_by_name.items():
        position = positions.get(name)
        if position is None:
            projected[name] = None
            continue
        point = torso(person, position)
        pixel = project(camera_pose, point, camera, camera_info)
        projected[name] = pixel
        horizontal = math.hypot(point[0] - eye[0], point[1] - eye[1])
        blocker = world.blocking(eye, point)
        details[name] = {
            "position": position, "torso": point, "pixel": pixel,
            "in_frame": pixel is not None and 0 <= pixel[0] <= camera.width and 0 <= pixel[1] <= camera.height,
            "slant_m": math.dist(eye, point),
            "depression_deg": math.degrees(math.atan2(eye[2] - point[2], horizontal)),
            "blocker": None if blocker is None else blocker.name,
            "camera_pitch_deg": camera_pose["pitch_deg"], "camera_yaw_deg": camera_pose["yaw_deg"],
            "camera_roll_deg": camera_pose["roll_deg"],
        }
    return projected, details
