from __future__ import annotations

import math
import re
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).parents[1]


def test_each_proximity_beam_points_where_ardupilot_is_told_it_points():
    # ArduPilot avoidance trusts the MAVLink orientation; a beam aimed elsewhere hides real
    # obstacles in that sector and reports phantom ones (all beams once pointed forward).
    launcher = (ROOT / "scripts/tof_bridges_indoor.sh").read_text(encoding="utf-8")
    bridged = {
        name: int(orientation)
        for name, orientation in re.findall(r"/(proximity_\w+)/scan:(\d+):", launcher)
    }
    model = ElementTree.parse(ROOT / "sim/models/horus_sensor_payload/model.sdf").getroot()
    sensors = {
        sensor.get("name"): [float(value) for value in sensor.findtext("pose").split()]
        for sensor in model.iter("sensor")
        if sensor.get("name", "").startswith("proximity_")
    }

    assert set(bridged) == set(sensors) and len(sensors) == 6
    for name, orientation in bridged.items():
        x, y, _z, roll, pitch, yaw = sensors[name]
        expected = -math.radians(45 * orientation)
        error = math.atan2(math.sin(yaw - expected), math.cos(yaw - expected))
        assert abs(error) < 1e-6 and roll == 0.0 and pitch == 0.0, name
        # The beam starts on its own side of the airframe, not across the body.
        if orientation != 0:
            assert x * math.cos(expected) + y * math.sin(expected) > 0.2, name
