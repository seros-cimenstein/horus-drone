#!/usr/bin/env python3
"""Print a markdown table for every perf/runs/<PREFIX>-run*/summary.json, plus a median row.

Usage: perf/aggregate.py PREFIX [PREFIX ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import median
from typing import Any

RUNS = Path(__file__).resolve().parent / "runs"
COLUMNS = (
    ("Run", lambda s: s["run"].split("_")[0]),
    ("Wall s", lambda s: s["mission_wall_s"]),
    ("RTF", lambda s: s["real_time_factor"]),
    ("Perception fps", lambda s: _get(s, "perception", "fps", "avg")),
    ("Frame ms", lambda s: _get(s, "perception", "frame_latency_ms", "avg")),
    ("Tool calls", lambda s: s["commander"]["tool_calls"]),
    ("Failed calls", lambda s: s["commander"]["failed_tool_calls"]),
    ("Model turn avg s", lambda s: _get(s, "commander", "model_turn_s", "avg")),
    ("Model turn max s", lambda s: _get(s, "commander", "model_turn_s", "max")),
    ("Tool round-trip avg s", lambda s: _get(s, "commander", "tool_round_trip_s", "avg")),
    ("Tool round-trip max s", lambda s: _get(s, "commander", "tool_round_trip_s", "max")),
    ("Success", lambda s: s.get(
        "s6_correct_target",
        s["mission_return_code"] == 0 and not s["commander"]["timed_out"] and not s.get("crash"),
    )),
    ("Crash", lambda s: bool(s.get("crash"))),
    ("Home before commanded", lambda s: s["ground_truth"].get("landed_before_commanded")),
    ("Landing error m", lambda s: s["ground_truth"]["landing_error_m"]),
)


def _get(summary: dict[str, Any], *keys: str) -> Any:
    value: Any = summary
    for key in keys:
        if not isinstance(value, dict) or value.get(key) is None:
            return None
        value = value[key]
    return value


def _cell(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def table(prefix: str) -> str:
    summaries = [json.loads(path.read_text()) for path in sorted(RUNS.glob(f"{prefix}-run*/summary.json"))]
    if not summaries:
        return f"No runs found for `{prefix}`.\n"
    lines = ["| " + " | ".join(name for name, _ in COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    rows = [[getter(summary) for _, getter in COLUMNS] for summary in summaries]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    medians: list[str] = ["**median**"]
    for index in range(1, len(COLUMNS)):
        values = [row[index] for row in rows]
        if all(isinstance(value, bool) or value is None for value in values):
            medians.append(f"{sum(1 for value in values if value)}/{len(values)} yes")
        else:
            numbers = [value for value in values if isinstance(value, (int, float))]
            medians.append("n/a" if not numbers else f"**{round(median(numbers), 3)}**")
    lines.append("| " + " | ".join(medians) + " |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    for name in sys.argv[1:]:
        print(table(name))
