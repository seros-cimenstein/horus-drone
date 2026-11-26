from __future__ import annotations

import json
import math
from pathlib import Path
import socket
import struct
import threading

import numpy as np

from horus.world.physics import (
    MAGIC_16,
    MAGIC_32,
    Airframe,
    MuJoCoQuadWorld,
    decode_servo_frame,
    encode_sitl_state,
)
from horus.world.scenes import build_scene_bundle
from horus.world.sensors import rangefinders_to_distance_readings


def test_servo_frame_decode_and_sitl_state_encode(tmp_path: Path) -> None:
    pwms16 = [1500] * 16
    pkt16 = struct.pack("<HHI16H", MAGIC_16, 800, 42, *pwms16)
    decoded16 = decode_servo_frame(pkt16)
    assert decoded16 is not None
    assert decoded16.frame_rate == 800 and decoded16.frame_count == 42 and len(decoded16.pwm) == 16

    pwms32 = [1200] * 32
    pkt32 = struct.pack("<HHI32H", MAGIC_32, 400, 99, *pwms32)
    decoded32 = decode_servo_frame(pkt32)
    assert decoded32 is not None and len(decoded32.pwm) == 32

    assert decode_servo_frame(b"bad") is None

    bundle = build_scene_bundle("indoor_sar", tmp_path / "assets")
    world = MuJoCoQuadWorld(bundle)
    raw = encode_sitl_state(world.last_state)
    assert raw.startswith(b"\n{") and raw.endswith(b"}\n")
    parsed = json.loads(raw.decode("utf-8").strip())
    assert set(parsed) == {"timestamp", "imu", "position", "velocity", "attitude"}


def test_airframe_hover_throttle_and_mujoco_takeoff_and_collision(tmp_path: Path) -> None:
    airframe = Airframe()
    hover = airframe.hover_throttle()
    assert 0.25 < hover < 0.65
    total_hover_n = 4.0 * airframe.thrust(hover * airframe.max_rpm, 0.0)
    assert abs(total_hover_n - airframe.mass_kg * 9.80665) < 0.05

    bundle = build_scene_bundle("indoor_sar", tmp_path / "assets")
    world = MuJoCoQuadWorld(bundle)

    # Resting on the floor pad: downward rangefinder should read ~0.05..0.15 m and north wall ~11 m? No, <=4 m cap.
    rf_rest = world.read_rangefinders_m()
    assert 0.05 <= rf_rest["range_down"] <= 0.20

    # Climb with above-hover throttle for 0.6 s.
    climb = min(1.0, hover + 0.18)
    for _ in range(480):
        state = world.step_throttles((climb, climb, climb, climb), 0.00125)
    assert -state.pos_ned[2] > 0.35  # altitude climbed above start

    # Rangefinders must return 7 valid MAVLink DISTANCE_SENSOR readings (cm).
    readings = rangefinders_to_distance_readings(world.read_rangefinders_m(), 1000)
    assert len(readings) == 7
    assert {r.orientation for r in readings} == {0, 1, 3, 4, 5, 7, 25}

    # Place quad just south of the west partition wall (y=4.0, x=-3.5) with initial northward velocity
    # and confirm MuJoCo contact physics stops it before the wall face (y=3.85).
    world.set_pose([-3.5, 3.60, 1.0])
    world.set_linear_velocity([0.0, 2.5, 0.0])
    for _ in range(400):
        world.step_throttles((hover, hover, hover, hover), 0.00125)
    assert world.data.xpos[world._quad_body_id][1] < 3.85
    assert world.read_rangefinders_m()["range_front"] < 0.6


def test_gimbal_setpoint_updates_nadir_camera_orientation(tmp_path: Path) -> None:
    bundle = build_scene_bundle("indoor_sar", tmp_path / "assets")
    world = MuJoCoQuadWorld(bundle)
    world.set_gimbal(-90.0, 0.0)
    world._apply_camera_poses()
    cam_mat = world.data.cam_xmat[world._drone_cam_id].reshape(3, 3)
    # MuJoCo camera looks along -Z axis of cam_xmat; at pitch=-90 deg, -cam_z should point straight down (0,0,-1).
    look_dir = -cam_mat[:, 2]
    assert look_dir[2] < -0.99
    assert math.hypot(look_dir[0], look_dir[1]) < 0.05


def test_udp_lockstep_roundtrip(tmp_path: Path) -> None:
    bundle = build_scene_bundle("indoor_sar", tmp_path / "assets")
    world = MuJoCoQuadWorld(bundle)
    pkt = struct.pack("<HHI16H", MAGIC_16, 800, 1, *([1000] * 16))
    count, reply = world.handle_servo_packet(pkt)
    assert count == 1 and reply is not None
    parsed = json.loads(reply.decode("utf-8").strip())
    assert parsed["timestamp"] > 0.0

    # Duplicate frame_count returns cached reply without advancing simulation time.
    count2, reply2 = world.handle_servo_packet(pkt, last_count=count, last_reply=reply)
    assert count2 == 1 and reply2 == reply
