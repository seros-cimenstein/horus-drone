from __future__ import annotations

from dataclasses import dataclass

import pytest

from horus.tof_bridge import (
    DistanceSensorBridge,
    MultiSensorBridge,
    SensorSpec,
    TofBridgeConfig,
    laser_scan_to_distance_sensor,
)
from horus.tof_bridge.cli import _PymavlinkSender, main, payload_sensors, payload_topic_root


@dataclass
class _Scan:
    range_min: float
    range_max: float
    ranges: tuple[float, ...]


class _FakeMavlink:
    def __init__(self):
        self.sent = []

    def distance_sensor_send(self, *message):
        self.sent.append(message)


class _FakeLink:
    def __init__(self):
        self.mav = _FakeMavlink()


def test_scan_conversion_uses_nearest_valid_return_and_mavlink_units():
    scan = _Scan(0.1, 10.0, (float("inf"), 4.2, 2.25, 0.05, float("nan")))

    reading = laser_scan_to_distance_sensor(
        scan,
        time_boot_ms=1234,
        orientation=3,
        sensor_id=9,
    )

    assert reading is not None
    assert (
        reading.time_boot_ms,
        reading.min_distance,
        reading.max_distance,
        reading.current_distance,
        reading.sensor_id,
        reading.orientation,
    ) == (1234, 10, 1000, 225, 9, 3)


def test_clear_scan_reports_maximum_range_but_invalid_only_scan_is_omitted():
    clear = _Scan(0.2, 8.0, (float("inf"), 9.0))
    invalid = _Scan(0.2, 8.0, (float("nan"), 0.1, float("-inf")))

    reading = laser_scan_to_distance_sensor(clear, time_boot_ms=0)

    assert reading is not None
    assert reading.current_distance == reading.max_distance == 800
    assert laser_scan_to_distance_sensor(invalid, time_boot_ms=1) is None


def test_bridge_sends_fresh_scans_no_faster_than_configured_rate():
    now = [0.0]
    sent = []
    bridge = DistanceSensorBridge(
        sent.append,
        TofBridgeConfig(rate_hz=20.0, orientation=1, sensor_id=2),
        monotonic=lambda: now[0],
        clock_ms=lambda: int(now[0] * 1000),
    )
    scan = _Scan(0.1, 5.0, (3.0,))

    first = bridge.on_scan(scan)
    now[0] = 0.01
    limited = bridge.on_scan(scan)
    now[0] = 0.05
    second = bridge.on_scan(scan)

    assert first is not None and first.current_distance == 300
    assert limited is None
    assert second is not None and second.time_boot_ms == 50
    assert sent == [first, second]


def test_downward_rangefinder_uses_mavlink_pitch_270_orientation():
    scan = _Scan(0.1, 4.0, (1.2,))

    reading = laser_scan_to_distance_sensor(scan, time_boot_ms=20, orientation=25, sensor_id=6)

    assert reading is not None
    assert reading.orientation == 25
    assert TofBridgeConfig(orientation=25, sensor_id=6).orientation == 25


def test_multi_sensor_bridge_sends_all_topics_over_one_mavlink_link():
    now = [0.0]
    link = _FakeLink()
    sensors = tuple(payload_sensors("horus_indoor", "horus_iris/horus_sensor_payload"))
    bridge = MultiSensorBridge(
        _PymavlinkSender(link),
        sensors,
        rate_hz=20.0,
        monotonic=lambda: now[0],
        clock_ms=lambda: 1234,
    )
    scan = _Scan(0.1, 4.0, (1.2,))

    for sensor in sensors:
        assert bridge.on_scan(sensor.topic, scan) is not None
    assert [(message[5], message[6]) for message in link.mav.sent] == [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 4),
        (4, 6),
        (5, 7),
        (6, 25),
    ]

    now[0] = 0.01
    for sensor in sensors:
        assert bridge.on_scan(sensor.topic, scan) is None
    assert len(link.mav.sent) == 7

    now[0] = 0.05
    for sensor in sensors:
        assert bridge.on_scan(sensor.topic, scan) is not None
    assert len(link.mav.sent) == 14


def test_payload_sensors_are_scoped_to_the_world_and_model_chain():
    indoor = payload_sensors("horus_indoor", "horus_iris/horus_sensor_payload")
    outdoor = payload_sensors("horus_forest_p12648",
                              "horus_iris/iris_with_gimbal/horus_sensor_payload")

    assert [sensor.topic for sensor in indoor] == [
        "/world/horus_indoor/model/horus_iris/model/horus_sensor_payload/link/payload/sensor/"
        "proximity_front/scan",
        "/world/horus_indoor/model/horus_iris/model/horus_sensor_payload/link/payload/sensor/"
        "proximity_front_right/scan",
        "/world/horus_indoor/model/horus_iris/model/horus_sensor_payload/link/payload/sensor/"
        "proximity_right/scan",
        "/world/horus_indoor/model/horus_iris/model/horus_sensor_payload/link/payload/sensor/"
        "proximity_rear/scan",
        "/world/horus_indoor/model/horus_iris/model/horus_sensor_payload/link/payload/sensor/"
        "proximity_left/scan",
        "/world/horus_indoor/model/horus_iris/model/horus_sensor_payload/link/payload/sensor/"
        "proximity_front_left/scan",
        "/world/horus_indoor/model/horus_iris/model/horus_sensor_payload/link/payload/sensor/"
        "range_down/scan",
    ]
    assert outdoor[0].topic == (
        "/world/horus_forest_p12648/model/horus_iris/model/iris_with_gimbal/model/"
        "horus_sensor_payload/link/payload/sensor/proximity_front/scan")
    assert [(sensor.orientation, sensor.sensor_id) for sensor in outdoor] == [
        (0, 0), (1, 1), (2, 2), (4, 3), (6, 4), (7, 5), (25, 6)]
    # Slash and comma chains describe the same scope, and the chain is what changes per world.
    assert payload_sensors("horus_indoor", "horus_iris,horus_sensor_payload") == indoor
    assert payload_topic_root("horus_indoor", "horus_iris/horus_sensor_payload") != (
        payload_topic_root("horus_forest_p12648", "horus_iris/horus_sensor_payload"))


def test_payload_topic_root_rejects_an_incomplete_scope():
    with pytest.raises(ValueError):
        payload_topic_root("", "horus_iris/horus_sensor_payload")
    with pytest.raises(ValueError):
        payload_topic_root("horus_indoor", "/")


def test_cli_requires_exactly_one_topic_source():
    # Mixing explicit topics with a derived payload, or giving half a scope, would silently
    # bridge the wrong topics; the parser must refuse before opening MAVLink.
    for argv in ([],
                 ["--world", "horus_indoor"],
                 ["--model-chain", "horus_iris/horus_sensor_payload"],
                 ["--world", "horus_indoor", "--model-chain", "a/b", "--sensor", "t:0:0"]):
        with pytest.raises(SystemExit):
            main(argv)


def test_startup_watch_warns_periodically_without_ever_exiting():
    # SITL's lockstep world does not run until a flight client connects, so a bridge started
    # first must keep waiting (and say why) instead of killing the simulation.
    from horus.tof_bridge.cli import _StartupWatch

    watch = _StartupWatch(timeout=60.0, interval=30.0, started=0.0)
    missing = ["/world/w/model/drone/model/payload/link/payload/sensor/proximity_front/scan"]

    assert watch.update(missing, now=0.0) is None
    assert watch.update(missing, now=59.9) is None

    first = watch.update(missing, now=60.0)
    assert first is not None and missing[0] in first and "no flight client" in first

    assert watch.update(missing, now=89.9) is None
    assert watch.update(missing, now=90.0) is not None
    assert watch.update([], now=200.0) is None
