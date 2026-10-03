from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from pymavlink import mavutil

from horus.config import FlightSettings
from horus.flight.link import MavlinkFlightLink


class FakeMav:
    def __init__(self) -> None:
        self.global_origin_messages: list[tuple[int, ...]] = []
        self.command_calls: list[tuple[object, ...]] = []
        self.position_target_calls: list[tuple[object, ...]] = []

    def set_gps_global_origin_send(self, *fields: int) -> None:
        self.global_origin_messages.append(fields)

    def command_long_send(self, *fields: object) -> None:
        self.command_calls.append(fields)

    def set_position_target_local_ned_send(self, *fields: object) -> None:
        self.position_target_calls.append(fields)


class FakeConnection:
    target_system = 1
    target_component = 1
    flightmode = "STABILIZE"

    def __init__(self) -> None:
        self.mav = FakeMav()
        self.closed = False

    def wait_heartbeat(self, timeout: float) -> SimpleNamespace:
        return SimpleNamespace(base_mode=0)

    def recv_match(self, blocking: bool, timeout: float) -> None:
        time.sleep(min(timeout, 0.01))
        return None

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("gps_free_mode", "expected_origin"),
    [
        (True, [(1, -353632620, 1491652370, 584000)]),
        (False, []),
    ],
)
def test_connect_sends_global_origin_only_in_gps_free_mode(
    monkeypatch: pytest.MonkeyPatch,
    gps_free_mode: bool,
    expected_origin: list[tuple[int, ...]],
) -> None:
    connection = FakeConnection()
    monkeypatch.setattr(
        mavutil,
        "mavlink_connection",
        lambda *args, **kwargs: connection,
    )
    settings = FlightSettings(
        gps_free_mode=gps_free_mode,
        global_origin_latitude_deg=-35.363262,
        global_origin_longitude_deg=149.165237,
        global_origin_altitude_m=584.0,
    )
    link = MavlinkFlightLink(settings)
    try:
        link.connect()
        assert connection.mav.global_origin_messages == expected_origin
        assert link.telemetry().pose is None
    finally:
        link.close()


@pytest.mark.parametrize(
    "origin",
    [
        (91.0, 149.165237, 584.0),
        (-35.363262, 181.0, 584.0),
        (-35.363262, 149.165237, float("inf")),
        (-35.363262, 149.165237, 2_147_484.0),
    ],
)
def test_invalid_gps_free_origin_is_rejected_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    origin: tuple[float, float, float],
) -> None:
    monkeypatch.setattr(
        mavutil,
        "mavlink_connection",
        lambda *args, **kwargs: pytest.fail("invalid origin opened a MAVLink connection"),
    )
    settings = FlightSettings(
        gps_free_mode=True,
        global_origin_latitude_deg=origin[0],
        global_origin_longitude_deg=origin[1],
        global_origin_altitude_m=origin[2],
    )
    with pytest.raises(ValueError):
        MavlinkFlightLink(settings).connect()


def test_guided_velocity_setpoint_is_a_velocity_only_local_ned_target() -> None:
    # Regression for the campaign's "precision landing does not descend" investigation: a
    # descent must reach the autopilot as a velocity-only LOCAL_NED target with vz positive
    # (down). ArduPilot ignores position/accel/yaw fields flagged in type_mask and drops the
    # whole message outside GUIDED, so the frame and mask have to be exact.
    import math

    connection = FakeConnection()
    link = MavlinkFlightLink(FlightSettings(), connection=connection)

    link.set_velocity_ned(0.0, 0.0, 0.15)
    link.set_velocity_ned(1.0, -2.0, -0.5, yaw_deg=30.0)

    first, second = connection.mav.position_target_calls
    assert first[3] == mavutil.mavlink.MAV_FRAME_LOCAL_NED
    assert first[4] == (1 | 2 | 4 | 64 | 128 | 256 | 1024 | 2048)  # ignore pos/accel/yaw only
    assert first[5:8] == (0.0, 0.0, 0.0)  # position ignored
    assert first[8:11] == (0.0, 0.0, 0.15)  # vx, vy, vz: positive vz is descent
    assert second[3] == mavutil.mavlink.MAV_FRAME_LOCAL_NED
    assert second[4] == (1 | 2 | 4 | 64 | 128 | 256 | 2048)  # yaw is used now
    assert second[8:11] == (1.0, -2.0, -0.5)
    assert second[14] == pytest.approx(math.radians(30.0))
    assert second[15] == 0.0


def test_control_messages_are_requested_fast_enough_for_low_real_time_factors() -> None:
    # SET_MESSAGE_INTERVAL is autopilot time; at the campaign's RTF ~0.18 a 5 Hz request delivered
    # a pose sample only every ~1.1 s of wall time (measured ages up to 2.15 s), tripping the
    # executor's 1.5 s wall staleness guard. Pose, heading and the vehicle clock must be requested
    # at 20 Hz so the wall gap stays ~0.3 s at RTF 0.18 and ~0.5 s at RTF 0.1.
    connection = FakeConnection()
    command_calls: list[tuple[object, ...]] = []
    connection.mav.command_long_send = lambda *fields: command_calls.append(fields)
    link = MavlinkFlightLink(FlightSettings(), connection=connection)

    link._request_telemetry()

    intervals = {
        call[4]: call[5]
        for call in command_calls
        if call[2] == mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL
    }
    assert intervals[mavutil.mavlink.MAVLINK_MSG_ID_SYSTEM_TIME] == 50_000
    assert intervals[mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE] == 50_000
    assert intervals[mavutil.mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED] == 50_000
    assert intervals[mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT] == 50_000
    assert intervals[253] == 200_000  # STATUSTEXT
    assert intervals[mavutil.mavlink.MAVLINK_MSG_ID_MOUNT_STATUS] == 200_000


def test_gimbal_command_uses_mount_control_and_checks_angles() -> None:
    connection = FakeConnection()
    command_calls: list[tuple[object, ...]] = []
    connection.mav.command_long_send = lambda *fields: command_calls.append(fields)
    link = MavlinkFlightLink(FlightSettings(), connection=connection)

    link.set_gimbal_pitch_yaw(-90.0, 25.0)

    assert command_calls == [
        (
            1,
            1,
            mavutil.mavlink.MAV_CMD_DO_MOUNT_CONTROL,
            0,
            -90.0,
            0.0,
            25.0,
            0.0,
            0.0,
            0.0,
            mavutil.mavlink.MAV_MOUNT_MODE_MAVLINK_TARGETING,
        )
    ]
    for pitch, yaw in (
        (-90.01, 0.0),
        (45.01, 0.0),
        (float("nan"), 0.0),
        (0.0, 160.01),
        (0.0, -160.01),
        (0.0, float("nan")),
    ):
        with pytest.raises(ValueError):
            link.set_gimbal_pitch_yaw(pitch, yaw)
    assert len(command_calls) == 1


def test_mount_status_stream_is_requested() -> None:
    connection = FakeConnection()
    link = MavlinkFlightLink(FlightSettings(), connection=connection)

    link._request_telemetry()

    assert any(
        call[2] == mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL
        and call[4] == mavutil.mavlink.MAVLINK_MSG_ID_MOUNT_STATUS
        and call[5] == 200_000
        for call in connection.mav.command_calls
    )


def test_mount_status_angles_are_exposed_in_degrees() -> None:
    link = MavlinkFlightLink(FlightSettings(), connection=FakeConnection())
    link._consume(
        SimpleNamespace(
            get_type=lambda: "MOUNT_STATUS",
            target_system=1,
            target_component=1,
            mount_mode=mavutil.mavlink.MAV_MOUNT_MODE_MAVLINK_TARGETING,
            pointing_a=-4500,
            pointing_b=250,
            pointing_c=9000,
        )
    )

    status = link.telemetry().mount_status
    assert status is not None
    assert (status.mount_mode, status.pitch_deg, status.roll_deg, status.yaw_deg) == (
        mavutil.mavlink.MAV_MOUNT_MODE_MAVLINK_TARGETING,
        -45.0,
        2.5,
        90.0,
    )
    assert link.telemetry().as_dict()["mount_status"] == status.as_dict()


def test_system_time_exposes_sitl_boot_clock():
    link = MavlinkFlightLink(FlightSettings(), connection=FakeConnection())

    link._consume(
        SimpleNamespace(get_type=lambda: "SYSTEM_TIME", time_boot_ms=12345)
    )

    assert link.telemetry().time_boot_ms == 12345
    assert link.telemetry().as_dict()["time_boot_ms"] == 12345


def test_downward_rangefinder_is_exposed_as_ground_distance():
    link = MavlinkFlightLink(FlightSettings(), connection=FakeConnection())

    link._consume(
        SimpleNamespace(
            get_type=lambda: "DISTANCE_SENSOR",
            current_distance=615,
            min_distance=10,
            max_distance=4000,
            orientation=25,
        )
    )

    telemetry = link.telemetry()

    # Orientation 25 is the downward rangefinder, not a horizontal proximity sector.
    assert telemetry.proximity == ()
    assert telemetry.ground_distance is not None
    assert telemetry.ground_distance.distance_m == pytest.approx(6.15)
    assert telemetry.ground_distance.orientation == 25
    assert telemetry.as_dict()["ground_distance"]["distance_m"] == pytest.approx(6.15)


def test_heading_and_range_ages_are_not_refreshed_by_other_telemetry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    link = MavlinkFlightLink(FlightSettings(), connection=FakeConnection())
    link._consume(SimpleNamespace(get_type=lambda: "ATTITUDE", roll=0.0, pitch=0.0, yaw=0.5))
    link._consume(
        SimpleNamespace(
            get_type=lambda: "DISTANCE_SENSOR",
            current_distance=250,
            min_distance=10,
            max_distance=600,
            orientation=0,
        )
    )

    # The heading source and range bridge stall while the autopilot keeps streaming other data.
    clock[0] = 105.0
    link._consume(SimpleNamespace(get_type=lambda: "SYSTEM_TIME", time_boot_ms=5000))
    telemetry = link.telemetry()

    assert telemetry.received_monotonic == 105.0
    assert telemetry.heading_updated_monotonic == 100.0
    assert telemetry.proximity[0].updated_monotonic == 100.0

    clock[0] = 106.0
    link._consume(SimpleNamespace(get_type=lambda: "ATTITUDE", roll=0.0, pitch=0.0, yaw=0.6))
    assert link.telemetry().heading_updated_monotonic == 106.0


def test_attitude_roll_pitch_and_mount_samples_carry_their_own_freshness(monkeypatch):
    import math

    from horus.perception.factory import camera_pose_from_telemetry

    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    link = MavlinkFlightLink(FlightSettings(), connection=FakeConnection())
    link._consume(SimpleNamespace(get_type=lambda: "ATTITUDE", roll=math.radians(4.0), pitch=math.radians(-2.0), yaw=0.0))
    link._consume(
        SimpleNamespace(
            get_type=lambda: "MOUNT_STATUS", target_system=1, target_component=1, mount_mode=2,
            pointing_a=-4500, pointing_b=150, pointing_c=0,
        )
    )
    link._consume(SimpleNamespace(get_type=lambda: "SYSTEM_TIME", time_boot_ms=42_000))
    # GPS keeps refreshing heading while ATTITUDE and MOUNT_STATUS stop arriving.
    clock[0] = 103.0
    link._consume(
        SimpleNamespace(
            get_type=lambda: "GLOBAL_POSITION_INT", vx=0, vy=0, vz=0, hdg=9000,
            lat=-353632620, lon=1491652370, relative_alt=5000,
        )
    )
    telemetry = link.telemetry()
    assert telemetry.vehicle_roll_deg == pytest.approx(4.0)
    assert telemetry.vehicle_pitch_deg == pytest.approx(-2.0)
    assert telemetry.as_dict()["attitude_age_s"] == pytest.approx(3.0)

    pose = camera_pose_from_telemetry(telemetry)
    assert pose["heading_age_s"] == 0.0
    assert pose["attitude_age_s"] == pytest.approx(3.0)
    assert pose["gimbal_age_s"] == pytest.approx(3.0)
    assert pose["gimbal_roll_deg"] == pytest.approx(1.5)
    assert (pose["vehicle_roll_deg"], pose["gimbal_pitch_deg"]) == (pytest.approx(4.0), -45.0)
    # The autopilot clock sample keeps its own age, so a frame's autopilot time can be estimated.
    assert (pose["time_boot_ms"], pose["time_boot_age_s"]) == (42_000, pytest.approx(3.0))
