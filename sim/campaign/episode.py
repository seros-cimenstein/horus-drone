"""The flight, ground-truth and observation context one campaign episode runs its scenarios in."""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import threading
import time
from typing import Any
import xml.etree.ElementTree as ET

from horus.events import TERMINAL_TASK_STATUSES
from horus.mcp_client import DEFAULT_MCP_URL, tool_json
from sim.campaign.camera_geometry import (
    CAMERA_POSE_CHAIN, CAMERA_POSE_HISTORY, camera_chain_relative_poses, camera_pose, camera_sensor_pose,
    project_people,
)
from sim.campaign.camera_stream import enable_camera_stream, read_camera_info
from sim.campaign.civilians import CivilianPoseTrace, static_gt_people
from sim.campaign.gz_topics import RTF_WINDOW_S, TopicReader, measured_rate, parse_pose_info, parse_stats
from sim.campaign.mcp_session import exception_summary, open_ready_session
from sim.campaign.scoring import candidate_bbox, match_candidates_to_people
from sim.campaign.ttl import goto_ttl_s, scaled_ttl

ROOT = Path("/horus")
STOP = threading.Event()
CLEARANCE_THRESHOLD_M = 1.5
CLEARANCE_ROUNDING_TOLERANCE_M = 0.005
GIMBAL_SDF = ROOT / "sim" / "models" / "horus_gimbal_small_3d_outdoor" / "model.sdf"
# The follow mission resolves output_dir relative to the mission file (see
# horus/repl/missions.py), so results land next to the mission, not at the episode root.
FOLLOW_OUTPUT_DIR = "follow_results"

Scenario = Callable[["Episode"], Awaitable[dict[str, Any]]]


def clearance_below_threshold(clearance_m: float) -> bool:
    return not clearance_m + CLEARANCE_ROUNDING_TOLERANCE_M >= CLEARANCE_THRESHOLD_M


def json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _skip_remaining(results: dict[str, Any], names: Iterable[str], reason: str) -> None:
    for name in names:
        results.setdefault(name, {"status": "skipped", "passed": False, "reason": reason})


class Episode:
    # Planner limits used before ``__init__`` resolves the world's flight profile (canonical
    # 150 m values; see sim/worldgen/plan.CANONICAL_*). ``__init__`` overwrites them from the
    # world profile; ``object.__new__`` test fixtures get the canonical defaults.
    vantage_bounds_m = 60.0
    max_leg_m = 60.0
    transit_speed_m_s = 3.0

    def __init__(self, args: argparse.Namespace):
        from sim.worldgen.geometry import CollisionWorld, load_collisions
        from sim.worldgen.people import PERSON_ASSETS
        from sim.worldgen.plan import Camera, Ground

        self.args = args
        self.ttl_scale = float(getattr(args, "ttl_scale", 1.0) or 1.0)
        self.kind = args.kind
        self.rng = random.Random(args.seed)
        self.manifest_path = Path(args.manifest)
        self.sdf_path = Path(args.sdf)
        self.directory = Path(args.episode_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        resource_paths = (self.sdf_path.parent / "models", ROOT / "sim" / "models", Path("/opt/ardupilot_gazebo/models"))
        self.world = CollisionWorld(load_collisions(self.sdf_path, resource_paths=resource_paths))
        self.ground = Ground(self.manifest, self.manifest_path)
        self.camera = Camera()
        self.camera_info: dict[str, Any] | None = None
        self.camera_sensor_pose = camera_sensor_pose(GIMBAL_SDF)
        self.camera_chain_relative: dict[str, tuple[float, float, float, float, float, float]] = {}
        try:
            self.camera_chain_relative = camera_chain_relative_poses(self.sdf_path, resource_paths)
        except (ET.ParseError, OSError, KeyError, ValueError) as exc:
            print(f"camera chain SDF fallback unavailable: {type(exc).__name__}: {exc}", flush=True)
        self.person_assets = PERSON_ASSETS
        self.people = list(self.manifest.get("people", []))
        # Grounder evidence: candidates seeded by the open-vocabulary pass can only come from it.
        self.open_vocab_candidates = 0
        self.mcp_grounder: dict[str, Any] = {}
        self.initial_camera_pose: Any = None
        self._person_models = {str(person.get("model", "")) for person in self.people}
        self.follow = self.manifest.get("follow", {})
        self.transit_alt = float(self.manifest["transit_altitude_m"])
        self._apply_flight_profile()
        self.path_history: list[dict[str, Any]] = []
        self.start_sim: float | None = None
        self._session: Any = None
        self._gt: TopicReader | None = None
        self._rtf: TopicReader | None = None
        self._server: subprocess.Popen[Any] | None = None
        self._server_log: Any = None
        self._takeoff_altitude_samples: list[dict[str, Any]] = []
        self._open_civilian_traces()

    def _apply_flight_profile(self) -> None:
        """Executor config and planner limits from the world's flight profile.

        Generated worlds run the procedural profile, canonical worlds the outdoor one
        (sim/scripts/world_profiles.py, shared with the launch scripts). It supplies the config
        the MCP server loads (HORUS_CONFIG/--config) and the planner limits: vantage bounds,
        cruise leg length and the expected transit speed for per-leg command TTLs.
        """
        from horus.config import load_config
        from sim.scripts.world_profiles import world_profile
        from sim.worldgen.plan import CANONICAL_MAX_LEG_M, CANONICAL_VANTAGE_BOUNDS_M

        self.procedural = not bool(self.manifest.get("canonical", True))
        profile_paths = world_profile(str(self.sdf_path))
        if profile_paths is None:
            raise RuntimeError(f"no flight profile for world {self.sdf_path.name}")
        self.config_container_path = profile_paths.config
        self.config_relative_path = profile_paths.config.removeprefix(f"{ROOT}/")
        profile = load_config(ROOT / self.config_relative_path).executor
        polygon = profile.geofence_polygon_ned
        if self.procedural and polygon:
            self.vantage_bounds_m = min(max(abs(north), abs(east)) for north, east in polygon)
            self.max_leg_m = float(profile.max_distance_m)
        else:
            # Canonical worlds keep the planner limits they shipped with: the 60 m actor
            # geofence inset and 60 m cruise legs.
            self.vantage_bounds_m = CANONICAL_VANTAGE_BOUNDS_M
            self.max_leg_m = CANONICAL_MAX_LEG_M
        cruise_gate = profile.cruise_min_altitude_m
        if (
            profile.cruise_speed_m_s is not None
            and cruise_gate is not None
            and self.transit_alt >= cruise_gate
        ):
            self.transit_speed_m_s = float(profile.cruise_speed_m_s)
        else:
            self.transit_speed_m_s = float(profile.max_speed_m_s)

    def _open_civilian_traces(self) -> None:
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
        self._static_gt_people = static_gt_people(self.people)

    @property
    def world_name(self) -> str:
        return str(self.manifest["world"])

    def _start_server(self) -> None:
        environment = dict(os.environ)
        environment["HORUS_CONFIG"] = self.config_container_path
        environment["HORUS_RUN_DIR"] = str(self.directory)
        environment["HORUS_EVENT_LOG"] = str(self.directory / "events.jsonl")
        self._server_log = (self.directory / "mcp.log").open("w", encoding="utf-8")
        self._server = subprocess.Popen(["/opt/horus-venv/bin/horus-mcp"], cwd=ROOT, env=environment,
                                        stdout=self._server_log, stderr=subprocess.STDOUT, start_new_session=True)

    async def _attach_session(self, session: Any) -> None:
        self._session = session
        await self._tool("get_state")

    async def _tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        result = await self._session.call_tool(name, dict(arguments or {}))
        payload = tool_json(result, name)
        images = [block for block in getattr(result, "content", None) or () if getattr(block, "type", None) == "image"]
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
        budget_s = max_wall_s if max_wall_s is not None else 120.0
        started = time.monotonic()
        deadline = started + budget_s
        result: dict[str, Any] = {"task_id": task_id, "submitted": dict(response)}
        next_observation = time.monotonic()
        while time.monotonic() < deadline and not STOP.is_set():
            status = await self._tool("get_task_status", {"task_id": task_id})
            if not isinstance(status, Mapping):
                raise RuntimeError(f"invalid status for task {task_id}: {status}")
            result = dict(status)
            self._record_gt()
            if status.get("status") in TERMINAL_TASK_STATUSES:
                return result
            if observer is not None and time.monotonic() >= next_observation:
                await observer()
                next_observation = time.monotonic() + 1.0
            await asyncio.sleep(0.25)
        try:
            await self._tool("cancel", {"task_id": task_id})
        except Exception:
            pass
        raise TimeoutError(
            f"task {task_id} did not reach a terminal status before wall deadline "
            f"(waited {time.monotonic() - started:.1f}s of a {budget_s:g}s budget, stop={STOP.is_set()})"
        )

    def _scaled_ttl(self, ttl_s: float) -> float:
        return scaled_ttl(ttl_s, self.ttl_scale)

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

    def _sim_now(self) -> float:
        sample = self._gt_latest()
        value = sample.get("sim_time") if isinstance(sample, Mapping) else None
        return float(value) if isinstance(value, (int, float)) else 0.0

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

    def _camera_pose(self, sample: Mapping[str, Any] | None) -> dict[str, Any] | None:
        return camera_pose(sample, self.camera_sensor_pose, self.camera_chain_relative)

    @staticmethod
    def _frame_sim_time(frame: Mapping[str, Any]) -> float | None:
        captured = frame.get("capture_sim_time")
        if isinstance(captured, (float, int)) and math.isfinite(float(captured)):
            return float(captured)
        sample = frame.get("gt_sample")
        sim_time = sample.get("sim_time") if isinstance(sample, Mapping) else None
        return float(sim_time) if isinstance(sim_time, (int, float)) else None

    def _person_projection(
        self, frame: Mapping[str, Any]
    ) -> tuple[dict[str, tuple[float, float] | None], dict[str, Any], dict[str, Any] | None]:
        pose = self._camera_pose(frame.get("gt_sample"))
        positions = self._people_gt(frame.get("gt_sample"), sim_time=self._frame_sim_time(frame))
        projected, details = project_people(pose, self.people, positions, world=self.world,
                                            camera=self.camera, camera_info=self.camera_info)
        return projected, details, pose

    def _note_candidate_sources(self, candidates: Any) -> None:
        """Count open-vocabulary seeds, which only exist when the grounder answered."""
        if not isinstance(candidates, Iterable):
            return
        for candidate in candidates:
            if isinstance(candidate, Mapping) and str(candidate.get("source")) == "open_vocab":
                self.open_vocab_candidates += 1

    def _match_frame(self, frame: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        """GT-match a frame's candidate table: ``(matching, projection details, camera pose)``."""
        candidates = frame.get("candidate_table", [])
        self._note_candidate_sources(candidates)
        projected, details, pose = self._person_projection(frame)
        return match_candidates_to_people(candidates, projected), details, pose

    def _detection_rows(self, frame: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[Any], dict[str, Any]]:
        matching, details, pose = self._match_frame(frame)
        matching["camera_pose"] = pose
        rows: list[dict[str, Any]] = []
        by_name = {str(person.get("model")): person for person in self.people}
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
        rtf = self._rtf.measured_rate() if self._rtf is not None else None
        if (isinstance(sim_time, (int, float)) and isinstance(wall_time, (int, float))
                and isinstance(observation_age, (int, float)) and rtf is not None and rtf > 0):
            capture_wall = frame["capture_wall_time"] - max(0.0, float(observation_age))
            frame["capture_sim_time"] = float(sim_time) + (capture_wall - float(wall_time)) * rtf
        elif isinstance(sim_time, (int, float)):
            frame["capture_sim_time"] = float(sim_time)
        self._record_gt()
        json_write(self.directory / "observations" / f"{name}.json", frame)
        return frame

    async def _navigate(self, eye: tuple[float, float, float], yaw_rad: float) -> dict[str, Any]:
        from sim.worldgen.plan import cruise_legs, heading_deg, legs_clearance, ned_pose
        start = self._drone()
        if start is None:
            return {"passed": False, "reason": "no ground-truth drone pose"}
        legs = cruise_legs(start[:3], eye, heading_deg(yaw_rad), self.transit_alt,
                           max_leg_m=self.max_leg_m)
        clearance = legs_clearance(self.world, start[:3], legs, drone_radius_m=0.6)
        if clearance_below_threshold(clearance):
            return {"passed": False, "reason": f"planned transit legs clearance {clearance:.3f} m < "
                    f"{CLEARANCE_THRESHOLD_M:.1f} m",
                    "min_clearance_m": clearance, "legs": legs}
        statuses = []
        previous = start[:3]
        for point, heading in legs:
            ttl_s = goto_ttl_s(math.dist(previous, point), self.transit_speed_m_s)
            response = await self._skill("goto", {"pose": ned_pose(point), "heading_deg": heading,
                                                  "ttl_s": ttl_s},
                                         timeout_s=max(180.0, ttl_s + 60.0))
            previous = point
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

    async def _observe_matched_person(
        self, person: Mapping[str, Any], label: str
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        """Fly to a safe vantage and return the live candidate GT-matched to ``person``."""
        from sim.worldgen.plan import person_point, plan_vantage
        point = person_point(person, self.ground, str(person.get("posture", "standing")))
        vantage = plan_vantage(self.world, self.ground, self.camera, str(person["model"]), point, self.rng,
                               transit_alt=self.transit_alt, require_los=True, min_clearance_m=1.5,
                               bounds=self.vantage_bounds_m, attempts=220)
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

    def _rtf_stats(self) -> dict[str, Any]:
        """Measured RTF (Δsim/Δwall) per ``RTF_WINDOW_S`` window over the whole episode."""
        samples = self._rtf.snapshot() if self._rtf else []
        factors = []
        start = 0
        for end in range(1, len(samples)):
            if samples[end]["wall_time"] - samples[start]["wall_time"] >= RTF_WINDOW_S:
                rate = measured_rate([samples[start], samples[end]], RTF_WINDOW_S)
                if rate is not None:
                    factors.append(rate)
                start = end
        ordered = sorted(factors)
        p10 = ordered[max(0, math.ceil(0.1 * len(ordered)) - 1)] if ordered else None
        return {"windows": len(factors), "window_s": RTF_WINDOW_S,
                "mean": statistics.mean(factors) if factors else None, "p10": p10,
                "overall": measured_rate([samples[0], samples[-1]], 0.0) if len(samples) > 1 else None}

    async def _start_ground_truth(self) -> None:
        """Enable the camera, start the GT/RTF readers and calibrate the planner camera."""
        from sim.worldgen.plan import Camera

        await self._enable_camera()
        self._rtf = TopicReader(f"/world/{self.world_name}/stats", self.directory / "rtf.jsonl",
                                parse_stats, stop=STOP, max_hz=1.0)
        self._gt = TopicReader(f"/world/{self.world_name}/pose/info", self.directory / "gt_pose.jsonl",
                               parse_pose_info, stop=STOP, max_hz=5.0, sample_latest=True,
                               rate_reference=self._rtf)
        await self._wait_for_gt()
        initial_camera_pose = self._camera_pose(self._gt_latest())
        if initial_camera_pose is None or self.camera_info is None:
            raise RuntimeError("Gazebo did not provide a complete camera pose and runtime CameraInfo")
        # The runtime CameraInfo is the calibration contract for the rendered camera; the
        # harness must plan vantages and project ground truth with the same optics.
        self.camera = Camera(
            pitch_deg=float(initial_camera_pose["pitch_deg"]),
            hfov_deg=float(self.camera_info["horizontal_fov_deg"]),
            width=int(self.camera_info["width"]), height=int(self.camera_info["height"]),
        )
        self.initial_camera_pose = initial_camera_pose

    async def _vehicle_loss_reason(self) -> str | None:
        """Why the remaining scenarios cannot run (ground contact, lost MCP/SITL), or None."""
        try:
            state = await self._tool("get_state")
            vehicle = state.get("vehicle", {}) if isinstance(state, Mapping) else {}
            drone = self._drone()
            crashed = (drone is not None and drone[2] <= self.ground.height(drone[0], drone[1]) + 0.2
                       and bool(vehicle.get("armed")))
            if crashed:
                return "vehicle unexpectedly contacted ground while armed"
            if not self._server or self._server.poll() is not None:
                return "MCP/SITL unavailable"
        except Exception as exc:
            return f"vehicle/SITL lost: {exc}"
        return None

    async def _campaign_body(self, scenarios: Mapping[str, Scenario]) -> dict[str, Any]:
        await self._start_ground_truth()
        names = [name for name in scenarios if name in self.args.tests]
        results: dict[str, Any] = {}
        for index, name in enumerate(names):
            if STOP.is_set():
                results[name] = {"status": "skipped", "passed": False, "reason": "episode received termination"}
                continue
            try:
                result = await scenarios[name](self)
                result.setdefault("status", "completed" if result.get("passed") else "failed")
                results[name] = result
            except Exception as exc:
                results[name] = {"passed": False, "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            json_write(self.directory / "tests" / f"{name}.json", results[name])
            reason = await self._vehicle_loss_reason()
            if reason is not None:
                _skip_remaining(results, names[index + 1:], reason)
                break
        return results

    async def run(self, scenarios: Mapping[str, Scenario]) -> dict[str, Any]:
        """Start the MCP server, open its session and run the requested scenarios in order."""
        STOP.clear()
        self._start_server()
        stack = await open_ready_session(DEFAULT_MCP_URL, os.environ["HORUS_MCP_TOKEN"],
                                         self._attach_session, STOP)
        try:
            return await self._campaign_body(scenarios)
        finally:
            try:
                await stack.aclose()
            except BaseException as exc:
                print(f"MCP session teardown failed: {exception_summary(exc)}", flush=True)

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
        topic = await asyncio.to_thread(enable_camera_stream, log=lambda line: print(line, flush=True))
        self.camera_info = await asyncio.to_thread(read_camera_info, topic)
        print(
            f"camera intrinsics from {self.camera_info['topic']}: "
            f"{self.camera_info['width']}x{self.camera_info['height']} "
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
