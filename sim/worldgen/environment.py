"""Sampled lighting and ground/foliage palettes that change the rendered scene.

Every field here is written into the world SDF or the generated textures/materials; the
manifest copy is for test oracles and reports only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import random


def _rgb(values: tuple[float, ...]) -> str:
    return " ".join(f"{value:.2f}" for value in values)


@dataclass(frozen=True)
class Environment:
    profile: str
    sun_direction: tuple[float, float, float]
    sun_diffuse: tuple[float, float, float]
    sun_specular: tuple[float, float, float]
    ambient: tuple[float, float, float]
    background: tuple[float, float, float]
    ground_tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    foliage_tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    surface_tint: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def scene_sdf(self) -> str:
        return (f"<scene><ambient>{_rgb(self.ambient)}</ambient>"
                f"<background>{_rgb(self.background)}</background><shadows>true</shadows></scene>")

    def sun_sdf(self) -> str:
        return ('<light type="directional" name="sun"><cast_shadows>true</cast_shadows>'
                f"<pose>0 0 90 0 0 0</pose><diffuse>{_rgb(self.sun_diffuse)} 1</diffuse>"
                f"<specular>{_rgb(self.sun_specular)} 1</specular>"
                f"<direction>{_rgb(self.sun_direction)}</direction></light>")

    @property
    def sun_elevation_deg(self) -> float:
        return math.degrees(math.asin(-self.sun_direction[2]))

    def tint(self, rgb: tuple[float, float, float], kind: str) -> tuple[float, float, float]:
        factor = {"ground": self.ground_tint, "foliage": self.foliage_tint,
                  "surface": self.surface_tint}[kind]
        return tuple(min(1.0, max(0.0, c * k)) for c, k in zip(rgb, factor))

    def manifest(self) -> dict:
        record = asdict(self)
        # JSON has no tuples; keep the in-memory manifest equal to its round-tripped form.
        for key in ("sun_direction", "sun_diffuse", "sun_specular", "ambient", "background",
                    "ground_tint", "foliage_tint", "surface_tint"):
            record[key] = list(record[key])
        record["sun_elevation_deg"] = round(self.sun_elevation_deg, 2)
        return record


CANONICAL_ENVIRONMENT = Environment(
    profile="canonical", sun_direction=(-0.42, 0.26, -0.87), sun_diffuse=(0.98, 0.95, 0.86),
    sun_specular=(0.34, 0.33, 0.30), ambient=(0.58, 0.60, 0.57), background=(0.68, 0.77, 0.84),
)

# (profile, sun elevation range deg, sun colour, ambient scale, sky colour)
_PROFILES = (
    ("clear_midday", (55.0, 78.0), (1.00, 0.97, 0.90), 0.60, (0.62, 0.75, 0.88)),
    ("clear_morning", (22.0, 38.0), (1.00, 0.90, 0.76), 0.52, (0.70, 0.78, 0.86)),
    ("late_afternoon", (14.0, 26.0), (1.00, 0.82, 0.62), 0.48, (0.80, 0.74, 0.66)),
    ("overcast", (40.0, 70.0), (0.62, 0.63, 0.65), 0.72, (0.70, 0.72, 0.74)),
    ("hazy", (30.0, 55.0), (0.86, 0.84, 0.78), 0.66, (0.82, 0.83, 0.82)),
)


def sample_environment(rng: random.Random, *, chaotic: bool) -> Environment:
    """Lighting/palette variation; chaotic worlds favour low-sun and flat-light profiles."""
    weights = (1, 1, 2, 2, 2) if chaotic else (3, 2, 1, 1, 1)
    profile, elevation_range, sun, ambient_scale, sky = rng.choices(_PROFILES, weights)[0]
    elevation = math.radians(rng.uniform(*elevation_range))
    azimuth = rng.uniform(-math.pi, math.pi)
    direction = (-math.cos(elevation) * math.cos(azimuth), -math.cos(elevation) * math.sin(azimuth),
                 -math.sin(elevation))
    brightness = rng.uniform(0.9, 1.05)
    ambient = tuple(round(ambient_scale * rng.uniform(0.95, 1.05), 3) for _ in range(3))
    ground = (rng.uniform(0.85, 1.12), rng.uniform(0.85, 1.1), rng.uniform(0.8, 1.1))
    # Foliage ranges from lush green to a dry late-summer cast.
    dry = rng.random() * (0.35 if not chaotic else 0.6)
    foliage = (1.0 + 0.45 * dry, 1.0 - 0.05 * dry, 1.0 - 0.35 * dry)
    surface = tuple(rng.uniform(0.85, 1.12) for _ in range(3))
    return Environment(
        profile=profile,
        sun_direction=tuple(round(value, 4) for value in direction),
        sun_diffuse=tuple(round(min(1.0, c * brightness), 3) for c in sun),
        sun_specular=tuple(round(c * 0.34, 3) for c in sun),
        ambient=ambient, background=sky,
        ground_tint=tuple(round(v, 3) for v in ground),
        foliage_tint=tuple(round(v, 3) for v in foliage),
        surface_tint=tuple(round(v, 3) for v in surface),
    )
