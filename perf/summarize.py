#!/usr/bin/env python3
"""Summarize one perf/run_mission.sh run directory into summary.json and summary.md.

Every figure is computed from the run's own logs; a value that cannot be measured is null/"n/a".
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

PEOPLE = ("person_red", "person_blue", "person_green", "person_laying")
LIVE_VIEW_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*perception (\S+) fps \| \S+ (\S+) ms \| track\+ReID (\S+) ms"
)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _stats(values: list[float], digits: int = 1) -> dict[str, float] | None:
    if not values:
        return None
    return {"min": round(min(values), digits), "avg": round(mean(values), digits),
            "max": round(max(values), digits), "n": len(values)}


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def commander(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Tool-call counts and timing. A model turn is the wait from the previous tool result (or the
    CLI start) until Claude issues its next tool call or final answer; a tool round-trip is one
    tool call until its result."""
    pending: dict[str, tuple[float, str]] = {}
    last_idle: float | None = None
    model_turns: list[float] = []
    tool_times: list[float] = []
    tools: dict[str, int] = {}
    failed: list[str] = []
    final: dict[str, Any] | None = None
    for event in events:
        kind, stamp = event.get("kind"), _epoch(event.get("timestamp"))
        if stamp is None:
            continue
        if kind == "claude_code_started":
            last_idle = stamp
        elif kind == "tool_use":
            name = str(event.get("tool", "")).removeprefix("mcp__horus__")
            tools[name] = tools.get(name, 0) + 1
            if not pending and last_idle is not None:
                model_turns.append(stamp - last_idle)
            pending[str(event.get("tool_use_id"))] = (stamp, name)
        elif kind == "tool_result":
            started = pending.pop(str(event.get("tool_use_id")), None)
            if started is not None:
                tool_times.append(stamp - started[0])
                if event.get("is_error"):
                    failed.append(f"{started[1]}: {str(event.get('summary'))[:160]}")
            last_idle = stamp
        elif kind == "final_result":
            if not pending and last_idle is not None:
                model_turns.append(stamp - last_idle)
            final = event
    return {
        "tool_calls": sum(tools.values()),
        "tool_calls_by_name": dict(sorted(tools.items())),
        "failed_tool_calls": len(failed),
        "failed_tool_call_details": failed,
        "model_turn_s": _stats(model_turns),
        "tool_round_trip_s": _stats(tool_times),
        "timed_out": any(event.get("kind") == "claude_code_timeout" for event in events),
        "num_turns": None if final is None else final.get("num_turns"),
        "cost_usd": None if final is None else final.get("total_cost_usd"),
        "final_report": None if final is None else final.get("text"),
    }


def perception(mcp_log: Path, started: float, ended: float) -> dict[str, Any]:
    fps, detector, tracker = [], [], []
    if mcp_log.exists():
        for line in mcp_log.read_text(encoding="utf-8", errors="replace").splitlines():
            match = LIVE_VIEW_RE.match(line)
            if not match:
                continue
            stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            if not started <= stamp <= ended:
                continue
            for bucket, text in ((fps, match.group(2)), (detector, match.group(3)), (tracker, match.group(4))):
                value = _number(text)
                if value is not None:
                    bucket.append(value)
    return {"fps": _stats(fps, 2), "detector_ms": _stats(detector), "track_reid_ms": _stats(tracker),
            "frame_latency_ms": _stats([d + t for d, t in zip(detector, tracker)])}


def real_time_factor(samples: list[dict[str, Any]], started: float, ended: float) -> float | None:
    window = [(w, s["sim_time_s"]) for s in samples
              if (w := _epoch(s.get("wall_time"))) is not None and started <= w <= ended]
    if len(window) < 2 or window[-1][0] <= window[0][0]:
        return None
    return round((window[-1][1] - window[0][1]) / (window[-1][0] - window[0][0]), 3)


def ground_truth(samples: list[dict[str, Any]], started: float, land_commanded: float | None) -> dict[str, Any]:
    samples = [s for s in samples if s.get("wall_time", 0) >= started]
    closest: dict[str, float] = {}
    airborne = []
    touchdown: float | None = None
    for sample in samples:
        drone = sample["models"].get("horus_iris")
        if drone is None or drone[2] < 0.5:
            if airborne and touchdown is None and drone is not None and drone[2] < 0.3:
                touchdown = sample["wall_time"]
            continue
        airborne.append(drone)
        for name in PEOPLE:
            if name in sample["models"]:
                other = sample["models"][name]
                closest[name] = min(closest.get(name, math.inf), math.hypot(drone[0] - other[0], drone[1] - other[1]))
    final = samples[-1]["models"] if samples else {}
    drone_final, pad = final.get("horus_iris"), final.get("home_apriltag_pad", [0.0, 0.0, 0.0])
    approached = min(closest, key=closest.get) if closest else None
    travelled = max((math.hypot(p[0] - airborne[0][0], p[1] - airborne[0][1]) for p in airborne), default=0.0)
    return {
        "took_off": bool(airborne),
        # True when the hold timeout sent the vehicle home before the commander asked for it.
        "landed_before_commanded": None if touchdown is None else (
            land_commanded is None or touchdown < land_commanded
        ),
        "closest_airborne_distance_m": {name: round(value, 2) for name, value in sorted(closest.items())},
        "approached_person": approached,
        "horizontal_travel_m": round(travelled, 2),
        "final_height_m": None if drone_final is None else round(drone_final[2], 2),
        "landing_error_m": None if drone_final is None else round(
            math.hypot(drone_final[0] - pad[0], drone_final[1] - pad[1]), 3),
    }


def main(run_dir: str) -> None:
    out = Path(run_dir)
    window = json.loads((out / "window.json").read_text())
    started, ended = float(window["started"]), float(window["ended"])
    config = yaml.safe_load((out / "horus.yaml").read_text())
    armed = None
    try:
        armed = json.loads((out / "final_state.json").read_text())["vehicle"]["armed"]
    except Exception:
        pass
    events = _jsonl(out / "events.jsonl")
    land_commanded = next(
        (_epoch(event.get("timestamp")) for event in events
         if event.get("kind") == "tool_use" and str(event.get("tool", "")).endswith(("__rth", "__land"))),
        None,
    )
    truth = ground_truth(_jsonl(out / "groundtruth.jsonl"), started, land_commanded)
    summary: dict[str, Any] = {
        "run": out.name,
        "mission": window["mission"],
        "commit": (out / "commit.txt").read_text().strip(),
        "uncommitted_changes": (out / "dirty.txt").read_text().split("\n")[:-1],
        "config": {
            "claude_model": config["commander"].get("claude_model"),
            "claude_effort": config["commander"].get("claude_effort"),
            "command_ttl_s": config["executor"].get("command_ttl_s"),
            "link_loss_rth_delay_s": config["executor"].get("link_loss_rth_delay_s"),
            "onnx_intra_op_num_threads": config["perception"].get("onnx_intra_op_num_threads"),
            "max_fps": config["perception"].get("max_fps"),
        },
        "mission_wall_s": round(ended - started, 1),
        "mission_return_code": window["return_code"],
        "real_time_factor": real_time_factor(_jsonl(out / "rtf.jsonl"), started, ended),
        "perception": perception(out / "mcp.log", started, ended),
        "commander": commander(events),
        "ground_truth": truth,
        "landed_disarmed": armed is False,
    }
    if "S6" in window["mission"]:
        closest = truth["closest_airborne_distance_m"]
        # Same rule as sim/scripts/s6_analyze.py: a real approach to person_red, nearest of all people.
        summary["s6_correct_target"] = (
            truth["approached_person"] == "person_red"
            and closest.get("person_red", math.inf) <= 2.0
            and truth["horizontal_travel_m"] >= 0.5
        )
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def span(stats: dict[str, float] | None, unit: str) -> str:
        return "n/a" if stats is None else f"{stats['min']} / {stats['avg']} / {stats['max']} {unit} (n={stats['n']})"

    cmd, per = summary["commander"], summary["perception"]
    lines = [
        f"### {out.name}",
        "",
        f"- Mission: `{summary['mission']}` at commit `{summary['commit'][:10]}`"
        + (" with uncommitted changes" if summary["uncommitted_changes"] else ""),
        "- Config: " + ", ".join(f"`{key}={value}`" for key, value in summary["config"].items()),
        f"- Mission wall-clock time: {summary['mission_wall_s']} s (exit code {summary['mission_return_code']})",
        f"- Sim real-time factor (average): {summary['real_time_factor']}",
        f"- Perception fps (min / avg / max): {span(per['fps'], 'fps')}",
        f"- Per-frame latency, detector + tracker (min / avg / max): {span(per['frame_latency_ms'], 'ms')}",
        f"- Commander tool calls: {cmd['tool_calls']}, failed: {cmd['failed_tool_calls']}",
        f"- Model turn time (min / avg / max): {span(cmd['model_turn_s'], 's')}",
        f"- Tool round-trip time (min / avg / max): {span(cmd['tool_round_trip_s'], 's')}",
        f"- Commander timed out: {cmd['timed_out']}",
        f"- Landed and disarmed: {summary['landed_disarmed']}; landing error: {truth['landing_error_m']} m",
        f"- Returned home by hold timeout before the commander asked: {truth['landed_before_commanded']}",
    ]
    if "s6_correct_target" in summary:
        lines.append(
            f"- S6 success (approached the red-shirt person): {summary['s6_correct_target']}; "
            f"closest approach: {truth['closest_airborne_distance_m']}"
        )
    for detail in cmd["failed_tool_call_details"]:
        lines.append(f"  - failed call: {detail}")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1])
