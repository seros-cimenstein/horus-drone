"""Report horizontal Gazebo ground-truth error from the AprilTag landing pad."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import subprocess

from sim.campaign.gz_topics import named_poses, scalar, topic_messages

DEFAULT_TOPIC = "/world/horus_indoor/pose/info"
DEFAULT_VEHICLE = "horus_iris"
DEFAULT_PAD = "home_apriltag_pad"


@dataclass(frozen=True, slots=True)
class LandingError:
    """Planar vehicle offset from the pad in Gazebo world coordinates."""

    dx_m: float
    dy_m: float
    distance_m: float
    vehicle_x_m: float
    vehicle_y_m: float
    pad_x_m: float
    pad_y_m: float


def parse_pose_info(
    message: str,
    vehicle_name: str = DEFAULT_VEHICLE,
    pad_name: str = DEFAULT_PAD,
) -> LandingError | None:
    """Parse one Gazebo ``gz.msgs.Pose_V`` text message; return ``None`` if incomplete."""
    poses: dict[str, tuple[float, float]] = {}
    for name, position, _orientation in named_poses(message):
        if name not in (vehicle_name, pad_name):
            continue
        # Protobuf text omits zero-valued fields, so a missing coordinate is 0.
        x, y = scalar(position, "x"), scalar(position, "y")
        poses[name] = (0.0 if x is None else x, 0.0 if y is None else y)
    if vehicle_name not in poses or pad_name not in poses:
        return None
    vehicle_x, vehicle_y = poses[vehicle_name]
    pad_x, pad_y = poses[pad_name]
    dx, dy = vehicle_x - pad_x, vehicle_y - pad_y
    return LandingError(dx, dy, math.hypot(dx, dy), vehicle_x, vehicle_y, pad_x, pad_y)


def _format(error: LandingError) -> str:
    return (
        f"landing_error_m={error.distance_m:.3f} "
        f"dx_m={error.dx_m:+.3f} dy_m={error.dy_m:+.3f} "
        f"vehicle=({error.vehicle_x_m:.3f},{error.vehicle_y_m:.3f}) "
        f"pad=({error.pad_x_m:.3f},{error.pad_y_m:.3f})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default=DEFAULT_TOPIC, help="Gazebo Pose_V topic")
    parser.add_argument("--vehicle", default=DEFAULT_VEHICLE, help="vehicle model name in Pose_V")
    parser.add_argument("--pad", default=DEFAULT_PAD, help="landing pad model name in Pose_V")
    args = parser.parse_args(argv)

    try:
        process = subprocess.Popen(
            ["gz", "topic", "-e", "-t", args.topic],
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        parser.error(f"unable to start Gazebo topic subscriber: {exc}")
    interrupted = False
    try:
        if process.stdout is not None:
            for message, _truncated in topic_messages(process.stdout):
                error = parse_pose_info(message, args.vehicle, args.pad)
                if error is not None:
                    print(_format(error), flush=True)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
    if not interrupted and process.returncode not in (0, -15):
        return int(process.returncode or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
