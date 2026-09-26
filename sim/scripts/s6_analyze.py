#!/usr/bin/env python3
"""Summarize one S6 run: which person the drone actually approached (Gazebo ground truth), how
close, whether it returned home and landed, the tools the commander called, and its final report."""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

PEOPLE = ("person_red", "person_blue", "person_green", "person_laying")


def main(prefix: str) -> None:
    samples = [json.loads(line) for line in Path(f"{prefix}_groundtruth.jsonl").read_text().splitlines() if line]
    closest: dict[str, float] = {}
    for sample in samples:
        models = sample["models"]
        drone = models.get("horus_iris")
        if drone is None or drone[2] < 0.5:  # only count while airborne
            continue
        for name in PEOPLE:
            if name in models:
                distance = math.hypot(drone[0] - models[name][0], drone[1] - models[name][1])
                closest[name] = min(closest.get(name, math.inf), distance)
    approached = min(closest, key=closest.get) if closest else None
    airborne = [s["models"]["horus_iris"] for s in samples if s["models"].get("horus_iris", [0, 0, 0])[2] >= 0.5]
    travelled = max(
        (math.hypot(p[0] - airborne[0][0], p[1] - airborne[0][1]) for p in airborne), default=0.0
    )
    final = samples[-1]["models"] if samples else {}
    drone_final = final.get("horus_iris")
    pad = final.get("home_apriltag_pad", [0.0, 0.0, 0.0])
    events = []
    events_path = Path(f"{prefix}_events.jsonl")
    if events_path.exists():
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    tools = Counter(
        str(event.get("tool") or event.get("name")).removeprefix("mcp__horus__")
        for event in events
        if event.get("kind") in ("claude_tool_use", "tool_use", "tool_call")
    )
    final_text = next(
        (event.get("text") or event.get("result") for event in reversed(events)
         if event.get("kind") in ("claude_code_result", "claude_result", "final_result") and (event.get("text") or event.get("result"))),
        None,
    )
    state_path = Path(f"{prefix}_final_state.json")
    armed = None
    try:
        armed = json.loads(state_path.read_text())["vehicle"]["armed"]
    except Exception:
        pass
    summary = {
        "run": Path(prefix).name,
        "closest_airborne_distance_m": {name: round(value, 2) for name, value in sorted(closest.items())},
        "approached_person": approached,
        "horizontal_travel_m": round(travelled, 2),
        # The red-shirt person stands 2.4 m from the pad, so require a real approach: the drone moved,
        # got within 2.0 m of person_red, and person_red is the person it came closest to.
        "success_correct_target": (
            approached == "person_red" and closest.get("person_red", math.inf) <= 2.0 and travelled >= 0.5
        ),
        "approach_distance_m": round(closest[approached], 2) if approached else None,
        "landed_disarmed": armed is False,
        "final_distance_from_pad_m": (
            round(math.hypot(drone_final[0] - pad[0], drone_final[1] - pad[1]), 3) if drone_final else None
        ),
        "commander_tool_calls": dict(tools),
        "commander_event_kinds": dict(Counter(event.get("kind") for event in events)),
        "commander_final_report": final_text,
        "commander_cost_usd": next(
            (event.get("total_cost_usd") for event in reversed(events) if event.get("kind") == "final_result"), None
        ),
        "wall_seconds": int(Path(f"{prefix}_wall_seconds.txt").read_text()) if Path(f"{prefix}_wall_seconds.txt").exists() else None,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main(sys.argv[1])
