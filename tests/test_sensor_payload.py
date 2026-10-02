from __future__ import annotations

import math
from pathlib import Path
from xml.etree import ElementTree

from horus.tof_bridge.cli import PAYLOAD_SENSORS

ROOT = Path(__file__).parents[1]


def test_each_proximity_beam_points_where_ardupilot_is_told_it_points():
    # ArduPilot avoidance trusts the MAVLink orientation; a beam aimed elsewhere hides real
    # obstacles in that sector and reports phantom ones (all beams once pointed forward).
    bridged = {name: orientation for name, orientation, _sensor_id in PAYLOAD_SENSORS
               if name.startswith("proximity_")}
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


def test_payload_sensors_cover_every_sensor_the_model_carries():
    model = ElementTree.parse(ROOT / "sim/models/horus_sensor_payload/model.sdf").getroot()
    names = {sensor.get("name") for sensor in model.iter("sensor")}
    bridged = {name for name, _orientation, _sensor_id in PAYLOAD_SENSORS}

    # An unbridged sensor is dead weight; a bridged one that does not exist never streams.
    assert bridged == names
    assert len({sensor_id for _name, _orientation, sensor_id in PAYLOAD_SENSORS}) == len(PAYLOAD_SENSORS)
    assert len({orientation for _name, orientation, _sensor_id in PAYLOAD_SENSORS}) == len(PAYLOAD_SENSORS)
    assert [name for name, orientation, _ in PAYLOAD_SENSORS if orientation == 25] == ["range_down"]
