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
    if len(sys.argv) != 3:
        raise SystemExit("usage: s5_rtf_sampler.py OUTPUT_JSONL STOP_FILE")
    output_path = Path(sys.argv[1])
    stop_path = Path(sys.argv[2])
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
            "/world/horus_indoor/stats",
            record,
        )
        if not subscribed:
            raise RuntimeError("could not subscribe to /world/horus_indoor/stats")
        print(f"sampling Gazebo RTF into {output_path}", flush=True)
        while not _STOP.wait(0.1):
            if stop_path.exists():
                break


if __name__ == "__main__":
    main()
