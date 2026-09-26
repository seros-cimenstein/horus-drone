#!/usr/bin/env python3
"""Judge one S5 run against Gazebo ground truth (design S5: follow(id) survives a ~2 s occlusion).

Occlusion: person_blue is fully hidden behind corridor_screen as seen from the drone's actual pose
(person width 0.5 m, height 1.75 m, projected onto the screen's near face). Survival: in the 3 s of
simulation time after the longest occlusion ends, the drone keeps pointing at person_blue, not at
person_red. Usage: s5_analyze.py GROUNDTRUTH.jsonl RESULT.json
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
PERSON_HALF_WIDTH_M = 0.25
PERSON_TOP_M = 1.75
SURVIVAL_WINDOW_S = 3.0
MIN_OCCLUSION_S = 1.5
MAX_HEADING_ERROR_DEG = 25.0


def _screen() -> tuple[float, float, float, float, float, float]:
    world = ElementTree.parse(ROOT / "sim/worlds/horus_indoor.sdf").getroot()
    model = world.find(".//model[@name='corridor_screen']")
    x, y, z, *_ = map(float, model.findtext("pose").split())
    sx, sy, sz = map(float, model.findtext("./link/collision/geometry/box/size").split())
    return x, y, z, sx, sy, sz


def hidden(drone: list[float], person: list[float], screen: tuple[float, ...]) -> bool:
    cx, cy, cz, sx, sy, sz = screen
    near, far = cy - sy / 2, cy + sy / 2
    dx, dy, dz = drone[0], drone[1], drone[2]
    px, py = person[0], person[1]
    if dy >= near or py <= far:
        return False
    fraction = (near - dy) / (py - dy)
    # Person edges perpendicular to the line of sight (top view), projected onto the near face.
    bearing = math.atan2(py - dy, px - dx)
    ox, oy = -math.sin(bearing) * PERSON_HALF_WIDTH_M, math.cos(bearing) * PERSON_HALF_WIDTH_M
    edges = []
    for ex, ey in ((px + ox, py + oy), (px - ox, py - oy)):
        f = (near - dy) / (ey - dy) if ey > dy else 0.0
        edges.append(dx + (ex - dx) * f)
    top = dz + (PERSON_TOP_M - dz) * fraction
    return cx - sx / 2 <= min(edges) and max(edges) <= cx + sx / 2 and top <= cz + sz / 2


def heading_error_deg(drone: list[float], target: list[float]) -> float:
    bearing = math.atan2(target[1] - drone[1], target[0] - drone[0])
    return abs(math.degrees((bearing - drone[3] + math.pi) % (2 * math.pi) - math.pi))


def main(gt_path: str, result_path: str) -> dict:
    result = json.loads(Path(result_path).read_text())
    start = datetime.fromisoformat(result["started_at"]).timestamp()
    end = datetime.fromisoformat(result["finished_at"]).timestamp()
    samples = [json.loads(line) for line in Path(gt_path).read_text().splitlines() if line.strip()]
    samples = [
        s for s in samples
        if start <= s["wall_time"] <= end and s.get("sim_time") is not None
        and "horus_iris" in s["models"] and s["models"]["horus_iris"][2] > 1.0
    ]
    screen = _screen()
    episodes, current = [], None
    for sample in samples:
        is_hidden = hidden(sample["models"]["horus_iris"], sample["models"]["person_blue"], screen)
        if is_hidden and current is None:
            current = [sample["sim_time"], sample["sim_time"]]
        elif is_hidden:
            current[1] = sample["sim_time"]
        elif current is not None:
            current[1] = sample["sim_time"]  # visible again at this sample
            episodes.append(current)
            current = None
    if current is not None:
        episodes.append(current)
    longest = max(episodes, key=lambda e: e[1] - e[0]) if episodes else None
    occlusion_s = (longest[1] - longest[0]) if longest else 0.0
    blue_errors, red_errors = [], []
    if longest:
        for sample in samples:
            if longest[1] <= sample["sim_time"] <= longest[1] + SURVIVAL_WINDOW_S:
                drone = sample["models"]["horus_iris"]
                blue_errors.append(heading_error_deg(drone, sample["models"]["person_blue"]))
                red_errors.append(heading_error_deg(drone, sample["models"]["person_red"]))
    blue_median = statistics.median(blue_errors) if blue_errors else None
    red_median = statistics.median(red_errors) if red_errors else None
    survived = (
        blue_median is not None and blue_median <= MAX_HEADING_ERROR_DEG and blue_median < red_median
    )
    reacquisition = result.get("reacquisition", {})
    summary = {
        "run_result": Path(result_path).name,
        "occlusion_episodes_s": [round(e[1] - e[0], 2) for e in episodes],
        "longest_occlusion_sim_s": round(occlusion_s, 2),
        "post_occlusion_heading_error_deg": {
            "to_blue_median": None if blue_median is None else round(blue_median, 1),
            "to_red_median": None if red_median is None else round(red_median, 1),
            "samples": len(blue_errors),
        },
        "tracker": {
            "same_identity": reacquisition.get("same_identity"),
            "reacquisitions_s": [
                round(e["time_to_reacquire_s"], 2)
                for e in result.get("follow", {}).get("tracking", {}).get("reacquisitions", [])
            ],
            "embedding_similarity": reacquisition.get("embedding_similarity"),
        },
        "occluded_long_enough": occlusion_s >= MIN_OCCLUSION_S,
        "survived_occlusion": survived,
        "passed": occlusion_s >= MIN_OCCLUSION_S and survived,
    }
    return summary


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], sys.argv[2]), indent=2))
