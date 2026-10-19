"""Scan scenario: a lawnmower scan of a clear area with people inside, scored on GT coverage."""
from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from sim.campaign.episode import Episode

Rectangle = tuple[float, float, float, float, str]
SCAN_SPACING_M = 6.0


def scan_rectangles(kind: str, manifest: Mapping[str, Any]) -> list[Rectangle]:
    """Candidate scan areas, largest first: the urban plaza, or the forest clearing and its cores."""
    areas = manifest.get("areas", {})
    rectangles: list[Rectangle] = []
    if kind == "urban" and isinstance(areas.get("plaza"), Mapping):
        center = areas["plaza"].get("center", [0, 0])
        half = float(areas["plaza"].get("half_size", 9.0)) + float(areas.get("sidewalk_offset_m", 0.0))
        rectangles.append((float(center[0]) - half, float(center[0]) + half,
                           float(center[1]) - half, float(center[1]) + half, "plaza-and-sidewalks"))
        half_small = max(6.0, half - float(areas.get("sidewalk_offset_m", 0.0)) / 2)
        rectangles.append((float(center[0]) - half_small, float(center[0]) + half_small,
                           float(center[1]) - half_small, float(center[1]) + half_small, "plaza"))
    elif kind == "forest" and isinstance(areas.get("clearing"), Mapping):
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


def scan_clearance(world: Any, rect: Rectangle, altitude: float) -> float:
    """Minimum static clearance of a 2 m grid over ``rect`` at ``altitude`` (rows and columns)."""
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
        minimum = min(minimum, world.segment_clearance(a, b, radius=0.6)[0])
    for first, second in zip(ys, ys[1:]):
        for x in xs:
            minimum = min(minimum, world.segment_clearance((x, first, altitude),
                                                           (x, second, altitude), radius=0.6)[0])
    return minimum


def plan_scan_area(kind: str, manifest: Mapping[str, Any], world: Any) -> tuple[Rectangle, float, float] | None:
    """First rectangle/altitude pair with at least 2 m static clearance."""
    for rect in scan_rectangles(kind, manifest):
        for altitude in (10.0, 12.0, 8.0):
            clearance = scan_clearance(world, rect, altitude)
            if clearance >= 2.0:
                return rect, altitude, clearance
    return None


async def run(episode: Episode) -> dict[str, Any]:
    if not await episode._ensure_airborne():
        return episode._takeoff_failure("vehicle could not take off")
    plan = plan_scan_area(episode.kind, episode.manifest, episode.world)
    if plan is None:
        return {"passed": False, "status": "skipped", "reason": "no scan rectangle has ≥2 m clearance",
                "candidates": [{"rect": rect, "clearance_10m": scan_clearance(episode.world, rect, 10.0)}
                               for rect in scan_rectangles(episode.kind, episode.manifest)]}
    (xmin, xmax, ymin, ymax, label), selected_alt, clearance = plan
    area = {"north_min_m": ymin, "north_max_m": ymax, "east_min_m": xmin, "east_max_m": xmax,
            "altitude_m": selected_alt, "spacing_m": SCAN_SPACING_M}
    inside_people = [str(person.get("model")) for person in episode.people
                     if (position := person.get("position")) is not None
                     and isinstance(position, (list, tuple)) and len(position) >= 2
                     and xmin <= float(position[0]) <= xmax and ymin <= float(position[1]) <= ymax]
    if not inside_people:
        # A clear rectangle with nobody inside can never score detection coverage; skipping
        # with an explicit reason is honest, and world_report keeps this from being routine.
        return {"passed": False, "status": "skipped",
                "reason": "no manifest person inside the planned scan area",
                "area_label": label, "area": area, "static_clearance_m": clearance}
    # Position at the first safe scan corner by using ordinary guarded goto.
    if episode._drone() is None:
        return {"passed": False, "reason": "GT drone pose unavailable"}
    route = await episode._navigate((xmin, ymin, float(selected_alt)), math.atan2(ymax - ymin, xmax - xmin))
    if not route.get("passed"):
        return {"passed": False, "reason": route.get("reason"), "clearance_m": clearance}
    episode._record_gt()
    seen: set[str] = set()
    total_models: set[str] = set()
    polls: list[dict[str, Any]] = []

    async def observe_scan() -> None:
        frame = await episode._observe(f"scan-{len(polls) + 1:04d}", save_frame=False)
        rows, false_positives, _matching = episode._detection_rows(frame)
        inside = []
        for person in episode.people:
            pos = episode._people_gt(frame.get("gt_sample")).get(str(person.get("model")))
            if pos and xmin <= pos[0] <= xmax and ymin <= pos[1] <= ymax:
                total_models.add(str(person.get("model")))
                inside.append(str(person.get("model")))
                row = next((row for row in rows if row["model"] == person.get("model") and row["detected"]), None)
                if row:
                    seen.add(str(person.get("model")))
        polls.append({"frame_id": frame.get("frame_id"), "timestamp": frame.get("timestamp"),
                      "matched_in_rectangle": inside, "matched_models": [row["model"] for row in rows if row["detected"]],
                      "false_positives": false_positives})

    scan_start_sim = episode._sim_now()
    submitted = await episode._tool("scan", {"area": area, "ttl_s": episode._scaled_ttl(240.0)})
    try:
        task = await episode._wait_tool_task(submitted, max_wall_s=600.0, observer=observe_scan)
    except Exception as exc:
        task = {"status": "failed", "error": str(exc)}
    await observe_scan()
    coverage = len(seen) / len(total_models) if total_models else 0.0
    passed = task.get("status") == "succeeded" and coverage > 0.0
    return {"passed": passed, "reason": None if passed else "scan task or detection coverage failed",
            "area_label": label, "area": area, "spacing_m": SCAN_SPACING_M, "altitude_m": selected_alt,
            "static_clearance_m": clearance, "task_result": task,
            "clamp_reasons": task.get("clamp_reason", task.get("clamp_reasons")),
            "coverage": {"inside_people": len(total_models), "matched": len(seen), "fraction": coverage,
                         "models": sorted(total_models), "matched_models": sorted(seen)},
            "duration_sim_s": episode._sim_now() - scan_start_sim,
            "poll_count": len(polls), "polls": polls}
