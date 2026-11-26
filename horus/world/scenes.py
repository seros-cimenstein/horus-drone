"""Built-in 3D scenes (MuJoCo MJCF + AprilTag pad + matching HORUS vector maps)."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import random
from typing import Any

import numpy as np

from horus.maps.model import Area, MapData, Place, Way

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
        subtitle="Room, doorway, corridor, furniture, AprilTag pad, and people in distinct shirts",
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
        subtitle="Woodland clearing, collision tree trunks & canopies, trails, and hikers",
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
        subtitle="City blocks, plaza, street furniture, vehicles, pedestrians, and full street map",
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
        subtitle="Seeded outdoor landscape with landmarks, trees, paths, and hikers",
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
    # Pure NumPy nearest-neighbor fallback
    scale = max(1, size_px // 10)
    small = np.repeat(np.repeat(_TAG36H11_ID0_FALLBACK, scale, axis=0), scale, axis=1)
    canvas = np.full((size_px, size_px), 245, dtype=np.uint8)
    h, w = min(size_px, small.shape[0]), min(size_px, small.shape[1])
    y0, x0 = (size_px - h) // 2, (size_px - w) // 2
    canvas[y0 : y0 + h, x0 : x0 + w] = small[:h, :w]
    return np.repeat(canvas[:, :, None], 3, axis=2)


def _write_png(path: Path, rgb: np.ndarray) -> None:
    """Write an RGB uint8 array as a PNG file (OpenCV or pure-stdlib zlib fallback)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import cv2

        cv2.imwrite(str(path), rgb[:, :, ::-1])
        return
    except Exception:
        pass
    import struct
    import zlib

    height, width, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def _generate_checker_rgb(size_px: int = 256, c1: tuple[int, int, int] = (200, 204, 210), c2: tuple[int, int, int] = (170, 176, 184), tiles: int = 8) -> np.ndarray:
    img = np.zeros((size_px, size_px, 3), dtype=np.uint8)
    step = max(1, size_px // tiles)
    for y in range(size_px):
        for x in range(size_px):
            img[y, x] = c1 if ((x // step) + (y // step)) % 2 == 0 else c2
    return img


def _apriltag_pad_mjcf(z: float = 0.002) -> str:
    """Build a crisp 8x8 Tag36h11 ID 0 landing pad at (0, 0, z) with white base and dark cells."""
    # Exact 8x8 bit matrix for DICT_APRILTAG_36h11 id=0 (1-bit black border + 6x6 payload).
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
    parts = [
        f'<geom name="apriltag_pad" type="box" pos="0 0 {z:.4f}" size="0.45 0.45 0.002" '
        f'rgba="0.96 0.96 0.96 1" contype="1" conaffinity="1"/>'
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
    pants_rgba: str = "0.15 0.20 0.32 1",
    posture: str = "standing",
    freejoint: bool = False,
) -> str:
    """Build a lightweight articulated-look humanoid prop detectable by vision & ToF."""
    skin = "0.86 0.70 0.56 1"
    hair = "0.14 0.10 0.08 1"
    rad = math.radians(yaw_deg)
    qw, qz = math.cos(rad * 0.5), math.sin(rad * 0.5)
    if posture == "lying":
        return f"""
    <body name="{name}" pos="{x:.3f} {y:.3f} 0.18" quat="{qw:.5f} 0 0 {qz:.5f}">
      <geom name="{name}_torso" type="box" size="0.28 0.18 0.11" pos="0 0 0" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_legs" type="box" size="0.34 0.14 0.09" pos="-0.58 0 -0.02" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_head" type="sphere" size="0.12" pos="0.40 0 0.02" rgba="{skin}" contype="1" conaffinity="1"/>
    </body>"""
    if posture == "sitting":
        return f"""
    <body name="{name}" pos="{x:.3f} {y:.3f} 0" quat="{qw:.5f} 0 0 {qz:.5f}">
      <geom name="{name}_legs" type="box" size="0.20 0.16 0.22" pos="0.12 0 0.22" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_torso" type="box" size="0.13 0.18 0.28" pos="0 0 0.72" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_head" type="sphere" size="0.12" pos="0 0 1.14" rgba="{skin}" contype="1" conaffinity="1"/>
    </body>"""
    joint_xml = f'<joint name="{name}_slide_x" type="slide" axis="1 0 0" range="-4 4" damping="50"/>\n      ' if freejoint else ""
    return f"""
    <body name="{name}" pos="{x:.3f} {y:.3f} 0" quat="{qw:.5f} 0 0 {qz:.5f}">
      {joint_xml}<geom name="{name}_legs" type="capsule" fromto="0 -0.08 0.02 0 -0.08 0.82" size="0.08" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_legs2" type="capsule" fromto="0 0.08 0.02 0 0.08 0.82" size="0.08" rgba="{pants_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_torso" type="box" size="0.13 0.22 0.30" pos="0 0 1.14" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_arm_l" type="capsule" fromto="0 0.26 0.90 0 0.26 1.38" size="0.055" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_arm_r" type="capsule" fromto="0 -0.26 0.90 0 -0.26 1.38" size="0.055" rgba="{shirt_rgba}" contype="1" conaffinity="1"/>
      <geom name="{name}_head" type="sphere" size="0.125" pos="0 0 1.58" rgba="{skin}" contype="1" conaffinity="1"/>
      <geom name="{name}_hair" type="sphere" size="0.128" pos="-0.02 0 1.62" rgba="{hair}" contype="0" conaffinity="0"/>
    </body>"""


def _quad_body_mjcf(home_pose: tuple[float, float, float]) -> str:
    hx, hy, hz = home_pose
    # F450 quadrotor: 0.12 m collision sphere + 4 visual arms/rotors + 7 rangefinder sites + drone_cam + spectator_cam.
    # Rangefinder sites point along their local +Z axis!
    # In MuJoCo site euler="rx ry rz" (deg) or quat:
    # - forward (+Y in quad frame): rotate +Z onto +Y -> euler="-90 0 0"
    # - 45 deg right (+X +Y): rotate around Z by -45 after -90 X -> zaxis="0.7071 0.7071 0"
    return f"""
    <body name="quad" pos="{hx:.3f} {hy:.3f} {hz:.3f}">
      <freejoint name="quad_root"/>
      <inertial pos="0 0 0" mass="1.5" diaginertia="0.0124 0.0124 0.0246"/>
      <geom name="quad_hull" type="sphere" size="0.12" rgba="0.14 0.16 0.20 1" contype="1" conaffinity="1" friction="0.9 0.02 0.001"/>
      <geom name="quad_plate" type="box" size="0.09 0.09 0.02" rgba="0.20 0.24 0.30 1" contype="0" conaffinity="0"/>
      <geom name="arm_fr" type="capsule" fromto="0 0 0 0.156 0.156 0" size="0.014" rgba="0.90 0.25 0.15 1" contype="0" conaffinity="0"/>
      <geom name="arm_fl" type="capsule" fromto="0 0 0 -0.156 0.156 0" size="0.014" rgba="0.90 0.25 0.15 1" contype="0" conaffinity="0"/>
      <geom name="arm_br" type="capsule" fromto="0 0 0 0.156 -0.156 0" size="0.014" rgba="0.22 0.25 0.28 1" contype="0" conaffinity="0"/>
      <geom name="arm_bl" type="capsule" fromto="0 0 0 -0.156 -0.156 0" size="0.014" rgba="0.22 0.25 0.28 1" contype="0" conaffinity="0"/>
      <geom name="rotor_fr" type="cylinder" pos="0.156 0.156 0.025" size="0.115 0.004" rgba="0.10 0.10 0.12 0.65" contype="0" conaffinity="0"/>
      <geom name="rotor_fl" type="cylinder" pos="-0.156 0.156 0.025" size="0.115 0.004" rgba="0.10 0.10 0.12 0.65" contype="0" conaffinity="0"/>
      <geom name="rotor_br" type="cylinder" pos="0.156 -0.156 0.025" size="0.115 0.004" rgba="0.10 0.10 0.12 0.65" contype="0" conaffinity="0"/>
      <geom name="rotor_bl" type="cylinder" pos="-0.156 -0.156 0.025" size="0.115 0.004" rgba="0.10 0.10 0.12 0.65" contype="0" conaffinity="0"/>
      <geom name="gimbal_pod" type="sphere" pos="0 0.07 0.03" size="0.028" rgba="0.08 0.09 0.12 1" contype="0" conaffinity="0"/>
      <site name="imu_site" pos="0 0 0"/>
      <site name="tof_front" pos="0 0.05 0" zaxis="0 1 0"/>
      <site name="tof_front_right" pos="0.035 0.035 0" zaxis="0.707107 0.707107 0"/>
      <site name="tof_back_right" pos="0.035 -0.035 0" zaxis="0.707107 -0.707107 0"/>
      <site name="tof_back" pos="0 -0.05 0" zaxis="0 -1 0"/>
      <site name="tof_back_left" pos="-0.035 -0.035 0" zaxis="-0.707107 -0.707107 0"/>
      <site name="tof_front_left" pos="-0.035 0.035 0" zaxis="-0.707107 0.707107 0"/>
      <site name="tof_down" pos="0 0 -0.05" zaxis="0 0 -1"/>
      <camera name="drone_cam" pos="0 0.08 -0.14" fovy="85" euler="90 0 0"/>
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


def _indoor_sar_scene(asset_dir: Path) -> tuple[str, MapData]:
    # Room: X in [-6, 6] (12m wide), Y in [-5, 11] (16m long), ceiling at Z=3.2m.
    # Divider wall at Y=4.0 with a 2.2m doorway centered at X=0 -> corridor Y in [4, 11].
    body_xml = f"""
    <!-- Floor, AprilTag home pad, walls, ceiling (ceiling invisible from above for overhead spectator) -->
    <geom name="floor" type="plane" size="12 14 0.1" material="floor_mat" contype="1" conaffinity="1"/>
    {_apriltag_pad_mjcf(0.002)}

    <geom name="wall_south" type="box" pos="0 -5.0 1.6" size="6.2 0.15 1.6" rgba="0.82 0.80 0.76 1" contype="1" conaffinity="1"/>
    <geom name="wall_north" type="box" pos="0 11.0 1.6" size="6.2 0.15 1.6" rgba="0.82 0.80 0.76 1" contype="1" conaffinity="1"/>
    <geom name="wall_west" type="box" pos="-6.0 3.0 1.6" size="0.15 8.2 1.6" rgba="0.78 0.77 0.74 1" contype="1" conaffinity="1"/>
    <geom name="wall_east" type="box" pos="6.0 3.0 1.6" size="0.15 8.2 1.6" rgba="0.78 0.77 0.74 1" contype="1" conaffinity="1"/>

    <!-- Partition wall with central 2.2m doorway -->
    <geom name="divider_west" type="box" pos="-3.55 4.0 1.6" size="2.45 0.15 1.6" rgba="0.74 0.75 0.78 1" contype="1" conaffinity="1"/>
    <geom name="divider_east" type="box" pos="3.55 4.0 1.6" size="2.45 0.15 1.6" rgba="0.74 0.75 0.78 1" contype="1" conaffinity="1"/>
    <geom name="divider_lintel" type="box" pos="0 4.0 2.75" size="1.15 0.15 0.45" rgba="0.74 0.75 0.78 1" contype="1" conaffinity="1"/>

    <!-- Furniture -->
    <geom name="table_west" type="box" pos="-3.8 1.2 0.38" size="0.75 0.50 0.38" rgba="0.48 0.34 0.22 1" contype="1" conaffinity="1"/>
    <geom name="crate_east" type="box" pos="3.6 1.5 0.45" size="0.55 0.55 0.45" rgba="0.58 0.46 0.28 1" contype="1" conaffinity="1"/>
    <geom name="pillar_corridor" type="box" pos="2.8 8.0 1.6" size="0.35 0.35 1.6" rgba="0.65 0.67 0.70 1" contype="1" conaffinity="1"/>

    <!-- People for SAR detection & ReID -->
    {_person_mjcf("person_red", 2.4, 2.6, yaw_deg=180, shirt_rgba="0.88 0.12 0.12 1", posture="standing")}
    {_person_mjcf("person_blue", -2.5, 2.2, yaw_deg=-25, shirt_rgba="0.12 0.35 0.85 1", posture="sitting")}
    {_person_mjcf("person_lying", -0.6, 8.6, yaw_deg=90, shirt_rgba="0.92 0.48 0.10 1", posture="lying")}
    {_person_mjcf("person_walker", 0.9, 7.2, yaw_deg=0, shirt_rgba="0.15 0.72 0.35 1", posture="standing", freejoint=True)}
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
    return body_xml, map_data


def _forest_sar_scene(asset_dir: Path) -> tuple[str, MapData]:
    trees = [
        (5.5, 6.0, 4.5), (-6.0, 5.0, 5.2), (8.5, -3.5, 4.8), (-7.5, -5.0, 5.0),
        (2.5, 14.0, 5.6), (-4.0, 15.5, 5.0), (11.0, 9.0, 5.4), (-11.0, 11.0, 4.9),
        (14.0, 1.5, 5.1), (-13.5, -2.0, 5.5), (0.5, -10.5, 4.7), (-6.5, -13.0, 5.2),
    ]
    tree_geoms = []
    obstacles = []
    for idx, (tx, ty, h) in enumerate(trees):
        tree_geoms.append(
            f'<geom name="tree_trunk_{idx}" type="cylinder" pos="{tx:.2f} {ty:.2f} {h*0.35:.2f}" '
            f'size="0.32 {h*0.35:.2f}" rgba="0.38 0.25 0.14 1" contype="1" conaffinity="1"/>\n'
            f'    <geom name="tree_canopy_{idx}" type="sphere" pos="{tx:.2f} {ty:.2f} {h*0.82:.2f}" '
            f'size="1.45" rgba="0.16 0.42 0.18 1" contype="1" conaffinity="1"/>'
        )
        obstacles.append(
            Area("tree", ((ty - 0.5, tx - 0.5), (ty + 0.5, tx - 0.5), (ty + 0.5, tx + 0.5), (ty - 0.5, tx + 0.5)), f"Tree {idx+1}")
        )
    body_xml = f"""
    <geom name="terrain" type="plane" size="45 45 0.1" rgba="0.24 0.36 0.20 1" contype="1" conaffinity="1"/>
    <geom name="trail_n" type="box" pos="0 7.0 0.003" size="1.4 16.0 0.003" rgba="0.52 0.43 0.32 1" contype="0" conaffinity="0"/>
    {_apriltag_pad_mjcf(0.005)}
    <geom name="boulder_1" type="sphere" pos="-2.8 9.2 0.45" size="0.75" rgba="0.45 0.45 0.46 1" contype="1" conaffinity="1"/>
    {"".join(tree_geoms)}
    {_person_mjcf("hiker_red", 3.6, 10.5, yaw_deg=150, shirt_rgba="0.88 0.15 0.15 1", posture="standing")}
    {_person_mjcf("hiker_yellow", -3.8, 12.8, yaw_deg=45, shirt_rgba="0.95 0.78 0.12 1", posture="sitting")}
    """
    map_data = MapData(
        places=(
            Place("Home Clearing", "helipad", 0.0, 0.0),
            Place("North Trailhead", "trail", 14.0, 0.0),
            Place("North Ridge", "landmark", 20.0, 0.0),
        ),
        ways=(Way("path", ((-8.0, 0.0), (0.0, 0.0), (14.0, 0.0), (22.0, 0.0)), "Forest Trail"),),
        areas=(Area("park", ((-25.0, -25.0), (25.0, -25.0), (25.0, 25.0), (-25.0, 25.0)), "Pine Woods"),),
        walkable=(Area("path", ((-10.0, -1.4), (23.0, -1.4), (23.0, 1.4), (-10.0, 1.4)), "Trail"),),
        obstacles=tuple(obstacles),
        attribution="HORUS Built-in 3D Scene: forest_sar",
    )
    return body_xml, map_data


def _urban_plaza_scene(asset_dir: Path) -> tuple[str, MapData]:
    buildings = [
        ("bldg_nw", -12.0, 15.0, 4.5, 4.5, 8.0, "0.62 0.64 0.68 1"),
        ("bldg_ne", 12.0, 15.0, 4.5, 4.5, 10.0, "0.56 0.60 0.66 1"),
        ("bldg_sw", -13.0, -10.0, 4.0, 4.0, 7.0, "0.68 0.62 0.56 1"),
        ("bldg_se", 13.0, -10.0, 4.0, 4.0, 6.0, "0.60 0.64 0.60 1"),
    ]
    bldg_xml = "\n    ".join(
        f'<geom name="{n}" type="box" pos="{x} {y} {h/2}" size="{sx} {sy} {h/2}" rgba="{rgba}" contype="1" conaffinity="1"/>'
        for n, x, y, sx, sy, h, rgba in buildings
    )
    obstacles = tuple(
        Area("building", ((y - sy, x - sx), (y + sy, x - sx), (y + sy, x + sx), (y - sy, x + sx)), n)
        for n, x, y, sx, sy, _, _ in buildings
    )
    body_xml = f"""
    <geom name="plaza_ground" type="plane" size="50 50 0.1" material="floor_mat" contype="1" conaffinity="1"/>
    <geom name="road_ns" type="box" pos="0 4.0 0.002" size="3.0 30.0 0.002" rgba="0.20 0.22 0.24 1" contype="0" conaffinity="0"/>
    <geom name="road_ew" type="box" pos="0 8.0 0.003" size="30.0 2.6 0.002" rgba="0.22 0.24 0.26 1" contype="0" conaffinity="0"/>
    {_apriltag_pad_mjcf(0.005)}
    <geom name="fountain" type="cylinder" pos="0 12.0 0.35" size="1.4 0.35" rgba="0.72 0.74 0.78 1" contype="1" conaffinity="1"/>
    <geom name="van_parked" type="box" pos="5.2 5.0 0.95" size="1.1 2.2 0.95" rgba="0.24 0.42 0.68 1" contype="1" conaffinity="1"/>
    {bldg_xml}
    {_person_mjcf("ped_red", -2.2, 10.8, yaw_deg=-60, shirt_rgba="0.88 0.14 0.14 1", posture="standing")}
    {_person_mjcf("ped_blue", 2.8, 12.5, yaw_deg=140, shirt_rgba="0.14 0.38 0.86 1", posture="standing")}
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
    return body_xml, map_data


def _procedural_scene(asset_dir: Path, seed: int) -> tuple[str, MapData]:
    rng = random.Random(seed)
    geoms = [
        '<geom name="terrain" type="plane" size="45 45 0.1" rgba="0.28 0.38 0.24 1" contype="1" conaffinity="1"/>',
        _apriltag_pad_mjcf(0.004),
    ]
    obstacles = []
    places = [Place("Home Pad", "helipad", 0.0, 0.0)]
    for i in range(10):
        angle = rng.uniform(0, math.tau)
        dist = rng.uniform(4.5, 18.0)
        tx, ty = math.cos(angle) * dist, math.sin(angle) * dist
        h = rng.uniform(3.5, 6.0)
        geoms.append(
            f'<geom name="proc_tree_{i}" type="cylinder" pos="{tx:.2f} {ty:.2f} {h*0.35:.2f}" size="0.30 {h*0.35:.2f}" rgba="0.38 0.25 0.14 1" contype="1" conaffinity="1"/>'
            f'<geom name="proc_canopy_{i}" type="sphere" pos="{tx:.2f} {ty:.2f} {h*0.80:.2f}" size="1.3" rgba="0.18 0.44 0.20 1" contype="1" conaffinity="1"/>'
        )
        obstacles.append(Area("tree", ((ty - 0.5, tx - 0.5), (ty + 0.5, tx - 0.5), (ty + 0.5, tx + 0.5), (ty - 0.5, tx + 0.5)), f"Tree {i+1}"))
    px, py = rng.uniform(-3.0, 3.0), rng.uniform(5.0, 10.0)
    geoms.append(_person_mjcf("proc_person_red", px, py, yaw_deg=rng.uniform(0, 360), shirt_rgba="0.88 0.14 0.14 1"))
    places.append(Place("North Clearing", "landmark", 8.0, 0.0))
    map_data = MapData(
        places=tuple(places),
        ways=(Way("path", ((0.0, 0.0), (8.0, 0.0)), "Survey Track"),),
        obstacles=tuple(obstacles),
        attribution=f"HORUS Built-in 3D Scene: procedural (seed={seed})",
    )
    return "\n    ".join(geoms), map_data


def build_scene_bundle(name: str, asset_dir: Path, *, seed: int = 42) -> SceneBundle:
    """Create MuJoCo XML, PNG textures, and MapData for ``name`` inside ``asset_dir``."""
    if name not in SCENE_SPECS:
        raise ValueError(f"unknown 3D scene {name!r}; expected one of {SCENE_NAMES}")
    spec = SCENE_SPECS[name]
    asset_dir = Path(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=True)
    tag_png = asset_dir / "apriltag_id0.png"
    floor_png = asset_dir / "floor_checker.png"
    _write_png(tag_png, generate_apriltag_36h11_id0(256))
    _write_png(floor_png, _generate_checker_rgb(256))

    if name == "indoor_sar":
        scene_bodies, map_data = _indoor_sar_scene(asset_dir)
    elif name == "forest_sar":
        scene_bodies, map_data = _forest_sar_scene(asset_dir)
    elif name == "urban_plaza":
        scene_bodies, map_data = _urban_plaza_scene(asset_dir)
    else:
        scene_bodies, map_data = _procedural_scene(asset_dir, seed)

    xml = f"""<mujoco model="horus_{name}">
  <compiler angle="degree" coordinate="local" assetdir="{asset_dir.as_posix()}"/>
  <option timestep="0.00125" gravity="0 0 -9.80665" integrator="Euler"/>
  <visual>
    <global offwidth="640" offheight="480"/>
    <headlight ambient="0.45 0.45 0.45" diffuse="0.7 0.7 0.7" specular="0.1 0.1 0.1"/>
  </visual>
  <asset>
    <texture name="apriltag_tex" type="2d" file="apriltag_id0.png"/>
    <material name="apriltag_mat" texture="apriltag_tex" texrepeat="1 1" texuniform="false" reflectance="0" specular="0"/>
    <texture name="floor_tex" type="2d" file="floor_checker.png"/>
    <material name="floor_mat" texture="floor_tex" texrepeat="12 12" reflectance="0.05"/>
  </asset>
  <worldbody>
    <light name="sun" pos="0 0 18" dir="0 0 -1" directional="true" diffuse="0.85 0.85 0.82"/>
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
