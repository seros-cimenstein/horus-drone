from __future__ import annotations

from dataclasses import dataclass

from horus.tof_bridge import (
    DistanceSensorBridge,
    MultiSensorBridge,
    SensorSpec,
    TofBridgeConfig,
    laser_scan_to_distance_sensor,
)
from horus.tof_bridge.cli import _PymavlinkSender


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
    sensors = (
        SensorSpec("/world/indoor/proximity_front/scan", 0, 0),
        SensorSpec("/world/indoor/proximity_front_right/scan", 1, 1),
        SensorSpec("/world/indoor/proximity_right/scan", 2, 2),
        SensorSpec("/world/indoor/proximity_rear/scan", 4, 3),
        SensorSpec("/world/indoor/proximity_left/scan", 6, 4),
        SensorSpec("/world/indoor/proximity_front_left/scan", 7, 5),
        SensorSpec("/world/indoor/range_down/scan", 25, 6),
    )
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
