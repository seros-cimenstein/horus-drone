"""World-neutral MAVLink side of the ToF bridges: the payload's sensor identities, the bridge's
MAVLink source identity, and the DISTANCE_SENSOR sender.

The Gazebo bridge (``cli.py``, LaserScan topics) and the GTA bridge (``gta.py``, HorusGta.dll
range packets) both send the same payload sensors through this module, so ArduPilot's
proximity avoidance and HORUS see identical messages in either world.
"""

from __future__ import annotations

import threading
from typing import Any

from .bridge import RING_BEAMS, DistanceSensorReading

BRIDGE_SOURCE_SYSTEM = 254
BRIDGE_SOURCE_COMPONENT = 196  # MAV_COMP_ID_OBSTACLE_AVOIDANCE

# The downward rangefinder (MAVLink PITCH_270), identical in both payload layouts.
PAYLOAD_DOWN_SENSOR = ("range_down", 25, 6)
# The standard HORUS ToF payload (sim/models/horus_sensor_payload): sensor name, MAVLink
# DISTANCE_SENSOR orientation and sensor ID, in id order. The horizontal sensors are the named
# RING_BEAMS directions; the payload SDF yaws each beam to the matching body direction.
PAYLOAD_SENSORS: tuple[tuple[str, int, int], ...] = (
    *sorted(((beam.name, beam.orientation, beam.sensor_id) for beam in RING_BEAMS if beam.name),
            key=lambda sensor: sensor[2]),
    PAYLOAD_DOWN_SENSOR,
)


class PymavlinkSender:
    """Adapt a pymavlink connection or MAVLink object to the bridge sender API."""

    def __init__(self, link: Any) -> None:
        mav = getattr(link, "mav", link)
        self._send = getattr(mav, "distance_sensor_send", None)
        if not callable(self._send):
            raise RuntimeError("MAVLink link has no distance_sensor_send method")
        self._lock = threading.Lock()

    def __call__(self, reading: DistanceSensorReading) -> None:
        with self._lock:
            self._send(
                reading.time_boot_ms,
                reading.min_distance,
                reading.max_distance,
                reading.current_distance,
                reading.sensor_type,
                reading.sensor_id,
                reading.orientation,
                reading.covariance,
            )


def open_mavlink(endpoint: str) -> Any:
    """A pymavlink connection to ``endpoint`` as the bridge's MAVLink source identity."""
    try:
        from pymavlink import mavutil
    except ImportError as exc:
        raise RuntimeError("pymavlink is unavailable; install it in the simulation container") from exc
    try:
        return mavutil.mavlink_connection(
            endpoint,
            source_system=BRIDGE_SOURCE_SYSTEM,
            source_component=BRIDGE_SOURCE_COMPONENT,
        )
    except Exception as exc:
        raise RuntimeError(f"could not open MAVLink endpoint {endpoint!r}: {exc}") from exc
