"""Built-in cross-platform Python/MuJoCo 3D test world (#4e2c) and SITL runner."""

from __future__ import annotations

import os
import sys

from horus.bootstrap import preferred_linux_mujoco_gl

_gl = preferred_linux_mujoco_gl()
if _gl is not None:
    os.environ.setdefault("MUJOCO_GL", _gl)

from horus.world.physics import Airframe, MuJoCoQuadWorld, QuadState, decode_servo_frame, encode_sitl_state
from horus.world.scenes import SCENE_NAMES, build_scene_bundle, generate_apriltag_36h11_id0
from horus.world.sitl import SitlProcess, compose_sitl_parms

__all__ = [
    "Airframe",
    "MuJoCoQuadWorld",
    "QuadState",
    "SCENE_NAMES",
    "SitlProcess",
    "build_scene_bundle",
    "compose_sitl_parms",
    "decode_servo_frame",
    "encode_sitl_state",
    "generate_apriltag_36h11_id0",
]
