"""Move the S5 blue-shirt mannequin through a two-second screen occlusion."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from scripts.gz_sim import _SimClock, load_gz_bindings


@dataclass(frozen=True, slots=True)
class Waypoint:
    x: float
    y: float
    yaw: float
    move_s: float
    hold_s: float = 0.0


# The route walks up the corridor's left lane, crosses behind corridor_screen (at least two seconds
# fully hidden from a follower to the south), comes back down the right lane and returns to the room.
WAYPOINTS = (
    Waypoint(-1.8, 3.0, -math.pi / 2, 0.0, 8.0),
    Waypoint(-1.0, 4.4, math.pi / 2, 2.5),
    Waypoint(-1.0, 6.0, math.pi / 2, 2.0),
    Waypoint(1.0, 6.0, 0.0, 3.0),
    Waypoint(1.0, 4.4, -math.pi / 2, 2.0),
    Waypoint(-1.8, 3.0, -math.pi / 2, 4.0),
)


def trajectory(rate_hz: float = 20.0) -> Iterator[tuple[float, float, float]]:
    """Yield interpolated world x/y/yaw poses at the requested update rate."""
    if not math.isfinite(rate_hz) or rate_hz <= 0:
        raise ValueError("rate_hz must be a finite positive number")
    while True:
        previous = WAYPOINTS[0]
        for _ in range(round(previous.hold_s * rate_hz)):
            yield previous.x, previous.y, previous.yaw
        for waypoint in WAYPOINTS[1:]:
            steps = max(1, round(waypoint.move_s * rate_hz))
            yaw_delta = (waypoint.yaw - previous.yaw + math.pi) % (2 * math.pi) - math.pi
            for step in range(1, steps + 1):
                fraction = step / steps
                yield (
                    previous.x + (waypoint.x - previous.x) * fraction,
                    previous.y + (waypoint.y - previous.y) * fraction,
                    previous.yaw + yaw_delta * fraction,
                )
            for _ in range(round(waypoint.hold_s * rate_hz)):
                yield waypoint.x, waypoint.y, waypoint.yaw
            previous = waypoint


def _set_pose(
    node: Any,
    pose_type: Any,
    boolean_type: Any,
    target_name: str,
    sample: tuple[float, float, float],
) -> None:
    x, y, yaw = sample
    request = pose_type()
    request.name = target_name
    request.position.x = x
    request.position.y = y
    request.position.z = 0.0
    request.orientation.z = math.sin(yaw / 2.0)
    request.orientation.w = math.cos(yaw / 2.0)
    # The first request also waits for service discovery, which is slower over an Ethernet GZ_IP.
    for _ in range(3):
        result, response = node.request(
            "/world/horus_indoor/set_pose", request, pose_type, boolean_type, 2000
        )
        if result and response.data:
            return
    raise RuntimeError(f"Gazebo set_pose failed for model {target_name!r}")


def run(
    node: Any,
    pose_type: Any,
    boolean_type: Any,
    *,
    rate_hz: float = 20.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Publish the blue mannequin's kinematic poses through Gazebo UserCommands.

    Samples are paced by ``clock`` (Gazebo simulation seconds in ``main``) so the route, including
    the two-second occlusion, keeps its designed duration in simulation time at any real-time factor.
    """
    interval = 1.0 / rate_hz
    next_update = clock()
    for sample in trajectory(rate_hz):
        _set_pose(node, pose_type, boolean_type, "person_blue", sample)
        next_update += interval
        while clock() < next_update:
            sleep(min(interval, 0.005))



def main() -> None:
    try:
        bindings = load_gz_bindings()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    node = bindings.node()
    sim_clock = _SimClock()
    sim_clock.wait_ready()
    print("Moving person_blue through the S5 corridor-screen occlusion at 20 Hz of simulation time", flush=True)
    try:
        run(node, bindings.pose, bindings.boolean, clock=lambda: sim_clock() or 0.0)
    finally:
        sim_clock.close()

if __name__ == "__main__":
    main()
