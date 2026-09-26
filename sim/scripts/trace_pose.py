#!/usr/bin/env python3
"""Record ArduPilot LOCAL_POSITION_NED samples to CSV."""

from __future__ import annotations

import argparse
import csv
import math
import time
from pathlib import Path

from pymavlink import mavutil


def _connect(connection: str, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return mavutil.mavlink_connection(connection, autoreconnect=False)
        except ConnectionRefusedError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", default="tcp:127.0.0.1:5762")
    parser.add_argument("--output", type=Path, default=Path("/horus/sim/logs/s1_trace.csv"))
    parser.add_argument("--duration-s", type=float, default=900.0)
    parser.add_argument("--rate-hz", type=float, default=2.0)
    args = parser.parse_args()
    if not math.isfinite(args.duration_s) or args.duration_s <= 0:
        parser.error("--duration-s must be finite and positive")
    if not math.isfinite(args.rate_hz) or args.rate_hz <= 0:
        parser.error("--rate-hz must be finite and positive")

    link = _connect(args.connection, timeout_s=60.0)
    try:
        heartbeat = link.wait_heartbeat(timeout=20.0)
        if heartbeat is None:
            raise TimeoutError(f"no heartbeat from {args.connection}")
        link.mav.command_long_send(
            heartbeat.get_srcSystem(),
            heartbeat.get_srcComponent(),
            mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
            0,
            mavutil.mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED,
            1_000_000.0 / args.rate_hz,
            0,
            0,
            0,
            0,
            0,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + args.duration_s
        samples = 0
        with args.output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(("time_boot_ms", "x_north_m", "y_east_m", "z_down_m"))
            stream.flush()
            print(f"Tracing LOCAL_POSITION_NED at {args.rate_hz:g} Hz from {args.connection}")
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                message = link.recv_match(
                    type="LOCAL_POSITION_NED", blocking=True, timeout=min(1.0, remaining)
                )
                if message is None:
                    continue
                writer.writerow(
                    (message.time_boot_ms, message.x, message.y, message.z)
                )
                stream.flush()
                samples += 1
        print(f"Wrote {samples} LOCAL_POSITION_NED samples to {args.output}")
        return 0
    except KeyboardInterrupt:
        print(f"Stopped after {samples} LOCAL_POSITION_NED samples")
    finally:
        link.close()


if __name__ == "__main__":
    raise SystemExit(main())
