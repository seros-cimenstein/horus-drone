"""Campaign aggregation and the ``summary.md`` report, from episode result records."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import math
import statistics
from typing import Any

from sim.campaign.scoring import score_detection_rows


def _numbers(values: Iterable[Any]) -> list[float]:
    return [float(value) for value in values if isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(float(value))]


def _distribution(values: Iterable[Any]) -> dict[str, Any]:
    numbers = _numbers(values)
    return {"count": len(numbers), "mean": statistics.mean(numbers) if numbers else None,
            "median": statistics.median(numbers) if numbers else None,
            "min": min(numbers) if numbers else None, "max": max(numbers) if numbers else None}


def _distribution_with_std(values: Iterable[Any]) -> dict[str, Any]:
    numbers = _numbers(values)
    stats = _distribution(numbers)
    stats["std"] = statistics.pstdev(numbers) if numbers else None
    return stats


def _new_accumulator() -> dict[str, Any]:
    return {
        "perception": [], "select": [], "approach_error": [], "orbit_error": [], "orbit_std": [],
        "orbit_visibility": [], "scan_coverage": [], "follow_reacquire": [], "landing_error": [],
        "hazard": [], "civilian": [], "rtf_mean": [], "rtf_p10": [],
        "camera_pose_vantages": [], "camera_info_hfov": [],
        "grounder": {"active": 0, "total": 0, "models": []},
    }


def _collect_perception(acc: dict[str, Any], perception: Mapping[str, Any], episode: Mapping[str, Any]) -> None:
    observations = perception.get("person_observations", [])
    if isinstance(observations, list):
        acc["perception"].extend(observations)
    frames = perception.get("vantage_frames", [])
    if not isinstance(frames, list):
        return
    for index, frame in enumerate(frames, 1):
        if not isinstance(frame, Mapping):
            continue
        pose = frame.get("actual_gt_camera_pose")
        if not isinstance(pose, Mapping):
            continue
        acc["camera_pose_vantages"].append({
            "world": episode.get("world"), "seed": episode.get("seed"),
            "vantage": frame.get("vantage", index), "target": frame.get("target"),
            "pitch_deg": pose.get("pitch_deg"), "yaw_offset_deg": pose.get("yaw_offset_deg"),
            "yaw_deg": pose.get("yaw_deg"), "roll_deg": pose.get("roll_deg"),
        })


def _collect_select(acc: dict[str, Any], selection: Mapping[str, Any], _episode: Mapping[str, Any]) -> None:
    if isinstance(selection.get("selected_matches_intended"), bool):
        acc["select"].append(selection["selected_matches_intended"])


def _collect_approach(acc: dict[str, Any], approach: Mapping[str, Any], _episode: Mapping[str, Any]) -> None:
    for trial in approach.get("trials", []):
        if isinstance(trial, Mapping):
            value = trial.get("distance_error_vs_5m_m")
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                acc["approach_error"].append(float(value))


def _collect_orbit(acc: dict[str, Any], orbit: Mapping[str, Any], _episode: Mapping[str, Any]) -> None:
    acc["orbit_error"].append(orbit.get("gt_radius_error_m"))
    acc["orbit_std"].append(orbit.get("gt_radius_std_m"))
    acc["orbit_visibility"].append(orbit.get("target_in_frame_fraction"))


def _collect_scan(acc: dict[str, Any], scan: Mapping[str, Any], _episode: Mapping[str, Any]) -> None:
    coverage = scan.get("coverage")
    if isinstance(coverage, Mapping):
        acc["scan_coverage"].append(coverage.get("fraction"))


def _collect_follow(acc: dict[str, Any], follow: Mapping[str, Any], _episode: Mapping[str, Any]) -> None:
    reacquire = follow.get("reacquisition_result", {})
    time_s = reacquire.get("reacquisition", {}).get("time_to_reacquire_s") if isinstance(reacquire, Mapping) else None
    acc["follow_reacquire"].append(time_s)


def _collect_rth(acc: dict[str, Any], rth: Mapping[str, Any], _episode: Mapping[str, Any]) -> None:
    acc["landing_error"].append(rth.get("landing_error_to_pad_m"))


def _collect_hazard(acc: dict[str, Any], hazard: Mapping[str, Any], _episode: Mapping[str, Any]) -> None:
    acc["hazard"].append({"passed": hazard.get("passed"), "crashed": hazard.get("crashed"),
                          "skipped": hazard.get("status") == "skipped"})


_TEST_COLLECTORS: dict[str, Callable[[dict[str, Any], Mapping[str, Any], Mapping[str, Any]], None]] = {
    "perception": _collect_perception, "select": _collect_select, "approach": _collect_approach,
    "orbit": _collect_orbit, "scan": _collect_scan, "follow": _collect_follow,
    "rth": _collect_rth, "hazard": _collect_hazard,
}


def _count_test_outcomes(buckets: dict[str, Any], tests: Mapping[str, Any], seed: Any) -> None:
    """Pass counts per test; skipped and not-run tests list the seed but are not attempts."""
    for name, result in tests.items():
        bucket = buckets.setdefault(name, {"total": 0, "passed": 0, "seeds": []})
        if isinstance(result, Mapping) and result.get("status") not in {"skipped", "not_run"}:
            bucket["total"] += 1
            bucket["passed"] += int(result.get("passed") is True)
            bucket["seeds"].append(seed)
        elif isinstance(result, Mapping):
            bucket["seeds"].append(seed)


def _collect_episode(acc: dict[str, Any], episode: Mapping[str, Any]) -> None:
    """Episode-wide signals: civilian check, RTF, CameraInfo and grounder state."""
    civilian = episode.get("civilian_check")
    if isinstance(civilian, Mapping) and isinstance(civilian.get("passed"), bool):
        acc["civilian"].append(civilian["passed"])
    rtf = episode.get("rtf")
    if isinstance(rtf, Mapping):
        acc["rtf_mean"].append(rtf.get("mean"))
        acc["rtf_p10"].append(rtf.get("p10"))
    camera_info = episode.get("camera_info")
    if isinstance(camera_info, Mapping):
        acc["camera_info_hfov"].append(camera_info.get("horizontal_fov_deg"))
    grounder = episode.get("grounder")
    if isinstance(grounder, Mapping):
        bucket = acc["grounder"]
        bucket["total"] += 1
        if grounder.get("active") is True:
            bucket["active"] += 1
        model = grounder.get("model")
        if isinstance(model, str) and model and model not in bucket["models"]:
            bucket["models"].append(model)


def _kind_metrics(acc: Mapping[str, Any]) -> dict[str, Any]:
    select = acc["select"]
    valid_hazards = [item for item in acc["hazard"] if item.get("skipped") is not True]
    skipped_hazards = [item for item in acc["hazard"] if item.get("skipped") is True]
    civilians = acc["civilian"]
    vantages = acc["camera_pose_vantages"]
    return {
        "select_correctness": {"correct": sum(select), "total": len(select),
                               "rate": sum(select) / len(select) if select else None},
        "approach_distance_error_m": _distribution(acc["approach_error"]),
        "orbit_radius_error_m": _distribution(acc["orbit_error"]),
        "orbit_radius_std_m": _distribution(acc["orbit_std"]),
        "orbit_target_in_frame_fraction": _distribution(acc["orbit_visibility"]),
        "scan_coverage_fraction": _distribution(acc["scan_coverage"]),
        "follow_reacquire_time_s": _distribution(acc["follow_reacquire"]),
        "landing_error_m": _distribution(acc["landing_error"]),
        "hazard": {"attempts": len(valid_hazards),
                   "safe_outcomes": sum(item.get("passed") is True for item in valid_hazards),
                   "crashes": sum(item.get("crashed") is True for item in valid_hazards),
                   "skipped_invalid_trials": len(skipped_hazards)},
        "civilian_checks": {"passed": sum(civilians), "total": len(civilians),
                            "pass_rate": sum(civilians) / len(civilians) if civilians else None},
        "grounder": acc["grounder"],
        "rtf_mean": _distribution(acc["rtf_mean"]), "rtf_p10": _distribution(acc["rtf_p10"]),
        "camera_info_hfov_deg": _distribution_with_std(acc["camera_info_hfov"]),
        "camera_pose": {
            "vantage_count": len(vantages),
            "pitch_deg": _distribution_with_std(pose.get("pitch_deg") for pose in vantages),
            "yaw_offset_deg": _distribution_with_std(pose.get("yaw_offset_deg") for pose in vantages),
            "roll_deg": _distribution_with_std(pose.get("roll_deg") for pose in vantages),
            "vantages": vantages,
        },
    }


def aggregate_campaign(episodes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate pass rates, perception strata, and headline metrics by world kind."""
    rows = [episode for episode in episodes if isinstance(episode, Mapping)]
    kinds: dict[str, dict[str, Any]] = {}
    accumulators: dict[str, dict[str, Any]] = {}
    for episode in rows:
        kind = str(episode.get("kind", "unknown"))
        kind_row = kinds.setdefault(kind, {"episodes": 0, "tests": {}})
        acc = accumulators.setdefault(kind, _new_accumulator())
        kind_row["episodes"] += 1
        tests = episode.get("tests", {})
        if isinstance(tests, Mapping):
            _count_test_outcomes(kind_row["tests"], tests, episode.get("seed"))
            for name, collect in _TEST_COLLECTORS.items():
                result = tests.get(name)
                if isinstance(result, Mapping):
                    collect(acc, result, episode)
        _collect_episode(acc, episode)
    for kind, kind_row in kinds.items():
        for bucket in kind_row["tests"].values():
            bucket["pass_rate"] = bucket["passed"] / bucket["total"] if bucket["total"] else None
        kind_row["detection"] = score_detection_rows(accumulators[kind]["perception"])
        kind_row["metrics"] = _kind_metrics(accumulators[kind])
    return {"episodes": len(rows), "by_kind": kinds}


def _cell(value: Any) -> str:
    """Markdown table cell text: pipes escaped, newlines flattened."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _angle_text(value: Any, *, signed: bool = False) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value):+.1f}" if signed else f"{float(value):.1f}"


def _angle_distribution_text(distribution: Any) -> str:
    if not isinstance(distribution, Mapping):
        return "n/a"
    mean, std = distribution.get("mean"), distribution.get("std")
    if not isinstance(mean, (int, float)) or not isinstance(std, (int, float)):
        return "n/a"
    return f"{float(mean):.1f} ± {float(std):.1f} (n={distribution.get('count', 0)})"


def _grounder_line(episodes: Iterable[Any]) -> str:
    rows = [episode.get("grounder") for episode in episodes
            if isinstance(episode, Mapping) and isinstance(episode.get("grounder"), Mapping)]
    if not rows:
        return "Grounder: not recorded"
    models = sorted({str(row["model"]) for row in rows if row.get("model")})
    return (f"Grounder: {sum(1 for row in rows if row.get('active') is True)}/{len(rows)} episodes active"
            + (f" ({', '.join(models)})" if models else ""))


def _pass_rate_lines(kinds: Mapping[str, Any]) -> list[str]:
    lines = ["## Test pass rates", "", "| Kind | Test | Passed / run | Rate | Seeds |", "|---|---|---:|---:|---|"]
    for kind in sorted(kinds):
        for test, result in sorted(kinds[kind].get("tests", {}).items()):
            rate = result.get("pass_rate")
            rate_text = "n/a" if rate is None else f"{rate:.1%}"
            seeds = ", ".join(str(seed) for seed in result.get("seeds", []))
            lines.append(f"| {kind} | {test} | {result['passed']} / {result['total']} | {rate_text} | {seeds} |")
    return lines


def _episode_lines(episodes: Iterable[Mapping[str, Any]]) -> list[str]:
    lines = ["## Episode results", "", "| World | Seed | Wall time | Status | Failure |", "|---|---:|---:|---|---|"]
    for episode in episodes:
        duration = episode.get("wall_duration_s")
        wall = "n/a" if duration is None else f"{duration:.1f}s ({duration / 60:.1f} min)"
        lines.append(f"| {episode.get('world', 'unknown')} | {episode.get('seed', '—')} | {wall} "
                     f"| {episode.get('status', 'unknown')} | {_cell(episode.get('failure') or '')} |")
    return lines


def _detection_lines(kinds: Mapping[str, Any]) -> list[str]:
    lines = ["## Detection recall", ""]
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
    return lines


_DISTRIBUTION_ROWS = (
    ("approach_distance_error_m", "approach signed distance error, m"),
    ("orbit_radius_error_m", "orbit radius signed error, m"),
    ("orbit_radius_std_m", "orbit radius standard deviation, m"),
    ("orbit_target_in_frame_fraction", "orbit target-in-frame fraction"),
    ("scan_coverage_fraction", "scan coverage fraction"),
    ("follow_reacquire_time_s", "follow reacquire time, s"),
    ("landing_error_m", "landing error, m"),
    ("rtf_mean", "mean RTF"),
    ("rtf_p10", "RTF p10"),
)


def _operational_lines(kind: str, metrics: Mapping[str, Any]) -> list[str]:
    select = metrics.get("select_correctness", {})
    select_rate = select.get("rate")
    select_text = "n/a" if select_rate is None else f"{select_rate:.1%}"
    lines = [f"| {kind} | select correctness | {select.get('correct', 0)}/{select.get('total', 0)} "
             f"({select_text}) |"]
    for key, label in _DISTRIBUTION_ROWS:
        stat = metrics.get(key, {})
        mean = stat.get("mean")
        text = "n/a" if mean is None else f"{mean:.3f} (n={stat.get('count', 0)})"
        lines.append(f"| {kind} | {label} | {text} |")
    hazard = metrics.get("hazard", {})
    lines.append(f"| {kind} | hazard safe / crashes | {hazard.get('safe_outcomes', 0)}/"
                 f"{hazard.get('attempts', 0)} / {hazard.get('crashes', 0)} |")
    if hazard.get("skipped_invalid_trials"):
        lines.append(f"| {kind} | hazard invalid trials skipped | {hazard['skipped_invalid_trials']} |")
    civilians = metrics.get("civilian_checks", {})
    rate = civilians.get("pass_rate")
    civilian_rate = "n/a" if rate is None else f"{rate:.1%}"
    lines.append(f"| {kind} | civilian checks | {civilians.get('passed', 0)}/{civilians.get('total', 0)} "
                 f"({civilian_rate}) |")
    grounder = metrics.get("grounder", {})
    grounder_models = ", ".join(str(model) for model in grounder.get("models", []) if model)
    grounder_suffix = f" ({grounder_models})" if grounder_models else ""
    lines.append(f"| {kind} | grounder active episodes | {grounder.get('active', 0)}/"
                 f"{grounder.get('total', 0)}{grounder_suffix} |")
    return lines


def _camera_pose_lines(kinds: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Measured camera pose", "",
        "| Kind | CameraInfo HFOV (°) | Vantages | Pitch mean ± σ (°) | Yaw offset mean ± σ (°) | Roll mean ± σ (°) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for kind in sorted(kinds):
        metrics = kinds[kind].get("metrics", {})
        pose = metrics.get("camera_pose", {})
        lines.append(
            f"| {kind} | {_angle_distribution_text(metrics.get('camera_info_hfov_deg', {}))} "
            f"| {pose.get('vantage_count', 0)} "
            f"| {_angle_distribution_text(pose.get('pitch_deg'))} "
            f"| {_angle_distribution_text(pose.get('yaw_offset_deg'))} "
            f"| {_angle_distribution_text(pose.get('roll_deg'))} |"
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
            seed = sample.get("seed")
            lines.append(
                f"| {kind} | {sample.get('world') or 'unknown'} | {seed if seed is not None else '—'} "
                f"| {sample.get('vantage', '—')} | {_cell(sample.get('target') or '—')} "
                f"| {_angle_text(sample.get('pitch_deg'))} "
                f"| {_angle_text(sample.get('yaw_deg'), signed=True)} "
                f"| {_angle_text(sample.get('yaw_offset_deg'), signed=True)} "
                f"| {_angle_text(sample.get('roll_deg'), signed=True)} |"
            )
    return lines


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    episodes = summary.get("episodes", [])
    aggregate = summary.get("aggregate", {})
    kinds = aggregate.get("by_kind", {}) if isinstance(aggregate, Mapping) else {}
    lines = ["# HORUS procedural campaign", "", f"Episodes: {summary.get('episode_count', 0)}", "",
             _grounder_line(episodes), "", *_pass_rate_lines(kinds), "", *_episode_lines(episodes), "",
             *_detection_lines(kinds), "",
             "## Operational metrics", "", "| Kind | Metric | Result |", "|---|---|---|"]
    for kind in sorted(kinds):
        lines.extend(_operational_lines(kind, kinds[kind].get("metrics", {})))
    lines.extend(["", *_camera_pose_lines(kinds), ""])
    return "\n".join(lines)
