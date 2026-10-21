from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from pymavlink import mavutil

from horus.config import FlightSettings
from horus.flight.link import MSG_ID_GIMBAL_DEVICE_ATTITUDE_STATUS, MountStatus, Pose, Telemetry
from horus.gta_bridge import (
    PACKET_SIZE,
    CameraPose,
    GtaTelemetryLink,
    compose_camera_pose,
    decode_packet,
    encode_packet,
    parse_target,
)

# Shared with the C# parser tests (gta/horus_mod/tests/Tests.cs).
VECTOR = json.loads(
    (Path(__file__).resolve().parents[1] / "gta/horus_mod/tests/pose_vector.json").read_text(encoding="utf-8")
)
VECTOR_POSE = CameraPose(
    VECTOR["north_m"], VECTOR["east_m"], VECTOR["down_m"],
    VECTOR["roll_deg"], VECTOR["pitch_deg"], VECTOR["yaw_deg"],
)
NOW = 100.0


def test_packet_is_56_bytes() -> None:
    assert PACKET_SIZE == 56
    assert len(encode_packet(0, VECTOR_POSE, 90.0)) == 56


def test_encoding_matches_the_shared_csharp_test_vector() -> None:
    assert encode_packet(VECTOR["seq"], VECTOR_POSE, VECTOR["hfov_deg"]).hex() == VECTOR["hex"]
    assert decode_packet(bytes.fromhex(VECTOR["hex"])) == (VECTOR["seq"], VECTOR_POSE, VECTOR["hfov_deg"])


def test_packet_layout_is_little_endian_in_contract_order() -> None:
    data = encode_packet(2**40 + 3, CameraPose(1.0, 2.0, 3.0, 4.0, 5.0, 6.0), 7.0)
    assert data[:4] == b"HPOS"
    assert struct.unpack_from("<I", data, 4) == (1,)
    assert struct.unpack_from("<Q", data, 8) == (2**40 + 3,)
    assert struct.unpack_from("<ddd", data, 16) == (1.0, 2.0, 3.0)
    assert struct.unpack_from("<ffff", data, 40) == (4.0, 5.0, 6.0, 7.0)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data[:-1],
        lambda data: data + b"\0",
        lambda data: b"XPOS" + data[4:],
        lambda data: data[:4] + struct.pack("<I", 2) + data[8:],
        lambda data: data[:16] + struct.pack("<d", math.nan) + data[24:],
        lambda data: data[:48] + struct.pack("<f", math.inf) + data[52:],
        lambda data: data[:52] + struct.pack("<f", 0.0),
        lambda data: data[:52] + struct.pack("<f", 180.0),
    ],
)
def test_decode_rejects_what_the_mod_rejects(mutate) -> None:
    with pytest.raises(ValueError):
        decode_packet(mutate(bytes.fromhex(VECTOR["hex"])))


def _telemetry(
    *,
    up_m: float = 12.0,
    heading_deg: float | None = 30.0,
    pose_age_s: float = 0.02,
    heading_age_s: float = 0.02,
    mount: MountStatus | None = None,
) -> Telemetry:
    return Telemetry(
        observed_at="",
        received_monotonic=NOW,
        pose=Pose(4.0, -2.5, up_m),
        pose_updated_monotonic=NOW - pose_age_s,
        heading_deg=heading_deg,
        heading_updated_monotonic=NOW - heading_age_s,
        mount_status=mount,
    )


def _mount(pitch: float, roll: float, yaw: float, age_s: float = 0.05) -> MountStatus:
    return MountStatus(1, 1, None, pitch, roll, yaw, "", NOW - age_s)


def test_without_gimbal_the_camera_uses_the_default_pitch_facing_the_nose() -> None:
    camera = compose_camera_pose(_telemetry(), NOW, default_pitch_deg=-45.0)
    assert camera == CameraPose(4.0, -2.5, -12.0, 0.0, -45.0, 30.0)


def test_gimbal_yaw_is_body_relative_and_pitch_roll_are_horizon_relative() -> None:
    camera = compose_camera_pose(
        _telemetry(heading_deg=350.0, mount=_mount(-60.0, 2.0, 25.0)), NOW, default_pitch_deg=-45.0
    )
    assert camera is not None
    assert (camera.roll_deg, camera.pitch_deg) == (2.0, -60.0)
    assert camera.yaw_deg == pytest.approx(15.0)  # 350 + 25, wrapped


def test_stale_gimbal_falls_back_to_the_default_pitch() -> None:
    camera = compose_camera_pose(
        _telemetry(mount=_mount(-90.0, 0.0, 40.0, age_s=1.5)), NOW, default_pitch_deg=-30.0
    )
    assert camera is not None
    assert (camera.pitch_deg, camera.roll_deg, camera.yaw_deg) == (-30.0, 0.0, 30.0)


@pytest.mark.parametrize(
    "telemetry",
    [
        Telemetry(observed_at="", received_monotonic=NOW, heading_deg=0.0, heading_updated_monotonic=NOW),
        _telemetry(pose_age_s=1.5),
        _telemetry(heading_deg=None),
        _telemetry(heading_age_s=1.5),
    ],
)
def test_no_packet_without_a_fresh_position_and_heading(telemetry: Telemetry) -> None:
    assert compose_camera_pose(telemetry, NOW, default_pitch_deg=0.0) is None


def test_link_messages_compose_like_horus_reads_them() -> None:
    link = GtaTelemetryLink(FlightSettings(), interval_us=20_000)
    link._consume(SimpleNamespace(
        get_type=lambda: "LOCAL_POSITION_NED", x=3.0, y=-1.0, z=-25.0, vx=0.0, vy=0.0, vz=-1.0,
    ))
    link._consume(SimpleNamespace(
        get_type=lambda: "ATTITUDE", roll=math.radians(5.0), pitch=math.radians(-3.0), yaw=math.radians(-90.0),
    ))
    link._consume(SimpleNamespace(
        get_type=lambda: "MOUNT_STATUS", target_system=1, target_component=1, mount_mode=2,
        pointing_a=-4500, pointing_b=150, pointing_c=-2000,
    ))
    telemetry = link.telemetry()
    camera = compose_camera_pose(telemetry, telemetry.received_monotonic, default_pitch_deg=0.0)
    assert camera is not None
    assert (camera.north_m, camera.east_m, camera.down_m) == (3.0, -1.0, -25.0)
    assert (camera.pitch_deg, camera.roll_deg) == (-45.0, 1.5)
    assert camera.yaw_deg == pytest.approx(250.0)  # heading 270 + gimbal -20


class _FakeMav:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def command_long_send(self, *fields: object) -> None:
        self.calls.append(fields)


def test_link_requests_the_pose_messages_at_the_bridge_rate() -> None:
    link = GtaTelemetryLink(FlightSettings(), interval_us=20_000)
    mav = _FakeMav()
    link._connection = SimpleNamespace(target_system=1, target_component=1, mav=mav)
    link._request_telemetry()
    intervals = {
        call[4]: call[5] for call in mav.calls if call[2] == mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL
    }
    assert intervals == {
        mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE: 20_000,
        mavutil.mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED: 20_000,
        mavutil.mavlink.MAVLINK_MSG_ID_MOUNT_STATUS: 20_000,
        MSG_ID_GIMBAL_DEVICE_ATTITUDE_STATUS: 20_000,
    }


def test_parse_target() -> None:
    assert parse_target("127.0.0.1:5611") == ("127.0.0.1", 5611)
    assert parse_target("gta-host:6000") == ("gta-host", 6000)
    for bad in ("5611", ":5611", "host:", "host:0", "host:70000", "host:x"):
        with pytest.raises(ValueError):
            parse_target(bad)
