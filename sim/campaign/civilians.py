"""Civilian ground truth for the episode: scenario traces, static people and the civilian oracle."""
from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping
import json
import math
from pathlib import Path
from typing import Any

AMBIENT_GROUP = "ambient"


def static_gt_people(people: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Manifest people whose spawn position stays authoritative until a trace covers them.

    The ambient scenario driver only traces group ``ambient`` agents, so everyone else
    (the follow target before its dedicated driver starts) needs the manifest fallback.
    """
    return [person for person in people
            if str(person.get("group") or AMBIENT_GROUP) != AMBIENT_GROUP]


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


def _trace_samples(trace: Path) -> tuple[dict[str, list[tuple[float, tuple[float, float, float, float]]]], int]:
    """Per-agent ``(t, (x, y, z, yaw))`` samples of a scenario trace and its malformed row count."""
    samples: dict[str, list[tuple[float, tuple[float, float, float, float]]]] = {}
    malformed = 0
    if trace.is_file():
        for line in trace.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                t = float(row["t"])
                for name, pose in row["agents"].items():
                    if len(pose) == 4:
                        samples.setdefault(name, []).append((t, tuple(float(v) for v in pose)))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                malformed += 1
    return samples, malformed


def civilian_check(manifest: Mapping[str, Any], manifest_path: Path, trace: Path) -> dict[str, Any]:
    """Oracle over the civilian trace: world bounds, pedestrian ground height and speed limits."""
    from sim.campaign.civilian_spec import load_scenario

    scenario = load_scenario((manifest_path.parent / manifest["scenario"]).resolve())
    bounds = manifest.get("bounds", {})
    xbounds = bounds.get("x", [-math.inf, math.inf])
    ybounds = bounds.get("y", [-math.inf, math.inf])
    specs = {agent.model: agent for agent in scenario.agents}
    samples, malformed = _trace_samples(trace)
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
