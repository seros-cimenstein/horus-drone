#!/usr/bin/env python3
"""Run reproducible HORUS outdoor campaigns through the real SITL/MCP stack.

This host-side entry point intentionally uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "sim" / "run.sh"
DEFAULT_TESTS = ("perception", "select", "approach", "orbit", "scan", "follow", "rth", "hazard")
VALID_TESTS = frozenset(DEFAULT_TESTS)
SEED_SPACE_MAX = 99999


def random_seeds(count: int, *, low: int = 1, high: int = SEED_SPACE_MAX + 1) -> list[int]:
    """Distinct seeds for a randomized campaign; callers print them for reproduction."""
    if count < 1 or count > high - low:
        raise ValueError(f"random seed count must be between 1 and {high - low}")
    return secrets.SystemRandom().sample(range(low, high), count)


def candidate_bbox(candidate: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    """Return a valid xyxy box from a HORUS candidate without mutating its payload."""
    box = candidate.get("bbox", candidate.get("box"))
    if isinstance(box, Mapping):
        raw = (box.get("x1", box.get("left")), box.get("y1", box.get("top")),
               box.get("x2", box.get("right")), box.get("y2", box.get("bottom")))
    elif isinstance(box, (list, tuple)) and len(box) == 4:
        raw = box
    else:
        return None
    try:
        values = tuple(float(value) for value in raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    x1, y1, x2, y2 = values
    return values if x2 > x1 and y2 > y1 else None


def bbox_area(candidate: Mapping[str, Any]) -> float:
    box = candidate_bbox(candidate)
    return 0.0 if box is None else (box[2] - box[0]) * (box[3] - box[1])


def candidate_center(candidate: Mapping[str, Any]) -> tuple[float, float] | None:
    box = candidate_bbox(candidate)
    return None if box is None else ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def selection_min_confidence() -> float:
    """The runner's detector-confidence floor for ``select_candidate`` (single source of truth)."""
    from horus.repl.app import DEFAULT_SELECTION_MIN_CONFIDENCE

    return float(DEFAULT_SELECTION_MIN_CONFIDENCE)


def selection_rule(attribute: str, equals: Any, *, min_confidence: float | None = None) -> dict[str, Any]:
    """Describe the selection rule the runner applies, for scoring metadata."""
    return {
        "attribute": attribute,
        "equals": equals,
        "choose": "largest",
        "min_confidence": selection_min_confidence() if min_confidence is None else float(min_confidence),
    }


def select_largest_attribute_candidate(
    candidates: Iterable[Mapping[str, Any]], *, attribute: str = "shirt_colour", equals: Any,
    min_confidence: float | None = None,
) -> Mapping[str, Any] | None:
    """Apply the runner's ``select_candidate`` rule, including its confidence floor.

    The runner owns this rule (``horus.repl.app._find_candidate``): largest exact-attribute
    match whose detector confidence passes the floor, low ID breaking ties. Scoring must use
    the same function, or the harness will credit selections the runner can never make.
    """
    from horus.repl.app import _find_candidate

    floor = selection_min_confidence() if min_confidence is None else float(min_confidence)
    return _find_candidate(candidates, attribute=attribute, equals=equals, choose="largest",
                           min_confidence=floor)


def match_candidates_to_people(
    candidates: Iterable[Mapping[str, Any]],
    projected_people: Mapping[str, tuple[float, float] | None],
    *,
    expand_px: float = 15.0,
    nearest_px: float = 40.0,
) -> dict[str, Any]:
    """Greedily make deterministic one-to-one torso-to-box associations.

    A torso inside a box expanded by ``expand_px`` is preferred. Otherwise the
    nearest candidate centre is eligible only within ``nearest_px``.
    """
    table = [candidate for candidate in candidates if isinstance(candidate, Mapping)]
    edges: list[tuple[int, float, int, str]] = []
    errors: dict[str, float | None] = {name: None for name in projected_people}
    for person_index, (name, point) in enumerate(projected_people.items()):
        if point is None or not all(math.isfinite(value) for value in point):
            continue
        choices: list[tuple[int, float, int]] = []
        for candidate_index, candidate in enumerate(table):
            box = candidate_bbox(candidate)
            centre = candidate_center(candidate)
            if box is None or centre is None:
                continue
            distance = math.hypot(point[0] - centre[0], point[1] - centre[1])
            inside = (box[0] - expand_px <= point[0] <= box[2] + expand_px
                      and box[1] - expand_px <= point[1] <= box[3] + expand_px)
            if inside:
                choices.append((0, distance, candidate_index))
            elif distance <= nearest_px:
                choices.append((1, distance, candidate_index))
        if choices:
            priority, distance, candidate_index = min(choices)
            edges.append((priority, distance, person_index, name))
            # Store the best possible error for diagnostics even if another person
            # wins the one-to-one conflict for this candidate.
            errors[name] = distance
    assigned_people: dict[str, Mapping[str, Any]] = {}
    assigned_indices: set[int] = set()
    for _priority, _distance, _person_index, name in sorted(edges):
        # Re-evaluate the candidate selected by this person's best edge. If it is
        # already taken, use the next eligible candidate while preserving priority.
        point = projected_people[name]
        available: list[tuple[int, float, int]] = []
        for candidate_index, candidate in enumerate(table):
            if candidate_index in assigned_indices:
                continue
            box = candidate_bbox(candidate)
            centre = candidate_center(candidate)
            if box is None or centre is None or point is None:
                continue
            distance = math.hypot(point[0] - centre[0], point[1] - centre[1])
            inside = (box[0] - expand_px <= point[0] <= box[2] + expand_px
                      and box[1] - expand_px <= point[1] <= box[3] + expand_px)
            if inside:
                available.append((0, distance, candidate_index))
            elif distance <= nearest_px:
                available.append((1, distance, candidate_index))
        if available:
            _priority, distance, index = min(available)
            assigned_people[name] = table[index]
            assigned_indices.add(index)
            errors[name] = distance
    unmatched = [candidate for index, candidate in enumerate(table) if index not in assigned_indices]
    return {"matches": assigned_people, "unmatched_candidates": unmatched, "center_error_px": errors}


def score_detection_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compute overall recall and stratified detection counts from person observations."""
    entries = [row for row in rows if isinstance(row, Mapping)]
    strata: dict[str, dict[str, dict[str, int]]] = {
        field: {} for field in ("slant_bin", "posture", "clothing", "los_blocked")
    }
    total = detected = 0
    confusion: dict[str, dict[str, int]] = {}
    for row in entries:
        total += 1
        hit = bool(row.get("detected"))
        detected += int(hit)
        for field in strata:
            key = str(row.get(field, "unknown"))
            bucket = strata[field].setdefault(key, {"seen": 0, "detected": 0})
            bucket["seen"] += 1
            bucket["detected"] += int(hit)
        if hit:
            actual = str(row.get("clothing", "unknown"))
            reported = str(row.get("shirt_colour", "unknown"))
            confusion.setdefault(actual, {})[reported] = confusion.setdefault(actual, {}).get(reported, 0) + 1
    return {"seen": total, "detected": detected, "recall": detected / total if total else None,
            "strata": strata, "shirt_colour_confusion": confusion}


def _distribution(values: Iterable[Any]) -> dict[str, Any]:
    numbers = [float(value) for value in values if isinstance(value, (int, float))
               and not isinstance(value, bool) and math.isfinite(float(value))]
    return {"count": len(numbers), "mean": statistics.mean(numbers) if numbers else None,
            "median": statistics.median(numbers) if numbers else None,
            "min": min(numbers) if numbers else None, "max": max(numbers) if numbers else None}


def _distribution_with_std(values: Iterable[Any]) -> dict[str, Any]:
    numbers = [float(value) for value in values if isinstance(value, (int, float))
               and not isinstance(value, bool) and math.isfinite(float(value))]
    stats = _distribution(numbers)
    stats["std"] = statistics.pstdev(numbers) if numbers else None
    return stats




def aggregate_campaign(episodes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate pass rates, perception strata, and headline metrics by world kind."""
    rows = [episode for episode in episodes if isinstance(episode, Mapping)]
    kinds: dict[str, dict[str, Any]] = {}
    for episode in rows:
        kind = str(episode.get("kind", "unknown"))
        kind_row = kinds.setdefault(kind, {
            "episodes": 0, "tests": {}, "perception": [], "select": [],
            "approach_error": [], "orbit_error": [], "orbit_std": [], "orbit_visibility": [],
            "scan_coverage": [], "follow_reacquire": [], "landing_error": [],
            "hazard": [], "civilian": [], "rtf_mean": [], "rtf_p10": [],
            "camera_pose_vantages": [], "camera_info_hfov": [],
            "grounder": {"active": 0, "total": 0, "models": []},
        })
        kind_row["episodes"] += 1
        tests = episode.get("tests", {})
        if isinstance(tests, Mapping):
            for name, result in tests.items():
                test_bucket = kind_row["tests"].setdefault(name, {"total": 0, "passed": 0, "seeds": []})
                if isinstance(result, Mapping) and result.get("status") not in {"skipped", "not_run"}:
                    test_bucket["total"] += 1
                    test_bucket["passed"] += int(result.get("passed") is True)
                    test_bucket["seeds"].append(episode.get("seed"))
                elif isinstance(result, Mapping):
                    test_bucket["seeds"].append(episode.get("seed"))
            perception = tests.get("perception")
            if isinstance(perception, Mapping):
                observations = perception.get("person_observations", [])
                if isinstance(observations, list):
                    kind_row["perception"].extend(observations)
                frames = perception.get("vantage_frames", [])
                if isinstance(frames, list):
                    for index, frame in enumerate(frames, 1):
                        if not isinstance(frame, Mapping):
                            continue
                        pose = frame.get("actual_gt_camera_pose")
                        if not isinstance(pose, Mapping):
                            continue
                        kind_row["camera_pose_vantages"].append({
                            "world": episode.get("world"), "seed": episode.get("seed"),
                            "vantage": frame.get("vantage", index), "target": frame.get("target"),
                            "pitch_deg": pose.get("pitch_deg"), "yaw_offset_deg": pose.get("yaw_offset_deg"),
                            "yaw_deg": pose.get("yaw_deg"), "roll_deg": pose.get("roll_deg"),
                        })
            selection = tests.get("select")
            if isinstance(selection, Mapping) and isinstance(selection.get("selected_matches_intended"), bool):
                kind_row["select"].append(selection["selected_matches_intended"])
            approach = tests.get("approach")
            if isinstance(approach, Mapping):
                for trial in approach.get("trials", []):
                    if isinstance(trial, Mapping):
                        value = trial.get("distance_error_vs_5m_m")
                        if isinstance(value, (int, float)) and math.isfinite(float(value)):
                            kind_row["approach_error"].append(float(value))
            orbit = tests.get("orbit")
            if isinstance(orbit, Mapping):
                kind_row["orbit_error"].append(orbit.get("gt_radius_error_m"))
                kind_row["orbit_std"].append(orbit.get("gt_radius_std_m"))
                kind_row["orbit_visibility"].append(orbit.get("target_in_frame_fraction"))
            scan = tests.get("scan")
            if isinstance(scan, Mapping):
                coverage = scan.get("coverage")
                if isinstance(coverage, Mapping):
                    kind_row["scan_coverage"].append(coverage.get("fraction"))
            follow = tests.get("follow")
            if isinstance(follow, Mapping):
                reacquire = follow.get("reacquisition_result", {})
                time_s = reacquire.get("reacquisition", {}).get("time_to_reacquire_s") if isinstance(reacquire, Mapping) else None
                kind_row["follow_reacquire"].append(time_s)
            rth = tests.get("rth")
            if isinstance(rth, Mapping):
                kind_row["landing_error"].append(rth.get("landing_error_to_pad_m"))
            hazard = tests.get("hazard")
            if isinstance(hazard, Mapping):
                kind_row["hazard"].append({"passed": hazard.get("passed"), "crashed": hazard.get("crashed"),
                                           "skipped": hazard.get("status") == "skipped"})
        civilian = episode.get("civilian_check")
        if isinstance(civilian, Mapping) and isinstance(civilian.get("passed"), bool):
            kind_row["civilian"].append(civilian["passed"])
        rtf = episode.get("rtf")
        if isinstance(rtf, Mapping):
            kind_row["rtf_mean"].append(rtf.get("mean"))
            kind_row["rtf_p10"].append(rtf.get("p10"))
        camera_info = episode.get("camera_info")
        if isinstance(camera_info, Mapping):
            kind_row["camera_info_hfov"].append(camera_info.get("horizontal_fov_deg"))
        grounder = episode.get("grounder")
        if isinstance(grounder, Mapping):
            bucket = kind_row["grounder"]
            bucket["total"] += 1
            if grounder.get("active") is True:
                bucket["active"] += 1
            model = grounder.get("model")
            if isinstance(model, str) and model and model not in bucket["models"]:
                bucket["models"].append(model)
    for kind_row in kinds.values():
        for bucket in kind_row["tests"].values():
            bucket["pass_rate"] = bucket["passed"] / bucket["total"] if bucket["total"] else None
        detection_rows = kind_row.pop("perception")
        select = kind_row.pop("select")
        approach = kind_row.pop("approach_error")
        orbit_error = kind_row.pop("orbit_error")
        orbit_std = kind_row.pop("orbit_std")
        orbit_visibility = kind_row.pop("orbit_visibility")
        scan_coverage = kind_row.pop("scan_coverage")
        follow_reacquire = kind_row.pop("follow_reacquire")
        landing_error = kind_row.pop("landing_error")
        hazards = kind_row.pop("hazard")
        valid_hazards = [item for item in hazards if item.get("skipped") is not True]
        skipped_hazards = [item for item in hazards if item.get("skipped") is True]
        civilians = kind_row.pop("civilian")
        rtf_mean = kind_row.pop("rtf_mean")
        rtf_p10 = kind_row.pop("rtf_p10")
        camera_pose_vantages = kind_row.pop("camera_pose_vantages")
        camera_info_hfov = kind_row.pop("camera_info_hfov")
        grounder = kind_row.pop("grounder")
        pitch_stats = _distribution_with_std(pose.get("pitch_deg") for pose in camera_pose_vantages)
        yaw_offset_stats = _distribution_with_std(pose.get("yaw_offset_deg") for pose in camera_pose_vantages)
        roll_stats = _distribution_with_std(pose.get("roll_deg") for pose in camera_pose_vantages)
        kind_row["detection"] = score_detection_rows(detection_rows)
        kind_row["metrics"] = {
            "select_correctness": {"correct": sum(select), "total": len(select),
                                   "rate": sum(select) / len(select) if select else None},
            "approach_distance_error_m": _distribution(approach),
            "orbit_radius_error_m": _distribution(orbit_error),
            "orbit_radius_std_m": _distribution(orbit_std),
            "orbit_target_in_frame_fraction": _distribution(orbit_visibility),
            "scan_coverage_fraction": _distribution(scan_coverage),
            "follow_reacquire_time_s": _distribution(follow_reacquire),
            "landing_error_m": _distribution(landing_error),
            "hazard": {"attempts": len(valid_hazards),
                       "safe_outcomes": sum(item.get("passed") is True for item in valid_hazards),
                       "crashes": sum(item.get("crashed") is True for item in valid_hazards),
                       "skipped_invalid_trials": len(skipped_hazards)},
            "civilian_checks": {"passed": sum(civilians), "total": len(civilians),
                                "pass_rate": sum(civilians) / len(civilians) if civilians else None},
            "grounder": grounder,
            "rtf_mean": _distribution(rtf_mean), "rtf_p10": _distribution(rtf_p10),
            "camera_info_hfov_deg": _distribution_with_std(camera_info_hfov),
            "camera_pose": {
                "vantage_count": len(camera_pose_vantages), "pitch_deg": pitch_stats,
                "yaw_offset_deg": yaw_offset_stats, "roll_deg": roll_stats,
                "vantages": camera_pose_vantages,
            },
        }
    return {"episodes": len(rows), "by_kind": kinds}


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    grounder_rows = [episode.get("grounder") for episode in summary.get("episodes", [])
                     if isinstance(episode, Mapping) and isinstance(episode.get("grounder"), Mapping)]
    grounder_models = sorted({str(row["model"]) for row in grounder_rows if row.get("model")})
    grounder_line = "Grounder: not recorded"
    if grounder_rows:
        grounder_line = (f"Grounder: {sum(1 for row in grounder_rows if row.get('active') is True)}"
                         f"/{len(grounder_rows)} episodes active"
                         + (f" ({', '.join(grounder_models)})" if grounder_models else ""))
    lines = ["# HORUS procedural campaign", "", f"Episodes: {summary.get('episode_count', 0)}", "",
             grounder_line, "",
             "## Test pass rates", "", "| Kind | Test | Passed / run | Rate | Seeds |", "|---|---|---:|---:|---|"]
    aggregate = summary.get("aggregate", {})
    kinds = aggregate.get("by_kind", {}) if isinstance(aggregate, Mapping) else {}
    for kind in sorted(kinds):
        kind_data = kinds[kind]
        for test, result in sorted(kind_data.get("tests", {}).items()):
            rate = result.get("pass_rate")
            rate_text = "n/a" if rate is None else f"{rate:.1%}"
            seeds = ", ".join(str(seed) for seed in result.get("seeds", []))
            lines.append(f"| {kind} | {test} | {result['passed']} / {result['total']} | {rate_text} | {seeds} |")
    lines.extend(["", "## Episode results", "", "| World | Seed | Wall time | Status | Failure |", "|---|---:|---:|---|---|"])
    for episode in summary.get("episodes", []):
        world = episode.get("world", "unknown")
        seed = episode.get("seed", "—")
        duration = episode.get("wall_duration_s")
        wall = "n/a" if duration is None else f"{duration:.1f}s ({duration / 60:.1f} min)"
        status = episode.get("status", "unknown")
        failure = str(episode.get("failure") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {world} | {seed} | {wall} | {status} | {failure} |")
    lines.extend(["", "## Detection recall", ""])
    for kind in sorted(kinds):
        detection = kinds[kind].get("detection", {})
        recall = detection.get("recall")
        lines.append(f"- **{kind}:** {detection.get('detected', 0)}/{detection.get('seen', 0)} "
                     f"({('n/a' if recall is None else f'{recall:.1%}')})")
        for stratum, buckets in detection.get("strata", {}).items():
            text = ", ".join(f"{key} {value['detected']}/{value['seen']}" for key, value in sorted(buckets.items()))
            lines.append(f"  - {stratum}: {text or 'no observations'}")
        confusion = detection.get("shirt_colour_confusion", {})
        if confusion:
            lines.append("  - shirt colour true→reported: " + "; ".join(
                f"{truth}→{reported}:{count}" for truth, matrix in sorted(confusion.items())
                for reported, count in sorted(matrix.items())))
    lines.extend(["", "## Operational metrics", "", "| Kind | Metric | Result |", "|---|---|---|"])
    for kind in sorted(kinds):
        metrics = kinds[kind].get("metrics", {})
        select = metrics.get("select_correctness", {})
        select_rate = select.get("rate")
        select_text = "n/a" if select_rate is None else f"{select_rate:.1%}"
        lines.append(f"| {kind} | select correctness | {select.get('correct', 0)}/{select.get('total', 0)} "
                     f"({select_text}) |")
        for key, label in (
            ("approach_distance_error_m", "approach signed distance error, m"),
            ("orbit_radius_error_m", "orbit radius signed error, m"),
            ("orbit_radius_std_m", "orbit radius standard deviation, m"),
            ("orbit_target_in_frame_fraction", "orbit target-in-frame fraction"),
            ("scan_coverage_fraction", "scan coverage fraction"),
            ("follow_reacquire_time_s", "follow reacquire time, s"),
            ("landing_error_m", "landing error, m"),
            ("rtf_mean", "mean RTF"),
            ("rtf_p10", "RTF p10"),
        ):
            stat = metrics.get(key, {})
            mean = stat.get("mean")
            text = "n/a" if mean is None else f"{mean:.3f} (n={stat.get('count', 0)})"
            lines.append(f"| {kind} | {label} | {text} |")
        hazard = metrics.get("hazard", {})
        civilians = metrics.get("civilian_checks", {})
        lines.append(f"| {kind} | hazard safe / crashes | {hazard.get('safe_outcomes', 0)}/"
                     f"{hazard.get('attempts', 0)} / {hazard.get('crashes', 0)} |")
        if hazard.get("skipped_invalid_trials"):
            lines.append(f"| {kind} | hazard invalid trials skipped | {hazard['skipped_invalid_trials']} |")
        rate = civilians.get("pass_rate")
        civilian_rate = "n/a" if rate is None else f"{rate:.1%}"
        lines.append(f"| {kind} | civilian checks | {civilians.get('passed', 0)}/{civilians.get('total', 0)} "
                     f"({civilian_rate}) |")
        grounder = metrics.get("grounder", {})
        grounder_models = ", ".join(str(model) for model in grounder.get("models", []) if model)
        grounder_suffix = f" ({grounder_models})" if grounder_models else ""
        lines.append(f"| {kind} | grounder active episodes | {grounder.get('active', 0)}/"
                     f"{grounder.get('total', 0)}{grounder_suffix} |")
    def angle_text(value: Any, *, signed: bool = False) -> str:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            return "n/a"
        return f"{float(value):+.1f}" if signed else f"{float(value):.1f}"

    def angle_distribution_text(distribution: Any) -> str:
        if not isinstance(distribution, Mapping):
            return "n/a"
        mean, std = distribution.get("mean"), distribution.get("std")
        if not isinstance(mean, (int, float)) or not isinstance(std, (int, float)):
            return "n/a"
        return f"{float(mean):.1f} ± {float(std):.1f} (n={distribution.get('count', 0)})"

    lines.extend([
        "", "## Measured camera pose", "",
        "| Kind | CameraInfo HFOV (°) | Vantages | Pitch mean ± σ (°) | Yaw offset mean ± σ (°) | Roll mean ± σ (°) |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for kind in sorted(kinds):
        metrics = kinds[kind].get("metrics", {})
        pose = metrics.get("camera_pose", {})
        hfov = metrics.get("camera_info_hfov_deg", {})
        lines.append(
            f"| {kind} | {angle_distribution_text(hfov)} "
            f"| {pose.get('vantage_count', 0)} "
            f"| {angle_distribution_text(pose.get('pitch_deg'))} "
            f"| {angle_distribution_text(pose.get('yaw_offset_deg'))} "
            f"| {angle_distribution_text(pose.get('roll_deg'))} |"
        )
    lines.extend([
        "", "### Per-vantage sensor pose", "",
        "| Kind | World | Seed | Vantage | Target | Pitch (°) | Camera yaw (°) | Yaw offset (°) | Roll (°) |",
        "|---|---|---:|---:|---|---:|---:|---:|---:|",
    ])
    for kind in sorted(kinds):
        pose = kinds[kind].get("metrics", {}).get("camera_pose", {})
        for sample in pose.get("vantages", []) if isinstance(pose, Mapping) else []:
            if not isinstance(sample, Mapping):
                continue
            target = str(sample.get("target") or "—").replace("|", "\\|").replace("\n", " ")
            seed = sample.get("seed")
            lines.append(
                f"| {kind} | {sample.get('world') or 'unknown'} | {seed if seed is not None else '—'} "
                f"| {sample.get('vantage', '—')} | {target} | {angle_text(sample.get('pitch_deg'))} "
                f"| {angle_text(sample.get('yaw_deg'), signed=True)} "
                f"| {angle_text(sample.get('yaw_offset_deg'), signed=True)} "
                f"| {angle_text(sample.get('roll_deg'), signed=True)} |"
            )
    lines.append("")
    return "\n".join(lines)


def _tail_copy(source: Path, destination: Path, lines: int = 300) -> None:
    if not source.is_file():
        return
    with source.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        block = 8192
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            take = min(block, size)
            size -= take
            stream.seek(size)
            data = stream.read(take) + data
    destination.write_bytes(b"\n".join(data.splitlines()[-lines:]) + b"\n")


def _run(command: list[str], *, cwd: Path = ROOT, timeout: float = 300.0,
         check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=capture, timeout=timeout)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}: {detail}")
    return result

def _run_before_deadline(command: list[str], deadline: float, *, cap: float = 300.0) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise TimeoutError("episode wall timeout before command: " + " ".join(command))
    return _run(command, timeout=min(cap, remaining))



def container_state(container: str) -> dict[str, Any]:
    """Best-effort docker state of the simulation container after an episode died.

    ``OOMKilled`` is the only place docker records why a killed process never wrote a result,
    so the campaign keeps it in the episode record instead of losing it to ``stop --purge``.
    """
    try:
        completed = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", container],
            capture_output=True, text=True, timeout=20.0, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return {"status": payload.get("Status"), "exit_code": payload.get("ExitCode"),
            "oom_killed": bool(payload.get("OOMKilled")), "error": str(payload.get("Error") or "")}


def _missing_result_failure(code: int | None, state: Mapping[str, Any] | None) -> str:
    """Explicit failure text for an episode that exited without writing episode_result.json."""
    detail = f"episode wrote no result file; exit code {code}"
    if isinstance(state, Mapping) and state:
        status = state.get("status")
        if state.get("oom_killed"):
            detail += f"; container {status or 'unknown'} OOM-killed (exit {state.get('exit_code')})"
        elif status and status != "running":
            detail += f"; container {status} (exit {state.get('exit_code')})"
    return f"{detail}; see episode.log"


def _episode_record(kind: str, seed: int | None, world: str, episode_dir: Path) -> dict[str, Any]:
    return {"kind": kind, "seed": seed, "world": world, "episode_dir": str(episode_dir),
            "started_at": datetime.now(timezone.utc).isoformat(), "tests": {}, "status": "failed"}


def _one_episode(args: argparse.Namespace, kind: str, seed: int | None) -> dict[str, Any]:
    world = f"horus_{kind}" if args.canonical else f"horus_{kind}_p{seed}"
    episode_dir = args.out / world
    episode_dir.mkdir(parents=True, exist_ok=True)
    record = _episode_record(kind, seed, world, episode_dir)
    started = time.monotonic()
    episode_deadline = started + args.timeout_minutes * 60.0
    active_run: Path | None = None
    token = secrets.token_urlsafe(32)
    try:
        if args.canonical:
            _run_before_deadline([sys.executable, "-m", "sim.worldgen", "--seed", "2026", "--kind", kind],
                                 episode_deadline)
            manifest = ROOT / "sim" / "worlds" / f"{world}.layout.json"
            sdf = ROOT / "sim" / "worlds" / f"{world}.sdf"
        else:
            _run_before_deadline([sys.executable, "-m", "sim.worldgen", "--procedural", "--seed", str(seed),
                                  "--kind", kind, "--out", "sim/generated"], episode_deadline)
            generated = ROOT / "sim" / "generated" / world
            manifest = generated / f"{world}.layout.json"
            sdf = generated / f"{world}.sdf"
        if not manifest.is_file() or not sdf.is_file():
            raise FileNotFoundError(f"world generator did not create {manifest} and {sdf}")
        record["manifest"] = str(manifest.relative_to(ROOT))
        record["sdf"] = str(sdf.relative_to(ROOT))
        # run.sh start returns once the world is running (up to 900 s for 1.2 km generated worlds).
        _run_before_deadline([str(RUNNER), "start", "/horus/" + str(sdf.relative_to(ROOT))],
                             episode_deadline, cap=960.0)
        active_id = (ROOT / "sim" / "run" / ".active").read_text(encoding="utf-8").strip()
        active_run = ROOT / "sim" / "run" / active_id
        record["sim_run_id"] = active_id
        episode_path = "/horus/" + str(episode_dir.relative_to(ROOT))
        command = ["docker", "exec", "--workdir", "/horus", "--env", f"HORUS_MCP_TOKEN={token}",
                   os.environ.get("HORUS_SIM_CONTAINER_NAME", "horus-sim"),
                   "/opt/horus-venv/bin/python", "/horus/sim/scripts/campaign_episode.py",
                   "--kind", kind, "--seed", str(seed if seed is not None else (2027 if kind == "forest" else 2028)),
                   "--manifest", "/horus/" + str(manifest.relative_to(ROOT)), "--sdf", "/horus/" + str(sdf.relative_to(ROOT)),
                   "--episode-dir", episode_path, "--tests", ",".join(args.tests), "--vantages", str(args.vantages),
                   "--ttl-scale", str(args.ttl_scale)]
        log_path = episode_dir / "episode.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
            try:
                code = process.wait(timeout=max(0.1, episode_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                record["failure"] = f"episode exceeded {args.timeout_minutes:g} minute wall timeout"
                try:
                    _run(["docker", "exec", os.environ.get("HORUS_SIM_CONTAINER_NAME", "horus-sim"),
                          "/opt/horus-venv/bin/python", "-c",
                          "import os,signal; p='/horus/" + str(episode_dir.relative_to(ROOT)) + "/episode.pid'; "
                          "pid=int(open(p).read()); os.kill(pid,signal.SIGTERM)"], timeout=15.0, check=False)
                    process.wait(timeout=30.0)
                except (subprocess.TimeoutExpired, OSError, RuntimeError):
                    process.kill()
                    process.wait()
                code = 124
            finally:
                (episode_dir / "episode.pid").unlink(missing_ok=True)
        result_file = episode_dir / "episode_result.json"
        if result_file.is_file():
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    record.update(payload)
            except (json.JSONDecodeError, OSError) as exc:
                record["failure"] = f"invalid episode result: {exc}"
        else:
            # A clean exit code still means "failed" when nothing was written: the episode was
            # interrupted (host OOM, container stop, SIGKILL) before it could report anything.
            state = container_state(os.environ.get("HORUS_SIM_CONTAINER_NAME", "horus-sim"))
            if state:
                record["container_state"] = state
            record["failure"] = _missing_result_failure(code, state)
        if code and not record.get("failure"):
            record["failure"] = f"episode process exited {code}; see episode.log"
        if record.get("status") != "passed" and not record.get("failure"):
            tests = record.get("tests", {})
            failed = [
                f"{name}: {result.get('reason') or result.get('status', 'failed')}"
                for name, result in tests.items()
                if isinstance(result, Mapping) and result.get("passed") is not True
            ] if isinstance(tests, Mapping) else []
            if failed:
                record["failure"] = "; ".join(failed)
            elif isinstance(record.get("civilian_check"), Mapping) and record["civilian_check"].get("passed") is False:
                record["failure"] = "civilian scenario check failed"
        record["status"] = "passed" if not record.get("failure") and record.get("status") == "passed" else "failed"
    except (Exception, KeyboardInterrupt) as exc:
        record["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        record["wall_duration_s"] = time.monotonic() - started
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        if active_run is None:
            active_file = ROOT / "sim" / "run" / ".active"
            if active_file.exists():
                active_run = ROOT / "sim" / "run" / active_file.read_text(encoding="utf-8").strip()
        if active_run is not None and active_run.is_dir():
            log_dir = active_run / "logs"
            civilians = log_dir / "civilians.jsonl"
            if civilians.is_file():
                shutil.copy2(civilians, episode_dir / "civilians.jsonl")
            for name in ("gazebo.log", "sitl.log"):
                _tail_copy(log_dir / name, episode_dir / f"{name}.tail.log")
        try:
            state = _run([str(RUNNER), "status"], timeout=20.0, check=False, capture=True)
            if "state=running" in (state.stdout or "") or "state=created" in (state.stdout or ""):
                _run([str(RUNNER), "stop", "--purge"], timeout=120.0, check=False, capture=True)
            elif active_run is not None and active_run.exists():
                shutil.rmtree(active_run)
        except Exception as exc:
            record["failure"] = (record.get("failure", "") + f"; teardown failed: {exc}").strip("; ")
            record["status"] = "failed"
        if (episode_dir / "events.jsonl").is_file():
            record["events"] = str((episode_dir / "events.jsonl").relative_to(ROOT))
        record["status"] = "passed" if not record.get("failure") and record.get("status") == "passed" else "failed"
    return record


def _parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", action="store_true", help="run each canonical world once")
    parser.add_argument("--kinds", nargs="+", choices=("forest", "urban"), default=("forest", "urban"))
    parser.add_argument("--seeds", nargs="+", type=int, default=None,
                        help="procedural seeds per kind (default: 1 2 3)")
    parser.add_argument("--random", type=int, metavar="N", default=None,
                        help="draw N distinct random seeds instead of --seeds and print them")
    parser.add_argument("--tests", default=",".join(DEFAULT_TESTS), help="comma-separated tests")
    parser.add_argument("--vantages", type=int, default=8)
    parser.add_argument(
        "--ttl-scale", type=float, default=1.0,
        help="multiply per-command TTLs for hosts that run the sim below real time",
    )
    parser.add_argument("--out", type=Path, help="campaign output directory")
    parser.add_argument(
        "--timeout-minutes", type=float, default=None,
        help="wall timeout per episode (default: 45 min x --ttl-scale, because a host that "
             "needs TTL scaling also needs proportionally more wall time)",
    )
    args = parser.parse_args(argv)
    selected = tuple(dict.fromkeys(part.strip() for part in args.tests.split(",") if part.strip()))
    unknown = sorted(set(selected) - VALID_TESTS)
    if not selected or unknown:
        parser.error(f"invalid --tests values: {', '.join(unknown) if unknown else 'empty list'}")
    if args.vantages < 1:
        parser.error("--vantages must be positive")
    if args.timeout_minutes is None:
        # A host slow enough to need TTL scaling needs the same factor of wall time: a full
        # 8-test episode is ~20 min of simulation, i.e. ~90 min wall at RTF 0.25.
        args.timeout_minutes = 45.0 * max(1.0, args.ttl_scale)
    if not math.isfinite(args.timeout_minutes) or args.timeout_minutes <= 0:
        parser.error("--timeout-minutes must be finite and positive")
    if not math.isfinite(args.ttl_scale) or not 0.0 < args.ttl_scale <= 20.0:
        parser.error("--ttl-scale must be finite and between 0 and 20")
    if args.random is not None:
        if args.seeds is not None:
            parser.error("--random and --seeds are mutually exclusive")
        if not 1 <= args.random <= SEED_SPACE_MAX:
            parser.error(f"--random must be between 1 and {SEED_SPACE_MAX}")
        args.seeds = random_seeds(args.random)
    elif args.seeds is None:
        args.seeds = [1, 2, 3]
    elif not args.seeds or any(seed < 1 for seed in args.seeds):
        parser.error("--seeds must contain positive integers")
    args.tests = selected
    args.out = args.out or ROOT / "runs" / "campaign" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.out = args.out if args.out.is_absolute() else ROOT / args.out
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_cli(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.random is not None:
        seeds_text = " ".join(str(seed) for seed in args.seeds)
        print(f"Random seeds ({args.random}): {seeds_text}", flush=True)
        print(f"Reproduce with: --seeds {seeds_text}", flush=True)
    episodes: list[dict[str, Any]] = []
    schedule = [(kind, None) for kind in args.kinds] if args.canonical else [
        (kind, seed) for kind in args.kinds for seed in args.seeds
    ]
    for index, (kind, seed) in enumerate(schedule, 1):
        print(f"[{index}/{len(schedule)}] {kind} seed={seed if seed is not None else 'canonical'}", flush=True)
        episode = _one_episode(args, kind, seed)
        episodes.append(episode)
        print(f"  {episode.get('status')}: {episode.get('wall_duration_s', 0.0):.1f}s"
              + (f" — {episode.get('failure')}" if episode.get("failure") else ""), flush=True)
        rtf_mean = ((episode.get("rtf") or {}) if isinstance(episode.get("rtf"), Mapping) else {}).get("mean")
        if isinstance(rtf_mean, (int, float)) and 0.0 < rtf_mean < 0.5:
            print(f"  local hint: measured mean RTF {rtf_mean:.2f}; use --ttl-scale "
                  f"{max(1, round(1.0 / rtf_mean))} and a wall timeout of at least "
                  f"45 min x that scale (this run used {args.timeout_minutes:g} min)", flush=True)
        summary = {"created_at": datetime.now(timezone.utc).isoformat(), "episode_count": len(episodes),
                   "seeds": list(args.seeds), "kinds": list(args.kinds), "tests": list(args.tests),
                   "random_seed_count": args.random, "episodes": episodes,
                   "aggregate": aggregate_campaign(episodes)}
        (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.out / "summary.md").write_text(render_summary_markdown(summary), encoding="utf-8")
    return 0 if all(episode.get("status") == "passed" for episode in episodes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
