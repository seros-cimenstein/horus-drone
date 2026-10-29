"""pymavlink loaded with the MAVLink 2 dialect, for every HORUS MAVLink link in the process.

pymavlink picks its wire protocol once per process from ``MAVLINK20`` (unset: MAVLink 1). The
gimbal-device messages (GIMBAL_DEVICE_SET_ATTITUDE 284, GIMBAL_DEVICE_ATTITUDE_STATUS 285) exist
only in MAVLink 2, and the flight and gimbal links share the module, so both load it from here.
"""

from __future__ import annotations

import os
from typing import Any


def load_mavutil() -> Any:
    """``pymavlink.mavutil`` with its ``mavlink`` module switched to the MAVLink 2 dialect."""
    os.environ["MAVLINK20"] = "1"
    from pymavlink import mavutil

    if mavutil.mavlink.WIRE_PROTOCOL_VERSION != "2.0":
        mavutil.set_dialect(mavutil.current_dialect)
    return mavutil
