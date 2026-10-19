"""Follow scenario: the REPL mission selects, follows and re-acquires the walking target."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any

from horus.mcp_client import DEFAULT_MCP_URL
from sim.campaign.camera_geometry import torso
from sim.campaign.episode import CLEARANCE_THRESHOLD_M, FOLLOW_OUTPUT_DIR, ROOT, Episode, clearance_below_threshold
from sim.campaign.scenarios.rth import return_home
from sim.campaign.scoring import match_candidates_to_people, selection_min_confidence
from sim.campaign.ttl import scale_mission_ttls

MISSION_WALL_TIMEOUT_S = 1500.0


def mission_id_matches(selected_id: Any, captured_id: Any) -> bool:
    """Both IDs must be present and equal; two missing IDs never match."""
    return selected_id is not None and captured_id is not None and selected_id == captured_id


def pin_selection_confidence(mission: dict[str, Any]) -> None:
    """Make a generated mission's ``select_candidate`` confidence floor explicit.

    The runner already defaults to the floor when the step omits ``min_confidence``; writing it
    into the mission keeps the artifact self-describing and in lockstep with harness scoring.
    """
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


def _event_epochs(path: Path, tool: str) -> list[float]:
    """Wall-clock epochs of the MCP events for ``tool`` in an ``events.jsonl``, in log order."""
    if not path.is_file():
        return []
    epochs: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
            if event.get("tool") == tool:
                epochs.append(datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00")).timestamp())
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return epochs


def _write_mission(episode: Episode, plan: Any) -> Path:
    from sim.worldgen.plan import follow_mission
    import yaml

    mission_dir = episode.directory / "mission"
    mission_dir.mkdir(parents=True, exist_ok=True)
    mission_path = mission_dir / f"{episode.world_name}-follow.yaml"
    trace = "/" + str(episode.follow_trace_path.relative_to(Path("/")))
    # The REPL resolves output_dir against the mission file, so results appear in
    # <episode>/mission/<FOLLOW_OUTPUT_DIR>; the harness must read that same path.
    mission = follow_mission(episode.manifest, plan, manifest_path=episode.manifest_path, mission_path=mission_path,
                             repo_root=ROOT, trace_path=trace, output_dir=FOLLOW_OUTPUT_DIR)
    pin_selection_confidence(mission)
    scale_mission_ttls(mission, episode.ttl_scale)
    mission_path.write_text(yaml.safe_dump(mission, sort_keys=False), encoding="utf-8")
    return mission_path


async def _selection_check(episode: Episode, output_line: str, intended_model: Any,
                           expected_colour: Any) -> dict[str, Any] | None:
    """GT-check the runner's ``select_candidate:`` output against a contemporaneous frame."""
    try:
        runner_selected = json.loads(output_line.split("select_candidate:", 1)[1].strip())
    except json.JSONDecodeError:
        return None
    selection_epochs = _event_epochs(episode.directory / "events.jsonl", "list_candidates")
    frame = await episode._observe("follow-runner-selection")
    projected, details, camera_pose = episode._person_projection(frame)
    matching = match_candidates_to_people(frame["candidate_table"], projected)
    matched_model = next((model for model, candidate in matching["matches"].items()
                          if candidate.get("id") == runner_selected.get("id")), None)
    attrs = runner_selected.get("attributes", {})
    runner_attribute = attrs.get("shirt_colour") if isinstance(attrs, Mapping) else None
    return {
        "selection_event_wall_time": selection_epochs[-1] if selection_epochs else None,
        "frame_capture_wall_time": frame.get("capture_wall_time"),
        "frame_id": frame.get("frame_id"), "frame_timestamp": frame.get("timestamp"),
        "frame_saved": frame.get("saved_frame"), "selected_candidate": runner_selected,
        "selected_id_gt_matched_model": matched_model, "intended_model": intended_model,
        "expected_shirt_colour": expected_colour, "runner_attribute": runner_attribute,
        "projected_target": details.get(str(intended_model)),
        "camera_pose": camera_pose,
        "passed": bool(matched_model == intended_model and runner_attribute == expected_colour),
    }


async def _run_mission(episode: Episode, mission_path: Path, intended_model: Any,
                       expected_colour: Any) -> tuple[int | None, dict[str, Any] | None]:
    """Run the follow mission through the REPL; GT-check its selection while it runs."""
    env = dict(os.environ)
    env["HORUS_CONFIG"] = episode.config_container_path
    env["PYTHONUNBUFFERED"] = "1"
    cmd = ["/opt/horus-venv/bin/horus", "--server-url", DEFAULT_MCP_URL,
           "--config", episode.config_relative_path, "--mission", str(mission_path)]
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
    timed_out = False
    deadline = time.monotonic() + MISSION_WALL_TIMEOUT_S
    while not output_closed or proc.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
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
        if selection_check is None and "select_candidate:" in output_line:
            selection_check = await _selection_check(episode, output_line, intended_model, expected_colour)
    output_thread.join(timeout=5.0)
    if timed_out:
        output_lines.append("mission subprocess wall timeout\\n")
    (episode.directory / "follow-mission.log").write_text("".join(output_lines), encoding="utf-8")
    return proc.returncode, selection_check


def _final_selection_check(selection_check: dict[str, Any] | None, selected: Any, intended_model: Any,
                           expected_colour: Any) -> dict[str, Any]:
    """The runner selection check, also requiring the mission's re-acquisition record to agree."""
    selection_check = selection_check or {
        "intended_model": intended_model, "expected_shirt_colour": expected_colour,
        "selected_id_gt_matched_model": None, "passed": False,
        "reason": "runner selection output was not observed with a contemporaneous frame",
    }
    selection_check["mission_selected_target"] = selected
    selected_id = selected.get("id") if isinstance(selected, Mapping) else None
    captured_id = selection_check.get("selected_candidate", {}).get("id") \
        if isinstance(selection_check.get("selected_candidate"), Mapping) else None
    selection_check["mission_id_matches_gt_checked_id"] = mission_id_matches(selected_id, captured_id)
    if selected is None:
        selection_check["reason"] = (selection_check.get("reason")
                                     or "follow mission produced no re-acquisition record")
    selection_check["passed"] = bool(selection_check.get("passed")
                                     and selection_check["mission_id_matches_gt_checked_id"])
    return selection_check


def _follow_ground_truth(episode: Episode, since_epoch: float, intended_model: Any,
                         follow_person: Mapping[str, Any] | None) -> tuple[float, float | None]:
    """Longest GT line-of-sight occlusion of the target and minimum static clearance while following."""
    gt_samples = episode._gt.snapshot() if episode._gt else []
    longest = current = 0.0
    prior = None
    path_points: list[tuple[float, float, float]] = []
    for sample in gt_samples:
        if float(sample.get("wall_time", 0.0)) < since_epoch:
            continue
        drone = episode._drone(sample)
        poses = sample.get("models", {})
        target = poses.get(str(intended_model)) if isinstance(poses, Mapping) else None
        if drone is None:
            continue
        path_points.append(drone[:3])
        if not isinstance(target, list) or len(target) < 3:
            continue
        target_torso = torso(follow_person or {"posture": "standing"}, tuple(float(v) for v in target[:3]))
        blocked = episode.world.blocking(drone[:3], target_torso) is not None
        sim_time = sample.get("sim_time")
        if prior is not None and isinstance(sim_time, (int, float)) and isinstance(prior, (int, float)):
            dt = max(0.0, float(sim_time) - float(prior))
            current = current + dt if blocked else 0.0
            longest = max(longest, current)
        prior = sim_time
    minimum_clearance = math.inf
    for point in path_points:
        clearance, _ = episode.world.clearance(point)
        minimum_clearance = min(minimum_clearance, clearance - 0.6)
    for first, second in zip(path_points, path_points[1:]):
        clearance, _ = episode.world.segment_clearance(first, second, radius=0.6)
        minimum_clearance = min(minimum_clearance, clearance)
    return longest, minimum_clearance if math.isfinite(minimum_clearance) else None


async def run(episode: Episode) -> dict[str, Any]:
    from sim.worldgen.plan import plan_follow

    landing = await return_home(episode)
    if not landing["passed"]:
        return {"passed": False, "reason": "pre-follow RTH did not land within 2 m", "pre_follow_rth": landing}
    plan = plan_follow(episode.world, episode.ground, episode.manifest)
    if (clearance_below_threshold(plan.lane_clearance_m)
            or clearance_below_threshold(plan.column_clearance_m)):
        return {"passed": False, "status": "skipped",
                "reason": f"planned follow path clearance below {CLEARANCE_THRESHOLD_M:.1f} m",
                "plan": {"lane_clearance_m": plan.lane_clearance_m, "column_clearance_m": plan.column_clearance_m,
                         "predicted_occlusion_s": plan.occluded_s}, "pre_follow_rth": landing}
    mission_path = _write_mission(episode, plan)
    before = time.time()
    intended_model = episode.follow.get("model")
    follow_person = next((person for person in episode.people if person.get("model") == intended_model), None)
    expected_colour = episode.follow.get("shirt_colour_attribute")
    returncode, selection_check = await _run_mission(episode, mission_path, intended_model, expected_colour)
    results_dir = episode._follow_results_dir()
    results = sorted(results_dir.glob("*.json")) if results_dir.exists() else []
    reacquire = json.loads(results[-1].read_text(encoding="utf-8")) if results else None
    selected = reacquire.get("selected_target") if isinstance(reacquire, Mapping) else None
    follow_epochs = _event_epochs(episode.directory / "events.jsonl", "follow")
    selection_check = _final_selection_check(selection_check, selected, intended_model, expected_colour)
    longest, minimum_clearance = _follow_ground_truth(
        episode, follow_epochs[0] if follow_epochs else before, intended_model, follow_person)
    passed = bool(returncode == 0 and isinstance(reacquire, Mapping)
                  and reacquire.get("assertions", {}).get("passed") is True
                  and selection_check["passed"])
    return {"passed": passed, "reason": None if passed else "runner/reacquisition or GT selection assertion failed",
            "pre_follow_rth": landing, "selection_check": selection_check,
            "plan": {"stage": plan.stage, "predicted_occlusion_s": plan.occluded_s,
                     "expected_occlusion_s": episode.follow.get("expected_occlusion_s"),
                     "lane_clearance_m": plan.lane_clearance_m, "column_clearance_m": plan.column_clearance_m,
                     "ttl_s": plan.ttl_s},
            "mission_exit_code": returncode,
            "reacquisition_result": reacquire, "selected_target_gt_check": selection_check,
            "gt_longest_predicted_occlusion_s": longest,
            "occlusion_expected_s": episode.follow.get("expected_occlusion_s"),
            "min_static_clearance_along_gt_path_m": minimum_clearance,
            "results_dir": str(results_dir.relative_to(ROOT)),
            "result_file": str(results[-1].relative_to(ROOT)) if results else None}
