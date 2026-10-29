from __future__ import annotations

import math
import socket
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from horus.config import GimbalSettings
from horus.gimbal import MavlinkGimbal
from horus.gimbal.device import GimbalDeviceEndpoint
from horus.gimbal.link import SETPOINT_RESEND_S
from horus.mavlink import load_mavutil

mavlink = load_mavutil().mavlink

DEVICE = (1, 154)
AUTOPILOT = (1, 1)
_LOCKS = mavlink.GIMBAL_DEVICE_FLAGS_ROLL_LOCK | mavlink.GIMBAL_DEVICE_FLAGS_PITCH_LOCK
VEHICLE_FRAME = _LOCKS | mavlink.GIMBAL_DEVICE_FLAGS_YAW_IN_VEHICLE_FRAME


def _quaternion(roll_deg: float, pitch_deg: float, yaw_deg: float) -> list[float]:
    """Yaw-pitch-roll Euler angles as a MAVLink (w, x, y, z) quaternion."""
    cr, sr = math.cos(math.radians(roll_deg) / 2), math.sin(math.radians(roll_deg) / 2)
    cp, sp = math.cos(math.radians(pitch_deg) / 2), math.sin(math.radians(pitch_deg) / 2)
    cy, sy = math.cos(math.radians(yaw_deg) / 2), math.sin(math.radians(yaw_deg) / 2)
    return [
        cr * cp * cy + sr * sp * sy,
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
    ]


def _euler(q: Any) -> tuple[float, float, float]:
    """(roll, pitch, yaw) degrees of a MAVLink (w, x, y, z) quaternion."""
    w, x, y, z = q
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


def _status(
    flags: int,
    yaw_deg: float,
    delta_yaw: float = math.nan,
    *,
    roll_deg: float = 2.0,
    pitch_deg: float = -30.0,
    source: tuple[int, int] = DEVICE,
) -> SimpleNamespace:
    return SimpleNamespace(
        get_type=lambda: "GIMBAL_DEVICE_ATTITUDE_STATUS",
        get_srcSystem=lambda: source[0],
        get_srcComponent=lambda: source[1],
        flags=flags,
        q=_quaternion(roll_deg, pitch_deg, yaw_deg),
        delta_yaw=delta_yaw,
    )


def _heartbeat(source: tuple[int, int] = DEVICE) -> SimpleNamespace:
    return SimpleNamespace(
        get_type=lambda: "HEARTBEAT", get_srcSystem=lambda: source[0], get_srcComponent=lambda: source[1]
    )


def _vehicle(
    heading_deg: float | None = 90.0,
    roll_deg: float | None = 0.0,
    pitch_deg: float | None = 0.0,
    updated: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        heading_deg=heading_deg,
        vehicle_roll_deg=roll_deg,
        vehicle_pitch_deg=pitch_deg,
        attitude_updated_monotonic=time.monotonic() if updated is None else updated,
    )


class FakeMav:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.setpoints: list[tuple[float, tuple]] = []
        self.commands: list[tuple] = []

    def gimbal_device_set_attitude_send(self, *fields: Any) -> None:
        self.setpoints.append((self._clock(), fields))

    def command_long_send(self, *fields: Any) -> None:
        self.commands.append(fields)


class FakeConnection:
    """Feeds ``script`` to the gimbal's reader: messages, or callables run in place of a receive.
    The reader stops once the script is exhausted."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.mav = FakeMav(clock)
        self.script: list[Any] = []
        self.gimbal: MavlinkGimbal | None = None
        self.closed = False

    def recv_match(self, blocking: bool, timeout: float) -> Any:
        if not self.script:
            self.gimbal._stop.set()
            return None
        step = self.script.pop(0)
        return step() if callable(step) else step

    def close(self) -> None:
        self.closed = True


def _gimbal(vehicle: Any = None, clock: Callable[[], float] = time.monotonic, **settings: Any) -> MavlinkGimbal:
    connection = FakeConnection(clock)
    gimbal = MavlinkGimbal(
        GimbalSettings(**settings), lambda: _vehicle() if vehicle is None else vehicle, connection=connection
    )
    connection.gimbal = gimbal
    return gimbal


@pytest.mark.parametrize(
    ("flags", "reported_yaw_deg", "delta_yaw_deg", "expected_yaw_deg"),
    [
        # Vehicle-frame yaw is already relative to the nose; the 90 deg heading must not apply.
        (VEHICLE_FRAME, 20.0, None, 20.0),
        # Earth-frame yaw without delta_yaw: the vehicle heading (90) converts it.
        (mavlink.GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME, 110.0, None, 20.0),
        # Earth-frame yaw with the message's own delta_yaw (80): it wins over the heading sample.
        (mavlink.GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME, 110.0, 80.0, 30.0),
        # Devices predating the frame flags: YAW_LOCK means earth frame, no lock vehicle frame;
        # delta_yaw is ignored without a frame flag.
        (mavlink.GIMBAL_DEVICE_FLAGS_YAW_LOCK, 110.0, 80.0, 20.0),
        (0, 20.0, 80.0, 20.0),
        # Wrapped into [-180, 180).
        (mavlink.GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME, -100.0, None, 170.0),
    ],
)
def test_gimbal_device_status_yaw_is_reported_relative_to_the_vehicle_nose(
    flags: int, reported_yaw_deg: float, delta_yaw_deg: float | None, expected_yaw_deg: float
) -> None:
    gimbal = _gimbal(_vehicle(heading_deg=90.0))
    delta = math.nan if delta_yaw_deg is None else math.radians(delta_yaw_deg)
    gimbal._consume(_status(flags, reported_yaw_deg, delta))

    attitude = gimbal.attitude()
    assert attitude is not None
    assert (attitude.pitch_deg, attitude.roll_deg, attitude.yaw_deg) == (
        pytest.approx(-30.0), pytest.approx(2.0), pytest.approx(expected_yaw_deg)
    )


@pytest.mark.parametrize(
    ("lean_roll_deg", "lean_pitch_deg", "reported_roll_deg", "reported_pitch_deg"),
    [
        # A servo demand while braking nose-up 8 deg: -30 deg horizon target minus the lean.
        (0.0, 8.0, 0.0, -38.0),
        # Banked 5 deg right: the servo rolls the camera 5 deg left to keep it level.
        (5.0, 0.0, -5.0, 0.0),
    ],
)
@pytest.mark.parametrize("body_frame", [True, False])
def test_body_frame_gimbal_reports_are_composed_with_the_vehicle_lean(
    lean_roll_deg: float, lean_pitch_deg: float, reported_roll_deg: float, reported_pitch_deg: float, body_frame: bool
) -> None:
    gimbal = _gimbal(_vehicle(roll_deg=lean_roll_deg, pitch_deg=lean_pitch_deg), status_body_frame=body_frame)
    gimbal._consume(_status(VEHICLE_FRAME, 0.0, roll_deg=reported_roll_deg, pitch_deg=reported_pitch_deg))

    attitude = gimbal.attitude()
    assert attitude is not None
    expected = (
        (reported_pitch_deg + lean_pitch_deg, reported_roll_deg + lean_roll_deg) if body_frame
        else (reported_pitch_deg, reported_roll_deg)
    )
    assert (attitude.pitch_deg, attitude.roll_deg, attitude.yaw_deg) == (
        pytest.approx(expected[0], abs=1e-6), pytest.approx(expected[1], abs=1e-6), pytest.approx(0.0, abs=1e-6)
    )


def test_unusable_gimbal_device_samples_leave_the_gimbal_unmeasured(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    # Earth-frame yaw with no delta_yaw and no vehicle heading cannot be made vehicle-relative.
    horizon = _gimbal(SimpleNamespace(
        heading_deg=None, vehicle_roll_deg=None, vehicle_pitch_deg=None, attitude_updated_monotonic=None
    ))
    horizon._consume(_status(mavlink.GIMBAL_DEVICE_FLAGS_YAW_IN_EARTH_FRAME, 110.0))
    nan_attitude = _status(VEHICLE_FRAME, 0.0)
    nan_attitude.q = [math.nan] * 4
    horizon._consume(nan_attitude)
    assert horizon.attitude() is None
    # A horizon-frame device needs no vehicle lean at all.
    horizon._consume(_status(VEHICLE_FRAME, 0.0))
    assert horizon.attitude() is not None

    # A body-frame report needs a fresh vehicle lean: never received, then too old.
    vehicle = SimpleNamespace(
        heading_deg=0.0, vehicle_roll_deg=None, vehicle_pitch_deg=None, attitude_updated_monotonic=None
    )
    body = _gimbal(vehicle, status_body_frame=True)
    body._consume(_status(VEHICLE_FRAME, 0.0))
    assert body.attitude() is None
    vehicle.vehicle_roll_deg, vehicle.vehicle_pitch_deg, vehicle.attitude_updated_monotonic = 0.0, 0.0, 100.0
    clock[0] += 1.5
    body._consume(_status(VEHICLE_FRAME, 0.0))
    assert body.attitude() is None
    vehicle.attitude_updated_monotonic = clock[0]
    body._consume(_status(VEHICLE_FRAME, 0.0))
    attitude = body.attitude()
    assert attitude is not None
    assert attitude.updated_monotonic == clock[0]
    assert attitude.as_dict(now=clock[0] + 0.25)["age_s"] == pytest.approx(0.25)


def test_status_from_anything_but_the_gimbal_device_is_ignored() -> None:
    gimbal = _gimbal()
    # The autopilot's own (or a relayed) gimbal status must not be taken for the device's.
    gimbal._consume(_status(VEHICLE_FRAME, 0.0, source=AUTOPILOT))
    gimbal._consume(_status(VEHICLE_FRAME, 0.0, source=(2, 154)))
    gimbal._consume(_heartbeat(AUTOPILOT))
    assert gimbal.attitude() is None
    assert gimbal.device_heartbeat_age_s() is None
    assert gimbal._connection.mav.commands == []

    gimbal._consume(_status(VEHICLE_FRAME, 0.0))
    assert gimbal.attitude() is not None


def test_point_sends_a_horizon_locked_setpoint_to_the_device_and_rejects_bad_angles() -> None:
    gimbal = _gimbal()
    sent = gimbal._connection.mav.setpoints

    gimbal.point(-30.0, 25.0)

    assert len(sent) == 1
    target_system, target_component, flags, q, *rates = sent[0][1]
    assert (target_system, target_component) == DEVICE
    # Roll and pitch locked to the horizon, yaw relative to the nose.
    assert flags == 44
    roll, pitch, yaw = _euler(q)
    assert (roll, pitch, yaw) == (pytest.approx(0.0, abs=1e-6), pytest.approx(-30.0), pytest.approx(25.0))
    # Attitude control only: no angular-rate setpoint.
    assert all(math.isnan(rate) for rate in rates)

    for pitch_deg, yaw_deg in (
        (-90.01, 0.0),
        (45.01, 0.0),
        (math.nan, 0.0),
        (0.0, 160.01),
        (0.0, -160.01),
        (0.0, math.nan),
    ):
        with pytest.raises(ValueError):
            gimbal.point(pitch_deg, yaw_deg)
    assert len(sent) == 1

    gimbal.point(-90.0)
    assert _euler(sent[-1][1][3])[1] == pytest.approx(-90.0, abs=1e-3)


def test_setpoint_is_resent_until_changed(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def advance(seconds: float) -> Callable[[], None]:
        def step() -> None:
            clock[0] += seconds

        return step

    gimbal = _gimbal(clock=lambda: clock[0])
    sent = gimbal._connection.mav.setpoints
    gimbal._connection.script = [advance(0.25), advance(0.25), advance(0.25), advance(0.5)]
    gimbal.point(-45.0)
    # A rejected request must not displace the held setpoint.
    with pytest.raises(ValueError):
        gimbal.point(-120.0)

    gimbal._run()

    assert [at for at, _ in sent] == [100.0, 100.0 + SETPOINT_RESEND_S, 101.25]
    assert all(_euler(fields[3])[1] == pytest.approx(-45.0) for _, fields in sent)


def test_status_rate_is_requested_once_on_the_first_device_heartbeat() -> None:
    gimbal = _gimbal()
    commands = gimbal._connection.mav.commands

    gimbal._consume(_heartbeat())
    gimbal._consume(_heartbeat())

    assert len(commands) == 1
    target_system, target_component, command, _, message_id, interval_us, *_ = commands[0]
    assert (target_system, target_component) == DEVICE
    assert command == mavlink.MAV_CMD_SET_MESSAGE_INTERVAL
    assert message_id == mavlink.MAVLINK_MSG_ID_GIMBAL_DEVICE_ATTITUDE_STATUS
    assert interval_us == pytest.approx(50_000)
    assert gimbal.device_heartbeat_age_s() is not None


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for(condition: Callable[[], Any], timeout_s: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def test_gimbal_and_simulated_device_agree_over_udp() -> None:
    port = _free_udp_port()
    gimbal = MavlinkGimbal(GimbalSettings(connection=f"udpin:127.0.0.1:{port}"), _vehicle)
    device = GimbalDeviceEndpoint(f"udpout:127.0.0.1:{port}")
    gimbal.connect()
    device.start()
    try:
        # The device announces itself first; HORUS can only answer an address it has heard from.
        _wait_for(lambda: gimbal.device_heartbeat_age_s() is not None)
        gimbal.point(-60.0, 15.0)
        target = _wait_for(device.target)
        assert target == (pytest.approx(-60.0), pytest.approx(15.0))

        device.publish(-59.0, 0.5, 14.0)
        attitude = _wait_for(gimbal.attitude)
        assert (attitude.pitch_deg, attitude.roll_deg, attitude.yaw_deg) == (
            pytest.approx(-59.0, abs=1e-3), pytest.approx(0.5, abs=1e-3), pytest.approx(14.0, abs=1e-3)
        )
    finally:
        device.close()
        gimbal.close()
