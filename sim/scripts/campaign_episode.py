#!/usr/bin/env python3
"""Execute one outdoor campaign episode inside the running simulation container."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import bisect
import queue
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import shutil
from typing import Any, Iterable, Mapping

ROOT = Path("/horus")
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
STOP = threading.Event()
CLEARANCE_THRESHOLD_M = 1.5
CLEARANCE_ROUNDING_TOLERANCE_M = 0.005

CAMERA_POSE_CHAIN = ("horus_iris", "iris_with_gimbal", "gimbal", "pitch_link")
CAMERA_POSE_HISTORY = frozenset(CAMERA_POSE_CHAIN)

PROJECTION_MIN_MATCHES = 3
PROJECTION_MAX_ERROR_PX = 15.0

# The MCP server boots with the world, so readiness is probed in bounded attempts.
MCP_READY_DEADLINE_S = 120.0
MCP_READY_ATTEMPT_TIMEOUT_S = 30.0

# Gazebo protobuf-text messages are echoed one message at a time; this only bounds a
# pathological stream. A message that exceeds it is flushed (never silently truncated).
TOPIC_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
TOPIC_BODY_MARKERS = ("pose {", "real_time_factor:")

# A hazard trial is only meaningful when the direct path is a real collision course:
# the 0.6 m drone radius is already subtracted, so <= -0.3 m means the hull cuts at
# least 0.3 m into the obstacle along the flown line.
HAZARD_MIN_PATH_PENETRATION_M = 0.3
# Hazard trial search: obstacle kinds worth trying, how far from the obstacle the trial starts,
# and the approach directions tried (a tangent obstacle needs a different crossing heading).
HAZARD_OBSTACLE_TOKENS = ("tree", "building", "roof", "canopy", "shelter", "vehicle")
HAZARD_APPROACH_DISTANCE_M = 8.0
HAZARD_APPROACH_HEADINGS_DEG = tuple(range(0, 360, 45))

ORBIT_RADIUS_M = 6.0
# How many GT-matched people the orbit test may visit while looking for a clear circle.
ORBIT_TARGET_ATTEMPTS = 4

# The follow mission resolves output_dir relative to the mission file (see
# horus/repl/app.py), so results land next to the mission, not at the episode root.
FOLLOW_OUTPUT_DIR = "follow_results"


def _exception_summary(exc: BaseException) -> str:
    """Flatten ExceptionGroup/TaskGroup wrappers into the underlying causes."""
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, (list, tuple)) and nested:
        inner = "; ".join(_exception_summary(item) for item in nested if isinstance(item, BaseException))
        label = f"{type(exc).__name__}: {exc}"
        return f"{label} [{inner}]" if inner else label
    return f"{type(exc).__name__}: {exc}"


def _message_brace_delta(text: str) -> int:
    """Net protobuf-text brace delta of a line, ignoring braces inside quoted strings."""
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return depth


def _topic_messages(lines: Iterable[str], *, max_message_bytes: int = TOPIC_MAX_MESSAGE_BYTES
                    ) -> Iterable[tuple[str, bool]]:
    """Split a ``gz topic -e`` protobuf-text stream into whole messages.

    Messages are delimited by blank lines, ``---`` markers or a top-level ``header {``
    once a body is present.  Nested ``header {`` blocks (every Pose_V entity has one)
    never split a message.  Yields ``(text, truncated)``; ``truncated`` is True only
    when one message exceeds ``max_message_bytes`` and had to be flushed early.
    """
    buffer: list[str] = []
    pending_bytes = 0
    depth = 0
    has_body = False

    def flush() -> str:
        nonlocal pending_bytes, depth, has_body
        text = "".join(buffer)
        buffer.clear()
        pending_bytes = 0
        depth = 0
        has_body = False
        return text

    for line in lines:
        stripped = line.strip()
        if stripped == "":
            if buffer and depth == 0:
                yield flush(), False
            continue
        if stripped == "---" or (stripped.startswith("header {") and depth == 0 and has_body):
            if buffer:
                yield flush(), False
            if stripped == "---":
                continue
        if not has_body and any(marker in line for marker in TOPIC_BODY_MARKERS):
            has_body = True
        depth = max(0, depth + _message_brace_delta(line))
        buffer.append(line)
        pending_bytes += len(line)
        if pending_bytes > max_message_bytes:
            yield flush(), True
    if buffer:
        yield flush(), False


def _hazard_path_is_collision_course(clearance_m: float) -> bool:
    """True when the planned direct path penetrates the obstacle by a clear margin."""
    return math.isfinite(clearance_m) and clearance_m <= -HAZARD_MIN_PATH_PENETRATION_M


def _mission_id_matches(selected_id: Any, captured_id: Any) -> bool:
    """Both IDs must be present and equal; two missing IDs never match."""
    return selected_id is not None and captured_id is not None and selected_id == captured_id


AMBIENT_GROUP = "ambient"


def _static_gt_people(people: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Manifest people whose spawn position stays authoritative until a trace covers them.

    The ambient scenario driver only traces group ``ambient`` agents, so everyone else
    (the follow target before its dedicated driver starts) needs the manifest fallback.
    """
    return [person for person in people
            if str(person.get("group") or AMBIENT_GROUP) != AMBIENT_GROUP]


def _pin_selection_confidence(mission: dict[str, Any]) -> None:
    """Make a generated mission's ``select_candidate`` confidence floor explicit.

    The runner already defaults to the floor when the step omits ``min_confidence``; writing it
    into the mission keeps the artifact self-describing and in lockstep with harness scoring.
    """
    from sim.scripts.procedural_campaign import selection_min_confidence

    floor = selection_min_confidence()
    steps = mission.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        selection = step.get("select_candidate")
        if isinstance(selection, Mapping):
            selection.setdefault("min_confidence", floor)


def _scale_mission_ttls(mission: dict[str, Any], scale: float) -> None:
    """Scale command TTLs in a generated mission for hosts that run the sim below real time.

    Only ``ttl_s`` values and tool-step wall waits scale; selection waits are wall-clock UI
    waits and stay fixed.
    """
    if scale == 1.0:
        return
    steps = mission.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        for key in ("ttl_s", "timeout_s"):
            value = step.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                step[key] = min(float(value) * scale, 3600.0)
        arguments = step.get("arguments")
        if isinstance(arguments, Mapping) and isinstance(arguments.get("ttl_s"), (int, float)):
            arguments["ttl_s"] = min(float(arguments["ttl_s"]) * scale, 3600.0)


def _projection_validation(errors: Iterable[float]) -> dict[str, Any]:
    valid_errors = [
        float(error) for error in errors
        if isinstance(error, (int, float)) and not isinstance(error, bool) and math.isfinite(float(error))
    ]
    median_error = statistics.median(valid_errors) if valid_errors else None
    return {
        "confident_match_count": len(valid_errors),
        "median_pixel_error": median_error,
        "minimum_confident_matches": PROJECTION_MIN_MATCHES,
        "threshold_px": PROJECTION_MAX_ERROR_PX,
        "passed": len(valid_errors) >= PROJECTION_MIN_MATCHES
        and median_error is not None and median_error <= PROJECTION_MAX_ERROR_PX,
    }


def _clearance_below_threshold(clearance_m: float) -> bool:
    return not clearance_m + CLEARANCE_ROUNDING_TOLERANCE_M >= CLEARANCE_THRESHOLD_M


def merge_pose_models(previous: Mapping[str, Any] | None, update: Mapping[str, Any]) -> dict[str, Any]:
    """Keep Gazebo's sparse Pose_V entities while advancing the snapshot timestamp."""
    combined = dict(previous.get("models", {})) if isinstance(previous, Mapping) else {}
    models = update.get("models")
    if isinstance(models, Mapping):
        combined.update(models)
    result = dict(update)
    result["models"] = combined
    return result


def interpolate_agent_positions(
    samples: list[tuple[float, Mapping[str, Any]]], sim_time: float
) -> dict[str, tuple[float, float, float]]:
    """Interpolate civilian XYZ positions between adjacent Gazebo trace samples."""
    if not samples:
        return {}
    times = [sample[0] for sample in samples]
    right = bisect.bisect_right(times, sim_time)
    if right == 0:
        left_index = right_index = 0
    elif right == len(samples):
        left_index = right_index = len(samples) - 1
    else:
        left_index, right_index = right - 1, right
    left_time, left_agents = samples[left_index]
    right_time, right_agents = samples[right_index]
    weight = 0.0 if right_time <= left_time else min(1.0, max(0.0, (sim_time - left_time) / (right_time - left_time)))
    positions: dict[str, tuple[float, float, float]] = {}
    for name in left_agents.keys() | right_agents.keys():
        left = left_agents.get(name)
        right = right_agents.get(name)
        if not isinstance(left, (list, tuple)) or len(left) < 3:
            pose = right
        elif not isinstance(right, (list, tuple)) or len(right) < 3:
            pose = left
        else:
            pose = tuple(float(left[index]) * (1.0 - weight) + float(right[index]) * weight for index in range(3))
        if isinstance(pose, (list, tuple)) and len(pose) >= 3:
            positions[str(name)] = tuple(float(pose[index]) for index in range(3))
    return positions


def _quat_from_rpy(roll: float, pitch: float, yaw: float) -> tuple[float, float, float, float]:
    cr, sr = math.cos(roll / 2.0), math.sin(roll / 2.0)
    cp, sp = math.cos(pitch / 2.0), math.sin(pitch / 2.0)
    cy, sy = math.cos(yaw / 2.0), math.sin(yaw / 2.0)
    return (
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
        cr * cp * cy + sr * sp * sy,
    )


def _quat_multiply(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    av, bv = a[:3], b[:3]
    cross = (
        av[1] * bv[2] - av[2] * bv[1],
        av[2] * bv[0] - av[0] * bv[2],
        av[0] * bv[1] - av[1] * bv[0],
    )
    q = tuple(a[3] * bv[i] + b[3] * av[i] + cross[i] for i in range(3)) + (
        a[3] * b[3] - sum(av[i] * bv[i] for i in range(3)),
    )
    norm = math.sqrt(sum(value * value for value in q))
    return tuple(value / norm for value in q)


def _quat_rotate(q: tuple[float, float, float, float], vector: tuple[float, float, float]) -> tuple[float, float, float]:
    xyz = q[:3]
    cross = lambda a, b: (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )
    first = cross(xyz, vector)
    second = cross(xyz, first)
    return tuple(vector[i] + 2.0 * (q[3] * first[i] + second[i]) for i in range(3))


def _sdf_pose(element: ET.Element | None) -> tuple[float, float, float, float, float, float]:
    if element is None or not element.text:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    values = [float(value) for value in element.text.split()]
    if len(values) != 6:
        raise ValueError(f"SDF pose requires six values: {element.text!r}")
    if element.attrib.get("degrees", "false").lower() == "true":
        values[3:] = [math.radians(value) for value in values[3:]]
    return tuple(values)


def _model_sdf_for_uri(uri: str, roots: Iterable[Path]) -> Path | None:
    name = (uri or "").removeprefix("model://")
    for root in roots:
        candidate = root / name / "model.sdf"
        if candidate.is_file():
            return candidate
    return None


def _include_pose_and_sdf(
    parent_sdf: Path, child_name: str, roots: Iterable[Path]
) -> tuple[tuple[float, float, float, float, float, float], Path | None]:
    """Static pose of an included child model and its own SDF (include pose wins)."""
    for include in ET.parse(parent_sdf).getroot().iter("include"):
        if include.findtext("name") != child_name:
            continue
        child_sdf = _model_sdf_for_uri(include.findtext("uri") or "", roots)
        pose_element = include.find("pose")
        if pose_element is not None and pose_element.text:
            return _sdf_pose(pose_element), child_sdf
        if child_sdf is not None:
            model = ET.parse(child_sdf).getroot().find("./model")
            if model is not None:
                return _sdf_pose(model.find("./pose")), child_sdf
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0), child_sdf
    raise KeyError(f"{parent_sdf} does not include model {child_name}")


def compose_camera_pose(
    models: Mapping[str, Any],
    camera_sensor_pose: tuple[float, float, float, float, float, float],
) -> dict[str, Any] | None:
    """Compose the outdoor camera transform from sparse Gazebo model poses."""
    transforms: list[tuple[str, tuple[float, float, float, float, float, float]]] = []
    for name in CAMERA_POSE_CHAIN:
        raw = models.get(name)
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            return None
        transforms.append((name, tuple(float(value) for value in raw[:6])))
    root = transforms[0][1]
    position = list(root[:3])
    orientation = _quat_from_rpy(root[4], root[5], root[3])
    for _name, pose in transforms[1:]:
        offset = _quat_rotate(orientation, pose[:3])
        position = [position[index] + offset[index] for index in range(3)]
        orientation = _quat_multiply(orientation, _quat_from_rpy(pose[4], pose[5], pose[3]))
    offset = _quat_rotate(orientation, camera_sensor_pose[:3])
    position = [position[index] + offset[index] for index in range(3)]
    orientation = _quat_multiply(
        orientation,
        _quat_from_rpy(camera_sensor_pose[3], camera_sensor_pose[4], camera_sensor_pose[5]),
    )
    forward = _quat_rotate(orientation, (1.0, 0.0, 0.0))
    up = _quat_rotate(orientation, (0.0, 0.0, 1.0))
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
        "pose_source": "Gazebo Pose_V hierarchy + SDF camera sensor pose",
        "pose_chain": [name for name, _pose in transforms] + ["camera_sensor"],
    }


class CivilianPoseTrace:
    """Incrementally read the active scenario trace and interpolate selected agents."""

    def __init__(self, path: Path, agent_names: set[str]):
        self.path = path
        self.agent_names = agent_names
        self.stream: Any = None
        self.samples: list[tuple[float, Mapping[str, Any]]] = []
        self.pending: tuple[float, Mapping[str, Any]] | None = None
        self.malformed_rows = 0

    def positions_at(self, sim_time: float) -> dict[str, tuple[float, float, float]]:
        if self.stream is None:
            if not self.path.is_file():
                return {}
            self.stream = self.path.open("r", encoding="utf-8")
        while True:
            if self.pending is not None:
                sample, self.pending = self.pending, None
            else:
                line = self.stream.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                    sample = (float(record["t"]), record["agents"])
                    if not isinstance(sample[1], Mapping):
                        raise TypeError("agents must be an object")
                    if self.agent_names:
                        sample = (sample[0], {name: sample[1][name] for name in self.agent_names if name in sample[1]})
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    self.malformed_rows += 1
                    continue
            if self.samples and sample[0] > sim_time:
                self.pending = sample
                break
            self.samples.append(sample)
            if sample[0] > sim_time:
                break
        return interpolate_agent_positions(self.samples, sim_time)

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def _signal_stop(_signum: int, _frame: Any) -> None:
    STOP.set()


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


GROUNDER_HEALTH_URL = "http://127.0.0.1:8100/health"
GROUNDER_HEALTH_TIMEOUT_S = 1.0


def _grounder_status(start: Mapping[str, Any] | None, end: Mapping[str, Any] | None,
                     mcp: Mapping[str, Any] | None = None,
                     open_vocab_candidates: int = 0) -> dict[str, Any]:
    """Episode grounder state from every available signal, recording which one decided it.

    The /health probes can both miss a service that is loading or already serving (the start
    probe runs before startup, and a probe outside the sim container cannot reach its loopback),
    so the MCP pipeline's own grounder status and observed ``open_vocab`` candidates count too.
    """
    start = start if isinstance(start, Mapping) else {}
    end = end if isinstance(end, Mapping) else {}
    mcp = mcp if isinstance(mcp, Mapping) else {}
    signals: dict[str, bool] = {
        "start_probe": bool(start.get("active")),
        "end_probe": bool(end.get("active")),
        "mcp_pipeline": str(mcp.get("status") or "") == "ok",
        "open_vocab_candidates": int(open_vocab_candidates) > 0,
    }
    active = any(signals.values())
    decided_by = [name for name, value in signals.items() if value] or ["no_signal"]
    return {
        "active": active,
        "model": end.get("model") or start.get("model") or mcp.get("model"),
        "detail": None if active else (end.get("detail") or start.get("detail") or mcp.get("reason")),
        "decided_by": decided_by,
        "signals": signals,
        "open_vocab_candidates": int(open_vocab_candidates),
        "start": dict(start),
        "end": dict(end),
        "mcp": dict(mcp),
    }


def probe_grounder(url: str = GROUNDER_HEALTH_URL,
                   timeout_s: float = GROUNDER_HEALTH_TIMEOUT_S) -> dict[str, Any]:
    """One health probe of the loopback grounding service; never raises when it is absent."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"active": False, "model": None, "detail": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        return {"active": False, "model": None, "detail": f"unexpected health response: {payload!r}"}
    model = payload.get("model")
    return {"active": True, "model": model if isinstance(model, str) else None,
            "detail": f"health probe ok at {url}"}


def _braced(text: str, label: str) -> list[str]:
    """Extract protobuf-text blocks for ``label`` while respecting quoted strings."""
    blocks: list[str] = []
    for match in re.finditer(rf"\b{re.escape(label)}\s*\{{", text):
        start = match.end()
        depth = 1
        quoted = escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[start:index])
                    break
    return blocks


def _scalar(block: str, field: str) -> float | None:
    match = re.search(rf"\b{re.escape(field)}\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)", block)
    return None if match is None else float(match.group(1))


def _parse_pose_info(text: str) -> dict[str, Any] | None:
    models: dict[str, list[float]] = {}
    for block in _braced(text, "pose"):
        name_match = re.search(r'\bname\s*:\s*"((?:\\.|[^"\\])*)"', block)
        position_blocks = _braced(block, "position")
        orientation_blocks = _braced(block, "orientation")
        if name_match is None or not position_blocks or not orientation_blocks:
            continue
        position = [_scalar(position_blocks[0], key) for key in ("x", "y", "z")]
        orientation = [_scalar(orientation_blocks[0], key) for key in ("x", "y", "z", "w")]
        if any(value is None for value in (*position, *orientation)):
            continue
        ox, oy, oz, ow = (float(value) for value in orientation)
        yaw = math.atan2(2.0 * (ow * oz + ox * oy), 1.0 - 2.0 * (oy * oy + oz * oz))
        roll = math.atan2(2.0 * (ow * ox + oy * oz), 1.0 - 2.0 * (ox * ox + oy * oy))
        sin_pitch = max(-1.0, min(1.0, 2.0 * (ow * oy - oz * ox)))
        pitch = math.asin(sin_pitch)
        models[name_match.group(1)] = [float(position[0]), float(position[1]), float(position[2]), yaw, roll, pitch]
    if not models:
        return None
    stamps = _braced(text, "stamp")
    sim_time = None
    if stamps:
        sec, nsec = _scalar(stamps[0], "sec"), _scalar(stamps[0], "nsec")
        if sec is not None:
            sim_time = sec + (nsec or 0.0) * 1e-9
    return {"wall_time": time.time(), "sim_time": sim_time, "models": models}


def _parse_stats(text: str) -> dict[str, float] | None:
    rtf = _scalar(text, "real_time_factor")
    if rtf is None or not math.isfinite(rtf):
        return None
    stamps = _braced(text, "sim_time")
    sim_time = None
    if stamps:
        sec, nsec = _scalar(stamps[0], "sec"), _scalar(stamps[0], "nsec")
        if sec is not None:
            sim_time = sec + (nsec or 0.0) * 1e-9
    return {"wall_time": time.time(), "real_time_factor": rtf, "sim_time_s": sim_time or 0.0}


def _repeated_scalars(block: str, field: str) -> list[float]:
    packed = re.search(rf"\b{re.escape(field)}\s*:\s*\[([^\]]*)\]", block)
    if packed is not None:
        return [float(value) for value in re.findall(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", packed.group(1))]
    return [
        float(match.group(1)) for match in re.finditer(
            rf"\b{re.escape(field)}\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)", block
        )
    ]


def _parse_camera_info(text: str, topic: str) -> dict[str, Any]:
    width, height = _scalar(text, "width"), _scalar(text, "height")
    if width is None or height is None or width <= 0 or height <= 0:
        raise ValueError("Gazebo CameraInfo has no valid image dimensions")
    matrices = _braced(text, "intrinsics")
    k = _repeated_scalars(matrices[0], "k") if matrices else []
    source = "intrinsics.k"
    if len(k) < 9:
        projections = _braced(text, "projection")
        p = _repeated_scalars(projections[0], "p") if projections else []
        if len(p) < 12:
            raise ValueError("Gazebo CameraInfo lacks a 3x3 intrinsic or 3x4 projection matrix")
        fx, cx, fy, cy = p[0], p[2], p[5], p[6]
        source = "projection.p"
    else:
        fx, cx, fy, cy = k[0], k[2], k[4], k[5]
    width, height = int(width), int(height)
    if fx <= 0 or fy <= 0 or not all(math.isfinite(value) for value in (fx, fy, cx, cy)):
        raise ValueError("Gazebo CameraInfo contains invalid focal lengths or principal point")
    hfov_rad = math.atan2(cx, fx) + math.atan2(width - cx, fx)
    return {
        "topic": topic, "width": width, "height": height, "fx_px": fx, "fy_px": fy,
        "cx_px": cx, "cy_px": cy, "horizontal_fov_rad": hfov_rad,
        "horizontal_fov_deg": math.degrees(hfov_rad), "intrinsics_source": source,
    }


class TopicReader:
    """Read Gazebo protobuf text through the CLI, never through Python transport bindings."""

    def __init__(self, topic: str, output: Path, parser: Any, *, max_hz: float = 5.0,
                 sample_latest: bool = False, rate_reference: TopicReader | None = None):
        self.topic, self.output, self.parser = topic, output, parser
        self.max_hz = max_hz
        self.sample_latest = sample_latest
        self.rate_reference = rate_reference
        self.last_sim_time: float | None = None
        self.rows: list[dict[str, Any]] = []
        self.latest_row: dict[str, Any] | None = None
        self.lock = threading.Lock()
        self.error: str | None = None
        self.parse_failures = 0
        self.truncated_messages = 0
        self.messages = 0
        self.process: subprocess.Popen[Any] | None = None
        if not sample_latest:
            self.process = subprocess.Popen(
                ["stdbuf", "-oL", "gz", "topic", "-e", "-t", topic], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        self.thread = threading.Thread(target=self._read, name=f"topic-{topic.rsplit('/', 1)[-1]}", daemon=True)
        self.thread.start()

    def _append(self, text: str) -> None:
        row = self.parser(text)
        if row is None:
            with self.lock:
                self.parse_failures += 1
                if self.error is None:
                    self.error = f"gz topic payload on {self.topic} did not parse"
            return
        sim_time = row.get("sim_time", row.get("sim_time_s"))
        if isinstance(sim_time, (int, float)) and self.last_sim_time is not None:
            if 0.0 <= float(sim_time) - self.last_sim_time < 1.0 / self.max_hz:
                return
        if isinstance(sim_time, (int, float)):
            self.last_sim_time = float(sim_time)
        with self.lock:
            self.messages += 1
            if isinstance(row.get("models"), Mapping):
                self.latest_row = merge_pose_models(self.latest_row, row)
            else:
                self.latest_row = row
            self.rows.append(row)
            if len(self.rows) > 20000:
                del self.rows[:1000]
        with self.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")

    def _read(self) -> None:
        if self.sample_latest:
            self._read_latest()
            return
        assert self.process is not None and self.process.stdout is not None
        for text, truncated in _topic_messages(self.process.stdout, max_message_bytes=TOPIC_MAX_MESSAGE_BYTES):
            if truncated:
                with self.lock:
                    self.truncated_messages += 1
                    self.error = (f"gz topic message on {self.topic} exceeded "
                                  f"{TOPIC_MAX_MESSAGE_BYTES} bytes; flushed early")
            self._append(text)
        if self.process.returncode not in (None, 0) and not STOP.is_set():
            self.error = f"gz topic reader exited with status {self.process.returncode}"

    def _read_latest(self) -> None:
        # Pose info can publish thousands of large snapshots per wall second. Repeated
        # one-message CLI subscriptions avoid building an unbounded stdout backlog.
        while not STOP.is_set():
            try:
                result = subprocess.run(
                    ["gz", "topic", "-e", "-n", "1", "-t", self.topic],
                    capture_output=True, text=True, timeout=5.0, check=False,
                )
                if result.returncode == 0:
                    self.error = None
                    for text, truncated in _topic_messages(result.stdout.splitlines(keepends=True),
                                                       max_message_bytes=TOPIC_MAX_MESSAGE_BYTES):
                        if truncated:
                            with self.lock:
                                self.truncated_messages += 1
                                self.error = (f"gz topic message on {self.topic} exceeded "
                                              f"{TOPIC_MAX_MESSAGE_BYTES} bytes; flushed early")
                        self._append(text)
                else:
                    self.error = f"gz topic sampler exited with status {result.returncode}"
            except subprocess.TimeoutExpired:
                self.error = "gz topic sampler timed out waiting for a pose message"
            reference = self.rate_reference.latest() if self.rate_reference is not None else None
            rtf = reference.get("real_time_factor") if isinstance(reference, Mapping) else None
            rate = float(rtf) if isinstance(rtf, (int, float)) and math.isfinite(float(rtf)) and rtf > 0 else 0.2
            interval = 1.0 / (self.max_hz * rate)
            STOP.wait(min(30.0, max(0.05, interval)))

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.rows)

    def latest(self) -> dict[str, Any] | None:
        with self.lock:
            return self.latest_row

    def status(self) -> dict[str, Any]:
        """Reader health for the episode result: errors are never swallowed."""
        with self.lock:
            return {
                "topic": self.topic,
                "error": self.error,
                "messages": self.messages,
                "parse_failures": self.parse_failures,
                "truncated_messages": self.truncated_messages,
                "samples": len(self.rows),
            }

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.thread.join(timeout=5)


SITL_CONSOLE_LOG = Path("/tmp/ArduCopter.log")


def _copy_sitl_console(directory: Path) -> str | None:
    """Copy SITL's console log (PreArm/arming reasons) beside the episode result.

    ArduPilot writes arming refusals to its own stdout, which start_sim.sh redirects to
    /tmp/ArduCopter.log inside the container; without this copy an episode that never armed
    leaves no evidence of why.
    """
    try:
        if not SITL_CONSOLE_LOG.is_file():
            return None
        destination = directory / "sitl-console.log"
        shutil.copyfile(SITL_CONSOLE_LOG, destination)
    except OSError:
        return None
    return destination.name


class Episode:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.ttl_scale = float(getattr(args, "ttl_scale", 1.0) or 1.0)
        self.kind = args.kind
        self.seed = args.seed
        self.rng = random.Random(args.seed)
        self.manifest_path = Path(args.manifest)
        self.sdf_path = Path(args.sdf)
        self.directory = Path(args.episode_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        from sim.worldgen.geometry import CollisionWorld, load_collisions
        from sim.worldgen.plan import Camera, Ground
        from sim.worldgen.people import PERSON_ASSETS
        resource_paths = (self.sdf_path.parent / "models", ROOT / "sim" / "models", Path("/opt/ardupilot_gazebo/models"))
        self.world = CollisionWorld(load_collisions(self.sdf_path, resource_paths=resource_paths))
        self.ground = Ground(self.manifest, self.manifest_path)
        self.camera = Camera()
        self.camera_info: dict[str, Any] | None = None
        self.gimbal_sdf_path = ROOT / "sim" / "models" / "horus_gimbal_small_3d_outdoor" / "model.sdf"
        gimbal_model = ET.parse(self.gimbal_sdf_path).getroot().find("./model")
        if gimbal_model is None:
            raise RuntimeError(f"outdoor gimbal SDF has no model: {self.gimbal_sdf_path}")
        sensor = gimbal_model.find(".//sensor[@name='camera']")
        if sensor is None:
            raise RuntimeError(f"outdoor gimbal SDF has no camera sensor: {self.gimbal_sdf_path}")
        self.camera_sensor_pose = _sdf_pose(sensor.find("./pose"))
        self.camera_chain_relative: dict[str, tuple[float, float, float, float, float, float]] = {}
        try:
            self.camera_chain_relative = self._camera_chain_relative_poses(resource_paths)
        except (ET.ParseError, OSError, KeyError, ValueError) as exc:
            print(f"camera chain SDF fallback unavailable: {type(exc).__name__}: {exc}", flush=True)
        self.person_assets = PERSON_ASSETS
        self.people = list(self.manifest.get("people", []))
        # Grounder evidence: candidates seeded by the open-vocabulary pass can only come from it.
        self.open_vocab_candidates = 0
        self.mcp_grounder: dict[str, Any] = {}
        self._person_models = {str(person.get("model", "")) for person in self.people}
        self.follow = self.manifest.get("follow", {})
        self.transit_alt = float(self.manifest["transit_altitude_m"])
        self.tests: dict[str, dict[str, Any]] = {}
        self.path_history: list[dict[str, Any]] = []
        self.start_wall = time.monotonic()
        self.start_epoch = time.time()
        self.start_sim: float | None = None
        self._session: Any = None
        self._gt: TopicReader | None = None
        self._rtf: TopicReader | None = None
        self._server: subprocess.Popen[Any] | None = None
        self._server_log: Any = None
        self._mission_logs: list[str] = []
        self._takeoff_altitude_samples: list[dict[str, Any]] = []
        run_id_path = ROOT / "sim" / "run" / ".active"
        run_id = run_id_path.read_text(encoding="utf-8").strip() if run_id_path.is_file() else ""
        self.civilian_trace_path = ROOT / "sim" / "run" / run_id / "logs" / "civilians.jsonl"
        self._civilian_trace = CivilianPoseTrace(
            self.civilian_trace_path, {str(person.get("model")) for person in self.people}
        )
        # The ambient driver only traces group "ambient" agents; the follow target is driven
        # later by its own mission and traced to follow_trace.jsonl next to the episode.
        self.follow_trace_path = self.directory / "follow_trace.jsonl"
        follow_group = str(self.follow.get("group") or "")
        follow_models = {str(person.get("model")) for person in self.people
                         if follow_group and str(person.get("group")) == follow_group}
        self._follow_trace = CivilianPoseTrace(self.follow_trace_path, follow_models)
        # People outside the ambient trace (e.g. the follow target before its driver starts)
        # have no live pose topic of their own; their manifest spawn position is authoritative
        # until a trace covers them.
        self._static_gt_people = _static_gt_people(self.people)

    @property
    def world_name(self) -> str:
        return str(self.manifest["world"])

    def _start_server(self) -> None:
        environment = dict(os.environ)
        environment["HORUS_CONFIG"] = f"/horus/config/horus_{self.kind}.yaml"
        environment["HORUS_RUN_DIR"] = str(self.directory)
        environment["HORUS_EVENT_LOG"] = str(self.directory / "events.jsonl")
        self._server_log = (self.directory / "mcp.log").open("w", encoding="utf-8")
        self._server = subprocess.Popen(["/opt/horus-venv/bin/horus-mcp"], cwd=ROOT, env=environment,
                                        stdout=self._server_log, stderr=subprocess.STDOUT, start_new_session=True)

    async def _tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        result = await self._session.call_tool(name, dict(arguments or {}))
        blocks = list(getattr(result, "content", []) or [])
        if getattr(result, "isError", False):
            detail = "\n".join(str(getattr(block, "text", "")) for block in blocks)
            raise RuntimeError(detail or f"MCP tool {name} failed")
        text = "\n".join(str(block.text) for block in blocks if getattr(block, "type", None) == "text")
        payload: Any = json.loads(text) if text else {}
        images = [block for block in blocks if getattr(block, "type", None) == "image"]
        if isinstance(payload, dict):
            payload["_image_data"] = [getattr(image, "data", None) for image in images]
            perception = payload.get("perception")
            grounder = perception.get("grounder") if isinstance(perception, Mapping) else None
            if isinstance(grounder, Mapping):
                self.mcp_grounder = dict(grounder)
        return payload

    async def _wait_tool_task(self, response: Any, *, max_wall_s: float | None = None,
                              observer: Any = None) -> dict[str, Any]:
        if not isinstance(response, Mapping) or not response.get("task_id"):
            return dict(response) if isinstance(response, Mapping) else {"result": response}
        task_id = str(response["task_id"])
        deadline = time.monotonic() + (max_wall_s if max_wall_s is not None else 120.0)
        result: dict[str, Any] = {"task_id": task_id, "submitted": dict(response)}
        next_observation = time.monotonic()
        while time.monotonic() < deadline and not STOP.is_set():
            status = await self._tool("get_task_status", {"task_id": task_id})
            if not isinstance(status, Mapping):
                raise RuntimeError(f"invalid status for task {task_id}: {status}")
            result = dict(status)
            self._record_gt()
            if status.get("status") in TERMINAL:
                return result
            if observer is not None and time.monotonic() >= next_observation:
                await observer()
                next_observation = time.monotonic() + 1.0
            await asyncio.sleep(0.25)
        try:
            await self._tool("cancel", {"task_id": task_id})
        except Exception:
            pass
        raise TimeoutError(f"task {task_id} did not reach a terminal status before wall deadline")

    def _scaled_ttl(self, ttl_s: float) -> float:
        """Scale a command TTL for hosts that run the sim below real time.

        The executor's TTL is wall-clock, so on a slow host a fixed TTL expires mid-motion
        (e.g. a 34 m take-off at RTF 0.2 needs ~240 s). ``--ttl-scale`` keeps the same
        sim-time budget; 1.0 leaves command timing unchanged.
        """
        return min(float(ttl_s) * self.ttl_scale, 3600.0)

    async def _skill(self, name: str, arguments: Mapping[str, Any], *, timeout_s: float | None = None,
                     observer: Any = None) -> dict[str, Any]:
        prepared = dict(arguments)
        if "ttl_s" in prepared:
            prepared["ttl_s"] = self._scaled_ttl(prepared["ttl_s"])
        submitted = await self._tool(name, prepared)
        max_wall_s = None if timeout_s is None else timeout_s * self.ttl_scale
        return await self._wait_tool_task(submitted, max_wall_s=max_wall_s, observer=observer)

    def _gt_latest(self) -> dict[str, Any] | None:
        return None if self._gt is None else self._gt.latest()

    def _drone(self, sample: Mapping[str, Any] | None = None) -> tuple[float, ...] | None:
        sample = sample or self._gt_latest()
        if not isinstance(sample, Mapping):
            return None
        models = sample.get("models", {})
        pose = models.get("horus_iris") if isinstance(models, Mapping) else None
        if not isinstance(pose, list) or len(pose) < 4:
            return None
        point = tuple(float(value) for value in pose[:6])
        if not all(math.isfinite(value) for value in point):
            return None
        return point

    def _record_takeoff_altitude(self, state: Any) -> None:
        vehicle = state.get("vehicle", {}) if isinstance(state, Mapping) else {}
        pose = vehicle.get("pose") if isinstance(vehicle, Mapping) else None
        raw_up = pose.get("up_m") if isinstance(pose, Mapping) else None
        mcp_up = float(raw_up) if isinstance(raw_up, (int, float)) and math.isfinite(float(raw_up)) else None
        gt_sample = self._gt_latest()
        drone = self._drone(gt_sample)
        gt_z = drone[2] if drone is not None else None
        self._takeoff_altitude_samples.append({
            "wall_time": time.time(),
            "sim_time": gt_sample.get("sim_time") if isinstance(gt_sample, Mapping) else None,
            "mcp_up_m": mcp_up,
            "gazebo_z_m": gt_z,
            "mcp_minus_gazebo_m": None if mcp_up is None or gt_z is None else mcp_up - gt_z,
        })


    def _record_gt(self) -> None:
        sample = self._gt_latest()
        if sample is not None:
            if self.path_history and sample.get("wall_time") == self.path_history[-1].get("wall_time"):
                return
            models = sample.get("models", {})
            # The target people are recorded too: the orbit test measures its ground-truth
            # radius from this history, so keeping only the camera chain made it unscorable.
            tracked = {
                name: models[name] for name in (CAMERA_POSE_HISTORY | self._person_models)
                if isinstance(models, Mapping) and name in models
            }
            self.path_history.append({
                "wall_time": sample.get("wall_time"), "sim_time": sample.get("sim_time"), "models": tracked,
            })
            if self.start_sim is None and isinstance(sample.get("sim_time"), (int, float)):
                self.start_sim = float(sample["sim_time"])

    def _people_gt(
        self, sample: Mapping[str, Any] | None = None, *, sim_time: float | None = None
    ) -> dict[str, tuple[float, float, float]]:
        sample = sample or self._gt_latest()
        if sim_time is None and isinstance(sample, Mapping):
            raw_time = sample.get("sim_time")
            sim_time = float(raw_time) if isinstance(raw_time, (int, float)) else None
        output = self._civilian_trace.positions_at(sim_time) if sim_time is not None else {}
        if sim_time is not None:
            # A driven follow target is not in the ambient trace; its own trace wins.
            output.update(self._follow_trace.positions_at(sim_time))
        models = sample.get("models", {}) if isinstance(sample, Mapping) else {}
        if isinstance(models, Mapping):
            for person in self.people:
                model = str(person.get("model", ""))
                pose = models.get(model)
                if model not in output and isinstance(pose, (list, tuple)) and len(pose) >= 3:
                    output[model] = (float(pose[0]), float(pose[1]), float(pose[2]))
        for person in self._static_gt_people:
            model = str(person.get("model", ""))
            if model in output:
                continue
            position = person.get("position")
            if isinstance(position, (list, tuple)) and len(position) >= 2:
                east, north = float(position[0]), float(position[1])
                up = self.ground.height(east, north) + float(person.get("z_offset") or 0.0)
                output[model] = (east, north, up)
        return output

    def reader_status(self) -> dict[str, Any]:
        """Topic-reader health so silent drops/parse failures reach the episode result."""
        def status(reader: TopicReader | None) -> dict[str, Any] | None:
            return None if reader is None else reader.status()

        return {
            "gt": status(self._gt),
            "rtf": status(self._rtf),
            "ambient_trace": {
                "path": str(self.civilian_trace_path),
                "malformed_rows": self._civilian_trace.malformed_rows,
            },
            "follow_trace": {
                "path": str(self.follow_trace_path),
                "malformed_rows": self._follow_trace.malformed_rows,
            },
        }

    def _follow_results_dir(self) -> Path:
        """Where the REPL writes the follow mission's re-acquisition record."""
        return self.directory / "mission" / FOLLOW_OUTPUT_DIR

    def gt_coverage(self) -> dict[str, Any]:
        """Report which manifest people currently resolve to a GT position, and from where."""
        sample = self._gt_latest()
        positions = self._people_gt(sample) if sample is not None else {}
        models = [str(person.get("model", "")) for person in self.people]
        missing = [model for model in models if model not in positions]
        return {
            "people": len(models), "with_gt": len(models) - len(missing), "missing": missing,
            "ambient_trace": str(self.civilian_trace_path),
            "ambient_trace_exists": self.civilian_trace_path.is_file(),
            "follow_trace": str(self.follow_trace_path),
            "follow_trace_exists": self.follow_trace_path.is_file(),
        }

    def _torso(self, person: Mapping[str, Any], position: tuple[float, float, float]) -> tuple[float, float, float]:
        from sim.worldgen.plan import POSTURE_TORSO_M
        posture = str(person.get("posture", "standing"))
        if posture not in POSTURE_TORSO_M:
            posture = "standing"
        return position[0], position[1], position[2] + POSTURE_TORSO_M[posture]

    def _camera_chain_relative_poses(
        self, roots: Iterable[Path]
    ) -> dict[str, tuple[float, float, float, float, float, float]]:
        """Static relative poses for the rigid camera-chain steps (joint links excluded)."""
        world_root = ET.parse(self.sdf_path).getroot()
        root_include = next(
            (include for include in world_root.iter("include")
             if include.findtext("name") == CAMERA_POSE_CHAIN[0]), None)
        if root_include is None:
            raise KeyError(f"world does not include {CAMERA_POSE_CHAIN[0]}")
        parent_sdf = _model_sdf_for_uri(root_include.findtext("uri") or "", roots)
        if parent_sdf is None:
            raise KeyError(f"cannot resolve {root_include.findtext('uri')}")
        relative: dict[str, tuple[float, float, float, float, float, float]] = {}
        for child in CAMERA_POSE_CHAIN[1:-1]:
            pose, child_sdf = _include_pose_and_sdf(parent_sdf, child, roots)
            relative[child] = pose
            if child_sdf is None:
                break
            parent_sdf = child_sdf
        return relative

    def _with_camera_chain_fallbacks(self, models: Mapping[str, Any]) -> dict[str, Any]:
        """Fill rigid chain models the sparse pose topic has not republished yet.

        Pose_V entries are parent-relative, so the SDF include pose is exactly the value to
        restore when a nested model that never moved (e.g. ``iris_with_gimbal`` on a parked
        drone) is missing while its parent and the joint-driven ``pitch_link`` are present.
        """
        resolved = dict(models)
        for child, relative in self.camera_chain_relative.items():
            if child not in resolved:
                # SDF poses are (x, y, z, roll, pitch, yaw); pose-topic entries are
                # (x, y, z, yaw, roll, pitch).
                resolved[child] = [float(relative[0]), float(relative[1]), float(relative[2]),
                                   float(relative[5]), float(relative[3]), float(relative[4])]
        return resolved

    def _camera_pose(self, sample: Mapping[str, Any] | None) -> dict[str, Any] | None:
        models = sample.get("models", {}) if isinstance(sample, Mapping) else {}
        if not isinstance(models, Mapping):
            return None
        return compose_camera_pose(self._with_camera_chain_fallbacks(models), self.camera_sensor_pose)

    def _project(self, camera_pose: Mapping[str, Any], point: tuple[float, float, float]) -> tuple[float, float] | None:
        if self.camera is None:
            return None
        eye = camera_pose["position"]
        orientation = camera_pose["quaternion_xyzw"]
        delta = tuple(point[i] - eye[i] for i in range(3))
        forward = _quat_rotate(orientation, (1.0, 0.0, 0.0))
        right = _quat_rotate(orientation, (0.0, -1.0, 0.0))
        up = _quat_rotate(orientation, (0.0, 0.0, 1.0))
        depth = sum(delta[i] * forward[i] for i in range(3))
        if depth <= 1e-6:
            return None
        # The rendered camera follows the SDF optics; CameraInfo publishes those intrinsics.
        intrinsics = self.camera_info or {}
        default_fx = self.camera.focal_px
        fx = float(intrinsics.get("fx_px", default_fx))
        fy = float(intrinsics.get("fy_px", default_fx))
        cx = float(intrinsics.get("cx_px", self.camera.width / 2.0))
        cy = float(intrinsics.get("cy_px", self.camera.height / 2.0))
        return (
            cx + fx * sum(delta[i] * right[i] for i in range(3)) / depth,
            cy - fy * sum(delta[i] * up[i] for i in range(3)) / depth,
        )

    def _frame_sim_time(self, frame: Mapping[str, Any]) -> float | None:
        captured = frame.get("capture_sim_time")
        if isinstance(captured, (float, int)) and math.isfinite(float(captured)):
            return float(captured)
        sample = frame.get("gt_sample")
        sim_time = sample.get("sim_time") if isinstance(sample, Mapping) else None
        return float(sim_time) if isinstance(sim_time, (int, float)) else None

    def _person_projection(
        self, frame: Mapping[str, Any]
    ) -> tuple[dict[str, tuple[float, float] | None], dict[str, Any], dict[str, Any] | None]:
        camera_pose = self._camera_pose(frame.get("gt_sample"))
        positions = self._people_gt(frame.get("gt_sample"), sim_time=self._frame_sim_time(frame))
        projected: dict[str, tuple[float, float] | None] = {}
        details: dict[str, Any] = {}
        people_by_name = {str(person.get("model")): person for person in self.people}
        if camera_pose is None:
            return {name: None for name in people_by_name}, details, None
        eye = camera_pose["position"]
        for name, person in people_by_name.items():
            position = positions.get(name)
            if position is None:
                projected[name] = None
                continue
            torso = self._torso(person, position)
            pixel = self._project(camera_pose, torso)
            projected[name] = pixel
            distance = math.dist(eye, torso)
            horizontal = math.hypot(torso[0] - eye[0], torso[1] - eye[1])
            depression = math.degrees(math.atan2(eye[2] - torso[2], horizontal))
            blocker = self.world.blocking(eye, torso)
            in_frame = pixel is not None and 0 <= pixel[0] <= self.camera.width and 0 <= pixel[1] <= self.camera.height
            details[name] = {
                "position": position, "torso": torso, "pixel": pixel, "in_frame": in_frame,
                "slant_m": distance, "depression_deg": depression,
                "blocker": None if blocker is None else blocker.name,
                "camera_pitch_deg": camera_pose["pitch_deg"], "camera_yaw_deg": camera_pose["yaw_deg"],
                "camera_roll_deg": camera_pose["roll_deg"],
            }
        return projected, details, camera_pose

    def _note_candidate_sources(self, candidates: Any) -> None:
        """Count open-vocabulary seeds, which only exist when the grounder answered."""
        if not isinstance(candidates, Iterable):
            return
        for candidate in candidates:
            if isinstance(candidate, Mapping) and str(candidate.get("source")) == "open_vocab":
                self.open_vocab_candidates += 1

    def _detection_rows(self, frame: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[Any], dict[str, Any]]:
        from sim.scripts.procedural_campaign import match_candidates_to_people, candidate_bbox
        candidates = frame.get("candidate_table", [])
        self._note_candidate_sources(candidates)
        projected, details, camera_pose = self._person_projection(frame)
        matching = match_candidates_to_people(candidates, projected)
        matching["camera_pose"] = camera_pose
        by_name = {str(person.get("model")): person for person in self.people}
        rows: list[dict[str, Any]] = []
        for name, person in by_name.items():
            detail = details.get(name)
            if not detail or not detail["in_frame"]:
                continue
            candidate = matching["matches"].get(name)
            attrs = candidate.get("attributes", {}) if isinstance(candidate, Mapping) else {}
            bbox = candidate_bbox(candidate) if isinstance(candidate, Mapping) else None
            expected_asset = self.person_assets.get(str(person.get("asset", "")), {})
            rows.append({
                "model": name, "role": person.get("role"), "posture": person.get("posture", "unknown"),
                "clothing": person.get("clothing", "unknown"), "expected_shirt_colour": person.get("shirt_colour_attribute"),
                "asset_shirt_colour": expected_asset.get("shirt_colour_attribute"),
                "detected": candidate is not None,
                "candidate_id": candidate.get("id") if isinstance(candidate, Mapping) else None,
                "class_label": candidate.get("class_name") if isinstance(candidate, Mapping) else None,
                "shirt_colour": attrs.get("shirt_colour") if isinstance(attrs, Mapping) else None,
                "bbox_height_px": None if bbox is None else bbox[3] - bbox[1],
                "slant_m": detail["slant_m"], "depression_deg": detail["depression_deg"],
                "los_blocked": detail["blocker"] is not None, "blocker": detail["blocker"],
                "slant_bin": "≤6" if detail["slant_m"] <= 6 else "6–9" if detail["slant_m"] <= 9
                    else "9–12" if detail["slant_m"] <= 12 else ">12",
                "projected_center_px": detail["pixel"], "center_error_px": matching["center_error_px"].get(name),
                "projection_pitch_deg": detail["camera_pitch_deg"],
                "projection_yaw_deg": detail["camera_yaw_deg"], "projection_roll_deg": detail["camera_roll_deg"],
                "raw_candidate": candidate,
            })
        return rows, matching["unmatched_candidates"], matching

    def _save_frame(self, frame: Mapping[str, Any], name: str) -> str | None:
        blobs = frame.get("_image_data", [])
        if not isinstance(blobs, list) or not blobs or not isinstance(blobs[0], str):
            return None
        import base64
        target = self.directory / "frames" / f"{name}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(blobs[0], validate=True))
        return str(target.relative_to(ROOT))

    async def _observe(self, name: str, *, save_frame: bool = True) -> dict[str, Any]:
        frame = await self._tool("list_candidates")
        if not isinstance(frame, dict) or not isinstance(frame.get("candidate_table"), list):
            raise RuntimeError(f"list_candidates returned invalid payload: {frame}")
        if save_frame:
            frame["saved_frame"] = self._save_frame(frame, name)
        frame["capture_wall_time"] = time.time()
        frame["gt_sample"] = self._gt_latest()
        sample = frame["gt_sample"]
        sim_time = sample.get("sim_time") if isinstance(sample, Mapping) else None
        wall_time = sample.get("wall_time") if isinstance(sample, Mapping) else None
        observation_age = frame.get("observation_age_s")
        rtf_sample = self._rtf.latest() if self._rtf is not None else None
        rtf = rtf_sample.get("real_time_factor") if isinstance(rtf_sample, Mapping) else None
        if (isinstance(sim_time, (int, float)) and isinstance(wall_time, (int, float))
                and isinstance(observation_age, (int, float)) and isinstance(rtf, (int, float))
                and math.isfinite(float(rtf)) and float(rtf) > 0):
            capture_wall = frame["capture_wall_time"] - max(0.0, float(observation_age))
            frame["capture_sim_time"] = float(sim_time) + (capture_wall - float(wall_time)) * float(rtf)
        elif isinstance(sim_time, (int, float)):
            frame["capture_sim_time"] = float(sim_time)
        self._record_gt()
        _json_write(self.directory / "observations" / f"{name}.json", frame)
        return frame


    def _vantage_candidates(self) -> list[Any]:
        from sim.worldgen.plan import person_point, plan_vantage
        available = [person for person in self.people if person.get("posture") in {"standing", "sitting", "lying"}]
        if not available:
            return []
        order = self.rng.sample(available, k=min(len(available), max(self.args.vantages * 2, self.args.vantages)))
        vantages: list[Any] = []
        blocked = None
        for person in order:
            posture = str(person["posture"])
            point = person_point(person, self.ground, posture)
            vantage = plan_vantage(self.world, self.ground, self.camera, str(person["model"]), point,
                                   self.rng, transit_alt=self.transit_alt, require_los=False,
                                   min_clearance_m=1.5, attempts=160)
            if vantage is None:
                continue
            if vantage.blocker and blocked is None:
                blocked = vantage
            elif vantage.blocker is None:
                vantages.append(vantage)
            if len(vantages) >= self.args.vantages and blocked is not None:
                break
        if blocked is not None:
            if len(vantages) >= self.args.vantages:
                vantages[-1] = blocked
            else:
                vantages.append(blocked)
        return vantages[:self.args.vantages]

    async def _navigate(self, eye: tuple[float, float, float], yaw_rad: float) -> dict[str, Any]:
        from sim.worldgen.plan import cruise_legs, heading_deg, legs_clearance, ned_pose
        start = self._drone()
        if start is None:
            return {"passed": False, "reason": "no ground-truth drone pose"}
        legs = cruise_legs(start[:3], eye, heading_deg(yaw_rad), self.transit_alt)
        clearance = legs_clearance(self.world, start[:3], legs, drone_radius_m=0.6)
        if _clearance_below_threshold(clearance):
            return {"passed": False, "reason": f"planned transit legs clearance {clearance:.3f} m < "
                    f"{CLEARANCE_THRESHOLD_M:.1f} m",
                    "min_clearance_m": clearance, "legs": legs}
        statuses = []
        for point, heading in legs:
            response = await self._skill("goto", {"pose": ned_pose(point), "heading_deg": heading, "ttl_s": 120.0},
                                         timeout_s=180.0)
            statuses.append(response)
            if response.get("status") != "succeeded":
                return {"passed": False, "reason": f"goto ended {response.get('status')}",
                        "min_clearance_m": clearance, "leg_results": statuses}
        self._record_gt()
        return {"passed": True, "min_clearance_m": clearance, "leg_results": statuses}

    async def _wait_gt(self, point: tuple[float, float, float], *, tolerance: float = 0.5,
                       settle_sim_s: float = 3.0, timeout_s: float = 180.0) -> bool:
        deadline = time.monotonic() + timeout_s
        settled_at: float | None = None
        last_sim: float | None = None
        while time.monotonic() < deadline and not STOP.is_set():
            sample = self._gt_latest()
            drone = self._drone(sample)
            sim_time = sample.get("sim_time") if isinstance(sample, Mapping) else None
            if isinstance(sim_time, (int, float)) and drone is not None and math.dist(drone[:3], point) <= tolerance:
                if settled_at is None:
                    settled_at = float(sim_time)
                if float(sim_time) - settled_at >= settle_sim_s:
                    return True
            else:
                settled_at = None
            last_sim = float(sim_time) if isinstance(sim_time, (int, float)) else last_sim
            await asyncio.sleep(0.2)
        return False

    async def _ensure_airborne(self) -> bool:
        state = await self._tool("get_state")
        vehicle = state.get("vehicle", {}) if isinstance(state, Mapping) else {}
        pose = vehicle.get("pose") if isinstance(vehicle, Mapping) else None
        if isinstance(vehicle, Mapping) and vehicle.get("armed") and isinstance(pose, Mapping):
            return True
        start = self._drone()
        if start is None:
            return False
        self._takeoff_altitude_samples = []
        self._record_takeoff_altitude(state)

        async def observe_takeoff() -> None:
            self._record_takeoff_altitude(await self._tool("get_state"))

        attempts: list[dict[str, Any]] = []
        statustext: list[Any] = []
        # A fresh sim needs tens of seconds of sim time for GPS/EKF convergence before the
        # vehicle may arm; at a low real-time factor that can outlast the skill TTL, so retry
        # once instead of failing the episode's first test outright.
        for _attempt in range(2):
            state = await self._tool("get_state")
            vehicle = state.get("vehicle", {}) if isinstance(state, Mapping) else {}
            if isinstance(vehicle, Mapping) and isinstance(vehicle.get("untrusted_statustext"), list):
                statustext = list(vehicle["untrusted_statustext"])
            result = await self._skill("take_off", {"altitude_m": self.transit_alt, "ttl_s": 180.0},
                                       timeout_s=240.0, observer=observe_takeoff)
            self._record_takeoff_altitude(await self._tool("get_state"))
            attempts.append(self._skill_summary(result))
            if result.get("status") == "succeeded":
                break
        self._takeoff_attempts = attempts
        self._takeoff_statustext = statustext
        if attempts[-1].get("status") != "succeeded":
            return False
        return await self._wait_gt((start[0], start[1], self.transit_alt),
                                   tolerance=0.5, settle_sim_s=1.0, timeout_s=300.0)

    @staticmethod
    def _skill_summary(result: Mapping[str, Any]) -> dict[str, Any]:
        """Keep a task result's failure evidence without any embedded image payloads."""
        payload = result.get("result") if isinstance(result.get("result"), Mapping) else {}
        return {"task_id": result.get("task_id"), "skill": result.get("skill"),
                "status": result.get("status"), "error": result.get("error") or payload.get("error"),
                "result_status": payload.get("status"), "clamp_reason": result.get("clamp_reason")}

    def _takeoff_failure(self, reason: str) -> dict[str, Any]:
        """Takeoff failure payload that keeps the skill results and SITL status texts visible."""
        return {"passed": False, "reason": reason,
                "takeoff_attempts": list(getattr(self, "_takeoff_attempts", [])),
                "vehicle_statustext": list(getattr(self, "_takeoff_statustext", []))}

    async def test_perception(self) -> dict[str, Any]:
        if not await self._ensure_airborne():
            return {"passed": False, "reason": "Gazebo ground truth did not reach transit altitude after takeoff",
                    "ground_truth_drone": self._drone(), "takeoff_altitude_samples": self._takeoff_altitude_samples,
                    "person_observations": []}
        vantages = self._vantage_candidates()
        if not vantages:
            return {"passed": False, "reason": "no collision-safe vantage could be planned", "person_observations": []}
        observations: list[dict[str, Any]] = []
        clearance_skips: list[dict[str, Any]] = []
        projection_errors: list[float] = []
        projection_frame_ids: list[Any] = []
        vantage_frames: list[dict[str, Any]] = []
        for index, vantage in enumerate(vantages, 1):
            route = await self._navigate(vantage.eye, vantage.yaw_rad)
            if not route.get("passed"):
                clearance_skips.append({"target": vantage.target, **route})
                continue
            settled = await self._wait_gt(vantage.eye, tolerance=0.5, settle_sim_s=3.0)
            if not settled:
                clearance_skips.append({"target": vantage.target, "reason": "GT position did not settle within 0.5 m"})
                continue
            frame = await self._observe(f"vantage-{index:02d}")
            rows, false_positives, matches = self._detection_rows(frame)
            camera_pose = matches["camera_pose"]
            frame_record = {
                "vantage": index, "frame_id": frame.get("frame_id"), "timestamp": frame.get("timestamp"),
                "capture_sim_time": frame.get("capture_sim_time"), "target": vantage.target,
                "planned_eye": vantage.eye, "planned_slant_m": vantage.slant_m,
                "planned_depression_deg": vantage.depression_deg, "planned_blocker": vantage.blocker,
                "actual_gt_camera_pose": camera_pose, "route_min_clearance_m": route.get("min_clearance_m"),
                "matched": {name: item.get("id") for name, item in matches["matches"].items()},
                "false_positives": false_positives, "people": rows,
                "raw_candidates": frame.get("candidate_table"), "saved_frame": frame.get("saved_frame"),
            }
            vantage_frames.append(frame_record)
            observations.extend(rows)
            frame_errors = [
                float(row["center_error_px"]) for row in rows
                if row.get("detected") and isinstance(row.get("center_error_px"), (int, float))
                and isinstance(row.get("raw_candidate"), Mapping)
                and float(row["raw_candidate"].get("confidence", 0.0)) >= 0.5
            ]
            if frame_errors:
                projection_errors.extend(frame_errors)
                projection_frame_ids.append(frame.get("frame_id"))
        from sim.scripts.procedural_campaign import score_detection_rows
        metrics = score_detection_rows(observations)
        blocked_seen = any(row.get("los_blocked") for row in observations)
        projection_validation = _projection_validation(projection_errors)
        projection_validation.update({
            "frames_with_confident_matches": projection_frame_ids,
            "pose_source": "Gazebo Pose_V hierarchy + SDF camera sensor pose",
            "camera_info": self.camera_info,
        })
        projection_ok = projection_validation["passed"]
        projection_median = projection_validation["median_pixel_error"]
        confident_match_count = projection_validation["confident_match_count"]
        camera_poses = [
            row["actual_gt_camera_pose"] for row in vantage_frames
            if isinstance(row.get("actual_gt_camera_pose"), Mapping)
        ]
        pitch_values = [float(pose["pitch_deg"]) for pose in camera_poses]
        yaw_offset_values = [float(pose["yaw_offset_deg"]) for pose in camera_poses
                             if isinstance(pose.get("yaw_offset_deg"), (float, int))]
        camera_pose_summary = {
            "vantages": len(camera_poses),
            "pitch_deg": {
                "mean": statistics.mean(pitch_values) if pitch_values else None,
                "std": statistics.pstdev(pitch_values) if pitch_values else None,
            },
            "yaw_offset_deg": {
                "mean": statistics.mean(yaw_offset_values) if yaw_offset_values else None,
                "std": statistics.pstdev(yaw_offset_values) if yaw_offset_values else None,
            },
        }
        result = {
            "passed": bool(observations) and projection_ok,
            "reason": None if observations and projection_ok else
                (f"only {confident_match_count} confident matched people across all perception vantages; "
                 f"at least {PROJECTION_MIN_MATCHES} required") if confident_match_count < PROJECTION_MIN_MATCHES
                else (f"episode-wide camera projection median error {projection_median:.1f} px exceeds "
                      f"{PROJECTION_MAX_ERROR_PX:.0f} px"),
            "requested_vantages": self.args.vantages, "visited_vantages": len(vantage_frames),
            "vantages": len(vantages), "clearance_skips": clearance_skips,
            "occluded_vantage_included": any(vantage.blocker is not None for vantage in vantages),
            "occluded_people_observed": blocked_seen, "projection_validation": projection_validation,
            "camera_pose_summary": camera_pose_summary, "metrics": metrics,
            "person_observations": observations, "vantage_frames": vantage_frames,
            "takeoff_altitude_samples": self._takeoff_altitude_samples,
        }
        return result
    async def _capture_intended(self, person: Mapping[str, Any], label: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        from sim.worldgen.plan import person_point, plan_vantage
        from sim.scripts.procedural_campaign import select_largest_attribute_candidate, selection_rule
        point = person_point(dict(person), self.ground, str(person.get("posture", "standing")))
        vantage = plan_vantage(self.world, self.ground, self.camera, str(person.get("model")), point,
                               self.rng, transit_alt=self.transit_alt, require_los=True,
                               min_clearance_m=1.5, attempts=400)
        if vantage is None:
            return None, None, "no clear collision-safe vantage for target"
        route = await self._navigate(vantage.eye, vantage.yaw_rad)
        if not route.get("passed"):
            return None, None, route.get("reason", "unsafe transit")
        if not await self._wait_gt(vantage.eye, tolerance=0.5, settle_sim_s=3.0):
            return None, None, "GT position failed to settle"
        frame = await self._observe(label)
        projected, details, camera_pose = self._person_projection(frame)
        matching = __import__("sim.scripts.procedural_campaign", fromlist=["match_candidates_to_people"]).match_candidates_to_people(
            frame["candidate_table"], projected)
        candidates = frame["candidate_table"]
        self._note_candidate_sources(candidates)
        wanted = person.get("shirt_colour_attribute")
        # Same rule (and confidence floor) the runner's select_candidate applies.
        selected = select_largest_attribute_candidate(candidates, equals=wanted)
        matched_target = matching["matches"].get(str(person.get("model")))
        return frame, {"selected": selected, "target_match": matched_target,
                       "match_details": details.get(str(person.get("model"))), "all_matches": matching["matches"],
                       "selection_rule": selection_rule("shirt_colour", wanted),
                       "camera_pose": camera_pose}, None

    async def test_select(self) -> dict[str, Any]:
        if not await self._ensure_airborne():
            return self._takeoff_failure("vehicle could not take off")
        intended = None
        if self.kind == "urban":
            red = [person for person in self.people if str(person.get("clothing", "")).lower() == "red"
                   and person.get("group", "ambient") == "ambient"]
            intended = red[0] if len(red) == 1 else None
            if intended is None:
                # Prefer a unique red-attribute crowd target but record ambiguity explicitly.
                red = [person for person in self.people if person.get("shirt_colour_attribute") == "red"
                       and person.get("group", "ambient") == "ambient"]
                intended = red[0] if red else None
        else:
            intended = next((person for person in self.people if person.get("model") == self.follow.get("model")), None)
        if intended is None:
            return {"passed": False, "reason": "manifest has no intended uniquely coloured target"}
        frame, selection, reason = await self._capture_intended(intended, "select-vantage")
        if frame is None or selection is None:
            return {"passed": False, "reason": reason}
        selected = selection["selected"]
        correct = isinstance(selected, Mapping) and isinstance(selection["target_match"], Mapping) \
            and selected.get("id") == selection["target_match"].get("id")
        return {"passed": bool(correct), "reason": None if correct else "largest exact-colour candidate did not match intended target",
                "intended_model": intended.get("model"), "expected_attribute": intended.get("shirt_colour_attribute"),
                "selected_candidate": selected, "selected_matches_intended": correct,
                "selection_rule": selection.get("selection_rule"),
                "target_candidate": selection["target_match"], "frame_id": frame.get("frame_id"),
                "frame_timestamp": frame.get("timestamp"), "confusions": [
                    {"model": person.get("model"), "clothing": person.get("clothing"),
                     "reported_attribute": person.get("shirt_colour_attribute")}
                    for person in self.people if person.get("shirt_colour_attribute") == intended.get("shirt_colour_attribute")
                    and person.get("model") != intended.get("model")],
                "raw_candidates": frame.get("candidate_table"), "saved_frame": frame.get("saved_frame"),
                "camera_pose": selection["camera_pose"]}

    async def _observe_matched_person(
        self, person: Mapping[str, Any], label: str
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        """Fly to a safe vantage and return the live candidate GT-matched to ``person``."""
        from sim.worldgen.plan import person_point, plan_vantage
        from sim.scripts.procedural_campaign import match_candidates_to_people
        point = person_point(person, self.ground, str(person.get("posture", "standing")))
        vantage = plan_vantage(self.world, self.ground, self.camera, str(person["model"]), point, self.rng,
                               transit_alt=self.transit_alt, require_los=True, min_clearance_m=1.5, attempts=220)
        if vantage is None:
            return None, "no collision-safe vantage"
        route = await self._navigate(vantage.eye, vantage.yaw_rad)
        if not route.get("passed"):
            return None, str(route.get("reason") or "unsafe transit")
        if not await self._wait_gt(vantage.eye, tolerance=0.5, settle_sim_s=3.0):
            return None, "GT position failed to settle"
        frame = await self._observe(label)
        projected, _details, _camera_pose = self._person_projection(frame)
        result = match_candidates_to_people(frame["candidate_table"], projected)
        candidate = result["matches"].get(str(person.get("model")))
        return (candidate, None) if isinstance(candidate, Mapping) else (None, "no current candidate GT-matched")

    async def _matched_target(self, posture: str | None = None) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, str | None]:
        pool = [person for person in self.people if posture is None or person.get("posture") == posture]
        self.rng.shuffle(pool)
        for index, person in enumerate(pool[:max(12, self.args.vantages * 3)]):
            candidate, _reason = await self._observe_matched_person(person, f"target-{posture or 'any'}-{index:02d}")
            if candidate is not None:
                return person, candidate, None
        return None, None, f"no current candidate GT-matched to {posture or 'any'} target"

    def _orbit_circle_clearance(self, target: tuple[float, float, float], *, altitude: float) -> float:
        """Minimum static clearance on the orbit circle flown at ``altitude``."""
        radius = ORBIT_RADIUS_M
        count = 48
        circle = [(target[0] + radius * math.cos(2 * math.pi * i / count),
                   target[1] + radius * math.sin(2 * math.pi * i / count), altitude) for i in range(count + 1)]
        return min((self.world.segment_clearance(a, b, radius=0.6)[0] for a, b in zip(circle, circle[1:])),
                   default=math.inf)

    async def _matched_orbit_target(
        self
    ) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None, str | None, list[dict[str, Any]], float | None]:
        """Choose a GT-matched visible target whose orbit circle is clear of static geometry.

        Tries several people before giving up: the first GT-matched person may stand too close
        to a tree, while another person in the same world supports the 6 m / 1.5 m orbit.
        """
        pool = list(self.people)
        self.rng.shuffle(pool)
        attempts: list[dict[str, Any]] = []
        matched_seen = 0
        best: tuple[Mapping[str, Any], Mapping[str, Any], float] | None = None
        for index, person in enumerate(pool[:max(12, self.args.vantages * 3)]):
            if matched_seen >= ORBIT_TARGET_ATTEMPTS:
                break
            candidate, reason = await self._observe_matched_person(person, f"orbit-target-{index:02d}")
            if candidate is None:
                attempts.append({"model": person.get("model"), "reason": reason})
                continue
            matched_seen += 1
            current = self._drone()
            target = self._people_gt().get(str(person.get("model")))
            if current is None or target is None:
                attempts.append({"model": person.get("model"), "target_id": candidate.get("id"),
                                 "reason": "missing current GT pose"})
                continue
            clearance = self._orbit_circle_clearance(target, altitude=current[2])
            if _clearance_below_threshold(clearance):
                attempts.append({"model": person.get("model"), "target_id": candidate.get("id"),
                                 "min_static_clearance_m": clearance,
                                 "reason": f"orbit circle clearance below {CLEARANCE_THRESHOLD_M:.1f} m"})
                if best is None or clearance > best[2]:
                    best = (person, candidate, clearance)
                continue
            attempts.append({"model": person.get("model"), "target_id": candidate.get("id"),
                             "min_static_clearance_m": clearance, "reason": None})
            return person, candidate, None, attempts, clearance
        if best is not None:
            return (best[0], best[1],
                    f"orbit circle clearance below {CLEARANCE_THRESHOLD_M:.1f} m for every visible "
                    f"candidate ({matched_seen} matched)", attempts, best[2])
        return None, None, "no current candidate GT-matched to any target", attempts, None

    async def _approach_one(self, person: Mapping[str, Any], candidate: Mapping[str, Any], *,
                            target_size: float | None, label: str) -> dict[str, Any]:
        from sim.worldgen.plan import POSTURE_TOP_M
        start_index = len(self.path_history)
        started = self._gt_latest()
        arguments: dict[str, Any] = {"id": candidate["id"], "dist_m": 5.0, "ttl_s": 120.0}
        if target_size is not None:
            arguments["target_size_m"] = target_size
        try:
            status = await self._skill("approach", arguments, timeout_s=240.0)
        except Exception as exc:
            return {"passed": False, "reason": f"approach action failed: {exc}", "arguments": arguments}
        self._record_gt()
        sample = self._gt_latest()
        drone = self._drone(sample)
        positions = self._people_gt(sample)
        target_pos = positions.get(str(person.get("model")))
        torso = self._torso(person, target_pos) if target_pos else None
        range_error = None if drone is None or torso is None else math.dist(drone[:3], torso) - 5.0
        route_clearance = self._trajectory_clearance(start_index)
        return {"label": label, "passed": status.get("status") == "succeeded", "arguments": arguments,
                "task_status": status, "gt_final_camera_to_torso_m": None if drone is None or torso is None else math.dist(drone[:3], torso),
                "distance_error_vs_5m_m": range_error, "static_clearance_along_gt_path_m": route_clearance,
                "target_model": person.get("model"), "target_posture": person.get("posture"),
                "target_visible_size_m": target_size, "start_gt": started, "end_gt": sample}

    def _trajectory_clearance(self, start_index: int = 0) -> float | None:
        points: list[tuple[float, float, float]] = []
        for sample in self.path_history[max(0, start_index):]:
            drone = self._drone(sample)
            if drone is not None:
                points.append(drone[:3])
        minimum = math.inf
        if not points:
            return None
        for point in points:
            clearance, _ = self.world.clearance(point)
            minimum = min(minimum, clearance - 0.6)
        for first, second in zip(points, points[1:]):
            clearance, _ = self.world.segment_clearance(first, second, radius=0.6)
            minimum = min(minimum, clearance)
        return minimum

    async def test_approach(self) -> dict[str, Any]:
        if not await self._ensure_airborne():
            return {**self._takeoff_failure("vehicle could not take off"), "trials": []}
        trials: list[dict[str, Any]] = []
        standing, visible, reason = await self._matched_target("standing")
        if standing is None or visible is None:
            return {"passed": False, "reason": reason, "trials": trials}
        trials.append(await self._approach_one(standing, visible, target_size=None, label="standing-no-target-size"))
        posture = next((p for p in ("lying", "sitting") if any(item.get("posture") == p for item in self.people)), None)
        if posture is None:
            return {"passed": False, "reason": "manifest contains no lying or sitting target", "trials": trials}
        prone, candidate, reason = await self._matched_target(posture)
        if prone is None or candidate is None:
            trials.extend([{"label": f"{posture}-no-target-size", "passed": False, "status": "unobservable", "reason": reason},
                           {"label": f"{posture}-with-target-size", "passed": False, "status": "unobservable", "reason": reason}])
            return {"passed": False, "reason": reason, "trials": trials}
        trials.append(await self._approach_one(prone, candidate, target_size=None, label=f"{posture}-no-target-size"))
        from sim.worldgen.plan import POSTURE_TOP_M
        visible_size = POSTURE_TOP_M[posture]
        # Approach may have moved away or the tracker ID may have expired; reacquire a live ID.
        reacquired, candidate2, reason = await self._matched_target(posture)
        if reacquired is not None and candidate2 is not None:
            trials.append(await self._approach_one(reacquired, candidate2, target_size=visible_size,
                                                   label=f"{posture}-with-target-size"))
        else:
            trials.append({"label": f"{posture}-with-target-size", "passed": False,
                           "status": "unobservable", "reason": reason, "target_visible_size_m": visible_size})
        return {"passed": all(trial.get("passed") is True for trial in trials), "trials": trials}

    async def test_orbit(self) -> dict[str, Any]:
        if not await self._ensure_airborne():
            return self._takeoff_failure("vehicle could not take off")
        person, candidate, reason, attempts, minimum = await self._matched_orbit_target()
        if person is None or candidate is None:
            return {"passed": False, "reason": reason, "status": "skipped",
                    "target_attempts": attempts, "min_static_clearance_m": minimum}
        current = self._drone()
        target = self._people_gt().get(str(person.get("model")))
        if current is None or target is None:
            return {"passed": False, "reason": "missing current GT pose", "target_attempts": attempts}
        radius = ORBIT_RADIUS_M
        minimum = self._orbit_circle_clearance(target, altitude=current[2])
        if _clearance_below_threshold(minimum):
            # The circle was re-checked at the settled vantage altitude; trust the stricter value.
            return {"passed": False, "status": "skipped",
                    "reason": f"orbit circle clearance below {CLEARANCE_THRESHOLD_M:.1f} m",
                    "min_static_clearance_m": minimum, "target_attempts": attempts}
        start_index = len(self.path_history)
        try:
            status = await self._skill("orbit", {"id": candidate["id"], "radius_m": radius, "ttl_s": 40.0},
                                       timeout_s=220.0)
        except Exception as exc:
            return {"passed": False, "reason": f"orbit failed: {exc}", "min_static_clearance_m": minimum,
                    "target_attempts": attempts}
        self._record_gt()
        samples = self.path_history[start_index:]
        distances: list[float] = []
        visible = 0
        for sample in samples:
            drone = self._drone(sample)
            poses = sample.get("models", {})
            target_pose = poses.get(str(person.get("model"))) if isinstance(poses, Mapping) else None
            if drone is None or not isinstance(target_pose, list) or len(target_pose) < 3:
                continue
            torso = self._torso(person, tuple(float(v) for v in target_pose[:3]))
            distances.append(math.hypot(drone[0] - torso[0], drone[1] - torso[1]))
            if self.camera.in_frame(drone[:3], drone[3], torso):
                visible += 1
        mean = statistics.mean(distances) if distances else None
        std = statistics.pstdev(distances) if len(distances) > 1 else (0.0 if distances else None)
        return {"passed": status.get("status") in {"succeeded", "expired"} and mean is not None,
                "task_status": status, "target_model": person.get("model"), "requested_radius_m": radius,
                "gt_radius_mean_m": mean, "gt_radius_std_m": std,
                "gt_radius_error_m": None if mean is None else mean - radius,
                "target_in_frame_fraction": visible / len(distances) if distances else None,
                "gt_samples": len(distances), "min_static_clearance_m": minimum,
                "target_attempts": attempts}

    def _scan_rectangles(self) -> list[tuple[float, float, float, float, str]]:
        areas = self.manifest.get("areas", {})
        rectangles: list[tuple[float, float, float, float, str]] = []
        if self.kind == "urban" and isinstance(areas.get("plaza"), Mapping):
            center = areas["plaza"].get("center", [0, 0])
            half = float(areas["plaza"].get("half_size", 9.0)) + float(areas.get("sidewalk_offset_m", 0.0))
            rectangles.append((float(center[0]) - half, float(center[0]) + half,
                               float(center[1]) - half, float(center[1]) + half, "plaza-and-sidewalks"))
            half_small = max(6.0, half - float(areas.get("sidewalk_offset_m", 0.0)) / 2)
            rectangles.append((float(center[0]) - half_small, float(center[0]) + half_small,
                               float(center[1]) - half_small, float(center[1]) + half_small, "plaza"))
        elif self.kind == "forest" and isinstance(areas.get("clearing"), Mapping):
            # Shared with the worldgen gate: clearing_scan_rectangle() is the same inscribed square.
            from sim.worldgen.validation import clearing_scan_rectangle
            rect = clearing_scan_rectangle(areas)
            if rect is None:
                return rectangles
            half = (rect[1] - rect[0]) / 2.0
            center = ((rect[0] + rect[1]) / 2.0, (rect[2] + rect[3]) / 2.0)
            for scale in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
                core = half * scale
                label = "clearing" if scale == 1.0 else f"clearing-core-{int(scale * 100)}pct"
                rectangles.append((float(center[0]) - core, float(center[0]) + core,
                                   float(center[1]) - core, float(center[1]) + core, label))
        return rectangles


    def _scan_clearance(self, rect: tuple[float, float, float, float, str], altitude: float) -> float:
        xmin, xmax, ymin, ymax, _label = rect
        spacing = 2.0
        xs = [xmin + (xmax - xmin) * i / max(1, math.ceil((xmax - xmin) / spacing))
              for i in range(max(1, math.ceil((xmax - xmin) / spacing)) + 1)]
        ys = [ymin + (ymax - ymin) * i / max(1, math.ceil((ymax - ymin) / spacing))
              for i in range(max(1, math.ceil((ymax - ymin) / spacing)) + 1)]
        minimum = math.inf
        grid = [(x, y, altitude) for y in ys for x in xs]
        for a, b in zip(grid, grid[1:]):
            # The row boundary jump is also checked, conservatively.
            minimum = min(minimum, self.world.segment_clearance(a, b, radius=0.6)[0])
        for first, second in zip(ys, ys[1:]):
            for x in xs:
                minimum = min(minimum, self.world.segment_clearance((x, first, altitude),
                    (x, second, altitude), radius=0.6)[0])
        return minimum


    def _plan_scan_area(self) -> tuple[tuple[float, float, float, float, str], float, float] | None:
        for rect in self._scan_rectangles():
            for altitude in (10.0, 12.0, 8.0):
                clearance = self._scan_clearance(rect, altitude)
                if clearance >= 2.0:
                    return rect, altitude, clearance
        return None


    async def test_scan(self) -> dict[str, Any]:
        if not await self._ensure_airborne():
            return self._takeoff_failure("vehicle could not take off")
        plan = self._plan_scan_area()
        if plan is None:
            rects = self._scan_rectangles()
            return {"passed": False, "status": "skipped", "reason": "no scan rectangle has ≥2 m clearance",
                    "candidates": [{"rect": rect, "clearance_10m": self._scan_clearance(rect, 10.0)}
                                   for rect in rects]}
        selected, selected_alt, clearance = plan
        xmin, xmax, ymin, ymax, label = selected
        inside_people = [str(person.get("model")) for person in self.people
                         if (position := person.get("position")) is not None
                         and isinstance(position, (list, tuple)) and len(position) >= 2
                         and xmin <= float(position[0]) <= xmax and ymin <= float(position[1]) <= ymax]
        if not inside_people:
            # A clear rectangle with nobody inside can never score detection coverage; skipping
            # with an explicit reason is honest, and world_report keeps this from being routine.
            return {"passed": False, "status": "skipped",
                    "reason": "no manifest person inside the planned scan area",
                    "area_label": label,
                    "area": {"north_min_m": ymin, "north_max_m": ymax,
                             "east_min_m": xmin, "east_max_m": xmax,
                             "altitude_m": selected_alt, "spacing_m": 6.0},
                    "static_clearance_m": clearance}
        area = {"north_min_m": ymin, "north_max_m": ymax, "east_min_m": xmin, "east_max_m": xmax,
                "altitude_m": selected_alt, "spacing_m": 6.0}
        # Position at the first safe scan corner by using ordinary guarded goto.
        from sim.worldgen.plan import heading_deg, ned_pose
        start = self._drone()
        if start is None:
            return {"passed": False, "reason": "GT drone pose unavailable"}
        scan_start = (xmin, ymin, float(selected_alt))
        route = await self._navigate(scan_start, math.atan2(ymax - ymin, xmax - xmin))
        if not route.get("passed"):
            return {"passed": False, "reason": route.get("reason"), "clearance_m": clearance}
        self._record_gt()
        seen: set[str] = set()
        total_models: set[str] = set()
        polls: list[dict[str, Any]] = []

        async def observe_scan() -> None:
            frame = await self._observe(f"scan-{len(polls) + 1:04d}", save_frame=False)
            rows, false_positives, _matching = self._detection_rows(frame)
            inside = []
            for person in self.people:
                pos = self._people_gt(frame.get("gt_sample")).get(str(person.get("model")))
                if pos and xmin <= pos[0] <= xmax and ymin <= pos[1] <= ymax:
                    total_models.add(str(person.get("model")))
                    inside.append(str(person.get("model")))
                    row = next((row for row in rows if row["model"] == person.get("model") and row["detected"]), None)
                    if row:
                        seen.add(str(person.get("model")))
            polls.append({"frame_id": frame.get("frame_id"), "timestamp": frame.get("timestamp"),
                          "matched_in_rectangle": inside, "matched_models": [row["model"] for row in rows if row["detected"]],
                          "false_positives": false_positives})
        scan_start_sim = self._sim_now()
        submitted = await self._tool("scan", {"area": area, "ttl_s": self._scaled_ttl(240.0)})
        try:
            task = await self._wait_tool_task(submitted, max_wall_s=600.0, observer=observe_scan)
        except Exception as exc:
            task = {"status": "failed", "error": str(exc)}
        await observe_scan()
        coverage = len(seen) / len(total_models) if total_models else 0.0
        return {"passed": task.get("status") == "succeeded" and coverage > 0.0,
                "reason": None if task.get("status") == "succeeded" and coverage > 0 else "scan task or detection coverage failed",
                "area_label": label, "area": area, "spacing_m": 6.0, "altitude_m": selected_alt,
                "static_clearance_m": clearance, "task_result": task,
                "clamp_reasons": task.get("clamp_reason", task.get("clamp_reasons")),
                "coverage": {"inside_people": len(total_models), "matched": len(seen), "fraction": coverage,
                             "models": sorted(total_models), "matched_models": sorted(seen)},
                "duration_sim_s": self._sim_now() - scan_start_sim,
                "poll_count": len(polls), "polls": polls}

    def _sim_now(self) -> float:
        sample = self._gt_latest()
        value = sample.get("sim_time") if isinstance(sample, Mapping) else None
        return float(value) if isinstance(value, (int, float)) else 0.0

    async def _wait_disarmed(self, limit_sim_s: float = 240.0) -> dict[str, Any]:
        start_sim = self._sim_now()
        armed_seen = False
        latest_state: Any = None
        wall_deadline = time.monotonic() + 900.0
        while time.monotonic() < wall_deadline and not STOP.is_set():
            latest_state = await self._tool("get_state")
            vehicle = latest_state.get("vehicle", {}) if isinstance(latest_state, Mapping) else {}
            armed = bool(vehicle.get("armed")) if isinstance(vehicle, Mapping) else False
            armed_seen = armed_seen or armed
            self._record_gt()
            drone = self._drone()
            ground = self.ground.height(drone[0], drone[1]) if drone is not None else 0.0
            if not armed and (armed_seen or drone is None or drone[2] <= ground + 0.5):
                return {"disarmed": True, "elapsed_sim_s": self._sim_now() - start_sim,
                        "state": latest_state, "armed_seen": armed_seen}
            elapsed = self._sim_now() - start_sim
            if elapsed >= limit_sim_s:
                break
            await asyncio.sleep(1.0)
        return {"disarmed": False, "elapsed_sim_s": self._sim_now() - start_sim,
                "state": latest_state, "armed_seen": armed_seen,
                "reason": "did not disarm within 240 simulation seconds"}

    def _airborne_altitude_m(self) -> float | None:
        """Ground-truth height above the local ground, or None without a GT drone pose."""
        drone = self._drone()
        if drone is None:
            return None
        return drone[2] - self.ground.height(drone[0], drone[1])

    async def _rth(self, *, require_airborne: bool = False) -> dict[str, Any]:
        start = self._gt_latest()
        start_sim = self._sim_now()
        if require_airborne:
            altitude = self._airborne_altitude_m()
            if altitude is None or altitude < 0.5:
                # RTH on a grounded vehicle disarms trivially and proves nothing about the
                # return-to-launch behaviour; the takeoff failure is already reported by the
                # test that attempted it, so this is a skip, not a second failure.
                return {"passed": False, "status": "skipped",
                        "reason": "vehicle was not airborne before RTH",
                        "airborne_altitude_m": altitude, "start_gt": start,
                        "rth_response": None, "disarm": None,
                        "landing_error_to_pad_m": None, "duration_sim_s": 0.0}
        response = await self._tool("rth")
        disarm = await self._wait_disarmed(240.0)
        self._record_gt()
        final = self._gt_latest()
        drone = self._drone(final)
        pad_error = math.hypot(drone[0], drone[1]) if drone is not None else None
        elapsed = self._sim_now() - start_sim
        return {"rth_response": response, "disarm": disarm, "start_gt": start, "final_gt": final,
                "landing_error_to_pad_m": pad_error, "duration_sim_s": elapsed,
                "passed": bool(disarm["disarmed"] and pad_error is not None and pad_error <= 2.0)}

    async def test_follow(self) -> dict[str, Any]:
        from sim.worldgen.plan import follow_mission, plan_follow
        landing = await self._rth()
        if not landing["passed"]:
            return {"passed": False, "reason": "pre-follow RTH did not land within 2 m", "pre_follow_rth": landing}
        plan = plan_follow(self.world, self.ground, self.manifest)
        if (_clearance_below_threshold(plan.lane_clearance_m)
                or _clearance_below_threshold(plan.column_clearance_m)):
            return {"passed": False, "status": "skipped",
                    "reason": f"planned follow path clearance below {CLEARANCE_THRESHOLD_M:.1f} m",
                    "plan": {"lane_clearance_m": plan.lane_clearance_m, "column_clearance_m": plan.column_clearance_m,
                             "predicted_occlusion_s": plan.occluded_s}, "pre_follow_rth": landing}
        mission_dir = self.directory / "mission"
        mission_dir.mkdir(parents=True, exist_ok=True)
        mission_path = mission_dir / f"{self.world_name}-follow.yaml"
        trace = "/" + str((self.directory / "follow_trace.jsonl").relative_to(Path("/")))
        # The REPL resolves output_dir against the mission file, so results appear in
        # <episode>/mission/<FOLLOW_OUTPUT_DIR>; the harness must read that same path.
        mission_output_dir = FOLLOW_OUTPUT_DIR
        mission = follow_mission(self.manifest, plan, manifest_path=self.manifest_path, mission_path=mission_path,
                                 repo_root=ROOT, trace_path=trace, output_dir=mission_output_dir)
        _pin_selection_confidence(mission)
        _scale_mission_ttls(mission, self.ttl_scale)
        import yaml
        mission_path.write_text(yaml.safe_dump(mission, sort_keys=False), encoding="utf-8")
        before = time.time()
        intended_model = self.follow.get("model")
        follow_person = next((person for person in self.people if person.get("model") == intended_model), None)
        expected_colour = self.follow.get("shirt_colour_attribute")
        env = dict(os.environ)
        env["HORUS_CONFIG"] = f"/horus/config/horus_{self.kind}.yaml"
        env["PYTHONUNBUFFERED"] = "1"
        cmd = ["/opt/horus-venv/bin/horus", "--server-url", "http://127.0.0.1:8000/mcp",
               "--config", f"config/horus_{self.kind}.yaml", "--mission", str(mission_path)]
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=1)
        output_queue: queue.Queue[str | None] = queue.Queue()

        def collect_output() -> None:
            assert proc.stdout is not None
            for output_line in proc.stdout:
                output_queue.put(output_line)
            output_queue.put(None)

        output_thread = threading.Thread(target=collect_output, name="follow-mission-output", daemon=True)
        output_thread.start()
        output_lines: list[str] = []
        output_closed = False
        selection_check: dict[str, Any] | None = None
        selection_event_wall: float | None = None
        mission_timed_out = False
        mission_deadline = time.monotonic() + 1500.0
        from sim.scripts.procedural_campaign import match_candidates_to_people
        while not output_closed or proc.poll() is None:
            if time.monotonic() >= mission_deadline:
                mission_timed_out = True
                proc.terminate()
                try:
                    proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                break
            try:
                output_line = await asyncio.to_thread(output_queue.get, True, 0.25)
            except queue.Empty:
                continue
            if output_line is None:
                output_closed = True
                continue
            output_lines.append(output_line)
            if selection_check is not None or "select_candidate:" not in output_line:
                continue
            selected_text = output_line.split("select_candidate:", 1)[1].strip()
            try:
                runner_selected = json.loads(selected_text)
            except json.JSONDecodeError:
                continue
            event_path = self.directory / "events.jsonl"
            if event_path.is_file():
                for event_line in event_path.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(event_line)
                        if event.get("tool") == "list_candidates":
                            selection_event_wall = datetime.fromisoformat(
                                str(event["timestamp"]).replace("Z", "+00:00")
                            ).timestamp()
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue
            frame = await self._observe("follow-runner-selection")
            projected, details, camera_pose = self._person_projection(frame)
            matching = match_candidates_to_people(frame["candidate_table"], projected)
            matched_model = next((model for model, candidate in matching["matches"].items()
                                  if candidate.get("id") == runner_selected.get("id")), None)
            attrs = runner_selected.get("attributes", {})
            runner_attribute = attrs.get("shirt_colour") if isinstance(attrs, Mapping) else None
            selection_check = {
                "selection_event_wall_time": selection_event_wall,
                "frame_capture_wall_time": frame.get("capture_wall_time"),
                "frame_id": frame.get("frame_id"), "frame_timestamp": frame.get("timestamp"),
                "frame_saved": frame.get("saved_frame"), "selected_candidate": runner_selected,
                "selected_id_gt_matched_model": matched_model, "intended_model": intended_model,
                "expected_shirt_colour": expected_colour, "runner_attribute": runner_attribute,
                "projected_target": details.get(str(intended_model)),
                "camera_pose": camera_pose,
                "passed": bool(matched_model == intended_model and runner_attribute == expected_colour),
            }
        output_thread.join(timeout=5.0)
        if mission_timed_out:
            output_lines.append("mission subprocess wall timeout\\n")
        (self.directory / "follow-mission.log").write_text("".join(output_lines), encoding="utf-8")
        results_dir = self._follow_results_dir()
        results = sorted(results_dir.glob("*.json")) if results_dir.exists() else []
        reacquire = json.loads(results[-1].read_text(encoding="utf-8")) if results else None
        selected = reacquire.get("selected_target") if isinstance(reacquire, Mapping) else None
        follow_event_epoch = before
        event_path = self.directory / "events.jsonl"
        if event_path.is_file():
            for line in event_path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                    if event.get("tool") == "follow":
                        follow_event_epoch = datetime.fromisoformat(
                            str(event["timestamp"]).replace("Z", "+00:00")
                        ).timestamp()
                        break
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
        selection_check = selection_check or {
            "intended_model": intended_model, "expected_shirt_colour": expected_colour,
            "selected_id_gt_matched_model": None, "passed": False,
            "reason": "runner selection output was not observed with a contemporaneous frame",
        }
        selection_check["mission_selected_target"] = selected
        selected_id = selected.get("id") if isinstance(selected, Mapping) else None
        captured_id = selection_check.get("selected_candidate", {}).get("id") \
            if isinstance(selection_check.get("selected_candidate"), Mapping) else None
        selection_check["mission_id_matches_gt_checked_id"] = _mission_id_matches(selected_id, captured_id)
        if selected is None:
            selection_check["reason"] = (selection_check.get("reason")
                                         or "follow mission produced no re-acquisition record")
        selection_check["passed"] = bool(selection_check.get("passed")
                                         and selection_check["mission_id_matches_gt_checked_id"])
        selected_gt = selection_check
        gt_samples = self._gt.snapshot() if self._gt else []
        longest = current = 0.0
        prior = None
        follow_samples = [sample for sample in gt_samples
                          if float(sample.get("wall_time", 0.0)) >= follow_event_epoch]
        minimum_clearance = math.inf
        path_points: list[tuple[float, float, float]] = []
        for sample in follow_samples:
            drone = self._drone(sample)
            poses = sample.get("models", {})
            target = poses.get(str(intended_model)) if isinstance(poses, Mapping) else None
            if drone is None:
                continue
            path_points.append(drone[:3])
            if not isinstance(target, list) or len(target) < 3:
                continue
            target_torso = self._torso(follow_person or {"posture": "standing"}, tuple(float(v) for v in target[:3]))
            blocked = self.world.blocking(drone[:3], target_torso) is not None
            sim_time = sample.get("sim_time")
            if prior is not None and isinstance(sim_time, (int, float)) and isinstance(prior, (int, float)):
                dt = max(0.0, float(sim_time) - float(prior))
                current = current + dt if blocked else 0.0
                longest = max(longest, current)
            prior = sim_time
        for point in path_points:
            clearance, _ = self.world.clearance(point)
            minimum_clearance = min(minimum_clearance, clearance - 0.6)
        for first, second in zip(path_points, path_points[1:]):
            clearance, _ = self.world.segment_clearance(first, second, radius=0.6)
            minimum_clearance = min(minimum_clearance, clearance)
        if not math.isfinite(minimum_clearance):
            minimum_clearance = None
        passed = bool(proc.returncode == 0 and isinstance(reacquire, Mapping)
                      and reacquire.get("assertions", {}).get("passed") is True
                      and selection_check["passed"])
        return {"passed": passed, "reason": None if passed else "runner/reacquisition or GT selection assertion failed",
                "pre_follow_rth": landing, "selection_check": selection_check,
                "plan": {"stage": plan.stage, "predicted_occlusion_s": plan.occluded_s,
                         "expected_occlusion_s": self.follow.get("expected_occlusion_s"),
                         "lane_clearance_m": plan.lane_clearance_m, "column_clearance_m": plan.column_clearance_m,
                         "ttl_s": plan.ttl_s},
                "mission_exit_code": None if proc is None else proc.returncode,
                "reacquisition_result": reacquire, "selected_target_gt_check": selected_gt,
                "gt_longest_predicted_occlusion_s": longest,
                "occlusion_expected_s": self.follow.get("expected_occlusion_s"),
                "min_static_clearance_along_gt_path_m": minimum_clearance,
                "results_dir": str((self.directory / "mission" / mission_output_dir).relative_to(ROOT)),
                "result_file": str(results[-1].relative_to(ROOT)) if results else None}

    async def test_rth(self) -> dict[str, Any]:
        return await self._rth(require_airborne=True)

    def _hazard_trial(self) -> dict[str, Any] | None:
        """Closest obstacle/approach pair with a real collision course and a safe approach leg.

        The direct path must penetrate the obstacle by ``HAZARD_MIN_PATH_PENETRATION_M`` for the
        0.6 m hull, and the transit from the current position to the start point must clear the
        world, so the trial exercises avoidance instead of being unrunnable. A tangent obstacle
        (e.g. +0.06 m clearance) is not a collision course and must not be chosen.
        """
        drone = self._drone()
        if drone is None:
            return None
        from sim.worldgen.plan import cruise_legs, heading_deg, legs_clearance
        trials: list[dict[str, Any]] = []
        for primitive in self.world.primitives:
            text = f"{primitive.model} {primitive.name}".lower()
            if not primitive.static or not any(token in text for token in HAZARD_OBSTACLE_TOKENS):
                continue
            cx, cy, cz = primitive.transform[:3]
            dz = min(max(cz, 3.0), self.transit_alt - 1.0)
            distance = math.hypot(cx - drone[0], cy - drone[1])
            if distance <= 0.0:
                continue
            for heading_step in HAZARD_APPROACH_HEADINGS_DEG:
                angle = math.radians(heading_step)
                ux, uy = math.cos(angle), math.sin(angle)
                start = (cx + ux * HAZARD_APPROACH_DISTANCE_M, cy + uy * HAZARD_APPROACH_DISTANCE_M, dz)
                destination = (cx - ux * HAZARD_APPROACH_DISTANCE_M, cy - uy * HAZARD_APPROACH_DISTANCE_M, dz)
                clearance, _blocker = self.world.segment_clearance(start, destination, radius=0.6)
                if not _hazard_path_is_collision_course(clearance):
                    continue
                yaw_rad = math.atan2(destination[1] - start[1], destination[0] - start[0])
                legs = cruise_legs(drone[:3], start, heading_deg(yaw_rad), self.transit_alt)
                approach_clearance = legs_clearance(self.world, drone[:3], legs, drone_radius_m=0.6)
                if _clearance_below_threshold(approach_clearance):
                    continue
                trials.append({"distance_m": distance, "obstacle": primitive.name,
                               "obstacle_model": primitive.model, "obstacle_pose": primitive.transform,
                               "start": start, "destination_far_side": destination, "yaw_rad": yaw_rad,
                               "direct_path_static_clearance_m": clearance,
                               "approach_min_clearance_m": approach_clearance})
        if not trials:
            return None
        return min(trials, key=lambda trial: trial["distance_m"])

    async def test_hazard(self) -> dict[str, Any]:
        if not await self._ensure_airborne():
            return {**self._takeoff_failure("vehicle could not take off"), "crashed": False}
        trial = self._hazard_trial()
        if trial is None:
            return {"passed": False, "status": "skipped",
                    "reason": (f"no static obstacle offers a collision course "
                               f"(at most -{HAZARD_MIN_PATH_PENETRATION_M:.2f} m for the 0.6 m hull) "
                               f"with a safe approach leg"),
                    "expected_safe_behavior": "blocked-or-avoided",
                    "obstacles_considered": [primitive.name for primitive in self.world.primitives
                                             if primitive.static and any(
                                                 token in f"{primitive.model} {primitive.name}".lower()
                                                 for token in HAZARD_OBSTACLE_TOKENS)],
                    "crashed": False}
        from sim.worldgen.plan import ned_pose
        start = trial["start"]
        destination = trial["destination_far_side"]
        clearance = trial["direct_path_static_clearance_m"]
        approach_route = await self._navigate(start, trial["yaw_rad"])
        if not approach_route.get("passed"):
            return {"passed": False, "status": "skipped", "reason": approach_route.get("reason"),
                    "obstacle": trial["obstacle"], "crashed": False}
        start_index = len(self.path_history)
        submitted = None
        crash = False
        failure = None
        proximity: list[Any] = []
        try:
            state_before = await self._tool("get_state")
            vehicle = state_before.get("vehicle", {}) if isinstance(state_before, Mapping) else {}
            if isinstance(vehicle, Mapping) and isinstance(vehicle.get("proximity"), list):
                proximity = vehicle["proximity"]
        except Exception as exc:
            print(f"pre-hazard get_state failed: {type(exc).__name__}: {exc}", flush=True)
        try:
            submitted = await self._tool("goto", {"pose": ned_pose(destination),
                "heading_deg": heading_deg(math.atan2(destination[1] - start[1], destination[0] - start[0])),
                "ttl_s": self._scaled_ttl(75.0)})
            task_id = submitted.get("task_id") if isinstance(submitted, Mapping) else None
            if not task_id:
                return {"passed": False, "reason": f"goto did not return task id: {submitted}",
                        "obstacle": primitive.name, "path_clearance_m": clearance, "crashed": False}
            deadline = time.monotonic() + 300.0
            status: dict[str, Any] = {}
            while time.monotonic() < deadline:
                try:
                    status = await self._tool("get_task_status", {"task_id": str(task_id)})
                    state = await self._tool("get_state")
                except Exception as exc:
                    failure = f"SITL/MCP unavailable during hazard trial: {exc}"
                    crash = True
                    break
                self._record_gt()
                gt = self._drone()
                roll = gt[4] if gt is not None and len(gt) > 4 else 0.0
                pitch = gt[5] if gt is not None and len(gt) > 5 else 0.0
                ground_z = self.ground.height(gt[0], gt[1]) if gt is not None else 0.0
                on_ground = gt is not None and gt[2] < ground_z + 0.35
                crash = abs(roll) > math.radians(60) or abs(pitch) > math.radians(60) or on_ground
                if crash:
                    failure = "unexpected ground contact or attitude exceeded 60 degrees"
                    break
                if status.get("status") in TERMINAL:
                    break
                await asyncio.sleep(0.5)
            if status.get("status") not in TERMINAL and not crash:
                failure = "hazard task did not terminate within bounded 300 s wall watchdog"
                try:
                    await self._tool("cancel", {"task_id": str(task_id)})
                except Exception:
                    pass
        except Exception as exc:
            failure = f"hazard goto failed: {exc}"
        route_clearance = self._trajectory_clearance(start_index)
        safe = not crash and status.get("status") in {"failed", "cancelled", "expired"} and clearance < 0.0
        result = {"passed": safe, "reason": None if safe else failure or "unsafe obstacle crossing was not blocked",
                  "expected_safe_behavior": "blocked-or-avoided", "obstacle": trial["obstacle"],
                  "obstacle_model": trial["obstacle_model"], "obstacle_pose": trial["obstacle_pose"],
                  "start": start, "destination_far_side": destination,
                  "approach_min_clearance_m": trial["approach_min_clearance_m"],
                  "executor_result": status, "direct_path_static_clearance_m": clearance,
                  "gt_min_clearance_m": route_clearance, "crashed": crash, "watchdog_failure": failure,
                  "vehicle_proximity_sensors": len(proximity), "proximity_available": bool(proximity)}
        if not proximity:
            result["environment_note"] = ("the outdoor airframe reports no proximity sensors, so the "
                                         "ArduPilot proximity-avoidance safeguard is unavailable in this world")
            if result["reason"]:
                result["reason"] = f"{result['reason']}; {result['environment_note']}"
        return result

    def _civilian_check(self) -> dict[str, Any]:
        from sim.scripts.civilian_life import load_scenario
        scenario_path = (self.manifest_path.parent / self.manifest["scenario"]).resolve()
        scenario = load_scenario(scenario_path)
        trace = self.civilian_trace_path
        bounds = self.manifest.get("bounds", {})
        xbounds = bounds.get("x", [-math.inf, math.inf])
        ybounds = bounds.get("y", [-math.inf, math.inf])
        specs = {agent.model: agent for agent in scenario.agents}
        samples: dict[str, list[tuple[float, tuple[float, float, float, float]]]] = {}
        malformed = 0
        if trace.is_file():
            for line in trace.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    t = float(row["t"])
                    agents = row["agents"]
                    for name, pose in agents.items():
                        if len(pose) == 4:
                            samples.setdefault(name, []).append((t, tuple(float(v) for v in pose)))
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    malformed += 1
        violations = []
        checked = 0
        max_speed_ratio = 0.0
        for name, poses in samples.items():
            spec = specs.get(name)
            if spec is None:
                continue
            for t, pose in poses:
                checked += 1
                x, y, z, _yaw = pose
                if not (xbounds[0] <= x <= xbounds[1] and ybounds[0] <= y <= ybounds[1]):
                    violations.append({"model": name, "type": "out_of_bounds", "t": t, "position": pose[:3]})
                if spec.kind == "pedestrian":
                    ground_z = scenario.ground.height(x, y) + spec.z_offset
                    if abs(z - ground_z) > 0.05:
                        violations.append({"model": name, "type": "pedestrian_ground_height", "t": t,
                                          "z_error_m": z - ground_z})
            # Events (startle, speed change) may take an agent up to its declared maximum
            # speed; the nominal speed is only the unperturbed walk/drive speed.
            maximum = float(spec.max_speed_m_s or spec.speed_m_s or 0.0) * 1.05
            for (t0, p0), (t1, p1) in zip(poses, poses[1:]):
                dt = t1 - t0
                if dt <= 0:
                    continue
                speed = math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / dt
                max_speed_ratio = max(max_speed_ratio, speed / maximum if maximum > 0 else speed)
                if speed > maximum + 1e-6:
                    violations.append({"model": name, "type": "speed_limit", "t": t1,
                                      "speed_m_s": speed, "maximum_m_s": maximum})
        return {"passed": trace.is_file() and checked > 0 and not violations,
                "trace": str(trace), "trace_exists": trace.is_file(), "checked_poses": checked,
                "malformed_rows": malformed, "agents": len(samples), "max_speed_ratio": max_speed_ratio,
                "violations": violations}

    def _rtf_stats(self) -> dict[str, Any]:
        samples = self._rtf.snapshot() if self._rtf else []
        factors = [float(row["real_time_factor"]) for row in samples if isinstance(row.get("real_time_factor"), (float, int))]
        ordered = sorted(factors)
        p10 = ordered[max(0, math.ceil(0.1 * len(ordered)) - 1)] if ordered else None
        return {"samples": len(factors), "mean": statistics.mean(factors) if factors else None,
                "p10": p10, "raw": samples}

    async def _campaign_body(self) -> dict[str, Any]:
        await self._enable_camera()
        self._rtf = TopicReader(f"/world/{self.world_name}/stats", self.directory / "rtf.jsonl",
                                _parse_stats, max_hz=1.0)
        self._gt = TopicReader(f"/world/{self.world_name}/pose/info", self.directory / "gt_pose.jsonl",
                               _parse_pose_info, max_hz=5.0, sample_latest=True,
                               rate_reference=self._rtf)
        await self._wait_for_gt()
        initial_camera_pose = self._camera_pose(self._gt_latest())
        if initial_camera_pose is None or self.camera_info is None:
            raise RuntimeError("Gazebo did not provide a complete camera pose and runtime CameraInfo")
        from sim.worldgen.plan import Camera
        # The runtime CameraInfo is the calibration contract for the rendered camera; the
        # harness must plan vantages and project ground truth with the same optics.
        self.camera = Camera(
            pitch_deg=float(initial_camera_pose["pitch_deg"]),
            hfov_deg=float(self.camera_info["horizontal_fov_deg"]),
            width=int(self.camera_info["width"]), height=int(self.camera_info["height"]),
        )
        self.initial_camera_pose = initial_camera_pose
        requested_order = ("perception", "select", "approach", "orbit", "scan", "follow", "rth", "hazard")
        results: dict[str, Any] = {}
        for name in requested_order:
            if name not in self.args.tests:
                continue
            if STOP.is_set():
                results[name] = {"status": "skipped", "passed": False, "reason": "episode received termination"}
                continue
            try:
                result = await getattr(self, f"test_{name}")()
                result.setdefault("status", "completed" if result.get("passed") else "failed")
                results[name] = result
            except Exception as exc:
                results[name] = {"passed": False, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            _json_write(self.directory / "tests" / f"{name}.json", results[name])
            try:
                state = await self._tool("get_state")
                vehicle = state.get("vehicle", {}) if isinstance(state, Mapping) else {}
                drone = self._drone()
                crashed = drone is not None and drone[2] <= self.ground.height(drone[0], drone[1]) + 0.2 and bool(vehicle.get("armed"))
                if crashed or not self._server or self._server.poll() is not None:
                    reason = "vehicle unexpectedly contacted ground while armed" if crashed else "MCP/SITL unavailable"
                    for remaining in requested_order[requested_order.index(name) + 1:]:
                        if remaining in self.args.tests and remaining not in results:
                            results[remaining] = {"status": "skipped", "passed": False, "reason": reason}
                    break
            except Exception as exc:
                for remaining in requested_order[requested_order.index(name) + 1:]:
                    if remaining in self.args.tests and remaining not in results:
                        results[remaining] = {"status": "skipped", "passed": False,
                                              "reason": f"vehicle/SITL lost: {exc}"}
                break
        return results

    async def _ready_session(self, url: str, headers: Mapping[str, str]) -> AsyncExitStack:
        """Open an MCP session that answers get_state; the caller owns the returned stack.

        The attempt deadline cancels this task via a watchdog because wrapping the anyio-based
        MCP client in ``asyncio.wait_for`` (a separate task) corrupts its task group, while
        ``anyio.fail_after`` cannot exit its scope around the client's nested task group.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        task = asyncio.current_task()
        watchdog = (asyncio.get_running_loop().call_later(MCP_READY_ATTEMPT_TIMEOUT_S, task.cancel)
                    if task is not None else None)
        stack = AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(streamablehttp_client(url, headers=headers))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            await self._tool("get_state")
        except BaseException as exc:
            if watchdog is not None:
                watchdog.cancel()
            teardown: BaseException | None = None
            try:
                await stack.aclose()
            except BaseException as close_exc:
                teardown = close_exc
            if teardown is not None and getattr(teardown, "exceptions", None):
                # The transport task group's exit carries the real cause (e.g. ConnectError);
                # surface that instead of an opaque "unhandled errors in a TaskGroup".
                raise teardown from exc
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
        return stack

    async def run(self) -> dict[str, Any]:
        STOP.clear()
        self._start_server()
        url = "http://127.0.0.1:8000/mcp"
        headers = {"Authorization": f"Bearer {os.environ['HORUS_MCP_TOKEN']}"}
        deadline = time.monotonic() + MCP_READY_DEADLINE_S
        attempts = 0
        last_error: BaseException | None = None
        while time.monotonic() < deadline and not STOP.is_set():
            attempts += 1
            try:
                stack = await self._ready_session(url, headers)
            except (Exception, asyncio.CancelledError) as exc:
                if STOP.is_set():
                    break
                # The MCP client can surface transport failures as an anyio scope
                # cancellation; that is not this task being cancelled, so retry.
                last_error = exc
                print(f"MCP readiness attempt {attempts} failed: "
                      f"{_exception_summary(exc)}", flush=True)
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    break
                continue
            try:
                return await self._campaign_body()
            finally:
                try:
                    await stack.aclose()
                except BaseException as exc:
                    print(f"MCP session teardown failed: {_exception_summary(exc)}", flush=True)
        if last_error is None:
            raise RuntimeError(f"MCP server did not become ready within {MCP_READY_DEADLINE_S:.0f}s")
        raise RuntimeError(f"MCP server did not become ready after {attempts} attempt(s): "
                           f"{_exception_summary(last_error)}")

    async def _wait_for_gt(self) -> None:
        # 60 s of wall time covers the sparse pose topic on a loaded host; the chain fallback
        # normally resolves the camera pose as soon as horus_iris and pitch_link are published.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            sample = self._gt_latest()
            if self._drone(sample) is not None and self._camera_pose(sample) is not None:
                break
            await asyncio.sleep(0.25)
        sample = self._gt_latest()
        if self._drone(sample) is None or self._camera_pose(sample) is None:
            models = sample.get("models", {}) if isinstance(sample, Mapping) else {}
            missing = ([name for name in CAMERA_POSE_CHAIN if name not in models]
                       if isinstance(models, Mapping) else list(CAMERA_POSE_CHAIN))
            detail = f" (missing: {', '.join(missing)})" if missing else ""
            raise RuntimeError("Gazebo pose topic did not yield a complete drone/gimbal camera pose chain"
                               + detail)
        self._record_gt()

    async def _enable_camera(self) -> None:
        deadline = time.monotonic() + 60.0
        camera_topic = None
        topics: list[str] = []
        while time.monotonic() < deadline:
            result = subprocess.run(["gz", "topic", "-l"], text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=10.0, check=False)
            topics = [line.strip() for line in result.stdout.splitlines()]
            camera_topic = next((topic for topic in topics
                                 if "/camera/image/" in topic and topic.endswith("/enable_streaming")), None)
            if camera_topic:
                break
            await asyncio.sleep(0.5)
        if not camera_topic:
            raise RuntimeError("timed out locating camera enable_streaming topic")
        for attempt in range(20):
            subprocess.run(["gz", "topic", "-t", camera_topic, "-m", "gz.msgs.Boolean", "-p", "data: 1"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10.0, check=False)
            probe = subprocess.run([sys.executable, "-c",
                "import socket,sys; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('0.0.0.0',5600)); "
                "s.settimeout(1.5);\ntry: s.recv(65535)\nexcept socket.timeout: sys.exit(1)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0, check=False)
            if probe.returncode == 0:
                print(f"camera RTP enabled on {camera_topic} after attempt {attempt + 1}", flush=True)
                break
        else:
            raise RuntimeError(f"camera RTP stream did not start on UDP 5600: {camera_topic}")

        camera_base = camera_topic.removesuffix("/image/enable_streaming")
        camera_info_topic = None
        info_deadline = time.monotonic() + 30.0
        last_error = "camera_info topic not yet advertised"
        while time.monotonic() < info_deadline:
            result = subprocess.run(["gz", "topic", "-l"], text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=10.0, check=False)
            topics = [line.strip() for line in result.stdout.splitlines()]
            matches = [topic for topic in topics if topic.endswith("/camera_info") and topic.startswith(camera_base)]
            if not matches:
                matches = [topic for topic in topics if topic.endswith("/camera_info")]
            camera_info_topic = matches[0] if matches else None
            if camera_info_topic:
                try:
                    message = subprocess.run(
                        ["gz", "topic", "-e", "-n", "1", "-t", camera_info_topic],
                        capture_output=True, text=True, timeout=5.0, check=False,
                    )
                    if message.returncode == 0:
                        self.camera_info = _parse_camera_info(message.stdout, camera_info_topic)
                        break
                    last_error = f"gz topic exited with status {message.returncode}"
                except (subprocess.TimeoutExpired, ValueError) as exc:
                    last_error = str(exc)
            await asyncio.sleep(0.5)
        if self.camera_info is None:
            raise RuntimeError(f"could not read runtime Gazebo CameraInfo from {camera_info_topic}: {last_error}")
        print(
            f"camera intrinsics from {camera_info_topic}: {self.camera_info['width']}x{self.camera_info['height']} "
            f"CameraInfo HFOV {self.camera_info['horizontal_fov_deg']:.2f} deg",
            flush=True,
        )

    def close(self) -> None:
        if self._gt:
            self._gt.stop()
        if self._rtf:
            self._rtf.stop()
        self._civilian_trace.close()
        self._follow_trace.close()
        if self._server and self._server.poll() is None:
            self._server.terminate()
            try:
                self._server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._server.kill()
                self._server.wait()
        if self._server_log:
            self._server_log.close()


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("forest", "urban"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--sdf", required=True)
    parser.add_argument("--episode-dir", required=True)
    parser.add_argument("--tests", required=True)
    parser.add_argument("--vantages", type=int, default=8)
    parser.add_argument(
        "--ttl-scale", type=float, default=1.0,
        help="multiply command TTLs and harness waits for hosts that run the sim below real time",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.ttl_scale) or not 0.0 < args.ttl_scale <= 20.0:
        parser.error("--ttl-scale must be finite and between 0 and 20")
    args.tests = tuple(name for name in args.tests.split(",") if name)
    return args


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    signal.signal(signal.SIGTERM, _signal_stop)
    signal.signal(signal.SIGINT, _signal_stop)
    directory = Path(args.episode_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "episode.pid").write_text(str(os.getpid()), encoding="utf-8")
    episode = None
    result: dict[str, Any] = {"kind": args.kind, "seed": args.seed,
                              "world": Path(args.manifest).name.removesuffix(".layout.json"),
                              "status": "failed", "tests": {}}
    started_wall, started_epoch = time.monotonic(), time.time()
    try:
        result["grounder"] = probe_grounder()
        episode = Episode(args)
        tests = asyncio.run(episode.run())
        result.update({
            "tests": tests, "status": "passed" if all(test.get("passed") is True for test in tests.values()) else "failed",
            "camera_info": episode.camera_info,
            "ttl_scale": args.ttl_scale,
            "initial_camera_pose": getattr(episode, "initial_camera_pose", None),
            "gt_coverage": episode.gt_coverage(),
        })
        result["civilian_check"] = episode._civilian_check()
        result["rtf"] = episode._rtf_stats()
        result["readers"] = episode.reader_status()
        sim = episode._gt.snapshot() if episode._gt else []
        result["sim_duration_s"] = None if len(sim) < 2 else sim[-1].get("sim_time", 0.0) - sim[0].get("sim_time", 0.0)
        result["rtf_wall_started_at"] = datetime.fromtimestamp(started_epoch, timezone.utc).isoformat()
        if result["civilian_check"].get("passed") is False:
            result["status"] = "failed"
    except BaseException as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}"
        result["status"] = "failed"
        import traceback
        (directory / "episode-traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return_code = 1
    else:
        return_code = 0
    finally:
        if episode is not None:
            episode.close()
            if not STOP.is_set():
                try:
                    result["civilian_check"] = episode._civilian_check()
                    result["rtf"] = episode._rtf_stats()
                    result["readers"] = episode.reader_status()
                    result["gt_coverage"] = episode.gt_coverage()
                    gt_rows = episode._gt.snapshot() if episode._gt else []
                    result["sim_duration_s"] = None if len(gt_rows) < 2 else gt_rows[-1].get("sim_time", 0.0) - gt_rows[0].get("sim_time", 0.0)
                except Exception as exc:
                    result["final_metrics_error"] = f"{type(exc).__name__}: {exc}"
        result["wall_duration_s"] = time.monotonic() - started_wall
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        # The start probe ran before the service finished loading; probe again at the end so a
        # grounder that was active for the tests is reported as active.
        result["grounder"] = _grounder_status(
            result.get("grounder"), probe_grounder(),
            getattr(episode, "mcp_grounder", None) if episode is not None else None,
            getattr(episode, "open_vocab_candidates", 0) if episode is not None else 0)
        console = _copy_sitl_console(directory)
        if console is not None:
            result["sitl_console_log"] = console
        _json_write(directory / "episode_result.json", result)
        (directory / "episode.pid").unlink(missing_ok=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
