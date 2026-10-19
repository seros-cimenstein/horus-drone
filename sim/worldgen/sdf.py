from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from sim.worldgen.environment import Environment

LATITUDE = -35.363262
LONGITUDE = 149.165237
ELEVATION = 584.0

# Collision bitmasks (two collisions are tested only when their masks share a bit). Kinematic
# actors must not generate contacts with the terrain they walk on; the drone keeps the default
# mask and collides with everything. Generated 1.2 km worlds keep each static collision (terrain,
# merged scenery mesh, home pad) on its own bit: once a dynamic body exists, dartsim tests every
# overlapping static pair each 1 ms step, which measured RTF 0.00 on Koko instead of ~0.47.
TERRAIN_COLLIDE_BITMASK = 0x0001
ACTOR_COLLIDE_BITMASK = 0x0002
PAD_COLLIDE_BITMASK = 0x0008
SCENERY_COLLIDE_BITMASK = 0x0010
# Generated worlds include this primitive-collision airframe (sim/scripts/derive_procedural_airframe.py):
# the outdoor airframe's mesh collisions against a 1.2 km terrain mesh also stall dartsim.
PROCEDURAL_DRONE_MODEL = "horus_iris_procedural"


SURFACE_COLOURS = {
    "asphalt": (0.24, 0.25, 0.26),
    "sidewalk": (0.48, 0.47, 0.43),
    "plaza_stone": (0.58, 0.54, 0.47),
    "urban_ground": (0.29, 0.38, 0.23),
}
TREE_BARK = (
    (0.27, 0.15, 0.09), (0.34, 0.19, 0.10),
    (0.25, 0.12, 0.07), (0.38, 0.22, 0.12),
)
TREE_CONIFER = (
    (0.12, 0.30, 0.14), (0.15, 0.34, 0.17),
    (0.09, 0.25, 0.12), (0.18, 0.37, 0.19),
)
TREE_DECIDUOUS = (
    (0.29, 0.43, 0.19), (0.36, 0.48, 0.21),
    (0.24, 0.38, 0.17), (0.32, 0.46, 0.24),
)


def f(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def floats(*values: float) -> str:
    """Space-separated ``f`` values (poses, sizes, scales)."""
    return " ".join(f(value) for value in values)


def rgba(rgb: tuple[float, ...], scale: float = 255.0) -> str:
    """An opaque SDF colour from channels in ``0..scale``, each clamped to 1."""
    return " ".join(f"{min(1.0, value / scale):.3f}" for value in rgb) + " 1"


def collide_surface(bitmask: int) -> str:
    """A collision ``<surface>`` that only contacts collisions sharing a ``bitmask`` bit."""
    return f"<surface><contact><collide_bitmask>{bitmask:#06x}</collide_bitmask></contact></surface>"


def pose(x: float, y: float, z: float, yaw: float = 0.0) -> str:
    return f"{f(x)} {f(y)} {f(z)} 0 0 {f(yaw)}"


def material(rgb: tuple[float, float, float], alpha: float = 1.0) -> str:
    colour = " ".join(f"{v:.3f}" for v in (*rgb, alpha))
    return f"<material><ambient>{colour}</ambient><diffuse>{colour}</diffuse><specular>0.08 0.08 0.08 1</specular></material>"


def visual(name: str, x: float, y: float, z: float, yaw: float, geometry: str,
           rgb: tuple[float, float, float], alpha: float = 1.0,
           cast_shadows: bool = True) -> str:
    shadow_tag = "" if cast_shadows else "<cast_shadows>false</cast_shadows>"
    return (
        f'<visual name="{name}"><pose>{pose(x, y, z, yaw)}</pose>{shadow_tag}'
        f"<geometry>{geometry}</geometry>{material(rgb, alpha)}</visual>"
    )


def collision_at(name: str, pose_text: str, geometry: str) -> str:
    return f'<collision name="{name}"><pose>{pose_text}</pose><geometry>{geometry}</geometry></collision>'


def collision(name: str, x: float, y: float, z: float, yaw: float, geometry: str) -> str:
    return collision_at(name, pose(x, y, z, yaw), geometry)


def box_geometry(x: float, y: float, z: float) -> str:
    return f"<box><size>{f(x)} {f(y)} {f(z)}</size></box>"


def sphere_geometry(radius: float) -> str:
    return f"<sphere><radius>{f(radius)}</radius></sphere>"


def cylinder_geometry(radius: float, length: float) -> str:
    return f"<cylinder><radius>{f(radius)}</radius><length>{f(length)}</length></cylinder>"


def cone_geometry(radius: float, length: float) -> str:
    return f"<cone><radius>{f(radius)}</radius><length>{f(length)}</length></cone>"


def static_model(name: str, contents: str, pose_text: str | None = None) -> str:
    root_pose = f"<pose>{pose_text}</pose>" if pose_text else ""
    return (
        f'<model name="{name}"><static>true</static>{root_pose}'
        f'<link name="link">{contents}</link></model>'
    )


def include(uri: str, name: str, x: float, y: float, z: float = 0.0,
            yaw: float = 0.0) -> str:
    return (
        f"<include><uri>model://{uri}</uri><name>{name}</name>"
        f"<pose>{pose(x, y, z, yaw)}</pose></include>"
    )


def world_header(name: str, env: Environment) -> str:
    return f'''<?xml version="1.0"?>
<sdf version="1.9"><world name="{name}">
  <physics name="1ms" type="ignore"><max_step_size>0.001</max_step_size><real_time_factor>1.0</real_time_factor></physics>
  <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
  <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors"><render_engine>ogre2</render_engine></plugin>
  <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
  <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
  <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu"/>
  <plugin filename="gz-sim-navsat-system" name="gz::sim::systems::NavSat"/>
  <spherical_coordinates><latitude_deg>{LATITUDE}</latitude_deg><longitude_deg>{LONGITUDE}</longitude_deg><elevation>{ELEVATION}</elevation><heading_deg>0</heading_deg><surface_model>EARTH_WGS84</surface_model></spherical_coordinates>
  {env.scene_sdf()}
  {env.sun_sdf()}
'''


def home_pad(world_dir: Path, collide_bitmask: int | None = None) -> str:
    source = ET.parse(world_dir / "horus_indoor.sdf").getroot()
    pad = next(model for model in source.iter("model") if model.get("name") == "home_apriltag_pad")
    if collide_bitmask is not None:
        for collision in pad.iter("collision"):
            surface = collision.find("surface")
            if surface is None:
                surface = ET.SubElement(collision, "surface")
            contact = surface.find("contact")
            if contact is None:
                contact = ET.SubElement(surface, "contact")
            ET.SubElement(contact, "collide_bitmask").text = f"{collide_bitmask:#06x}"
    return ET.tostring(pad, encoding="unicode")


def pedestrian_include(agent: dict, ground_height: float = 0.0) -> str:
    x, y = agent["position"]
    z = agent.get("z", ground_height)
    yaw = agent.get("yaw", 0.0)
    return include(agent["asset"], agent["model"], x, y, z, yaw)
