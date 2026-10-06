#!/usr/bin/env python3
"""Report what each scan task did in one perf run.

Usage: perf/scan_report.py perf/runs/<run>
Reads <run>/mcp_events.jsonl (the MCP server's event log; tool results there are cut at 500
characters) for submit/finish times, <run>/scan_task_<id>.json (full get_task_status output saved by
run_mission.sh) for the complete result, and <run>/mcp.log for the executor's skip warnings.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

TERMINAL = ("succeeded", "failed", "cancelled", "expired")
TASK_ID_RE = re.compile(r'"task_id":"([0-9a-f]+)"')
STATUS_RE = re.compile(r'"status":"([a-z]+)"')


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def main(run_dir: str) -> None:
    out = Path(run_dir)
    scans: dict[str, dict[str, Any]] = {}
    rejected = []
    for line in (out / "mcp_events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        tool, result = row.get("tool"), row.get("result")
        text = result if isinstance(result, str) else json.dumps(result)
        task = TASK_ID_RE.search(text or "")
        status = STATUS_RE.search(text or "")
        if tool == "scan":
            if task is None:
                rejected.append(row.get("error") or text)
            else:
                scans[task.group(1)] = {"submitted": _epoch(row["timestamp"])}
        elif tool in ("wait_for_task", "get_task_status") and task and task.group(1) in scans:
            scan = scans[task.group(1)]
            if "finished_after_s" not in scan and status and status.group(1) in TERMINAL:
                # Event timestamps mark the start of a call; a blocking wait ends latency_ms later.
                ended = _epoch(row["timestamp"]) + float(row.get("latency_ms") or 0.0) / 1000.0
                scan["finished_after_s"] = round(ended - scan["submitted"], 1)
    report: dict[str, Any] = {"rejected_scan_calls": rejected, "scans": []}
    for task_id, scan in scans.items():
        entry: dict[str, Any] = {"task_id": task_id, "observed_finished_after_s": scan.get("finished_after_s")}
        full_path = out / f"scan_task_{task_id}.json"
        if full_path.exists():
            try:
                full = json.loads(full_path.read_text())
                full = json.loads(full["result"]) if isinstance(full.get("result"), str) else full
                skipped = full.get("skipped_waypoints", (full.get("result") or {}).get("skipped_waypoints"))
                entry.update({
                    "status": full.get("status"),
                    "error": full.get("error"),
                    "waypoints": len(full.get("executed_target") or []),
                    "skipped_waypoints": skipped,
                })
            except (json.JSONDecodeError, AttributeError, TypeError):
                entry["status_file_unreadable"] = full_path.name
        report["scans"].append(entry)
    log = out / "mcp.log"
    if log.exists():
        report["executor_warnings"] = [
            line for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
            if "horus.executor" in line and "WARNING" in line
        ]
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
