"""Low-poly procedural animals built from SDF primitives.

No licensed, anatomically real animal meshes with usable licences were found (Gazebo Fuel's
"Dog"/"Horse" hits are scanned toys), so these are deliberately simple primitive figures with
species-typical size, proportions and coat colours. They are kinematic: posed by the scenario
driver with species-appropriate speed, acceleration and turn limits, and have no rigged gait.
They are not photorealistic and detector recall on them is a measured result, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random

from sim.worldgen.sdf import ACTOR_COLLIDE_BITMASK, f


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


def species(name: str) -> Species:
    try:
        return SPECIES[name]
    except KeyError:
        raise ValueError(f"unknown animal species {name!r}; known: {sorted(SPECIES)}") from None


def _mat(rgb: tuple[int, int, int]) -> str:
    colour = " ".join(f"{c / 255:.3f}" for c in rgb) + " 1"
    return f"<material><ambient>{colour}</ambient><diffuse>{colour}</diffuse></material>"


class _Figure:
    """Collects primitive visuals and their exact axis-aligned bounds in the model frame."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.low = [math.inf] * 3
        self.high = [-math.inf] * 3

    def _extend(self, points) -> None:
        for point in points:
            for axis in range(3):
                self.low[axis] = min(self.low[axis], point[axis])
                self.high[axis] = max(self.high[axis], point[axis])

    def add(self, name: str, x: float, y: float, z: float, shape: str, size: tuple[float, ...],
            rgb: tuple[int, int, int], pitch: float = 0.0) -> None:
        if shape == "box":
            geometry = f"<box><size>{f(size[0])} {f(size[1])} {f(size[2])}</size></box>"
            half = (size[0] / 2, size[1] / 2, size[2] / 2)
            corners = [(sx * half[0], sy * half[1], sz * half[2])
                       for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
        elif shape == "cylinder":
            radius, length = size
            geometry = f"<cylinder><radius>{f(radius)}</radius><length>{f(length)}</length></cylinder>"
            # Exact extents of a z-axis cylinder rotated by ``pitch`` about y: the end discs.
            corners = []
            for end in (-length / 2, length / 2):
                for angle in range(0, 360, 5):
                    a = math.radians(angle)
                    corners.append((radius * math.cos(a), radius * math.sin(a), end))
        elif shape == "sphere":
            (radius,) = size
            geometry = f"<sphere><radius>{f(radius)}</radius></sphere>"
            corners = [(sx * radius, sy * radius, sz * radius)
                       for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
            pitch = 0.0
        else:
            raise ValueError(shape)
        c, s = math.cos(pitch), math.sin(pitch)
        self._extend((x + px * c + pz * s, y + py, z - px * s + pz * c) for px, py, pz in corners)
        self.parts.append(f'<visual name="{name}"><pose>{f(x)} {f(y)} {f(z)} 0 {f(pitch)} 0</pose>'
                          f"<geometry>{geometry}</geometry>{_mat(rgb)}</visual>")


def _quadruped(kind: Species, k: float, coat: tuple[int, int, int], dark: tuple[int, int, int]) -> _Figure:
    figure = _Figure()
    length, width, height = kind.length_m * k, kind.width_m * k, kind.height_m * k
    leg_h = height * (0.55 if kind.name == "deer" else 0.42)
    body_h = height * (0.28 if kind.name == "deer" else 0.32)
    body_z = leg_h + body_h / 2
    figure.add("torso", 0, 0, body_z, "box", (length * 0.62, width, body_h), coat)
    for i, (dx, dy) in enumerate(((0.24, 0.35), (0.24, -0.35), (-0.24, 0.35), (-0.24, -0.35))):
        figure.add(f"leg_{i}", dx * length, dy * width, leg_h / 2, "cylinder",
                   (width * (0.12 if kind.name == "deer" else 0.16), leg_h), dark)
    neck_len = height - body_z - (0.10 if kind.name == "deer" else 0.06) * k
    figure.add("neck", length * 0.30, 0, body_z + neck_len * 0.45, "cylinder",
               (width * 0.22, max(0.05, neck_len * 0.8)), coat, pitch=0.5)
    head_r = width * (0.34 if kind.name == "deer" else 0.42)
    head_x, head_z = length * 0.40, height - head_r
    figure.add("head", head_x, 0, head_z, "sphere", (head_r,), coat)
    figure.add("snout", head_x + head_r * 1.1, 0, head_z - head_r * 0.2, "box",
               (head_r * 1.2, head_r * 0.9, head_r * 0.8), dark)
    if kind.name in ("dog", "cat"):
        for side in (1, -1):
            figure.add(f"ear_{side}", head_x - head_r * 0.2, side * head_r * 0.6, head_z + head_r * 0.8, "box",
                       (head_r * 0.35, head_r * 0.25, head_r * 0.6), dark)
        figure.add("tail", -length * 0.40, 0, body_z + body_h * 0.3, "cylinder",
                   (width * 0.08, length * (0.3 if kind.name == "dog" else 0.5)), coat, pitch=-1.0)
    else:  # deer: pale rump patch
        figure.add("rump", -length * 0.31, 0, body_z, "box", (0.02, width * 0.8, body_h * 0.8), (220, 210, 190))
    return figure


def _bird(kind: Species, k: float, coat: tuple[int, int, int]) -> _Figure:
    figure = _Figure()
    length, height = kind.length_m * k, kind.height_m * k
    body_r = height * 0.32
    beak = (70, 70, 72) if kind.name == "crow" else (200, 150, 110)
    figure.add("body", 0, 0, height * 0.42, "sphere", (body_r,), coat)
    figure.add("tail", -length * 0.38, 0, height * 0.40, "box", (length * 0.35, body_r * 0.9, 0.02 * k), coat, pitch=-0.2)
    figure.add("head", length * 0.30, 0, height * 0.80, "sphere", (body_r * 0.55,), coat)
    figure.add("beak", length * 0.30 + body_r * 0.7, 0, height * 0.78, "box", (body_r * 0.5, body_r * 0.2, body_r * 0.2), beak)
    for side, name in ((1, "leg_l"), (-1, "leg_r")):
        figure.add(name, 0, side * body_r * 0.35, height * 0.07, "cylinder", (0.006 * k, height * 0.14), (190, 110, 90))
    return figure


def _figure(name: str, scale: float, coat_index: int = 0) -> _Figure:
    kind = species(name)
    coat = kind.coats[coat_index % len(kind.coats)]
    dark = tuple(max(0, round(c * 0.55)) for c in coat)
    return _bird(kind, scale, coat) if name in ("pigeon", "crow") else _quadruped(kind, scale, coat, dark)


def animal_envelope(name: str, scale: float) -> dict[str, float]:
    """Bounds of the rendered primitives (rounded outward to the millimetre)."""
    figure = _figure(name, scale)
    low = [math.floor(value * 1000) / 1000 for value in figure.low]
    high = [math.ceil(value * 1000) / 1000 for value in figure.high]
    return {"x_min_m": low[0], "x_max_m": high[0], "y_min_m": low[1], "y_max_m": high[1],
            "z_min_m": low[2], "top_z_m": high[2],
            "torso_z_m": round((low[2] + high[2]) / 2, 3), "height_m": high[2]}


def animal_model(model_name: str, name: str, scale: float, coat_index: int, x: float, y: float,
                 z: float, yaw: float) -> str:
    """An inline kinematic animal model (visuals plus one rendered-envelope collision box)."""
    figure = _figure(name, scale, coat_index)
    env = animal_envelope(name, scale)
    size = (env["x_max_m"] - env["x_min_m"], env["y_max_m"] - env["y_min_m"], env["top_z_m"] - env["z_min_m"])
    centre = ((env["x_max_m"] + env["x_min_m"]) / 2, (env["y_max_m"] + env["y_min_m"]) / 2,
              (env["top_z_m"] + env["z_min_m"]) / 2)
    collision = (f'<collision name="body_envelope"><pose>{f(centre[0])} {f(centre[1])} {f(centre[2])} 0 0 0</pose>'
                 f"<geometry><box><size>{f(size[0])} {f(size[1])} {f(size[2])}</size></box></geometry>"
                 f"<surface><contact><collide_bitmask>{ACTOR_COLLIDE_BITMASK:#06x}</collide_bitmask>"
                 "</contact></surface></collision>")
    return (f'<model name="{model_name}"><static>false</static>'
            f"<pose>{f(x)} {f(y)} {f(z)} 0 0 {f(yaw)}</pose><link name=\"body\">"
            "<inertial><mass>20</mass><inertia><ixx>1</ixx><ixy>0</ixy><ixz>0</ixz><iyy>1</iyy>"
            "<iyz>0</iyz><izz>1</izz></inertia></inertial><gravity>false</gravity><kinematic>true</kinematic>"
            + collision + "".join(figure.parts) + "</link></model>")


def sample_individual(rng: random.Random, name: str) -> tuple[float, int]:
    kind = species(name)
    return round(rng.uniform(*kind.scale), 3), rng.randrange(len(kind.coats))

