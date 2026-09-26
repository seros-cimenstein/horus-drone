#!/usr/bin/env python3
"""Record Gazebo ground-truth x/y/z/yaw of named models at a fixed rate to JSON lines (runs in the sim container).

Each line: {"wall_time", "sim_time", "models": {name: [x, y, z, yaw_rad]}} in Gazebo world (ENU) coordinates.

Usage: gt_trace.py OUTPUT.jsonl [--rate-hz 2] [--models horus_iris,person_red,...]
Stops on SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import threading
import time

DEFAULT_MODELS = "horus_iris,person_red,person_blue,person_green,person_laying,home_apriltag_pad"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    parser.add_argument("--rate-hz", type=float, default=2.0)
    parser.add_argument("--models", default=DEFAULT_MODELS)
    parser.add_argument("--topic", default="/world/horus_indoor/pose/info")
    args = parser.parse_args()
    from gz.msgs10.pose_v_pb2 import Pose_V
    from gz.transport13 import Node

    wanted = set(args.models.split(","))
    latest: dict[str, list[float]] = {}
    sim_time: list[float | None] = [None]
    lock = threading.Lock()

    def on_poses(message: Pose_V) -> None:
        with lock:
            sim_time[0] = message.header.stamp.sec + message.header.stamp.nsec * 1e-9
            for pose in message.pose:
                if pose.name in wanted:
                    q = pose.orientation
                    yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
                    latest[pose.name] = [pose.position.x, pose.position.y, pose.position.z, yaw]

    node = Node()
    if not node.subscribe(Pose_V, args.topic, on_poses):
        raise SystemExit(f"could not subscribe to {args.topic}")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with open(args.output, "a", encoding="utf-8") as out:
        while not stop.is_set():
            with lock:
                snapshot = dict(latest)
                stamp = sim_time[0]
            if snapshot:
                out.write(json.dumps({"wall_time": time.time(), "sim_time": stamp, "models": snapshot}) + "\n")
                out.flush()
            stop.wait(1.0 / args.rate_hz)


if __name__ == "__main__":
    main()
