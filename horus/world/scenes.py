"""Built-in 3D scenes (MuJoCo MJCF + procedural PBR textures + AprilTag pad + matching vector maps)."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import random

import numpy as np

from horus.maps.model import Area, MapData, Place, Way
from horus.world.textures import generate_scene_textures, write_png

SCENE_NAMES = ("indoor_sar", "forest_sar", "urban_plaza", "procedural")

# Canonical Tag36h11 ID 0 fallback bitmap (10x10 with 1px white border -> 8x8 tag, 6x6 data)
# Used only if cv2.aruco is unavailable; otherwise cv2.aruco generates the exact marker.
_TAG36H11_ID0_FALLBACK = np.array(
    [
        [1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
        [1, 0, 0, 0, 0, 0, 0, 0, 0, 1],
        [1, 0, 1, 0, 0, 1, 1, 0, 0, 1],
        [1, 0, 1, 1, 0, 1, 0, 1, 0, 1],
        [1, 0, 0, 1, 1, 0, 1, 0, 0, 1],
        [1, 0, 1, 0, 1, 1, 0, 1, 0, 1],
        [1, 0, 0, 1, 0, 0, 1, 1, 0, 1],
        [1, 0, 1, 1, 0, 1, 0, 0, 0, 1],
        [1, 0, 0, 0, 0, 0, 0, 0, 0, 1],
        [1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
    ],
    dtype=np.uint8,
) * 255


@dataclass(frozen=True, slots=True)
class SceneSpec:
    """Description of one built-in 3D scene."""

    name: str
    title: str
    subtitle: str
    indoor: bool
    home_pose: tuple[float, float, float] = (0.0, 0.0, 0.121)
    default_prompts: tuple[str, ...] = ()


SCENE_SPECS: dict[str, SceneSpec] = {
    "indoor_sar": SceneSpec(
        name="indoor_sar",
        title="Indoor SAR Room",
        subtitle="Operations facility with terrazzo floor, sunlit windows, corridor, furniture, AprilTag pad, and SAR subjects",
        indoor=True,
        default_prompts=(
            "Take off and find the person in the red shirt, then hover 2 m away.",
            "Fly down the north corridor and report everyone you see.",
            "Test wall avoidance: try flying 8 m north toward the wall.",
            "Return home and land on the AprilTag pad.",
        ),
    ),
    "forest_sar": SceneSpec(
        name="forest_sar",
        title="Outdoor Forest",
        subtitle="Alpine woodland clearing with conifers, deciduous trees, boulders, campsite, trail, and hikers",
        indoor=False,
        default_prompts=(
            "Take off to 4 m and search along the trail for hikers.",
            "Find the hiker near the clearing and report their position.",
            "Return home and land on the AprilTag pad.",
        ),
    ),
    "urban_plaza": SceneSpec(
        name="urban_plaza",
        title="Urban Plaza",
        subtitle="City square with glass & brick high-rises, water fountain, vehicles, streetlights, and pedestrians",
        indoor=False,
        default_prompts=(
            "Find Central Plaza on the map and fly there at 6 m altitude.",
            "Search the plaza for pedestrians and mark any person you spot.",
            "Return home and land on the AprilTag pad.",
        ),
    ),
    "procedural": SceneSpec(
        name="procedural",
        title="Procedural World",
        subtitle="Seeded countryside valley with ranger shelter, campsite, rock outcrops, mixed forest, and hikers",
        indoor=False,
        default_prompts=(
            "Take off to 5 m, inspect nearby landmarks, and look for hikers.",
            "Return home and land on the AprilTag pad.",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class SceneBundle:
    """Generated MJCF XML, texture asset files, and matching vector MapData."""

    name: str
    xml: str
    map_data: MapData
    asset_dir: Path
    seed: int = 0
    indoor: bool = True


def generate_apriltag_36h11_id0(size_px: int = 256) -> np.ndarray:
    """Generate an RGB uint8 image of AprilTag 36h11 ID 0 with a white landing-pad border."""
    try:
        import cv2

        aruco = getattr(cv2, "aruco", None)
        if aruco is not None and hasattr(aruco, "DICT_APRILTAG_36h11"):
            get_dict = getattr(aruco, "getPredefinedDictionary", None) or getattr(aruco, "Dictionary_get", None)
            if callable(get_dict):
                dictionary = get_dict(aruco.DICT_APRILTAG_36h11)
                marker_px = max(64, int(size_px * 0.72))
                if hasattr(aruco, "generateImageMarker"):
                    marker = aruco.generateImageMarker(dictionary, 0, marker_px)
                else:
                    marker = np.zeros((marker_px, marker_px), dtype=np.uint8)
                    aruco.drawMarker(dictionary, 0, marker_px, marker, 1)
                canvas = np.full((size_px, size_px), 245, dtype=np.uint8)
                offset = (size_px - marker_px) // 2
                canvas[offset : offset + marker_px, offset : offset + marker_px] = marker
                return np.repeat(canvas[:, :, None], 3, axis=2)
    except Exception:
        pass
    scale = max(1, size_px // 10)
    small = np.repeat(np.repeat(_TAG36H11_ID0_FALLBACK, scale, axis=0), scale, axis=1)
    canvas = np.full((size_px, size_px), 245, dtype=np.uint8)
    h, w = min(size_px, small.shape[0]), min(size_px, small.shape[1])
    y0, x0 = (size_px - h) // 2, (size_px - w) // 2
    canvas[y0 : y0 + h, x0 : x0 + w] = small[:h, :w]
    return np.repeat(canvas[:, :, None], 3, axis=2)


def _apriltag_pad_mjcf(z: float = 0.002) -> str:
    """Build a crisp 8x8 Tag36h11 ID 0 landing pad framed inside a dark rubber helipad mat."""
    grid8 = (
        (0, 0, 0, 0, 0, 0, 0, 0),
        (0, 0, 0, 1, 0, 0, 0, 0),
        (0, 0, 1, 1, 0, 1, 0, 0),
        (0, 0, 0, 0, 1, 0, 1, 0),
        (0, 0, 0, 0, 1, 1, 0, 0),
        (0, 1, 0, 1, 1, 1, 0, 0),
        (0, 1, 0, 1, 0, 1, 1, 0),
        (0, 0, 0, 0, 0, 0, 0, 0),
    )
    cell = 0.08
    # Outer dark rubber helipad mat border strictly outside +/-0.45 m so white quiet zone stays clean.
    mat_z = z - 0.0005
    parts = [
        f'<geom name="pad_mat_n" type="box" pos="0 0.51 {mat_z:.4f}" size="0.57 0.06 0.002" rgba="0.12 0.13 0.15 1" contype="0" conaffinity="0"/>',
        f'<geom name="pad_mat_s" type="box" pos="0 -0.51 {mat_z:.4f}" size="0.57 0.06 0.002" rgba="0.12 0.13 0.15 1" contype="0" conaffinity="0"/>',
        f'<geom name="pad_mat_w" type="box" pos="-0.51 0 {mat_z:.4f}" size="0.06 0.45 0.002" rgba="0.12 0.13 0.15 1" contype="0" conaffinity="0"/>',
        f'<geom name="pad_mat_e" type="box" pos="0.51 0 {mat_z:.4f}" size="0.06 0.45 0.002" rgba="0.12 0.13 0.15 1" contype="0" conaffinity="0"/>',
        # Subtle high-vis orange corner accents on outer mat
        f'<geom name="pad_accent_nw" type="box" pos="-0.51 0.51 {z:.4f}" size="0.05 0.05 0.002" rgba="0.92 0.42 0.08 1" contype="0" conaffinity="0"/>',
        f'<geom name="pad_accent_ne" type="box" pos="0.51 0.51 {z:.4f}" size="0.05 0.05 0.002" rgba="0.92 0.42 0.08 1" contype="0" conaffinity="0"/>',
        f'<geom name="pad_accent_sw" type="box" pos="-0.51 -0.51 {z:.4f}" size="0.05 0.05 0.002" rgba="0.92 0.42 0.08 1" contype="0" conaffinity="0"/>',
        f'<geom name="pad_accent_se" type="box" pos="0.51 -0.51 {z:.4f}" size="0.05 0.05 0.002" rgba="0.92 0.42 0.08 1" contype="0" conaffinity="0"/>',
        # Exact white quiet zone + collision surface at (0, 0, z)
        f'<geom name="apriltag_pad" type="box" pos="0 0 {z:.4f}" size="0.45 0.45 0.002" '
        f'rgba="0.96 0.96 0.96 1" contype="1" conaffinity="1"/>',
    ]
    cell_z = z + 0.0025
    for r in range(8):
        for c in range(8):
            if grid8[r][c] == 0:
                cx = (c - 3.5) * cell
                cy = (3.5 - r) * cell
                parts.append(
                    f'<geom name="tag_c_{r}_{c}" type="box" pos="{cx:.4f} {cy:.4f} {cell_z:.4f}" '
                    f'size="{cell * 0.505:.4f} {cell * 0.505:.4f} 0.001" rgba="0.03 0.03 0.03 1" '
                    f'contype="0" conaffinity="0"/>'
                )
    return "\n    ".join(parts)


def _person_mjcf(
    name: str,
    x: float,
    y: float,
    *,
    yaw_deg: float = 0.0,
    shirt_rgba: str = "0.85 0.14 0.14 1",
    pants_rgba: str = "0.15 0.20 0.30 1",
    skin_rgba: str = "0.85 0.69 0.55 1",
    hair_rgba: str = "0.15 0.10 0.08 1",
    posture: str = "standing",
    freejoint: bool = False,
    backpack: bool = False,
    hi_vis: bool = False,
    z_base: float = 0.0,
) -> str:
    """Build an anatomically proportioned multi-part humanoid detectable by YOLO & ReID."""
    rad = math.radians(yaw_deg)
    qw, qz = math.cos(rad * 0.5), math.sin(rad * 0.5)
    shoe_rgba = "0.14 0.14 0.16 1"
    belt_rgba = "0.12 0.10 0.08 1"
    pack_rgba = "0.22 0.28 0.20 1"

    if posture == "lying":
        # Recovery posture resting on ground/mat along local X axis (head +X, feet -X)
        return f"""
    <body name="{name}" pos="{x:.3f} {y:.3f} {z_base + 0.02:.3f}" quat="{qw:.5f} 0 0 {qz:.5f}">
      <geom name="{name}_shoe_l" type="box" pos="-0.82 0.09 0.07" size="0.05 0.045 0.08" rgba="{shoe_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_shoe_r" type="box" pos="-0.82 -0.09 0.07" size="0.05 0.045 0.08" rgba="{shoe_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_calf_l" type="capsule" fromto="-0.78 0.09 0.08 -0.44 0.09 0.09" size="0.062" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_calf_r" type="capsule" fromto="-0.78 -0.09 0.08 -0.44 -0.09 0.09" size="0.062" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_thigh_l" type="capsule" fromto="-0.44 0.09 0.09 -0.10 0.09 0.10" size="0.075" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_thigh_r" type="capsule" fromto="-0.44 -0.09 0.09 -0.10 -0.09 0.10" size="0.075" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_pelvis" type="box" pos="-0.06 0 0.11" size="0.10 0.16 0.09" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_torso" type="box" pos="0.22 0 0.13" size="0.22 0.18 0.105" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_chest" type="ellipsoid" pos="0.28 0 0.16" size="0.18 0.17 0.09" rgba="{shirt_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_arm_l" type="capsule" fromto="0.34 0.22 0.11 0.02 0.24 0.08" size="0.05" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_arm_r" type="capsule" fromto="0.34 -0.22 0.11 0.02 -0.24 0.08" size="0.05" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_hand_l" type="sphere" pos="-0.02 0.24 0.07" size="0.042" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_hand_r" type="sphere" pos="-0.02 -0.24 0.07" size="0.042" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_neck" type="capsule" fromto="0.42 0 0.13 0.49 0 0.14" size="0.048" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_head" type="ellipsoid" pos="0.58 0 0.14" size="0.115 0.095 0.105" rgba="{skin_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_hair" type="ellipsoid" pos="0.59 0 0.10" size="0.118 0.098 0.085" rgba="{hair_rgba}" contype="0" conaffinity="0"/>
    </body>"""

    if posture == "sitting":
        # Seated facing +X (seat surface ~0.42-0.46 m)
        return f"""
    <body name="{name}" pos="{x:.3f} {y:.3f} {z_base:.3f}" quat="{qw:.5f} 0 0 {qz:.5f}">
      <geom name="{name}_shoe_l" type="box" pos="0.42 0.10 0.04" size="0.10 0.048 0.04" rgba="{shoe_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_shoe_r" type="box" pos="0.42 -0.10 0.04" size="0.10 0.048 0.04" rgba="{shoe_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_calf_l" type="capsule" fromto="0.39 0.10 0.07 0.37 0.10 0.45" size="0.058" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_calf_r" type="capsule" fromto="0.39 -0.10 0.07 0.37 -0.10 0.45" size="0.058" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_thigh_l" type="capsule" fromto="0.37 0.10 0.47 0.03 0.10 0.47" size="0.072" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_thigh_r" type="capsule" fromto="0.37 -0.10 0.47 0.03 -0.10 0.47" size="0.072" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_pelvis" type="box" pos="0 0 0.50" size="0.11 0.16 0.08" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_torso" type="box" pos="-0.02 0 0.78" size="0.115 0.18 0.24" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_chest" type="ellipsoid" pos="0.00 0 0.85" size="0.125 0.185 0.18" rgba="{shirt_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_arm_l" type="capsule" fromto="0.0 0.21 0.96 0.16 0.18 0.64" size="0.048" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_arm_r" type="capsule" fromto="0.0 -0.21 0.96 0.16 -0.18 0.64" size="0.048" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_hand_l" type="sphere" pos="0.21 0.16 0.59" size="0.040" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_hand_r" type="sphere" pos="0.21 -0.16 0.59" size="0.040" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_neck" type="capsule" fromto="-0.01 0 1.01 0.01 0 1.09" size="0.048" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_head" type="ellipsoid" pos="0.02 0 1.20" size="0.10 0.092 0.115" rgba="{skin_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_hair" type="ellipsoid" pos="-0.01 0 1.24" size="0.105 0.098 0.095" rgba="{hair_rgba}" contype="0" conaffinity="0"/>
    </body>"""

    joint_xml = f'<joint name="{name}_slide_x" type="slide" axis="1 0 0" range="-4 4" damping="50"/>\n      ' if freejoint else ""
    pack_xml = (
        f'<geom name="{name}_pack" type="box" pos="-0.17 0 1.14" size="0.08 0.14 0.20" rgba="{pack_rgba}" contype="0" conaffinity="0"/>\n      '
        f'<geom name="{name}_pack_top" type="cylinder" pos="-0.17 0 1.36" size="0.075 0.11" euler="90 0 0" rgba="0.72 0.30 0.14 1" contype="0" conaffinity="0"/>'
        if backpack
        else ""
    )
    vest_xml = (
        f'<geom name="{name}_hivis1" type="box" pos="0 0 1.00" size="0.127 0.192 0.022" rgba="0.92 0.94 0.18 1" contype="0" conaffinity="0"/>\n      '
        f'<geom name="{name}_hivis2" type="box" pos="0 0 1.15" size="0.127 0.194 0.022" rgba="0.92 0.94 0.18 1" contype="0" conaffinity="0"/>'
        if hi_vis
        else ""
    )
    return f"""
    <body name="{name}" pos="{x:.3f} {y:.3f} {z_base:.3f}" quat="{qw:.5f} 0 0 {qz:.5f}">
      {joint_xml}<geom name="{name}_shoe_l" type="box" pos="0.04 0.095 0.038" size="0.105 0.048 0.038" rgba="{shoe_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_shoe_r" type="box" pos="0.04 -0.095 0.038" size="0.105 0.048 0.038" rgba="{shoe_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_calf_l" type="capsule" fromto="0 0.095 0.07 0 0.095 0.45" size="0.058" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_calf_r" type="capsule" fromto="0 -0.095 0.07 0 -0.095 0.45" size="0.058" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_thigh_l" type="capsule" fromto="0 0.095 0.46 0 0.09 0.86" size="0.074" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_thigh_r" type="capsule" fromto="0 -0.095 0.46 0 -0.09 0.86" size="0.074" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_belt" type="box" pos="0 0 0.88" size="0.120 0.180 0.032" rgba="{belt_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_torso" type="box" pos="0 0 1.13" size="0.116 0.182 0.24" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_chest" type="ellipsoid" pos="0.005 0 1.22" size="0.122 0.192 0.17" rgba="{shirt_rgba}" contype="0" conaffinity="0"/>
      {vest_xml}
      {pack_xml}
      <geom name="{name}_shldr_l" type="sphere" pos="0 0.215 1.33" size="0.068" rgba="{shirt_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_shldr_r" type="sphere" pos="0 -0.215 1.33" size="0.068" rgba="{shirt_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_uarm_l" type="capsule" fromto="0 0.225 1.32 0.01 0.235 1.04" size="0.050" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_uarm_r" type="capsule" fromto="0 -0.225 1.32 0.01 -0.235 1.04" size="0.050" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_farm_l" type="capsule" fromto="0.01 0.235 1.04 0.05 0.235 0.80" size="0.042" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_farm_r" type="capsule" fromto="0.01 -0.235 1.04 0.05 -0.235 0.80" size="0.042" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_hand_l" type="sphere" pos="0.06 0.235 0.77" size="0.040" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_hand_r" type="sphere" pos="0.06 -0.235 0.77" size="0.040" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_neck" type="capsule" fromto="0 0 1.38 0 0 1.46" size="0.048" rgba="{skin_rgba}" contype="0" conaffinity="0"/>
      <geom name="{name}_head" type="ellipsoid" pos="0.01 0 1.58" size="0.10 0.090 0.115" rgba="{skin_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_hair" type="ellipsoid" pos="-0.02 0 1.62" size="0.104 0.096 0.095" rgba="{hair_rgba}" contype="0" conaffinity="0"/>
    </body>"""


def _quad_body_mjcf(home_pose: tuple[float, float, float]) -> str:
    hx, hy, hz = home_pose
    # F450 quadrotor: exact 0.12 m collision sphere + detailed carbon/orange airframe, skids, motors, GPS mast & gimbal.
    return f"""
    <body name="quad" pos="{hx:.3f} {hy:.3f} {hz:.3f}">
      <freejoint name="quad_root"/>
      <inertial pos="0 0 0" mass="1.5" diaginertia="0.0124 0.0124 0.0246"/>
      <geom name="quad_hull" type="sphere" size="0.12" rgba="0.10 0.11 0.14 0.0" contype="1" conaffinity="1" friction="0.9 0.02 0.001"/>
      <!-- Carbon-fiber dual center plates & battery -->
      <geom name="quad_plate_low" type="box" pos="0 0 -0.012" size="0.088 0.088 0.008" rgba="0.14 0.16 0.20 1" contype="0" conaffinity="0"/>
      <geom name="quad_plate_top" type="box" pos="0 0 0.018" size="0.082 0.082 0.006" rgba="0.18 0.20 0.24 1" contype="0" conaffinity="0"/>
      <geom name="quad_standoffs" type="box" pos="0 0 0.003" size="0.068 0.068 0.010" rgba="0.28 0.30 0.34 1" contype="0" conaffinity="0"/>
      <geom name="quad_battery" type="box" pos="0 -0.01 0.042" size="0.038 0.068 0.020" rgba="0.10 0.38 0.72 1" contype="0" conaffinity="0"/>
      <geom name="quad_bat_strap" type="box" pos="0 -0.01 0.043" size="0.040 0.016 0.022" rgba="0.08 0.08 0.10 1" contype="0" conaffinity="0"/>
      <!-- GPS mast & dome -->
      <geom name="gps_mast" type="cylinder" pos="0 -0.055 0.062" size="0.005 0.036" rgba="0.20 0.22 0.25 1" contype="0" conaffinity="0"/>
      <geom name="gps_puck" type="cylinder" pos="0 -0.055 0.098" size="0.028 0.007" rgba="0.12 0.13 0.16 1" contype="0" conaffinity="0"/>
      <!-- Landing skids -->
      <geom name="skid_l_strut1" type="capsule" fromto="-0.065 0.045 -0.015 -0.095 0.055 -0.105" size="0.006" rgba="0.18 0.20 0.24 1" contype="0" conaffinity="0"/>
      <geom name="skid_l_strut2" type="capsule" fromto="-0.065 -0.045 -0.015 -0.095 -0.055 -0.105" size="0.006" rgba="0.18 0.20 0.24 1" contype="0" conaffinity="0"/>
      <geom name="skid_l_rail" type="capsule" fromto="-0.095 -0.115 -0.108 -0.095 0.115 -0.108" size="0.008" rgba="0.12 0.13 0.16 1" contype="0" conaffinity="0"/>
      <geom name="skid_r_strut1" type="capsule" fromto="0.065 0.045 -0.015 0.095 0.055 -0.105" size="0.006" rgba="0.18 0.20 0.24 1" contype="0" conaffinity="0"/>
      <geom name="skid_r_strut2" type="capsule" fromto="0.065 -0.045 -0.015 0.095 -0.055 -0.105" size="0.006" rgba="0.18 0.20 0.24 1" contype="0" conaffinity="0"/>
      <geom name="skid_r_rail" type="capsule" fromto="0.095 -0.115 -0.108 0.095 0.115 -0.108" size="0.008" rgba="0.12 0.13 0.16 1" contype="0" conaffinity="0"/>
      <!-- 4 Arms (front orange, rear dark slate) + motor bells + hubs + rotor discs + nav LEDs -->
      <geom name="arm_fr" type="capsule" fromto="0.05 0.05 0 0.162 0.162 0.004" size="0.014" rgba="0.92 0.30 0.10 1" contype="0" conaffinity="0"/>
      <geom name="arm_fl" type="capsule" fromto="-0.05 0.05 0 -0.162 0.162 0.004" size="0.014" rgba="0.92 0.30 0.10 1" contype="0" conaffinity="0"/>
      <geom name="arm_br" type="capsule" fromto="0.05 -0.05 0 0.162 -0.162 0.004" size="0.014" rgba="0.18 0.20 0.24 1" contype="0" conaffinity="0"/>
      <geom name="arm_bl" type="capsule" fromto="-0.05 -0.05 0 -0.162 -0.162 0.004" size="0.014" rgba="0.18 0.20 0.24 1" contype="0" conaffinity="0"/>
      <geom name="motor_fr" type="cylinder" pos="0.162 0.162 0.016" size="0.018 0.012" rgba="0.32 0.35 0.40 1" contype="0" conaffinity="0"/>
      <geom name="motor_fl" type="cylinder" pos="-0.162 0.162 0.016" size="0.018 0.012" rgba="0.32 0.35 0.40 1" contype="0" conaffinity="0"/>
      <geom name="motor_br" type="cylinder" pos="0.162 -0.162 0.016" size="0.018 0.012" rgba="0.32 0.35 0.40 1" contype="0" conaffinity="0"/>
      <geom name="motor_bl" type="cylinder" pos="-0.162 -0.162 0.016" size="0.018 0.012" rgba="0.32 0.35 0.40 1" contype="0" conaffinity="0"/>
      <geom name="hub_fr" type="cylinder" pos="0.162 0.162 0.032" size="0.007 0.007" rgba="0.80 0.82 0.85 1" contype="0" conaffinity="0"/>
      <geom name="hub_fl" type="cylinder" pos="-0.162 0.162 0.032" size="0.007 0.007" rgba="0.80 0.82 0.85 1" contype="0" conaffinity="0"/>
      <geom name="hub_br" type="cylinder" pos="0.162 -0.162 0.032" size="0.007 0.007" rgba="0.80 0.82 0.85 1" contype="0" conaffinity="0"/>
      <geom name="hub_bl" type="cylinder" pos="-0.162 -0.162 0.032" size="0.007 0.007" rgba="0.80 0.82 0.85 1" contype="0" conaffinity="0"/>
      <geom name="rotor_fr" type="cylinder" pos="0.162 0.162 0.030" size="0.122 0.003" rgba="0.10 0.12 0.15 0.45" contype="0" conaffinity="0"/>
      <geom name="rotor_fl" type="cylinder" pos="-0.162 0.162 0.030" size="0.122 0.003" rgba="0.10 0.12 0.15 0.45" contype="0" conaffinity="0"/>
      <geom name="rotor_br" type="cylinder" pos="0.162 -0.162 0.030" size="0.122 0.003" rgba="0.10 0.12 0.15 0.45" contype="0" conaffinity="0"/>
      <geom name="rotor_bl" type="cylinder" pos="-0.162 -0.162 0.030" size="0.122 0.003" rgba="0.10 0.12 0.15 0.45" contype="0" conaffinity="0"/>
      <geom name="led_fr" type="sphere" pos="0.162 0.162 -0.004" size="0.009" rgba="0.15 0.95 0.30 1" contype="0" conaffinity="0"/>
      <geom name="led_fl" type="sphere" pos="-0.162 0.162 -0.004" size="0.009" rgba="0.95 0.18 0.15 1" contype="0" conaffinity="0"/>
      <!-- Nose gimbal camera pod -->
      <geom name="gimbal_pod" type="sphere" pos="0 0.105 -0.048" size="0.026" rgba="0.09 0.10 0.13 1" contype="0" conaffinity="0"/>
      <geom name="gimbal_lens" type="cylinder" pos="0 0.128 -0.052" size="0.013 0.010" euler="90 0 0" rgba="0.04 0.05 0.07 1" contype="0" conaffinity="0"/>
      <site name="imu_site" pos="0 0 0"/>
      <site name="tof_front" pos="0 0.05 0" zaxis="0 1 0"/>
      <site name="tof_front_right" pos="0.035 0.035 0" zaxis="0.707107 0.707107 0"/>
      <site name="tof_back_right" pos="0.035 -0.035 0" zaxis="0.707107 -0.707107 0"/>
      <site name="tof_back" pos="0 -0.05 0" zaxis="0 -1 0"/>
      <site name="tof_back_left" pos="-0.035 -0.035 0" zaxis="-0.707107 -0.707107 0"/>
      <site name="tof_front_left" pos="-0.035 0.035 0" zaxis="-0.707107 0.707107 0"/>
      <site name="tof_down" pos="0 0 -0.05" zaxis="0 0 -1"/>
      <camera name="drone_cam" pos="0 0.155 -0.052" fovy="85" euler="90 0 0"/>
    </body>
    <camera name="spectator_cam" pos="-2.6 -3.2 2.2" xyaxes="0.78 -0.62 0 0.28 0.35 0.89" fovy="65"/>"""


def _sensors_mjcf() -> str:
    return """
  <sensor>
    <rangefinder name="range_front" site="tof_front" cutoff="4.0"/>
    <rangefinder name="range_front_right" site="tof_front_right" cutoff="4.0"/>
    <rangefinder name="range_back_right" site="tof_back_right" cutoff="4.0"/>
    <rangefinder name="range_back" site="tof_back" cutoff="4.0"/>
    <rangefinder name="range_back_left" site="tof_back_left" cutoff="4.0"/>
    <rangefinder name="range_front_left" site="tof_front_left" cutoff="4.0"/>
    <rangefinder name="range_down" site="tof_down" cutoff="4.0"/>
  </sensor>"""


def _conifer_mjcf(idx: int, x: float, y: float, height: float, *, prefix: str = "tree") -> str:
    """Multi-tiered realistic pine/spruce tree with bark trunk and layered evergreen boughs."""
    trunk_h = height * 0.44
    r_base = max(0.22, min(0.35, height * 0.055))
    tint = 0.90 + ((idx * 17) % 5) * 0.06
    c1 = f"{0.13 * tint:.3f} {0.32 * tint:.3f} {0.14 * tint:.3f} 1"
    c2 = f"{0.16 * tint:.3f} {0.38 * tint:.3f} {0.16 * tint:.3f} 1"
    c3 = f"{0.19 * tint:.3f} {0.44 * tint:.3f} {0.19 * tint:.3f} 1"
    c4 = f"{0.22 * tint:.3f} {0.49 * tint:.3f} {0.21 * tint:.3f} 1"
    return (
        f'<geom name="{prefix}_trunk_{idx}" type="cylinder" pos="{x:.2f} {y:.2f} {trunk_h * 0.5:.2f}" '
        f'size="{r_base:.2f} {trunk_h * 0.5:.2f}" material="bark_mat" contype="1" conaffinity="1"/>\n    '
        f'<geom name="{prefix}_tier1_{idx}" type="ellipsoid" pos="{x:.2f} {y:.2f} {height * 0.38:.2f}" '
        f'size="{height * 0.28:.2f} {height * 0.28:.2f} {height * 0.10:.2f}" material="foliage_mat" rgba="{c1}" contype="1" conaffinity="1"/>\n    '
        f'<geom name="{prefix}_tier2_{idx}" type="ellipsoid" pos="{x:.2f} {y:.2f} {height * 0.52:.2f}" '
        f'size="{height * 0.23:.2f} {height * 0.23:.2f} {height * 0.10:.2f}" material="foliage_mat" rgba="{c2}" contype="1" conaffinity="1"/>\n    '
        f'<geom name="{prefix}_tier3_{idx}" type="ellipsoid" pos="{x:.2f} {y:.2f} {height * 0.66:.2f}" '
        f'size="{height * 0.18:.2f} {height * 0.18:.2f} {height * 0.10:.2f}" material="foliage_mat" rgba="{c2}" contype="0" conaffinity="0"/>\n    '
        f'<geom name="{prefix}_tier4_{idx}" type="ellipsoid" pos="{x:.2f} {y:.2f} {height * 0.80:.2f}" '
        f'size="{height * 0.12:.2f} {height * 0.12:.2f} {height * 0.11:.2f}" material="foliage_mat" rgba="{c3}" contype="0" conaffinity="0"/>\n    '
        f'<geom name="{prefix}_tip_{idx}" type="ellipsoid" pos="{x:.2f} {y:.2f} {height * 0.93:.2f}" '
        f'size="{height * 0.065:.2f} {height * 0.065:.2f} {height * 0.10:.2f}" material="foliage_mat" rgba="{c4}" contype="0" conaffinity="0"/>'
    )


def _deciduous_mjcf(idx: int, x: float, y: float, height: float, *, prefix: str = "oak") -> str:
    """Broadleaf deciduous tree with bark trunk and organic clustered crown lobes."""
    trunk_h = height * 0.52
    r_trunk = max(0.24, min(0.36, height * 0.055))
    crown_z = height * 0.72
    cr = height * 0.26
    tint = 0.92 + ((idx * 13) % 4) * 0.06
    c_main = f"{0.20 * tint:.3f} {0.44 * tint:.3f} {0.18 * tint:.3f} 1"
    c_top = f"{0.25 * tint:.3f} {0.50 * tint:.3f} {0.21 * tint:.3f} 1"
    return (
        f'<geom name="{prefix}_trunk_{idx}" type="cylinder" pos="{x:.2f} {y:.2f} {trunk_h * 0.5:.2f}" '
        f'size="{r_trunk:.2f} {trunk_h * 0.5:.2f}" material="bark_mat" contype="1" conaffinity="1"/>\n    '
        f'<geom name="{prefix}_crown_c_{idx}" type="sphere" pos="{x:.2f} {y:.2f} {crown_z:.2f}" '
        f'size="{cr:.2f}" material="foliage_mat" rgba="{c_main}" contype="1" conaffinity="1"/>\n    '
        f'<geom name="{prefix}_crown_l1_{idx}" type="sphere" pos="{x + cr * 0.55:.2f} {y + cr * 0.30:.2f} {crown_z - cr * 0.12:.2f}" '
        f'size="{cr * 0.78:.2f}" material="foliage_mat" rgba="{c_main}" contype="0" conaffinity="0"/>\n    '
        f'<geom name="{prefix}_crown_l2_{idx}" type="sphere" pos="{x - cr * 0.52:.2f} {y - cr * 0.28:.2f} {crown_z - cr * 0.08:.2f}" '
        f'size="{cr * 0.75:.2f}" material="foliage_mat" rgba="{c_main}" contype="0" conaffinity="0"/>\n    '
        f'<geom name="{prefix}_crown_top_{idx}" type="sphere" pos="{x - cr * 0.12:.2f} {y + cr * 0.22:.2f} {crown_z + cr * 0.48:.2f}" '
        f'size="{cr * 0.74:.2f}" material="foliage_mat" rgba="{c_top}" contype="0" conaffinity="0"/>'
    )


def _tent_mjcf(name: str, x: float, y: float, yaw_deg: float = 0.0, rgba: str = "0.90 0.42 0.12 1") -> str:
    rad = math.radians(yaw_deg)
    qw, qz = math.cos(rad * 0.5), math.sin(rad * 0.5)
    return f"""
    <body name="{name}" pos="{x:.2f} {y:.2f} 0" quat="{qw:.5f} 0 0 {qz:.5f}">
      <geom name="{name}_floor" type="box" pos="0 0 0.03" size="1.05 0.78 0.03" rgba="0.18 0.20 0.22 1" contype="1" conaffinity="1"/>
      <geom name="{name}_slope_l" type="box" pos="0 0.39 0.56" size="1.06 0.56 0.025" euler="45 0 0" rgba="{rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_slope_r" type="box" pos="0 -0.39 0.56" size="1.06 0.56 0.025" euler="-45 0 0" rgba="{rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_back" type="box" pos="-1.03 0 0.44" size="0.025 0.65 0.44" rgba="0.72 0.34 0.10 1" contype="1" conaffinity="1"/>
      <geom name="{name}_ridge" type="cylinder" pos="0 0 0.96" size="0.025 1.08" euler="0 90 0" rgba="0.78 0.78 0.80 1" contype="0" conaffinity="0"/>
    </body>"""


def _vehicle_mjcf(
    name: str,
    x: float,
    y: float,
    yaw_deg: float,
    *,
    body_rgba: str = "0.18 0.38 0.64 1",
    kind: str = "van",
) -> str:
    rad = math.radians(yaw_deg)
    qw, qz = math.cos(rad * 0.5), math.sin(rad * 0.5)
    if kind == "sedan":
        return f"""
    <body name="{name}" pos="{x:.2f} {y:.2f} 0" quat="{qw:.5f} 0 0 {qz:.5f}">
      <geom name="{name}_chassis" type="box" pos="0 0 0.42" size="0.90 2.05 0.26" rgba="{body_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_cabin" type="box" pos="0 -0.18 0.82" size="0.78 1.05 0.22" rgba="{body_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_windshield" type="box" pos="0 0.88 0.78" size="0.72 0.16 0.16" euler="-30 0 0" rgba="0.16 0.24 0.34 1" contype="0" conaffinity="0"/>
      <geom name="{name}_rear_win" type="box" pos="0 -1.24 0.78" size="0.72 0.16 0.16" euler="30 0 0" rgba="0.16 0.24 0.34 1" contype="0" conaffinity="0"/>
      <geom name="{name}_side_win" type="box" pos="0 -0.18 0.83" size="0.79 0.92 0.14" rgba="0.16 0.24 0.34 1" contype="0" conaffinity="0"/>
      <geom name="{name}_bumper_f" type="box" pos="0 2.08 0.28" size="0.88 0.06 0.10" rgba="0.14 0.15 0.17 1" contype="0" conaffinity="0"/>
      <geom name="{name}_bumper_r" type="box" pos="0 -2.08 0.28" size="0.88 0.06 0.10" rgba="0.14 0.15 0.17 1" contype="0" conaffinity="0"/>
      <geom name="{name}_hl_l" type="box" pos="-0.62 2.06 0.46" size="0.14 0.02 0.07" rgba="0.95 0.95 0.85 1" contype="0" conaffinity="0"/>
      <geom name="{name}_hl_r" type="box" pos="0.62 2.06 0.46" size="0.14 0.02 0.07" rgba="0.95 0.95 0.85 1" contype="0" conaffinity="0"/>
      <geom name="{name}_wheel_fl" type="cylinder" pos="-0.88 1.28 0.30" size="0.30 0.12" euler="0 90 0" rgba="0.08 0.08 0.09 1" contype="0" conaffinity="0"/>
      <geom name="{name}_wheel_fr" type="cylinder" pos="0.88 1.28 0.30" size="0.30 0.12" euler="0 90 0" rgba="0.08 0.08 0.09 1" contype="0" conaffinity="0"/>
      <geom name="{name}_wheel_rl" type="cylinder" pos="-0.88 -1.28 0.30" size="0.30 0.12" euler="0 90 0" rgba="0.08 0.08 0.09 1" contype="0" conaffinity="0"/>
      <geom name="{name}_wheel_rr" type="cylinder" pos="0.88 -1.28 0.30" size="0.30 0.12" euler="0 90 0" rgba="0.08 0.08 0.09 1" contype="0" conaffinity="0"/>
      <geom name="{name}_rim_fl" type="cylinder" pos="-0.99 1.28 0.30" size="0.16 0.02" euler="0 90 0" rgba="0.72 0.74 0.78 1" contype="0" conaffinity="0"/>
      <geom name="{name}_rim_fr" type="cylinder" pos="0.99 1.28 0.30" size="0.16 0.02" euler="0 90 0" rgba="0.72 0.74 0.78 1" contype="0" conaffinity="0"/>
      <geom name="{name}_rim_rl" type="cylinder" pos="-0.99 -1.28 0.30" size="0.16 0.02" euler="0 90 0" rgba="0.72 0.74 0.78 1" contype="0" conaffinity="0"/>
      <geom name="{name}_rim_rr" type="cylinder" pos="0.99 -1.28 0.30" size="0.16 0.02" euler="0 90 0" rgba="0.72 0.74 0.78 1" contype="0" conaffinity="0"/>
    </body>"""
    return f"""
    <body name="{name}" pos="{x:.2f} {y:.2f} 0" quat="{qw:.5f} 0 0 {qz:.5f}">
      <geom name="{name}_cargo" type="box" pos="0 -0.35 1.05" size="1.02 1.85 0.75" rgba="{body_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_cab" type="box" pos="0 1.65 0.84" size="0.98 0.60 0.56" rgba="{body_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_windshield" type="box" pos="0 2.24 1.05" size="0.88 0.04 0.28" euler="-14 0 0" rgba="0.16 0.24 0.34 1" contype="0" conaffinity="0"/>
      <geom name="{name}_side_win" type="box" pos="0 1.72 1.06" size="0.99 0.42 0.24" rgba="0.16 0.24 0.34 1" contype="0" conaffinity="0"/>
      <geom name="{name}_stripe" type="box" pos="0 -0.35 0.92" size="1.03 1.80 0.09" rgba="0.92 0.58 0.10 1" contype="0" conaffinity="0"/>
      <geom name="{name}_bumper" type="box" pos="0 2.26 0.34" size="1.00 0.08 0.12" rgba="0.12 0.13 0.15 1" contype="0" conaffinity="0"/>
      <geom name="{name}_hl_l" type="box" pos="-0.72 2.26 0.54" size="0.15 0.02 0.08" rgba="0.98 0.96 0.85 1" contype="0" conaffinity="0"/>
      <geom name="{name}_hl_r" type="box" pos="0.72 2.26 0.54" size="0.15 0.02 0.08" rgba="0.98 0.96 0.85 1" contype="0" conaffinity="0"/>
      <geom name="{name}_wheel_fl" type="cylinder" pos="-0.98 1.55 0.34" size="0.34 0.13" euler="0 90 0" rgba="0.08 0.08 0.09 1" contype="0" conaffinity="0"/>
      <geom name="{name}_wheel_fr" type="cylinder" pos="0.98 1.55 0.34" size="0.34 0.13" euler="0 90 0" rgba="0.08 0.08 0.09 1" contype="0" conaffinity="0"/>
      <geom name="{name}_wheel_rl" type="cylinder" pos="-0.98 -1.35 0.34" size="0.34 0.13" euler="0 90 0" rgba="0.08 0.08 0.09 1" contype="0" conaffinity="0"/>
      <geom name="{name}_wheel_rr" type="cylinder" pos="0.98 -1.35 0.34" size="0.34 0.13" euler="0 90 0" rgba="0.08 0.08 0.09 1" contype="0" conaffinity="0"/>
      <geom name="{name}_rim_fl" type="cylinder" pos="-1.10 1.55 0.34" size="0.18 0.02" euler="0 90 0" rgba="0.72 0.74 0.78 1" contype="0" conaffinity="0"/>
      <geom name="{name}_rim_fr" type="cylinder" pos="1.10 1.55 0.34" size="0.18 0.02" euler="0 90 0" rgba="0.72 0.74 0.78 1" contype="0" conaffinity="0"/>
      <geom name="{name}_rim_rl" type="cylinder" pos="-1.10 -1.35 0.34" size="0.18 0.02" euler="0 90 0" rgba="0.72 0.74 0.78 1" contype="0" conaffinity="0"/>
      <geom name="{name}_rim_rr" type="cylinder" pos="1.10 -1.35 0.34" size="0.18 0.02" euler="0 90 0" rgba="0.72 0.74 0.78 1" contype="0" conaffinity="0"/>
    </body>"""


def _indoor_sar_scene() -> tuple[str, str, str, MapData]:
    assets_xml = """
    <texture name="skybox" type="skybox" file="sky_indoor.png" gridsize="6 1" gridlayout="UDLRFB"/>
    <texture name="floor_tex" type="2d" file="terrazzo_floor.png"/>
    <material name="floor_mat" texture="floor_tex" texuniform="true" texrepeat="0.55 0.55" reflectance="0.04" specular="0.22" shininess="0.30"/>
    <texture name="wall_tex" type="2d" file="plaster_wall.png"/>
    <material name="wall_mat" texture="wall_tex" texuniform="true" texrepeat="0.45 0.45" specular="0.10" shininess="0.15"/>
    <texture name="wood_tex" type="2d" file="wood_oak.png"/>
    <material name="wood_mat" texture="wood_tex" texuniform="true" texrepeat="1.2 1.2" specular="0.28" shininess="0.38"/>
    <texture name="crate_tex" type="2d" file="crate_wood.png"/>
    <material name="crate_mat" texture="crate_tex" texuniform="true" texrepeat="1.0 1.0" specular="0.12"/>
    <texture name="metal_tex" type="2d" file="brushed_metal.png"/>
    <material name="metal_mat" texture="metal_tex" texuniform="true" texrepeat="1.0 1.0" specular="0.55" shininess="0.65"/>
    <texture name="rug_tex" type="2d" file="fabric_rug.png"/>
    <material name="rug_mat" texture="rug_tex" texuniform="true" texrepeat="1.5 1.5" specular="0.02"/>
    """
    visual_xml = """
    <global offwidth="640" offheight="480"/>
    <quality shadowsize="4096" offsamples="4"/>
    <headlight ambient="0.22 0.23 0.25" diffuse="0.34 0.34 0.34" specular="0.08 0.08 0.08"/>
    <map znear="0.001" zfar="10"/>
    """
    # Room: X in [-6, 6] (12m wide), Y in [-5, 11] (16m long), wall height 3.2m.
    # Divider wall at Y=4.0 with central 2.2m doorway (X in [-1.1, 1.1]); west divider face at Y=3.85.
    body_xml = f"""
    <!-- Indoor facility lighting: warm overhead LED panels + soft window daylight fill -->
    <light name="led_main_s" pos="0 -1.5 3.10" dir="0 0 -1" directional="false" diffuse="0.72 0.70 0.66" specular="0.25 0.25 0.25" attenuation="0.8 0.04 0.01" cutoff="85" castshadow="true"/>
    <light name="led_main_n" pos="0 2.0 3.10" dir="0 0 -1" directional="false" diffuse="0.72 0.70 0.66" specular="0.25 0.25 0.25" attenuation="0.8 0.04 0.01" cutoff="85" castshadow="true"/>
    <light name="led_corr" pos="0 7.5 3.10" dir="0 0 -1" directional="false" diffuse="0.68 0.69 0.72" specular="0.20 0.20 0.20" attenuation="0.8 0.04 0.01" cutoff="85" castshadow="false"/>
    <light name="daylight_fill" pos="-5.5 1.5 2.4" dir="0.85 0.1 -0.45" directional="true" diffuse="0.35 0.42 0.52" castshadow="false"/>

    <!-- Architectural terrazzo floor, woven lounge rug, and safety corridor striping -->
    <geom name="floor" type="plane" size="12 14 0.1" material="floor_mat" contype="1" conaffinity="1"/>
    <geom name="lounge_rug" type="box" pos="-2.6 1.6 0.002" size="1.85 1.45 0.002" material="rug_mat" contype="0" conaffinity="0"/>
    <geom name="lane_line_w" type="box" pos="-1.08 7.5 0.002" size="0.035 3.2 0.001" rgba="0.92 0.72 0.10 1" contype="0" conaffinity="0"/>
    <geom name="lane_line_e" type="box" pos="1.08 7.5 0.002" size="0.035 3.2 0.001" rgba="0.92 0.72 0.10 1" contype="0" conaffinity="0"/>
    {_apriltag_pad_mjcf(0.002)}

    <!-- Perimeter walls with dark slate baseboards & crown trim -->
    <geom name="wall_south" type="box" pos="0 -5.0 1.6" size="6.2 0.15 1.6" material="wall_mat" contype="1" conaffinity="1"/>
    <geom name="wall_north" type="box" pos="0 11.0 1.6" size="6.2 0.15 1.6" material="wall_mat" contype="1" conaffinity="1"/>
    <geom name="wall_west" type="box" pos="-6.0 3.0 1.6" size="0.15 8.2 1.6" material="wall_mat" contype="1" conaffinity="1"/>
    <geom name="wall_east" type="box" pos="6.0 3.0 1.6" size="0.15 8.2 1.6" material="wall_mat" contype="1" conaffinity="1"/>

    <geom name="baseboard_s" type="box" pos="0 -4.84 0.07" size="6.0 0.015 0.07" rgba="0.22 0.24 0.27 1" contype="0" conaffinity="0"/>
    <geom name="baseboard_n" type="box" pos="0 10.84 0.07" size="6.0 0.015 0.07" rgba="0.22 0.24 0.27 1" contype="0" conaffinity="0"/>
    <geom name="baseboard_w" type="box" pos="-5.84 3.0 0.07" size="0.015 8.0 0.07" rgba="0.22 0.24 0.27 1" contype="0" conaffinity="0"/>
    <geom name="baseboard_e" type="box" pos="5.84 3.0 0.07" size="0.015 8.0 0.07" rgba="0.22 0.24 0.27 1" contype="0" conaffinity="0"/>

    <!-- Window frames & sky-lit glass panels along West and East walls -->
    <geom name="win_w1_frame" type="box" pos="-5.84 0.8 1.75" size="0.03 1.35 0.80" rgba="0.20 0.22 0.25 1" contype="0" conaffinity="0"/>
    <geom name="win_w1_glass" type="box" pos="-5.82 0.8 1.75" size="0.02 1.25 0.70" rgba="0.62 0.78 0.92 1" contype="0" conaffinity="0"/>
    <geom name="win_w1_mull" type="box" pos="-5.81 0.8 1.75" size="0.025 0.03 0.72" rgba="0.20 0.22 0.25 1" contype="0" conaffinity="0"/>
    <geom name="win_w2_frame" type="box" pos="-5.84 7.4 1.75" size="0.03 1.35 0.80" rgba="0.20 0.22 0.25 1" contype="0" conaffinity="0"/>
    <geom name="win_w2_glass" type="box" pos="-5.82 7.4 1.75" size="0.02 1.25 0.70" rgba="0.62 0.78 0.92 1" contype="0" conaffinity="0"/>
    <geom name="win_e1_frame" type="box" pos="5.84 0.8 1.75" size="0.03 1.35 0.80" rgba="0.20 0.22 0.25 1" contype="0" conaffinity="0"/>
    <geom name="win_e1_glass" type="box" pos="5.82 0.8 1.75" size="0.02 1.25 0.70" rgba="0.62 0.78 0.92 1" contype="0" conaffinity="0"/>

    <!-- Partition wall at Y=4.0 with central 2.2m doorway & architectural door frame + EXIT sign -->
    <geom name="divider_west" type="box" pos="-3.55 4.0 1.6" size="2.45 0.15 1.6" material="wall_mat" contype="1" conaffinity="1"/>
    <geom name="divider_east" type="box" pos="3.55 4.0 1.6" size="2.45 0.15 1.6" material="wall_mat" contype="1" conaffinity="1"/>
    <geom name="divider_lintel" type="box" pos="0 4.0 2.75" size="1.15 0.15 0.45" material="wall_mat" contype="1" conaffinity="1"/>
    <geom name="door_jamb_w" type="box" pos="-1.10 4.0 1.15" size="0.04 0.17 1.15" material="metal_mat" contype="0" conaffinity="0"/>
    <geom name="door_jamb_e" type="box" pos="1.10 4.0 1.15" size="0.04 0.17 1.15" material="metal_mat" contype="0" conaffinity="0"/>
    <geom name="door_head" type="box" pos="0 4.0 2.30" size="1.14 0.17 0.04" material="metal_mat" contype="0" conaffinity="0"/>
    <geom name="exit_sign" type="box" pos="0 3.82 2.54" size="0.22 0.02 0.09" rgba="0.10 0.68 0.28 1" contype="0" conaffinity="0"/>

    <!-- Wall whiteboard & fire extinguisher -->
    <geom name="whiteboard_frame" type="box" pos="2.9 3.84 1.65" size="1.15 0.02 0.65" material="metal_mat" contype="0" conaffinity="0"/>
    <geom name="whiteboard_face" type="box" pos="2.9 3.82 1.65" size="1.08 0.015 0.58" rgba="0.95 0.96 0.98 1" contype="0" conaffinity="0"/>
    <geom name="extinguisher_body" type="cylinder" pos="-1.42 3.76 0.95" size="0.09 0.26" rgba="0.82 0.12 0.10 1" contype="0" conaffinity="0"/>
    <geom name="extinguisher_nozzle" type="box" pos="-1.42 3.76 1.24" size="0.04 0.05 0.04" rgba="0.12 0.12 0.14 1" contype="0" conaffinity="0"/>

    <!-- Detailed Conference Table (west side) with chairs & laptop -->
    <geom name="table_top" type="box" pos="-3.8 1.2 0.73" size="0.78 0.54 0.03" material="wood_mat" contype="1" conaffinity="1"/>
    <geom name="table_leg1" type="box" pos="-4.48 0.74 0.36" size="0.04 0.04 0.36" material="metal_mat" contype="1" conaffinity="1"/>
    <geom name="table_leg2" type="box" pos="-3.12 0.74 0.36" size="0.04 0.04 0.36" material="metal_mat" contype="1" conaffinity="1"/>
    <geom name="table_leg3" type="box" pos="-4.48 1.66 0.36" size="0.04 0.04 0.36" material="metal_mat" contype="1" conaffinity="1"/>
    <geom name="table_leg4" type="box" pos="-3.12 1.66 0.36" size="0.04 0.04 0.36" material="metal_mat" contype="1" conaffinity="1"/>
    <geom name="laptop_base" type="box" pos="-3.85 1.15 0.77" size="0.18 0.13 0.01" rgba="0.24 0.26 0.30 1" contype="0" conaffinity="0"/>
    <geom name="laptop_screen" type="box" pos="-3.98 1.15 0.89" size="0.01 0.13 0.11" euler="0 -18 0" rgba="0.18 0.42 0.68 1" contype="0" conaffinity="0"/>
    <!-- Chair for person_blue at (-2.5, 2.2) facing -25 deg -->
    <geom name="chair_seat" type="box" pos="-2.50 2.20 0.44" size="0.24 0.24 0.03" rgba="0.22 0.25 0.30 1" contype="1" conaffinity="1"/>
    <geom name="chair_back" type="box" pos="-2.72 2.30 0.76" size="0.03 0.23 0.26" euler="0 0 -25" rgba="0.22 0.25 0.30 1" contype="1" conaffinity="1"/>
    <geom name="chair_pedestal" type="cylinder" pos="-2.50 2.20 0.22" size="0.035 0.22" material="metal_mat" contype="0" conaffinity="0"/>
    <geom name="chair_base" type="cylinder" pos="-2.50 2.20 0.03" size="0.26 0.025" material="metal_mat" contype="0" conaffinity="0"/>

    <!-- Supply Crates & Industrial Shelving Rack (east side) -->
    <geom name="crate_east" type="box" pos="3.6 1.5 0.45" size="0.55 0.55 0.45" material="crate_mat" contype="1" conaffinity="1"/>
    <geom name="crate_small" type="box" pos="4.45 2.15 0.28" size="0.32 0.32 0.28" material="crate_mat" contype="1" conaffinity="1"/>
    <geom name="shelf_post1" type="box" pos="4.8 -1.8 1.0" size="0.03 0.03 1.0" material="metal_mat" contype="1" conaffinity="1"/>
    <geom name="shelf_post2" type="box" pos="5.5 -1.8 1.0" size="0.03 0.03 1.0" material="metal_mat" contype="1" conaffinity="1"/>
    <geom name="shelf_post3" type="box" pos="4.8 -0.4 1.0" size="0.03 0.03 1.0" material="metal_mat" contype="1" conaffinity="1"/>
    <geom name="shelf_post4" type="box" pos="5.5 -0.4 1.0" size="0.03 0.03 1.0" material="metal_mat" contype="1" conaffinity="1"/>
    <geom name="shelf_lvl1" type="box" pos="5.15 -1.1 0.55" size="0.38 0.74 0.02" material="wood_mat" contype="1" conaffinity="1"/>
    <geom name="shelf_lvl2" type="box" pos="5.15 -1.1 1.25" size="0.38 0.74 0.02" material="wood_mat" contype="1" conaffinity="1"/>
    <geom name="shelf_bin1" type="box" pos="5.15 -1.4 0.72" size="0.24 0.22 0.14" rgba="0.16 0.42 0.76 1" contype="0" conaffinity="0"/>
    <geom name="shelf_bin2" type="box" pos="5.15 -0.8 1.42" size="0.24 0.22 0.14" rgba="0.88 0.42 0.12 1" contype="0" conaffinity="0"/>

    <!-- Potted plant & rescue mat -->
    <geom name="plant_pot" type="cylinder" pos="-5.1 -3.8 0.24" size="0.24 0.24" rgba="0.58 0.34 0.22 1" contype="1" conaffinity="1"/>
    <geom name="plant_soil" type="cylinder" pos="-5.1 -3.8 0.47" size="0.21 0.02" rgba="0.20 0.14 0.10 1" contype="0" conaffinity="0"/>
    <geom name="plant_leaf1" type="ellipsoid" pos="-5.1 -3.8 0.78" size="0.28 0.28 0.24" rgba="0.18 0.46 0.20 1" contype="0" conaffinity="0"/>
    <geom name="plant_leaf2" type="ellipsoid" pos="-4.95 -3.72 0.96" size="0.18 0.18 0.16" rgba="0.24 0.54 0.24 1" contype="0" conaffinity="0"/>
    <geom name="rescue_mat" type="box" pos="-0.6 8.4 0.015" size="0.48 1.05 0.015" rgba="0.18 0.34 0.52 1" contype="1" conaffinity="1"/>

    <!-- Corridor Structural Pillar with hazard collar -->
    <geom name="pillar_corridor" type="box" pos="2.8 8.0 1.6" size="0.35 0.35 1.6" material="metal_mat" contype="1" conaffinity="1"/>
    <geom name="pillar_collar" type="box" pos="2.8 8.0 0.18" size="0.37 0.37 0.18" rgba="0.92 0.68 0.10 1" contype="0" conaffinity="0"/>

    <!-- People for SAR detection & ReID -->
    {_person_mjcf("person_red", 2.4, 2.6, yaw_deg=180, shirt_rgba="0.88 0.12 0.12 1", posture="standing", hi_vis=True)}
    {_person_mjcf("person_blue", -2.5, 2.2, yaw_deg=-25, shirt_rgba="0.12 0.35 0.85 1", posture="sitting")}
    {_person_mjcf("person_lying", -0.6, 8.6, yaw_deg=90, shirt_rgba="0.92 0.46 0.10 1", posture="lying", z_base=0.03)}
    {_person_mjcf("person_walker", 0.9, 7.2, yaw_deg=0, shirt_rgba="0.15 0.70 0.35 1", posture="standing", freejoint=True)}
    """
    map_data = MapData(
        places=(
            Place("Home Pad", "helipad", 0.0, 0.0),
            Place("Doorway", "door", 4.0, 0.0),
            Place("North Corridor", "corridor", 7.8, 0.0),
            Place("Main Room", "room", 0.5, 0.0),
        ),
        ways=(
            Way("corridor", ((-3.0, 0.0), (4.0, 0.0), (9.5, 0.0)), "Main Path"),
        ),
        areas=(
            Area("room", ((-5.0, -6.0), (4.0, -6.0), (4.0, 6.0), (-5.0, 6.0)), "Main Room"),
            Area("corridor", ((4.0, -6.0), (11.0, -6.0), (11.0, 6.0), (4.0, 6.0)), "North Corridor"),
        ),
        walkable=(
            Area("floor", ((-4.8, -5.8), (10.8, -5.8), (10.8, 5.8), (-4.8, 5.8)), "Indoor Floor"),
        ),
        obstacles=(
            Area("wall", ((3.85, -6.0), (4.15, -6.0), (4.15, -1.1), (3.85, -1.1)), "West Partition"),
            Area("wall", ((3.85, 1.1), (4.15, 1.1), (4.15, 6.0), (3.85, 6.0)), "East Partition"),
            Area("furniture", ((0.7, -4.55), (1.7, -4.55), (1.7, -3.05), (0.7, -3.05)), "Table"),
            Area("pillar", ((7.65, 2.45), (8.35, 2.45), (8.35, 3.15), (7.65, 3.15)), "Pillar"),
        ),
        attribution="HORUS Built-in 3D Scene: indoor_sar",
    )
    return assets_xml, visual_xml, body_xml, map_data


def _forest_sar_scene() -> tuple[str, str, str, MapData]:
    assets_xml = """
    <texture name="skybox" type="skybox" file="sky_daylight.png" gridsize="6 1" gridlayout="UDLRFB"/>
    <texture name="grass_tex" type="2d" file="forest_grass.png"/>
    <material name="grass_mat" texture="grass_tex" texuniform="true" texrepeat="0.35 0.35" specular="0.04" shininess="0.08"/>
    <texture name="trail_tex" type="2d" file="dirt_trail.png"/>
    <material name="trail_mat" texture="trail_tex" texuniform="true" texrepeat="0.50 0.50" specular="0.04"/>
    <texture name="bark_tex" type="2d" file="tree_bark.png"/>
    <material name="bark_mat" texture="bark_tex" texuniform="true" texrepeat="1.2 1.2" specular="0.03"/>
    <texture name="foliage_tex" type="2d" file="foliage_pine.png"/>
    <material name="foliage_mat" texture="foliage_tex" texuniform="true" texrepeat="0.7 0.7" specular="0.06"/>
    <texture name="rock_tex" type="2d" file="rock_granite.png"/>
    <material name="rock_mat" texture="rock_tex" texuniform="true" texrepeat="0.8 0.8" specular="0.12"/>
    <texture name="wood_tex" type="2d" file="wood_oak.png"/>
    <material name="wood_mat" texture="wood_tex" texuniform="true" texrepeat="1.2 1.2" specular="0.15"/>
    """
    visual_xml = """
    <global offwidth="640" offheight="480"/>
    <quality shadowsize="4096" offsamples="4"/>
    <headlight ambient="0.20 0.22 0.25" diffuse="0.30 0.30 0.30" specular="0.06 0.06 0.06"/>
    <rgba haze="0.72 0.82 0.90 1"/>
    <map fogstart="0.45" fogend="1.8" znear="0.0004" zfar="5.0"/>
    """
    conifers = [
        (5.5, 6.0, 5.4), (-6.0, 5.0, 6.0), (8.5, -3.5, 5.2), (-7.5, -5.0, 5.8),
        (2.8, 14.5, 6.2), (-4.2, 15.8, 5.6), (11.2, 9.2, 6.4), (-11.0, 11.2, 5.5),
        (14.0, 1.5, 5.8), (-13.5, -2.0, 6.2), (0.8, -11.0, 5.1), (-6.5, -13.0, 5.9),
        (6.8, 18.5, 6.0), (-8.5, 19.5, 6.3), (15.5, 13.5, 5.7), (-15.0, 6.5, 5.9),
    ]
    oaks = [
        (-4.5, -1.8, 4.6), (6.5, 12.2, 5.0), (-9.2, 15.0, 4.8), (10.5, -9.5, 5.2),
    ]
    tree_geoms: list[str] = []
    obstacles: list[Area] = []
    for idx, (tx, ty, h) in enumerate(conifers):
        tree_geoms.append(_conifer_mjcf(idx, tx, ty, h, prefix="pine"))
        obstacles.append(
            Area("tree", ((ty - 0.6, tx - 0.6), (ty + 0.6, tx - 0.6), (ty + 0.6, tx + 0.6), (ty - 0.6, tx + 0.6)), f"Pine {idx + 1}")
        )
    for idx, (tx, ty, h) in enumerate(oaks):
        tree_geoms.append(_deciduous_mjcf(idx, tx, ty, h, prefix="oak"))
        obstacles.append(
            Area("tree", ((ty - 0.6, tx - 0.6), (ty + 0.6, tx - 0.6), (ty + 0.6, tx + 0.6), (ty - 0.6, tx + 0.6)), f"Oak {idx + 1}")
        )

    boulders = [
        (-2.8, 9.2, 0.38, 0.72, 0.62, 0.50),
        (-3.5, 9.8, 0.25, 0.45, 0.40, 0.34),
        (4.6, 2.8, 0.30, 0.58, 0.52, 0.42),
        (-5.2, 11.4, 0.34, 0.64, 0.55, 0.44),
        (3.2, -5.8, 0.28, 0.50, 0.48, 0.38),
    ]
    boulder_xml = "\n    ".join(
        f'<geom name="boulder_{i}" type="ellipsoid" pos="{bx:.2f} {by:.2f} {bz:.2f}" '
        f'size="{sx:.2f} {sy:.2f} {sz:.2f}" material="rock_mat" contype="1" conaffinity="1"/>'
        for i, (bx, by, bz, sx, sy, sz) in enumerate(boulders)
    )

    shrubs = [
        (2.4, 4.2, 0.45), (-2.6, 3.8, 0.42), (3.6, 7.6, 0.48), (-3.8, 6.2, 0.40),
        (1.9, 13.0, 0.50), (-2.2, 16.5, 0.46), (5.2, -1.5, 0.48), (-4.8, -4.5, 0.50),
    ]
    shrub_xml = "\n    ".join(
        f'<geom name="shrub_{i}" type="ellipsoid" pos="{sx:.2f} {sy:.2f} {sr * 0.6:.2f}" '
        f'size="{sr:.2f} {sr * 1.15:.2f} {sr * 0.75:.2f}" material="foliage_mat" rgba="0.22 0.46 0.20 1" contype="0" conaffinity="0"/>'
        for i, (sx, sy, sr) in enumerate(shrubs)
    )

    body_xml = f"""
    <!-- Golden-hour directional sun + cool sky hemisphere fill -->
    <light name="sun" pos="-18 14 26" dir="0.48 -0.35 -0.80" directional="true" diffuse="0.98 0.92 0.80" specular="0.25 0.25 0.20" castshadow="true"/>
    <light name="sky_fill" pos="15 -12 20" dir="-0.4 0.3 -0.85" directional="true" diffuse="0.24 0.32 0.44" castshadow="false"/>

    <!-- Textured forest floor, clearing perimeter hills, and segmented dirt trail -->
    <geom name="terrain" type="plane" size="65 65 0.1" material="grass_mat" contype="1" conaffinity="1"/>
    <geom name="hill_nw" type="ellipsoid" pos="-24 22 -0.6" size="14 12 3.2" material="grass_mat" contype="0" conaffinity="0"/>
    <geom name="hill_ne" type="ellipsoid" pos="26 24 -0.5" size="15 13 3.6" material="grass_mat" contype="0" conaffinity="0"/>
    <geom name="hill_sw" type="ellipsoid" pos="-25 -22 -0.6" size="14 12 2.8" material="grass_mat" contype="0" conaffinity="0"/>
    <geom name="trail_s" type="box" pos="0 -3.5 0.002" size="1.35 4.8 0.002" material="trail_mat" contype="0" conaffinity="0"/>
    <geom name="trail_mid" type="box" pos="0.2 6.8 0.002" size="1.35 6.0 0.002" euler="0 0 -4" material="trail_mat" contype="0" conaffinity="0"/>
    <geom name="trail_n" type="box" pos="0.5 17.5 0.002" size="1.35 5.8 0.002" euler="0 0 6" material="trail_mat" contype="0" conaffinity="0"/>
    {_apriltag_pad_mjcf(0.005)}

    <!-- Campsite: A-frame tent, stone fire ring, fallen sitting log, trailhead sign -->
    {_tent_mjcf("sar_tent", -4.4, 12.2, yaw_deg=35, rgba="0.92 0.40 0.10 1")}
    <geom name="camp_log" type="cylinder" pos="-3.8 12.8 0.18" size="0.18 0.95" euler="90 0 45" material="bark_mat" contype="1" conaffinity="1"/>
    <geom name="fire_ring_1" type="sphere" pos="-2.4 11.4 0.10" size="0.14" material="rock_mat" contype="0" conaffinity="0"/>
    <geom name="fire_ring_2" type="sphere" pos="-2.1 11.7 0.10" size="0.14" material="rock_mat" contype="0" conaffinity="0"/>
    <geom name="fire_ring_3" type="sphere" pos="-2.7 11.7 0.10" size="0.14" material="rock_mat" contype="0" conaffinity="0"/>
    <geom name="fire_ring_4" type="sphere" pos="-2.4 12.0 0.10" size="0.14" material="rock_mat" contype="0" conaffinity="0"/>
    <geom name="fire_ash" type="cylinder" pos="-2.4 11.7 0.02" size="0.26 0.02" rgba="0.18 0.16 0.15 1" contype="0" conaffinity="0"/>
    <geom name="sign_post" type="cylinder" pos="1.85 3.6 0.65" size="0.06 0.65" material="wood_mat" contype="1" conaffinity="1"/>
    <geom name="sign_board" type="box" pos="1.85 3.6 1.18" size="0.44 0.04 0.14" euler="0 0 -15" material="wood_mat" contype="0" conaffinity="0"/>

    {boulder_xml}
    {shrub_xml}
    {"    ".join(tree_geoms)}

    <!-- Hikers for SAR search & tracking -->
    {_person_mjcf("hiker_red", 3.6, 10.5, yaw_deg=150, shirt_rgba="0.88 0.14 0.14 1", posture="standing", backpack=True)}
    {_person_mjcf("hiker_yellow", -3.8, 12.8, yaw_deg=45, shirt_rgba="0.95 0.78 0.12 1", posture="sitting")}
    {_person_mjcf("hiker_blue", 1.2, 18.2, yaw_deg=-110, shirt_rgba="0.14 0.42 0.88 1", posture="standing", backpack=True)}
    """
    map_data = MapData(
        places=(
            Place("Home Clearing", "helipad", 0.0, 0.0),
            Place("Campsite", "camp", 12.2, -4.4),
            Place("North Trailhead", "trail", 14.0, 0.0),
            Place("North Ridge", "landmark", 20.0, 0.0),
        ),
        ways=(Way("path", ((-8.0, 0.0), (0.0, 0.0), (14.0, 0.2), (22.0, 0.5)), "Forest Trail"),),
        areas=(Area("park", ((-25.0, -25.0), (25.0, -25.0), (25.0, 25.0), (-25.0, 25.0)), "Pine Woods"),),
        walkable=(Area("path", ((-10.0, -1.4), (23.0, -1.4), (23.0, 1.4), (-10.0, 1.4)), "Trail"),),
        obstacles=tuple(obstacles),
        attribution="HORUS Built-in 3D Scene: forest_sar",
    )
    return assets_xml, visual_xml, body_xml, map_data


def _urban_plaza_scene() -> tuple[str, str, str, MapData]:
    assets_xml = """
    <texture name="skybox" type="skybox" file="sky_urban.png" gridsize="6 1" gridlayout="UDLRFB"/>
    <texture name="paver_tex" type="2d" file="paver_plaza.png"/>
    <material name="paver_mat" texture="paver_tex" texuniform="true" texrepeat="0.45 0.45" specular="0.18" shininess="0.25"/>
    <texture name="asphalt_tex" type="2d" file="asphalt_road.png"/>
    <material name="asphalt_mat" texture="asphalt_tex" texuniform="true" texrepeat="0.40 0.40" specular="0.12" shininess="0.18"/>
    <texture name="glass_tex" type="2d" file="facade_glass.png"/>
    <material name="glass_mat" texture="glass_tex" texuniform="true" texrepeat="0.32 0.32" reflectance="0.25" specular="0.70" shininess="0.80"/>
    <texture name="brick_tex" type="2d" file="facade_brick.png"/>
    <material name="brick_mat" texture="brick_tex" texuniform="true" texrepeat="0.32 0.32" specular="0.08"/>
    <texture name="concrete_tex" type="2d" file="facade_concrete.png"/>
    <material name="concrete_mat" texture="concrete_tex" texuniform="true" texrepeat="0.35 0.35" specular="0.12"/>
    <texture name="water_tex" type="2d" file="water_pool.png"/>
    <material name="water_mat" texture="water_tex" texuniform="true" texrepeat="0.6 0.6" reflectance="0.35" specular="0.80" shininess="0.90"/>
    <texture name="foliage_tex" type="2d" file="foliage_pine.png"/>
    <material name="foliage_mat" texture="foliage_tex" texuniform="true" texrepeat="0.8 0.8" specular="0.06"/>
    <texture name="bark_tex" type="2d" file="tree_bark.png"/>
    <material name="bark_mat" texture="bark_tex" texuniform="true" texrepeat="1.2 1.2" specular="0.04"/>
    <texture name="wood_tex" type="2d" file="wood_oak.png"/>
    <material name="wood_mat" texture="wood_tex" texuniform="true" texrepeat="1.2 1.2" specular="0.2"/>
    """
    visual_xml = """
    <global offwidth="640" offheight="480"/>
    <quality shadowsize="4096" offsamples="4"/>
    <headlight ambient="0.22 0.23 0.25" diffuse="0.32 0.32 0.32" specular="0.08 0.08 0.08"/>
    <rgba haze="0.78 0.82 0.86 1"/>
    <map fogstart="0.45" fogend="1.8" znear="0.0004" zfar="5.0"/>
    """
    buildings = [
        ("bldg_nw", -12.0, 15.0, 4.5, 4.5, 9.5, "glass_mat", "0.14 0.42 0.62 1"),
        ("bldg_ne", 12.0, 15.0, 4.5, 4.5, 11.5, "brick_mat", "0.78 0.24 0.16 1"),
        ("bldg_sw", -13.0, -10.0, 4.0, 4.0, 7.5, "concrete_mat", "0.18 0.52 0.36 1"),
        ("bldg_se", 13.0, -10.0, 4.0, 4.0, 6.8, "glass_mat", "0.82 0.46 0.14 1"),
    ]
    bldg_parts: list[str] = []
    for n, x, y, sx, sy, h, mat, awning in buildings:
        bldg_parts.append(
            f'<geom name="{n}" type="box" pos="{x} {y} {h / 2}" size="{sx} {sy} {h / 2}" material="{mat}" contype="1" conaffinity="1"/>\n    '
            # Ground-floor dark plinth + parapet + rooftop HVAC unit
            f'<geom name="{n}_plinth" type="box" pos="{x} {y} 1.3" size="{sx + 0.08} {sy + 0.08} 1.3" rgba="0.22 0.24 0.27 1" contype="0" conaffinity="0"/>\n    '
            f'<geom name="{n}_awning" type="box" pos="{x + (-sx - 0.45 if x > 0 else sx + 0.45)} {y} 2.65" size="0.55 {sy * 0.72} 0.06" rgba="{awning}" contype="0" conaffinity="0"/>\n    '
            f'<geom name="{n}_parapet" type="box" pos="{x} {y} {h + 0.18}" size="{sx + 0.12} {sy + 0.12} 0.18" rgba="0.34 0.36 0.40 1" contype="0" conaffinity="0"/>\n    '
            f'<geom name="{n}_hvac" type="box" pos="{x - sx * 0.35} {y + sy * 0.25} {h + 0.65}" size="0.85 0.65 0.45" rgba="0.52 0.55 0.58 1" contype="0" conaffinity="0"/>'
        )

    # Distant background skyline silhouettes (non-blocking)
    skyline = [
        (-28.0, 28.0, 5.5, 5.0, 18.0, "glass_mat"),
        (-10.0, 32.0, 6.0, 5.0, 22.0, "concrete_mat"),
        (12.0, 33.0, 5.5, 5.5, 16.0, "brick_mat"),
        (30.0, 24.0, 5.0, 6.0, 20.0, "glass_mat"),
        (-30.0, -24.0, 5.0, 5.0, 15.0, "brick_mat"),
        (28.0, -24.0, 5.5, 5.5, 17.0, "concrete_mat"),
    ]
    skyline_xml = "\n    ".join(
        f'<geom name="skyline_{i}" type="box" pos="{x} {y} {h / 2}" size="{sx} {sy} {h / 2}" material="{mat}" contype="0" conaffinity="0"/>'
        for i, (x, y, sx, sy, h, mat) in enumerate(skyline)
    )

    # Crosswalk stripes and lane dashes
    stripes: list[str] = []
    for k in range(-2, 3):
        stripes.append(
            f'<geom name="xwalk_s_{k}" type="box" pos="{k * 0.95:.2f} 4.7 0.004" size="0.26 0.95 0.002" rgba="0.92 0.92 0.92 1" contype="0" conaffinity="0"/>'
        )
        stripes.append(
            f'<geom name="xwalk_n_{k}" type="box" pos="{k * 0.95:.2f} 11.3 0.004" size="0.26 0.95 0.002" rgba="0.92 0.92 0.92 1" contype="0" conaffinity="0"/>'
        )
    for dy in (-14, -8, -2, 14, 20, 26):
        stripes.append(
            f'<geom name="dash_ns_{dy}" type="box" pos="0 {dy} 0.004" size="0.08 1.4 0.002" rgba="0.94 0.76 0.14 1" contype="0" conaffinity="0"/>'
        )

    # Streetlights & plaza benches
    lamps = [(-3.4, 2.5), (3.4, 2.5), (-3.4, 14.5), (3.4, 14.5)]
    lamp_xml = "\n    ".join(
        f'<geom name="lamp_pole_{i}" type="cylinder" pos="{lx} {ly} 2.1" size="0.06 2.1" rgba="0.18 0.20 0.24 1" contype="1" conaffinity="1"/>\n    '
        f'<geom name="lamp_arm_{i}" type="box" pos="{lx + (-0.35 if lx > 0 else 0.35)} {ly} 4.15" size="0.38 0.04 0.04" rgba="0.18 0.20 0.24 1" contype="0" conaffinity="0"/>\n    '
        f'<geom name="lamp_globe_{i}" type="sphere" pos="{lx + (-0.65 if lx > 0 else 0.65)} {ly} 4.05" size="0.14" rgba="1.0 0.95 0.78 1" contype="0" conaffinity="0"/>'
        for i, (lx, ly) in enumerate(lamps)
    )

    benches = [(-3.6, 12.0, 0), (3.6, 12.0, 180)]
    bench_xml = "\n    ".join(
        f'<geom name="bench_seat_{i}" type="box" pos="{bx} {by} 0.42" size="0.28 0.85 0.04" material="wood_mat" contype="1" conaffinity="1"/>\n    '
        f'<geom name="bench_leg1_{i}" type="box" pos="{bx} {by - 0.65} 0.21" size="0.26 0.05 0.21" rgba="0.16 0.18 0.20 1" contype="0" conaffinity="0"/>\n    '
        f'<geom name="bench_leg2_{i}" type="box" pos="{bx} {by + 0.65} 0.21" size="0.26 0.05 0.21" rgba="0.16 0.18 0.20 1" contype="0" conaffinity="0"/>'
        for i, (bx, by, _) in enumerate(benches)
    )

    planters = [(-5.8, 12.2), (5.8, 12.2), (-5.8, 3.6), (5.8, 3.6)]
    planter_xml = "\n    ".join(
        f'<geom name="planter_box_{i}" type="box" pos="{px} {py} 0.25" size="0.55 0.55 0.25" rgba="0.62 0.62 0.60 1" contype="1" conaffinity="1"/>\n    '
        f'{_deciduous_mjcf(i + 20, px, py, 3.4, prefix="plaza_tree")}'
        for i, (px, py) in enumerate(planters)
    )

    obstacles = tuple(
        Area("building", ((y - sy, x - sx), (y + sy, x - sx), (y + sy, x + sx), (y - sy, x + sx)), n)
        for n, x, y, sx, sy, _, _, _ in buildings
    )
    body_xml = f"""
    <light name="sun" pos="-20 12 28" dir="0.52 -0.32 -0.78" directional="true" diffuse="0.98 0.93 0.84" specular="0.32 0.30 0.26" castshadow="true"/>
    <light name="sky_fill" pos="18 -14 22" dir="-0.4 0.3 -0.85" directional="true" diffuse="0.26 0.34 0.46" castshadow="false"/>

    <!-- Granite paver plaza, weathered asphalt streets, and crosswalks -->
    <geom name="plaza_ground" type="plane" size="75 75 0.1" material="paver_mat" contype="1" conaffinity="1"/>
    <geom name="road_ns" type="box" pos="0 4.0 0.002" size="3.0 34.0 0.002" material="asphalt_mat" contype="0" conaffinity="0"/>
    <geom name="road_ew" type="box" pos="0 8.0 0.003" size="34.0 2.6 0.002" material="asphalt_mat" contype="0" conaffinity="0"/>
    {"    ".join(stripes)}
    {_apriltag_pad_mjcf(0.005)}

    <!-- Tiered Central Fountain at (0, 12.0) -->
    <geom name="fountain_rim" type="cylinder" pos="0 12.0 0.26" size="1.55 0.26" rgba="0.72 0.73 0.75 1" contype="1" conaffinity="1"/>
    <geom name="fountain_water" type="cylinder" pos="0 12.0 0.44" size="1.34 0.04" material="water_mat" contype="0" conaffinity="0"/>
    <geom name="fountain_col" type="cylinder" pos="0 12.0 0.72" size="0.28 0.48" rgba="0.74 0.75 0.77 1" contype="1" conaffinity="1"/>
    <geom name="fountain_bowl" type="cylinder" pos="0 12.0 1.15" size="0.58 0.08" rgba="0.76 0.77 0.79 1" contype="0" conaffinity="0"/>
    <geom name="fountain_jet" type="cylinder" pos="0 12.0 1.48" size="0.06 0.26" rgba="0.78 0.92 0.98 0.75" contype="0" conaffinity="0"/>

    <!-- Detailed vehicles -->
    {_vehicle_mjcf("van_parked", 5.2, 5.0, yaw_deg=0.0, body_rgba="0.22 0.40 0.66 1", kind="van")}
    {_vehicle_mjcf("sedan_parked", -5.2, 10.6, yaw_deg=90.0, body_rgba="0.72 0.18 0.16 1", kind="sedan")}

    {"    ".join(bldg_parts)}
    {skyline_xml}
    {lamp_xml}
    {bench_xml}
    {planter_xml}

    <!-- Pedestrians -->
    {_person_mjcf("ped_red", -2.2, 10.8, yaw_deg=-60, shirt_rgba="0.88 0.14 0.14 1", posture="standing")}
    {_person_mjcf("ped_blue", 2.8, 12.5, yaw_deg=140, shirt_rgba="0.14 0.38 0.86 1", posture="standing")}
    {_person_mjcf("ped_bench", -3.6, 12.0, yaw_deg=0, shirt_rgba="0.92 0.64 0.14 1", posture="sitting")}
    """
    map_data = MapData(
        places=(
            Place("Home Pad", "helipad", 0.0, 0.0),
            Place("Central Plaza", "plaza", 12.0, 0.0),
            Place("Main Fountain", "fountain", 12.0, 0.0),
            Place("North West Block", "building", 15.0, -12.0),
            Place("North East Block", "building", 15.0, 12.0),
        ),
        ways=(
            Way("road", ((-20.0, 0.0), (0.0, 0.0), (25.0, 0.0)), "North Avenue"),
            Way("road", ((8.0, -25.0), (8.0, 0.0), (8.0, 25.0)), "Plaza Crossway"),
        ),
        areas=(Area("plaza", ((6.0, -7.0), (18.0, -7.0), (18.0, 7.0), (6.0, 7.0)), "Central Plaza"),),
        walkable=(Area("footpath", ((5.0, -8.0), (19.0, -8.0), (19.0, 8.0), (5.0, 8.0)), "Plaza Walk"),),
        obstacles=obstacles,
        attribution="HORUS Built-in 3D Scene: urban_plaza",
    )
    return assets_xml, visual_xml, body_xml, map_data


def _procedural_scene(seed: int) -> tuple[str, str, str, MapData]:
    rng = random.Random(seed)
    assets_xml = """
    <texture name="skybox" type="skybox" file="sky_daylight.png" gridsize="6 1" gridlayout="UDLRFB"/>
    <texture name="grass_tex" type="2d" file="forest_grass.png"/>
    <material name="grass_mat" texture="grass_tex" texuniform="true" texrepeat="0.35 0.35" specular="0.04"/>
    <texture name="trail_tex" type="2d" file="dirt_trail.png"/>
    <material name="trail_mat" texture="trail_tex" texuniform="true" texrepeat="0.50 0.50" specular="0.04"/>
    <texture name="bark_tex" type="2d" file="tree_bark.png"/>
    <material name="bark_mat" texture="bark_tex" texuniform="true" texrepeat="1.2 1.2" specular="0.03"/>
    <texture name="foliage_tex" type="2d" file="foliage_pine.png"/>
    <material name="foliage_mat" texture="foliage_tex" texuniform="true" texrepeat="0.7 0.7" specular="0.06"/>
    <texture name="rock_tex" type="2d" file="rock_granite.png"/>
    <material name="rock_mat" texture="rock_tex" texuniform="true" texrepeat="0.8 0.8" specular="0.12"/>
    <texture name="wood_tex" type="2d" file="wood_oak.png"/>
    <material name="wood_mat" texture="wood_tex" texuniform="true" texrepeat="1.2 1.2" specular="0.15"/>
    """
    visual_xml = """
    <global offwidth="640" offheight="480"/>
    <quality shadowsize="4096" offsamples="4"/>
    <headlight ambient="0.20 0.22 0.25" diffuse="0.30 0.30 0.30" specular="0.06 0.06 0.06"/>
    <rgba haze="0.74 0.83 0.92 1"/>
    <map fogstart="0.45" fogend="1.8" znear="0.0004" zfar="5.0"/>
    """
    geoms = [
        '<light name="sun" pos="-16 14 26" dir="0.46 -0.35 -0.81" directional="true" diffuse="0.98 0.92 0.80" specular="0.25 0.25 0.20" castshadow="true"/>',
        '<light name="sky_fill" pos="14 -12 20" dir="-0.4 0.3 -0.85" directional="true" diffuse="0.24 0.32 0.44" castshadow="false"/>',
        '<geom name="terrain" type="plane" size="65 65 0.1" material="grass_mat" contype="1" conaffinity="1"/>',
        '<geom name="survey_track" type="box" pos="0 6.5 0.002" size="1.25 9.5 0.002" material="trail_mat" contype="0" conaffinity="0"/>',
        _apriltag_pad_mjcf(0.004),
    ]
    obstacles: list[Area] = []
    places = [Place("Home Pad", "helipad", 0.0, 0.0)]

    # Seeded rustic ranger shelter / cabin
    cab_side = -1.0 if rng.random() < 0.5 else 1.0
    cx, cy = cab_side * rng.uniform(5.2, 6.8), rng.uniform(7.5, 10.5)
    geoms.append(
        f'<geom name="cabin_body" type="box" pos="{cx:.2f} {cy:.2f} 1.25" size="1.8 1.4 1.25" material="wood_mat" contype="1" conaffinity="1"/>\n    '
        f'<geom name="cabin_roof_l" type="box" pos="{cx:.2f} {cy + 0.72:.2f} 2.68" size="2.05 0.98 0.08" euler="-24 0 0" rgba="0.34 0.22 0.16 1" contype="0" conaffinity="0"/>\n    '
        f'<geom name="cabin_roof_r" type="box" pos="{cx:.2f} {cy - 0.72:.2f} 2.68" size="2.05 0.98 0.08" euler="24 0 0" rgba="0.34 0.22 0.16 1" contype="0" conaffinity="0"/>\n    '
        f'<geom name="cabin_ridge" type="box" pos="{cx:.2f} {cy:.2f} 3.06" size="2.06 0.08 0.08" rgba="0.26 0.17 0.12 1" contype="0" conaffinity="0"/>'
    )
    obstacles.append(
        Area("building", ((cy - 1.5, cx - 1.9), (cy + 1.5, cx - 1.9), (cy + 1.5, cx + 1.9), (cy - 1.5, cx + 1.9)), "Field Shelter")
    )

    # Seeded tent opposite the cabin
    tx_tent = -cab_side * rng.uniform(4.2, 5.8)
    ty_tent = rng.uniform(8.5, 12.0)
    geoms.append(_tent_mjcf("proc_tent", tx_tent, ty_tent, yaw_deg=rng.uniform(-45, 45), rgba="0.18 0.50 0.78 1"))

    # Seeded rock outcrops
    for r_i in range(4):
        rx = rng.uniform(-12.0, 12.0)
        ry = rng.uniform(-10.0, 16.0)
        if math.hypot(rx, ry) < 3.0:
            rx += 4.5
        rs = rng.uniform(0.45, 0.85)
        geoms.append(
            f'<geom name="proc_rock_{r_i}" type="ellipsoid" pos="{rx:.2f} {ry:.2f} {rs * 0.45:.2f}" '
            f'size="{rs:.2f} {rs * 0.85:.2f} {rs * 0.60:.2f}" material="rock_mat" contype="1" conaffinity="1"/>'
        )

    for i in range(16):
        angle = rng.uniform(0, math.tau)
        dist = rng.uniform(5.0, 20.0)
        tx, ty = math.cos(angle) * dist, math.sin(angle) * dist
        if abs(tx - cx) < 2.8 and abs(ty - cy) < 2.5:
            tx += cab_side * 4.0
        h = rng.uniform(4.2, 6.4)
        if i % 3 == 0:
            geoms.append(_deciduous_mjcf(i, tx, ty, h * 0.85, prefix="proc_oak"))
        else:
            geoms.append(_conifer_mjcf(i, tx, ty, h, prefix="proc_pine"))
        obstacles.append(Area("tree", ((ty - 0.6, tx - 0.6), (ty + 0.6, tx - 0.6), (ty + 0.6, tx + 0.6), (ty - 0.6, tx + 0.6)), f"Tree {i+1}"))

    px, py = rng.uniform(-2.8, 2.8), rng.uniform(5.2, 9.8)
    geoms.append(_person_mjcf("proc_person_red", px, py, yaw_deg=rng.uniform(0, 360), shirt_rgba="0.88 0.14 0.14 1", backpack=True))
    geoms.append(_person_mjcf("proc_person_yellow", -cab_side * rng.uniform(2.0, 3.8), rng.uniform(11.0, 14.0), yaw_deg=rng.uniform(0, 360), shirt_rgba="0.95 0.78 0.12 1"))
    places.append(Place("North Clearing", "landmark", 8.0, 0.0))
    places.append(Place("Field Shelter", "building", cy, cx))
    map_data = MapData(
        places=tuple(places),
        ways=(Way("path", ((0.0, 0.0), (8.0, 0.0), (15.0, 0.0)), "Survey Track"),),
        obstacles=tuple(obstacles),
        attribution=f"HORUS Built-in 3D Scene: procedural (seed={seed})",
    )
    return assets_xml, visual_xml, "\n    ".join(geoms), map_data


def build_scene_bundle(name: str, asset_dir: Path, *, seed: int = 42) -> SceneBundle:
    """Create MuJoCo XML, procedural PBR PNG textures, and MapData for ``name`` inside ``asset_dir``."""
    if name not in SCENE_SPECS:
        raise ValueError(f"unknown 3D scene {name!r}; expected one of {SCENE_NAMES}")
    spec = SCENE_SPECS[name]
    asset_dir = Path(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=True)
    write_png(asset_dir / "apriltag_id0.png", generate_apriltag_36h11_id0(256))
    generate_scene_textures(name, asset_dir)

    if name == "indoor_sar":
        assets_xml, visual_xml, scene_bodies, map_data = _indoor_sar_scene()
    elif name == "forest_sar":
        assets_xml, visual_xml, scene_bodies, map_data = _forest_sar_scene()
    elif name == "urban_plaza":
        assets_xml, visual_xml, scene_bodies, map_data = _urban_plaza_scene()
    else:
        assets_xml, visual_xml, scene_bodies, map_data = _procedural_scene(seed)

    xml = f"""<mujoco model="horus_{name}">
  <compiler angle="degree" coordinate="local" assetdir="{asset_dir.as_posix()}"/>
  <option timestep="0.00125" gravity="0 0 -9.80665" integrator="Euler"/>
  <visual>
    {visual_xml}
  </visual>
  <asset>
    <texture name="apriltag_tex" type="2d" file="apriltag_id0.png"/>
    <material name="apriltag_mat" texture="apriltag_tex" texrepeat="1 1" texuniform="false" reflectance="0" specular="0"/>
    {assets_xml}
  </asset>
  <worldbody>
    {scene_bodies}
    {_quad_body_mjcf(spec.home_pose)}
  </worldbody>
  {_sensors_mjcf()}
</mujoco>
"""
    return SceneBundle(
        name=name,
        xml=xml,
        map_data=map_data,
        asset_dir=asset_dir,
        seed=seed,
        indoor=spec.indoor,
    )
