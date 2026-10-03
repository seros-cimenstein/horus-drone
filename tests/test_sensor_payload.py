from __future__ import annotations

import math
from pathlib import Path
from xml.etree import ElementTree

from horus.tof_bridge.cli import PAYLOAD_SENSORS
from sim.scripts.derive_procedural_airframe import SENSOR_RATE_HZ

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


def test_procedural_payload_carries_exactly_the_ring_and_the_down_rangefinder():
    # Generated worlds render one 8-beam proximity ring instead of six single-beam ToFs; the
    # ring's sample angles must land on the 45-degree directions the bridge fans out to MAVLink.
    model = ElementTree.parse(
        ROOT / "sim/models/horus_sensor_payload_procedural/model.sdf").getroot()
    sensors = {sensor.get("name"): sensor for sensor in model.iter("sensor")}

    assert set(sensors) == {"proximity_ring", "range_down"}
    ring = sensors["proximity_ring"]
    assert ring.get("type") == "gpu_lidar"
    assert [float(value) for value in ring.findtext("pose").split()] == [0.0, 0.0, 0.08, 0.0, 0.0, 0.0]
    assert ring.findtext("update_rate") == str(SENSOR_RATE_HZ)
    assert sensors["range_down"].findtext("update_rate") == str(SENSOR_RATE_HZ)
    assert ring.findtext("always_on") == "true"
    assert ring.findtext("visualize") == "false"
    assert [float(ring.findtext(f"lidar/range/{tag}"))
            for tag in ("min", "max", "resolution")] == [0.05, 4.0, 0.01]
    assert int(ring.findtext("lidar/scan/vertical/samples")) == 1

    horizontal = ring.find("lidar/scan/horizontal")
    samples = int(horizontal.findtext("samples"))
    min_angle = float(horizontal.findtext("min_angle"))
    max_angle = float(horizontal.findtext("max_angle"))
    assert samples == 8
    assert math.isclose(min_angle, -math.pi, abs_tol=1e-9)
    assert math.isclose(max_angle, 3 * math.pi / 4, abs_tol=1e-9)
    step = (max_angle - min_angle) / (samples - 1)
    assert [round(math.degrees(min_angle + index * step), 3) for index in range(samples)] == [
        -180.0, -135.0, -90.0, -45.0, 0.0, 45.0, 90.0, 135.0]


def test_procedural_airframe_includes_the_ring_payload_under_the_topic_name():
    text = (ROOT / "sim/models/horus_iris_with_gimbal_procedural/model.sdf").read_text()

    assert "<uri>model://horus_sensor_payload_procedural</uri>" in text
    # The include name keeps the gz-sim topic chain .../model/horus_sensor_payload/... intact.
    assert "<name>horus_sensor_payload</name>" in text
