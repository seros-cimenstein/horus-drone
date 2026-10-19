"""Procedural animals with smooth lofted meshes, fur/feather textures and kinematic motion.

Each species gets one committed, generator-built mesh model (``write_animal_models``) under
``sim/models/horus_animal_<species>/``: a smooth lofted body, neck, head, ears, tail, legs and
wings as appropriate, UV-mapped with a neutral fur/feather texture and tinted per individual by
the material diffuse colour (species coat). Animals are kinematic: posed by the scenario driver
with species-appropriate speed, acceleration and turn limits, and have no rigged gait. The
rendered-envelope collision box convention is unchanged: one box bounds the posed mesh.

The meshes are pure-standard-library geometry (no numpy/PIL); ``python3 -c "from
sim.worldgen.fauna import write_animal_models; write_animal_models()"`` regenerates the
committed files deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from pathlib import Path

from sim.worldgen.materials import feather_texture, fur_texture
from sim.worldgen.meshes import LoftMesh
from sim.worldgen.sdf import ACTOR_COLLIDE_BITMASK, collide_surface, f, rgba

# Texture seed offsets so every species' committed texture has a unique file name and content
# (Ogre2 caches textures by file name).
_TEXTURE_SEEDS = {"dog": 11, "cat": 12, "deer": 13, "pigeon": 14, "crow": 15}


@dataclass(frozen=True)
class Species:
    name: str
    length_m: float  # nose to tail base, unscaled
    width_m: float
    height_m: float  # ground to top of head, unscaled
    scale: tuple[float, float]
    walk_m_s: tuple[float, float]
    max_speed_m_s: float
    accel_m_s2: float
    radius_m: float  # interaction radius, unscaled
    coats: tuple[tuple[int, int, int], ...]
    coco_class: str | None  # COCO detector class for this species; None when COCO has none


SPECIES: dict[str, Species] = {species.name: species for species in (
    Species("dog", 0.80, 0.26, 0.62, (0.6, 1.2), (1.0, 1.6), 3.5, 3.0, 0.45,
            ((30, 28, 26), (120, 80, 44), (196, 160, 110), (228, 224, 214), (110, 110, 112)), "dog"),
    Species("cat", 0.46, 0.14, 0.28, (0.85, 1.1), (0.4, 0.8), 2.5, 3.0, 0.3,
            ((40, 38, 36), (190, 120, 60), (140, 136, 130), (226, 220, 210)), "cat"),
    # COCO has no deer class: detections are expected only from an open-vocabulary grounder.
    Species("deer", 1.45, 0.38, 1.35, (0.85, 1.1), (0.6, 1.3), 4.0, 2.5, 0.85,
            ((126, 88, 56), (148, 108, 70), (104, 74, 50)), None),
    Species("pigeon", 0.30, 0.12, 0.26, (0.9, 1.1), (0.3, 0.6), 1.5, 3.0, 0.2,
            ((120, 124, 132), (86, 88, 96), (170, 170, 176)), "bird"),
    Species("crow", 0.42, 0.14, 0.30, (0.9, 1.1), (0.3, 0.6), 1.5, 3.0, 0.22,
            ((22, 22, 26), (34, 34, 40)), "bird"),
)}

_BIRDS = ("pigeon", "crow")


def species(name: str) -> Species:
    try:
        return SPECIES[name]
    except KeyError:
        raise ValueError(f"unknown animal species {name!r}; known: {sorted(SPECIES)}") from None


# Mesh construction lives in sim.worldgen.meshes.LoftMesh (shared with the dressing meshes).


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _quadruped_mesh(kind: Species, k: float = 1.0) -> LoftMesh:
    mesh = LoftMesh()
    length, width, height = kind.length_m * k, kind.width_m * k, kind.height_m * k
    # Proportions per species (fractions of height for legs/body; of width for head/snout).
    profiles = {
        "dog": dict(leg=0.40, body=0.35, head_c=0.14, head_a=0.46, head_b=0.42, snout=0.40,
                    head_z=1.00, chest_gap=0.30, ear="drop", tail_len=0.30, tail_rise=0.30),
        "cat": dict(leg=0.40, body=0.42, head_c=0.16, head_a=0.44, head_b=0.46, snout=0.30,
                    head_z=1.00, chest_gap=0.26, ear="up", tail_len=0.55, tail_rise=0.65),
        "deer": dict(leg=0.50, body=0.30, head_c=0.10, head_a=0.30, head_b=0.34, snout=0.48,
                     head_z=1.10, chest_gap=0.40, ear="leaf", tail_len=0.08, tail_rise=0.0),
    }
    profile = profiles[kind.name]
    deer = kind.name == "deer"
    leg_h = height * profile["leg"]
    body_h = height * profile["body"]
    body_z = leg_h + body_h / 2
    nose_x = length / 2
    head_c = height * profile["head_c"]
    head_b = width * profile["head_b"]
    head_a = width * profile["head_a"]
    snout_len = width * profile["snout"]
    head_x = nose_x - snout_len - head_a * 0.55
    head_z = height * profile["head_z"] - head_c
    chest_x = head_x - head_a - width * profile["chest_gap"]
    rump_x = -length * (0.42 if deer else 0.45)
    # Body: an arched loft with a full middle and rounded rump/chest.
    sections = 8
    points, radii = [], []
    for index in range(sections):
        t = index / (sections - 1)
        x = _lerp(rump_x, chest_x, t)
        arch = body_h * 0.06 * math.sin(math.pi * t)
        taper = 0.52 + 0.48 * math.sin(math.pi * (0.05 + 0.9 * t))
        points.append((x, 0.0, body_z + arch + body_h * 0.05))
        radii.append((width / 2 * taper, body_h / 2 * taper))
    mesh.add_tube(points, radii, segments=14)
    # Neck rising from the chest to the head base.
    neck_base = (chest_x - width * 0.02, 0.0, body_z + body_h * (0.45 if deer else 0.28))
    head_base = (head_x - head_a * 0.55, 0.0, head_z - head_c * (0.30 if deer else 0.15))
    neck_r = (width * (0.22 if deer else 0.32), body_h * (0.34 if deer else 0.40))
    head_r = (width * (0.16 if deer else 0.25), head_c * (0.70 if deer else 0.85))
    mesh.add_tube([neck_base, _lerp3(neck_base, head_base, 0.55), head_base],
                  [neck_r, tuple(0.9 * value for value in neck_r), head_r], segments=12)
    mesh.add_ellipsoid((head_x, 0.0, head_z), (head_a, head_b, head_c), segments=12, rings=6)
    # Snout toward +x.
    mesh.add_tube([(head_x + head_a * 0.30, 0.0, head_z - head_c * 0.30),
                   (nose_x - snout_len * 0.35, 0.0, head_z - head_c * 0.45),
                   (nose_x, 0.0, head_z - head_c * 0.55)],
                  [(head_b * 0.70, head_c * 0.80), (head_b * 0.52, head_c * 0.60),
                   (head_b * 0.36, head_c * 0.40)], segments=10)
    # Ears: upright flaps (cat), drop flaps (dog), leaf ears (deer).
    for side in (1, -1):
        if deer:
            base = (head_x - head_a * 0.2, side * head_b * 0.55, head_z + head_c * 0.45)
            tip = (head_x - head_a * 0.55, side * head_b * 1.45, head_z + head_c * 1.05)
        elif kind.name == "dog":
            base = (head_x - head_a * 0.25, side * head_b * 0.50, head_z + head_c * 0.55)
            tip = (head_x - head_a * 0.45, side * head_b * 0.95, head_z - head_c * 0.35)
        else:
            base = (head_x - head_a * 0.15, side * head_b * 0.45, head_z + head_c * 0.30)
            tip = (head_x - head_a * 0.25, side * head_b * 0.85, head_z + head_c * 1.10)
        mesh.add_tube([base, _lerp3(base, tip, 0.55), tip],
                      [(head_b * 0.22, head_c * 0.28), (head_b * 0.16, head_c * 0.24),
                       (head_b * 0.06, head_c * 0.10)], segments=8)
    if deer:  # antlers: one main beam and one tine per side, tips above the head (normalised)
        for side in (1, -1):
            base = (head_x - head_a * 0.10, side * head_b * 0.35, head_z + head_c * 0.75)
            bend = (head_x - head_a * 0.55, side * head_b * 0.95, height * 1.20)
            tip = (head_x - head_a * 1.05, side * head_b * 1.15, height * 1.24)
            mesh.add_tube([base, bend, tip],
                          [(width * 0.045, width * 0.045), (width * 0.030, width * 0.030),
                           (width * 0.018, width * 0.018)], segments=6)
            mesh.add_tube([bend, (head_x - head_a * 0.15, side * head_b * 1.25, height * 1.12)],
                          [(width * 0.030, width * 0.030), (width * 0.015, width * 0.015)], segments=6)
    # Legs with a broad upper mass, narrow ankle and a paw.
    for x_sign, x_anchor in ((1, chest_x - width * 0.18), (-1, rump_x + length * 0.10)):
        for side in (1, -1):
            top = (x_anchor, side * width * 0.30, body_z - body_h * 0.15)
            knee = (x_anchor + x_sign * width * 0.03, side * width * 0.30, leg_h * 0.50)
            foot = (x_anchor + x_sign * width * 0.06, side * width * 0.30, height * 0.008)
            leg_r = width * (0.075 if deer else 0.13 if kind.name == "dog" else 0.10)
            mesh.add_tube([top, knee, foot],
                          [(leg_r * 1.25, leg_r * 1.35), (leg_r * 0.80, leg_r * 0.90),
                           (leg_r * 0.70, leg_r * 0.80)], segments=8)
            mesh.add_ellipsoid((foot[0] + width * 0.05, foot[1], leg_r * 0.40),
                               (leg_r * 1.5, leg_r * 1.3, leg_r * 0.50), segments=8, rings=3)
    # Tail: carried up for dog/cat, short for deer.
    tail_base = (rump_x + length * 0.02, 0.0, body_z + body_h * 0.30)
    tail_len = length * profile["tail_len"]
    rise = height * profile["tail_rise"]
    if deer:
        mesh.add_tube([tail_base, (rump_x - tail_len * 0.5, 0.0, body_z + body_h * 0.42),
                       (rump_x - tail_len, 0.0, body_z + body_h * 0.20)],
                      [(width * 0.10, width * 0.10), (width * 0.08, width * 0.08),
                       (width * 0.05, width * 0.06)], segments=8)
    else:
        mesh.add_tube([tail_base,
                       (rump_x - tail_len * 0.35, 0.0, body_z + rise * 0.45),
                       (rump_x - tail_len * 0.7, 0.0, body_z + rise * 0.85),
                       (rump_x - tail_len, 0.0, body_z + rise)],
                      [(width * 0.10, width * 0.10), (width * 0.08, width * 0.08),
                       (width * 0.06, width * 0.06), (width * 0.035, width * 0.035)], segments=8)
    return mesh


def _bird_mesh(kind: Species, k: float = 1.0) -> LoftMesh:
    mesh = LoftMesh()
    length, width, height = kind.length_m * k, kind.width_m * k, kind.height_m * k
    body_z = height * 0.44
    body_rx, body_rz = width * 0.42, height * 0.26
    tail_x, chest_x = -length * 0.30, length * 0.16
    sections = 6
    points, radii = [], []
    for index in range(sections):
        t = index / (sections - 1)
        profile = 0.60 + 0.40 * math.sin(math.pi * (0.08 + 0.84 * t))
        points.append((_lerp(tail_x, chest_x, t), 0.0, body_z))
        radii.append((body_rx * profile, body_rz * profile))
    mesh.add_tube(points, radii, segments=12)
    head_z = height - height * 0.13
    head_x = length * 0.26
    mesh.add_tube([(chest_x - width * 0.02, 0.0, body_z + body_rz * 0.4),
                   (head_x - width * 0.18, 0.0, _lerp(body_z, head_z, 0.7)), (head_x, 0.0, head_z)],
                  [(width * 0.28, height * 0.16), (width * 0.26, height * 0.15),
                   (width * 0.27, height * 0.15)], segments=10)
    mesh.add_ellipsoid((head_x, 0.0, head_z), (width * 0.34, width * 0.30, height * 0.13),
                       segments=10, rings=5)
    beak = height * (0.16 if kind.name == "crow" else 0.11)
    mesh.add_tube([(head_x + width * 0.26, 0.0, head_z - height * 0.01),
                   (head_x + width * 0.26 + beak * 0.6, 0.0, head_z - height * 0.02),
                   (head_x + width * 0.26 + beak, 0.0, head_z - height * 0.03)],
                  [(width * 0.16, height * 0.05), (width * 0.10, height * 0.035),
                   (width * 0.05, height * 0.02)], segments=8)
    # Folded wings along the flanks.
    for side in (1, -1):
        shoulder = (length * 0.08, side * width * 0.40, body_z + body_rz * 0.25)
        tip = (-length * 0.20, side * width * 0.48, body_z - body_rz * 0.20)
        mesh.add_tube([shoulder, _lerp3(shoulder, tip, 0.5), tip],
                      [(width * 0.30, height * 0.16), (width * 0.24, height * 0.13),
                       (width * 0.10, height * 0.06)], segments=10)
    # Tail fan: a flat loft.
    mesh.add_tube([(tail_x, 0.0, body_z),
                   (tail_x - length * 0.08, 0.0, body_z - height * 0.01),
                   (tail_x - length * 0.17, 0.0, body_z - height * 0.03)],
                  [(width * 0.34, body_rz * 0.35), (width * 0.44, body_rz * 0.22),
                   (width * 0.40, body_rz * 0.12)], segments=10)
    # Legs.
    for side in (1, -1):
        top = (length * 0.02, side * width * 0.20, body_z - body_rz * 0.55)
        mesh.add_tube([top, (length * 0.03, side * width * 0.20, height * 0.07),
                       (length * 0.035, side * width * 0.21, height * 0.006)],
                      [(width * 0.075, width * 0.075), (width * 0.055, width * 0.055),
                       (width * 0.075, width * 0.055)], segments=8)
    return mesh


def _lerp3(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def species_mesh(name: str, k: float = 1.0) -> LoftMesh:
    """The generator mesh of one species at scale ``k`` (canonical size at k=1)."""
    kind = species(name)
    mesh = _bird_mesh(kind, k) if name in _BIRDS else _quadruped_mesh(kind, k)
    # Ear tips and raised tails may overshoot the nominal height; squash z so the mesh tops out
    # at the species height and the manifest envelope keeps its declared size.
    high = max(point[2] for point in mesh.positions)
    target = kind.height_m * k
    if high > 1e-9 and abs(high - target) > 1e-9:
        factor = target / high
        mesh.positions = [(x, y, z * factor) for x, y, z in mesh.positions]
    return mesh


def animal_texture(name: str, *, size: int = 256) -> bytes:
    """The neutral fur/feather map for one species (tinted per individual by the material)."""
    seed = _TEXTURE_SEEDS[name]
    return feather_texture(seed, size=size) if name in _BIRDS else fur_texture(seed, size=size)


def _mesh_asset(name: str) -> tuple[str, str]:
    """(OBJ text, MTL text) for one species' canonical mesh."""
    texture = f"{name}_{'feather' if name in _BIRDS else 'fur'}.png"
    return species_mesh(name).obj_text(
        name, f"{name}.mtl", name, texture,
        header=f"HORUS procedural {name} mesh (regenerate with sim.worldgen.fauna.write_animal_models)")


def _texture_uri(name: str) -> str:
    suffix = "feather" if name in _BIRDS else "fur"
    return f"model://horus_animal_{name}/materials/textures/{name}_{suffix}.png"


def write_animal_models(model_dir: Path | str) -> list[str]:
    """Write the committed standalone model of every species; returns the written paths."""
    model_dir = Path(model_dir)
    written: list[str] = []
    for name in SPECIES:
        directory = model_dir / f"horus_animal_{name}"
        meshes = directory / "meshes"
        textures = directory / "materials" / "textures"
        meshes.mkdir(parents=True, exist_ok=True)
        textures.mkdir(parents=True, exist_ok=True)
        obj, mtl = _mesh_asset(name)
        (meshes / f"{name}.obj").write_text(obj, encoding="utf-8")
        (meshes / f"{name}.mtl").write_text(mtl, encoding="utf-8")
        (textures / f"{name}_{'feather' if name in _BIRDS else 'fur'}.png").write_bytes(
            animal_texture(name))
        low, high = species_mesh(name).bounds()
        size = tuple(high[axis] - low[axis] for axis in range(3))
        centre = tuple((high[axis] + low[axis]) / 2 for axis in range(3))
        model_sdf = (
            '<?xml version="1.0"?>\n<sdf version="1.9">\n'
            f'  <model name="horus_animal_{name}">\n    <static>true</static>\n    <link name="link">\n'
            f'      <visual name="body">\n'
            f'        <geometry><mesh><uri>model://horus_animal_{name}/meshes/{name}.obj</uri></mesh></geometry>\n'
            f'        <material><diffuse>1 1 1 1</diffuse><pbr><metal>'
            f'<albedo_map>{_texture_uri(name)}</albedo_map>'
            f'<metalness>0.0</metalness><roughness>1.0</roughness></metal></pbr></material>\n'
            f'      </visual>\n'
            f'      <collision name="body_envelope"><pose>{f(centre[0])} {f(centre[1])} {f(centre[2])} 0 0 0</pose>'
            f'<geometry><box><size>{f(size[0])} {f(size[1])} {f(size[2])}</size></box></geometry></collision>\n'
            f'    </link>\n  </model>\n</sdf>\n')
        (directory / "model.sdf").write_text(model_sdf, encoding="utf-8")
        (directory / "model.config").write_text(
            '<?xml version="1.0"?>\n'
            f'<model><name>horus_animal_{name}</name><version>1.0</version>'
            '<sdf version="1.9">model.sdf</sdf><author><name>HORUS</name></author>'
            f'<description>Procedural {name} mesh (HORUS-generated loft geometry and neutral '
            'fur/feather texture); kinematic actors tint the material per individual.</description></model>\n',
            encoding="utf-8")
        written.append(str(directory / "model.sdf"))
    return written


def animal_envelope(name: str, scale: float) -> dict[str, float]:
    """Bounds of the rendered mesh at ``scale`` (rounded outward to the millimetre)."""
    mesh = species_mesh(name, scale)
    low, high = mesh.bounds()
    low = [math.floor(value * 1000) / 1000 for value in low]
    high = [math.ceil(value * 1000) / 1000 for value in high]
    return {"x_min_m": low[0], "x_max_m": high[0], "y_min_m": low[1], "y_max_m": high[1],
            "z_min_m": low[2], "top_z_m": high[2],
            "torso_z_m": round((low[2] + high[2]) / 2, 3), "height_m": high[2]}


def _coat_material(name: str, rgb: tuple[int, int, int]) -> str:
    colour = rgba(rgb)
    return (f"<material><ambient>{colour}</ambient><diffuse>{colour}</diffuse><pbr><metal>"
            f"<albedo_map>{_texture_uri(name)}</albedo_map>"
            f"<metalness>0.0</metalness><roughness>1.0</roughness></metal></pbr></material>")


def animal_model(model_name: str, name: str, scale: float, coat_index: int, x: float, y: float,
                 z: float, yaw: float) -> str:
    """An inline kinematic animal model (lofted mesh visual plus one envelope collision box)."""
    kind = species(name)
    coat = kind.coats[coat_index % len(kind.coats)]
    env = animal_envelope(name, scale)
    size = (env["x_max_m"] - env["x_min_m"], env["y_max_m"] - env["y_min_m"],
            env["top_z_m"] - env["z_min_m"])
    centre = ((env["x_max_m"] + env["x_min_m"]) / 2, (env["y_max_m"] + env["y_min_m"]) / 2,
              (env["top_z_m"] + env["z_min_m"]) / 2)
    collision = (f'<collision name="body_envelope"><pose>{f(centre[0])} {f(centre[1])} {f(centre[2])} 0 0 0</pose>'
                 f"<geometry><box><size>{f(size[0])} {f(size[1])} {f(size[2])}</size></box></geometry>"
                 f"{collide_surface(ACTOR_COLLIDE_BITMASK)}</collision>")
    visual = (f'<visual name="body"><geometry><mesh>'
              f'<uri>model://horus_animal_{name}/meshes/{name}.obj</uri>'
              f'<scale>{f(scale)} {f(scale)} {f(scale)}</scale></mesh></geometry>'
              f"{_coat_material(name, coat)}</visual>")
    return (f'<model name="{model_name}"><static>false</static>'
            f"<pose>{f(x)} {f(y)} {f(z)} 0 0 {f(yaw)}</pose><link name=\"body\">"
            "<inertial><mass>20</mass><inertia><ixx>1</ixx><ixy>0</ixy><ixz>0</ixz><iyy>1</iyy>"
            "<iyz>0</iyz><izz>1</izz></inertia></inertial><gravity>false</gravity><kinematic>true</kinematic>"
            + collision + visual + "</link></model>")


def sample_individual(rng: random.Random, name: str) -> tuple[float, int]:
    kind = species(name)
    return round(rng.uniform(*kind.scale), 3), rng.randrange(len(kind.coats))
