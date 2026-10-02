#!/usr/bin/env python3
"""Summarize one ground-truth run: target approach, return/landing state, and commander report."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

DEFAULT_TARGET_MODEL = "person_red"
DEFAULT_DISTRACTOR_MODELS = ("person_blue", "person_green", "person_laying")


def analyze(
    prefix: str | Path,
    *,
    target_model: str = DEFAULT_TARGET_MODEL,
    distractor_models: tuple[str, ...] = DEFAULT_DISTRACTOR_MODELS,
    success_distance_m: float = 2.0,
) -> dict[str, object]:
    if not math.isfinite(success_distance_m) or success_distance_m < 0:
        raise ValueError("success distance must be a finite non-negative number")
    prefix = Path(prefix)
    people = tuple(dict.fromkeys((target_model, *distractor_models)))
    samples = [
        json.loads(line)
        for line in Path(f"{prefix}_groundtruth.jsonl").read_text().splitlines()
        if line.strip()
    ]
    closest: dict[str, float] = {}
    for sample in samples:
        models = sample["models"]
        drone = models.get("horus_iris")
        if drone is None or drone[2] < 0.5:
            continue
        for name in people:
            if name in models:
                distance = math.hypot(drone[0] - models[name][0], drone[1] - models[name][1])
                closest[name] = min(closest.get(name, math.inf), distance)
    approached = min(closest, key=closest.get) if closest else None
    airborne = [
        sample["models"]["horus_iris"]
        for sample in samples
        if sample["models"].get("horus_iris", [0, 0, 0])[2] >= 0.5
    ]
    travelled = max(
        (math.hypot(p[0] - airborne[0][0], p[1] - airborne[0][1]) for p in airborne),
        default=0.0,
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
        (
            event.get("text") or event.get("result")
            for event in reversed(events)
            if event.get("kind") in ("claude_code_result", "claude_result", "final_result")
            and (event.get("text") or event.get("result"))
        ),
        None,
    )
    state_path = Path(f"{prefix}_final_state.json")
    armed = None
    try:
        armed = json.loads(state_path.read_text())["vehicle"]["armed"]
    except Exception:
        pass
    return {
        "run": prefix.name,
        "target_model": target_model,
        "distractor_models": list(distractor_models),
        "success_distance_m": success_distance_m,
        "closest_airborne_distance_m": {
            name: round(value, 2) for name, value in sorted(closest.items())
        },
        "approached_person": approached,
        "horizontal_travel_m": round(travelled, 2),
        "success_correct_target": (
            approached == target_model
            and closest.get(target_model, math.inf) <= success_distance_m
            and travelled >= 0.5
        ),
        "approach_distance_m": round(closest[approached], 2) if approached else None,
        "landed_disarmed": armed is False,
        "final_distance_from_pad_m": (
            round(math.hypot(drone_final[0] - pad[0], drone_final[1] - pad[1]), 3)
            if drone_final
            else None
        ),
        "commander_tool_calls": dict(tools),
        "commander_event_kinds": dict(Counter(event.get("kind") for event in events)),
        "commander_final_report": final_text,
        "commander_cost_usd": next(
            (
                event.get("total_cost_usd")
                for event in reversed(events)
                if event.get("kind") == "final_result"
            ),
            None,
        ),
        "wall_seconds": (
            int(Path(f"{prefix}_wall_seconds.txt").read_text())
            if Path(f"{prefix}_wall_seconds.txt").exists()
            else None
        ),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", help="run prefix used by the *_groundtruth.jsonl files")
    parser.add_argument("--target-model", default=DEFAULT_TARGET_MODEL)
    parser.add_argument(
        "--distractor-models",
        nargs="+",
        default=DEFAULT_DISTRACTOR_MODELS,
        help="one or more model names compared against the target",
    )
    parser.add_argument("--success-distance-m", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        summary = analyze(
            args.prefix,
            target_model=args.target_model,
            distractor_models=tuple(args.distractor_models),
            success_distance_m=args.success_distance_m,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
