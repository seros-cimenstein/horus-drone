#!/usr/bin/env python3
"""Capture Gazebo world real-time-factor samples as newline-delimited JSON."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import threading
import time

from gz.msgs10.world_stats_pb2 import WorldStatistics
from gz.transport13 import Node


_STOP = threading.Event()


def _stop(_signum: int, _frame: object) -> None:
    _STOP.set()


def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit("usage: s5_rtf_sampler.py OUTPUT_JSONL STOP_FILE [WORLD]")
    output_path = Path(sys.argv[1])
    stop_path = Path(sys.argv[2])
    world = sys.argv[3] if len(sys.argv) == 4 else "horus_indoor"
    topic = f"/world/{world}/stats"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    write_lock = threading.Lock()
    with output_path.open("w", encoding="utf-8") as output:
        def record(message: WorldStatistics) -> None:
            sim_time = message.sim_time
            sample = {
                "wall_time": datetime.now(timezone.utc).isoformat(),
                "sim_time_s": float(sim_time.sec) + float(sim_time.nsec) * 1e-9,
                "real_time_factor": float(message.real_time_factor),
            }
            line = json.dumps(sample, separators=(",", ":"))
            with write_lock:
                output.write(line + "\n")
                output.flush()

        node = Node()
        subscribed = node.subscribe(
            WorldStatistics,
            topic,
            record,
        )
        if not subscribed:
            raise RuntimeError(f"could not subscribe to {topic}")
        print(f"sampling Gazebo RTF from {topic} into {output_path}", flush=True)
        while not _STOP.wait(0.1):
            if stop_path.exists():
                break


if __name__ == "__main__":
    main()
