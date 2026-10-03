from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from horus.tof_bridge import (
    DistanceSensorBridge,
    MultiSensorBridge,
    RING_BEAMS,
    RingSensorSpec,
    SensorSpec,
    TofBridgeConfig,
    laser_scan_to_distance_sensor,
    ring_scan_to_distance_sensors,
    sensor_ids,
)
from horus.tof_bridge.cli import (
    PAYLOAD_SENSORS,
    _PymavlinkSender,
    main,
    payload_ring_sensors,
    payload_sensors,
    payload_specs,
    payload_topic_root,
)


@dataclass
class _Scan:
    range_min: float
    range_max: float
    ranges: tuple[float, ...]


@dataclass
class _RingScan:
    range_min: float
    range_max: float
    angle_min: float
    angle_step: float
    ranges: tuple[float, ...]
    angle_max: float | None = None


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


# The ring payload's eight beams, in scan order (SDF angle -> MAVLink orientation, sensor id).
_RING_DIRECTIONS = {
    -180.0: (4, 3),
    -135.0: (3, 7),
    -90.0: (2, 2),
    -45.0: (1, 1),
    0.0: (0, 0),
    45.0: (7, 5),
    90.0: (6, 4),
    135.0: (5, 8),
}


def _ring_scan(ranges: tuple[float, ...], *, angle_min: float = -math.pi,
               angle_step: float = math.pi / 4) -> _RingScan:
    return _RingScan(0.05, 4.0, angle_min, angle_step, ranges)


def test_ring_beams_keep_the_old_sensor_identities_and_offsets():
    # The ring replaces the six single-beam ToFs: every old direction keeps its MAVLink
    # orientation, DISTANCE_SENSOR id and face radius; the two rear diagonals are new.
    by_angle = {beam.angle_deg: beam for beam in RING_BEAMS}
    assert set(by_angle) == set(_RING_DIRECTIONS)
    for angle, (orientation, sensor_id) in _RING_DIRECTIONS.items():
        beam = by_angle[angle]
        assert (beam.orientation, beam.sensor_id) == (orientation, sensor_id)
        expected_offset = 0.24 if angle == 0.0 else (
            math.hypot(0.30, 0.30) if angle in (-45.0, 45.0) else 0.32)
        assert beam.radial_offset_m == pytest.approx(expected_offset)

    old_ids = {orientation: sensor_id for name, orientation, sensor_id in PAYLOAD_SENSORS
               if name != "range_down"}
    for angle, (orientation, sensor_id) in _RING_DIRECTIONS.items():
        if orientation in old_ids:
            assert sensor_id == old_ids[orientation], angle
        else:
            assert orientation in (3, 5), angle
    assert [by_angle[angle].sensor_id for angle in (-135.0, 135.0)] == [7, 8]
    # No id collisions across the whole generated payload, range_down's id 6 included.
    all_ids = [beam.sensor_id for beam in RING_BEAMS] + [6]
    assert len(set(all_ids)) == len(all_ids) == 9


def test_ring_scan_fans_each_beam_out_with_the_old_face_distance():
    readings = ring_scan_to_distance_sensors(_ring_scan((2.0,) * 8), time_boot_ms=7)

    assert [(reading.orientation, reading.sensor_id) for reading in readings] == [
        (4, 3), (3, 7), (2, 2), (1, 1), (0, 0), (7, 5), (6, 4), (5, 8)]
    assert [reading.current_distance for reading in readings] == [
        168, 168, 168, 158, 176, 158, 168, 168]
    assert all(reading.time_boot_ms == 7 and reading.min_distance == 5
               and reading.max_distance == 400 for reading in readings)
    # The ring reuses the single-beam converter: same range, same offset, same centimetres.
    single = laser_scan_to_distance_sensor(
        _Scan(0.05, 4.0, (2.0,)), time_boot_ms=7, orientation=0, sensor_id=0, radial_offset_m=0.24)
    assert single is not None and single.current_distance == 176


def test_ring_treats_no_return_and_out_of_range_beams_like_the_single_beam_path():
    readings = ring_scan_to_distance_sensors(
        _ring_scan((float("inf"), 9.0, float("nan"), float("-inf"), 2.0, float("nan"), 0.01, 4.0)),
        time_boot_ms=11)

    # +inf and > range_max are clear (max range); NaN, -inf and < range_min emit nothing; a
    # return at range_max is a real measurement and is offset-compensated like any other.
    assert [(reading.orientation, reading.sensor_id, reading.current_distance)
            for reading in readings] == [(4, 3, 400), (3, 7, 400), (0, 0, 176), (5, 8, 368)]


def test_ring_skips_beams_that_miss_the_45_degree_directions():
    # A five-degree offset puts every beam outside the small matching tolerance...
    assert ring_scan_to_distance_sensors(
        _ring_scan((1.0,) * 8, angle_min=math.radians(-175.0)), time_boot_ms=0) == []
    # ...while sub-degree slop still maps every beam to its intended direction.
    readings = ring_scan_to_distance_sensors(
        _ring_scan((1.0,) * 8, angle_min=math.radians(-179.5)), time_boot_ms=0)
    assert [(reading.orientation, reading.sensor_id) for reading in readings] == [
        (4, 3), (3, 7), (2, 2), (1, 1), (0, 0), (7, 5), (6, 4), (5, 8)]


def test_ring_normalizes_the_half_turn_beam_to_the_rear_direction():
    # +180 degrees and -180 degrees are the same bearing; the scan may report either.
    readings = ring_scan_to_distance_sensors(
        _ring_scan((1.0,) * 8, angle_min=math.pi), time_boot_ms=0)

    assert [(reading.orientation, reading.sensor_id) for reading in readings] == [
        (4, 3), (3, 7), (2, 2), (1, 1), (0, 0), (7, 5), (6, 4), (5, 8)]


def test_ring_derives_beam_angles_from_angle_max_when_step_is_unset():
    scan = _RingScan(0.05, 4.0, -math.pi, 0.0, (2.0,) * 8, angle_max=3 * math.pi / 4)

    readings = ring_scan_to_distance_sensors(scan, time_boot_ms=0)

    assert [reading.current_distance for reading in readings] == [
        168, 168, 168, 158, 176, 158, 168, 168]
    with pytest.raises(ValueError):
        ring_scan_to_distance_sensors(
            _RingScan(0.05, 4.0, -math.pi, 0.0, (1.0,) * 8), time_boot_ms=0)


def test_payload_ring_layout_fans_out_only_the_ring_and_keeps_the_down_link():
    world, chain = "horus_forest_p12648", "horus_iris/iris_with_gimbal/horus_sensor_payload"
    root = payload_topic_root(world, chain)

    ring, down = payload_ring_sensors(world, chain)

    assert isinstance(ring, RingSensorSpec)
    assert ring.topic == f"{root}/proximity_ring/scan"
    assert down == SensorSpec(f"{root}/range_down/scan", 25, 6)
    assert sorted(sensor_id for spec in (ring, down) for sensor_id in sensor_ids(spec)) == list(range(9))
    assert len({spec.topic for spec in (ring, down)}) == 2


def test_payload_specs_default_to_the_unchanged_seven_sensor_layout():
    world, chain = "horus_forest_p12648", "horus_iris/iris_with_gimbal/horus_sensor_payload"

    seven = payload_specs(world, chain)

    assert seven == payload_sensors(world, chain)
    assert all(type(sensor) is SensorSpec for sensor in seven)
    assert [(sensor.orientation, sensor.sensor_id) for sensor in seven] == [
        (0, 0), (1, 1), (2, 2), (4, 3), (6, 4), (7, 5), (25, 6)]
    assert payload_specs(world, chain, "ring") == payload_ring_sensors(world, chain)
    with pytest.raises(ValueError):
        payload_specs(world, chain, "fancy")


def test_multi_sensor_bridge_fans_a_ring_scan_out_to_one_message_per_beam():
    now = [0.0]
    link = _FakeLink()
    ring, down = payload_ring_sensors(
        "horus_forest_p12648", "horus_iris/iris_with_gimbal/horus_sensor_payload")
    bridge = MultiSensorBridge(
        _PymavlinkSender(link), (ring, down), rate_hz=20.0,
        monotonic=lambda: now[0], clock_ms=lambda: 1234)
    scan = _ring_scan((1.0,) * 8)

    assert len(bridge.on_scan(ring.topic, scan)) == 8
    assert bridge.on_scan(down.topic, _Scan(0.05, 4.0, (1.2,))) is not None
    assert [(message[5], message[6]) for message in link.mav.sent] == [
        (3, 4), (7, 3), (2, 2), (1, 1), (0, 0), (5, 7), (4, 6), (8, 5), (6, 25)]

    now[0] = 0.01
    assert bridge.on_scan(ring.topic, scan) == []
    assert bridge.on_scan(down.topic, _Scan(0.05, 4.0, (1.2,))) is None
    assert len(link.mav.sent) == 9


def test_cli_layout_only_applies_to_the_derived_payload():
    for argv in (["--sensor", "t:0:0", "--layout", "ring"],
                 ["--world", "horus_forest_p1", "--model-chain", "a/b", "--layout", "bogus"]):
        with pytest.raises(SystemExit):
            main(argv)


def test_cli_ring_layout_bridges_the_ring_and_the_down_topics(monkeypatch):
    from horus.tof_bridge import cli

    captured = {}
    monkeypatch.setattr(cli, "_open_mavlink", lambda endpoint: object())
    monkeypatch.setattr(
        cli, "_serve",
        lambda sensors, link, rate_hz, timeout: captured.setdefault("sensors", sensors))

    assert main(["--world", "horus_forest_p12648",
                 "--model-chain", "horus_iris/iris_with_gimbal/horus_sensor_payload",
                 "--layout", "ring"]) == 0
    ring, down = captured["sensors"]
    assert isinstance(ring, RingSensorSpec)
    assert ring.topic.endswith("/proximity_ring/scan")
    assert down.topic.endswith("/range_down/scan")


def test_startup_watch_names_the_ring_and_down_topics_when_scans_are_missing():
    from horus.tof_bridge.cli import _StartupWatch

    ring, down = payload_ring_sensors(
        "horus_forest_p12648", "horus_iris/iris_with_gimbal/horus_sensor_payload")

    warning = _StartupWatch(timeout=0.0, interval=30.0, started=0.0).update(
        [ring.topic, down.topic], now=0.0)

    assert warning is not None
    assert ring.topic in warning and down.topic in warning
