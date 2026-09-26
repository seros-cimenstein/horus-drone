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

    def set_gps_global_origin_send(self, *fields: int) -> None:
        self.global_origin_messages.append(fields)


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


def test_system_time_exposes_sitl_boot_clock():
    link = MavlinkFlightLink(FlightSettings(), connection=FakeConnection())

    link._consume(
        SimpleNamespace(get_type=lambda: "SYSTEM_TIME", time_boot_ms=12345)
    )

    assert link.telemetry().time_boot_ms == 12345
    assert link.telemetry().as_dict()["time_boot_ms"] == 12345
