from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from pymavlink import mavutil
from pymavlink.dialects.v20 import common as mavlink2

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
    # A descent must reach the autopilot as a velocity-only LOCAL_NED target with vz positive
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
    # SET_MESSAGE_INTERVAL is autopilot time: at RTF ~0.18 a 5 Hz request delivers a pose sample
    # only every ~1.1 s of wall time, past the executor's 1.5 s wall staleness guard once jitter
    # adds up. Pose, heading and the vehicle clock must be requested at 20 Hz so the wall gap
    # stays ~0.3 s at RTF 0.18 and ~0.5 s at RTF 0.1.
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
    assert intervals[mavutil.mavlink.MAVLINK_MSG_ID_STATUSTEXT] == 200_000
    assert intervals[mavutil.mavlink.MAVLINK_MSG_ID_MOUNT_STATUS] == 200_000
    assert intervals[mavlink2.MAVLINK_MSG_ID_GIMBAL_DEVICE_ATTITUDE_STATUS] == 200_000


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


def _quaternion(roll_deg: float, pitch_deg: float, yaw_deg: float) -> list[float]:
    """Yaw-pitch-roll Euler angles as a MAVLink (w, x, y, z) quaternion."""
    import math

    cr, sr = math.cos(math.radians(roll_deg) / 2), math.sin(math.radians(roll_deg) / 2)
    cp, sp = math.cos(math.radians(pitch_deg) / 2), math.sin(math.radians(pitch_deg) / 2)
    cy, sy = math.cos(math.radians(yaw_deg) / 2), math.sin(math.radians(yaw_deg) / 2)
    return [
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    ]


def _gimbal_device_status(
    flags: int, yaw_deg: float, delta_yaw: float = float("nan"), *, roll_deg: float = 2.0, pitch_deg: float = -30.0
) -> SimpleNamespace:
    return SimpleNamespace(
        get_type=lambda: "GIMBAL_DEVICE_ATTITUDE_STATUS",
        target_system=0,
        target_component=0,
        flags=flags,
        q=_quaternion(roll_deg, pitch_deg, yaw_deg),
        delta_yaw=delta_yaw,
    )


_LOCKS = mavlink2.GIMBAL_DEVICE_FLAGS_ROLL_LOCK | mavlink2.GIMBAL_DEVICE_FLAGS_PITCH_LOCK


@pytest.mark.parametrize(
    ("flags", "reported_yaw_deg", "delta_yaw_deg", "expected_yaw_deg"),
    [
        # ArduPilot's flags: yaw already relative to the nose; the 90 deg heading must not apply.
        (_LOCKS | mavlink2.GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME, 20.0, None, 20.0),
        # Earth-frame yaw without delta_yaw: the vehicle heading (90) converts it.
        (mavlink2.GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME, 110.0, None, 20.0),
        # Earth-frame yaw with the message's own delta_yaw (80): it wins over the heading sample.
        (mavlink2.GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME, 110.0, 80.0, 30.0),
        # Devices predating the frame flags: YAW_LOCK means earth frame, no lock vehicle frame;
        # delta_yaw is ignored without a frame flag.
        (mavlink2.GIMBAL_DEVICE_FLAGS_YAW_LOCK, 110.0, 80.0, 20.0),
        (0, 20.0, 80.0, 20.0),
        # Wrapped into [-180, 180).
        (mavlink2.GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME, -100.0, None, 170.0),
    ],
)
def test_gimbal_device_status_yaw_is_reported_relative_to_the_vehicle_nose(
    flags: int, reported_yaw_deg: float, delta_yaw_deg: float | None, expected_yaw_deg: float
) -> None:
    import math

    link = MavlinkFlightLink(FlightSettings(), connection=FakeConnection())
    link._consume(SimpleNamespace(get_type=lambda: "ATTITUDE", roll=0.0, pitch=0.0, yaw=math.radians(90.0)))
    delta = float("nan") if delta_yaw_deg is None else math.radians(delta_yaw_deg)
    link._consume(_gimbal_device_status(flags, reported_yaw_deg, delta))

    status = link.telemetry().mount_status
    assert status is not None
    assert status.mount_mode is None
    assert (status.pitch_deg, status.roll_deg, status.yaw_deg) == (
        pytest.approx(-30.0), pytest.approx(2.0), pytest.approx(expected_yaw_deg)
    )


@pytest.mark.parametrize(
    ("lean_roll_deg", "lean_pitch_deg", "reported_roll_deg", "reported_pitch_deg"),
    [
        # ArduPilot servo demand while braking nose-up 8 deg: -30 deg horizon target minus the lean.
        (0.0, 8.0, 0.0, -38.0),
        # Banked 5 deg right: the servo rolls the camera 5 deg left to keep it level.
        (5.0, 0.0, -5.0, 0.0),
    ],
)
@pytest.mark.parametrize("body_frame", [True, False])
def test_body_frame_gimbal_reports_are_composed_with_the_vehicle_lean(
    lean_roll_deg: float, lean_pitch_deg: float, reported_roll_deg: float, reported_pitch_deg: float, body_frame: bool
) -> None:
    import math

    link = MavlinkFlightLink(FlightSettings(gimbal_status_body_frame=body_frame), connection=FakeConnection())
    link._consume(SimpleNamespace(
        get_type=lambda: "ATTITUDE", roll=math.radians(lean_roll_deg), pitch=math.radians(lean_pitch_deg),
        yaw=math.radians(90.0),
    ))
    link._consume(_gimbal_device_status(
        _LOCKS | mavlink2.GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME, 0.0,
        roll_deg=reported_roll_deg, pitch_deg=reported_pitch_deg,
    ))

    status = link.telemetry().mount_status
    assert status is not None
    expected = (
        (reported_pitch_deg + lean_pitch_deg, reported_roll_deg + lean_roll_deg) if body_frame
        else (reported_pitch_deg, reported_roll_deg)
    )
    assert (status.pitch_deg, status.roll_deg, status.yaw_deg) == (
        pytest.approx(expected[0], abs=1e-6), pytest.approx(expected[1], abs=1e-6), pytest.approx(0.0, abs=1e-6)
    )


def test_unusable_gimbal_device_samples_leave_the_gimbal_unmeasured(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    horizon = MavlinkFlightLink(FlightSettings(gimbal_status_body_frame=False), connection=FakeConnection())
    # Earth-frame yaw with no delta_yaw and no vehicle heading yet cannot be made vehicle-relative.
    horizon._consume(_gimbal_device_status(mavlink2.GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME, 110.0))
    nan_attitude = _gimbal_device_status(mavlink2.GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME, 0.0)
    nan_attitude.q = [float("nan")] * 4
    horizon._consume(nan_attitude)
    assert horizon.telemetry().mount_status is None

    # A body-frame report needs a fresh vehicle lean: never received, then too old.
    body = MavlinkFlightLink(FlightSettings(), connection=FakeConnection())
    vehicle_frame = _gimbal_device_status(mavlink2.GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME, 0.0)
    body._consume(vehicle_frame)
    assert body.telemetry().mount_status is None
    body._consume(SimpleNamespace(get_type=lambda: "ATTITUDE", roll=0.0, pitch=0.0, yaw=0.0))
    clock[0] += 1.5
    body._consume(vehicle_frame)
    assert body.telemetry().mount_status is None
    clock[0] += 0.1
    body._consume(SimpleNamespace(get_type=lambda: "ATTITUDE", roll=0.0, pitch=0.0, yaw=0.0))
    body._consume(vehicle_frame)
    assert body.telemetry().mount_status is not None


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


def test_velocity_comes_from_the_message_that_supplies_the_pose():
    link = MavlinkFlightLink(FlightSettings(), connection=FakeConnection())

    def global_position(vx_cm_s: int) -> SimpleNamespace:
        return SimpleNamespace(
            get_type=lambda: "GLOBAL_POSITION_INT", vx=vx_cm_s, vy=-50, vz=20, hdg=0,
            lat=-353632620, lon=1491652370, relative_alt=2000,
        )

    # Without LOCAL_POSITION_NED the GLOBAL_POSITION_INT fallback supplies pose and velocity.
    link._consume(global_position(150))
    assert link.telemetry().velocity.as_dict() == {"north_m_s": 1.5, "east_m_s": -0.5, "down_m_s": 0.2}

    # Once local NED arrives it alone feeds pose and velocity.
    link._consume(SimpleNamespace(
        get_type=lambda: "LOCAL_POSITION_NED", x=1.0, y=2.0, z=-3.0, vx=0.25, vy=0.5, vz=-0.125))
    link._consume(global_position(900))
    telemetry = link.telemetry()
    assert telemetry.pose.as_dict() == {"north_m": 1.0, "east_m": 2.0, "up_m": 3.0}
    assert telemetry.velocity.as_dict() == {"north_m_s": 0.25, "east_m_s": 0.5, "down_m_s": -0.125}
