"""Seeded human appearance: body mesh, proportions, skin, hair, clothing and accessories.

Two CC0 MakeHuman bodies exist in the tree (OpenRobotics "Standing person" and "Casual
female"); variation comes from non-uniform body scale, skin/hair material tints, a
generated per-actor garment texture over each mesh's own clothing UVs, trouser tint, and
primitive accessories. This is simulation dressing, not a demographic model: labels such as
``skin_tone`` are rendering parameters for coverage reporting and are never given to the
onboard runtime.

``shirt_colour_attribute`` is the *design* expectation for HORUS's red/green/blue torso
classifier: set only for solid, strongly saturated red/green/blue tops, otherwise None.
``possible_shirt_colours`` lists every classifier colour that a garment could plausibly
produce (e.g. orange may read as red, hi-vis lime as green), and is what uniqueness checks
use. Neither is a camera measurement; campaigns record the measured attribute separately.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import random

RGB = tuple[int, int, int]

# name -> (rgb, classifier colours this fabric could plausibly read as)
COLOURS: dict[str, tuple[RGB, tuple[str, ...]]] = {
    "red": ((200, 28, 36), ("red",)), "crimson": ((165, 18, 34), ("red",)),
    "scarlet": ((226, 52, 40), ("red",)), "orange": ((240, 108, 22), ("red",)),
    "green": ((42, 150, 62), ("green",)), "kelly_green": ((70, 176, 50), ("green",)),
    "lime": ((160, 214, 40), ("green",)), "blue": ((32, 74, 186), ("blue",)),
    "royal_blue": ((44, 96, 210), ("blue",)), "sky_blue": ((110, 160, 214), ("blue",)),
    "navy": ((26, 36, 72), ("blue",)), "teal": ((24, 120, 124), ("green", "blue")),
    "maroon": ((110, 28, 40), ("red",)), "pink": ((222, 120, 150), ("red",)),
    "purple": ((96, 52, 140), ("blue", "red")), "yellow": ((232, 204, 44), ()),
    "mustard": ((196, 150, 40), ()), "olive": ((104, 108, 52), ("green",)),
    "khaki": ((176, 160, 118), ()), "beige": ((210, 196, 168), ()),
    "brown": ((104, 70, 44), ()), "white": ((236, 236, 232), ()),
    "light_grey": ((184, 186, 186), ()), "grey": ((128, 130, 132), ()),
    "charcoal": ((62, 64, 68), ()), "black": ((24, 24, 26), ()),
    "hivis_lime": ((200, 238, 24), ("green",)), "hivis_orange": ((250, 110, 18), ("red",)),
    "reflective_silver": ((200, 204, 206), ()), "woodland_green": ((74, 88, 50), ("green",)),
    "woodland_brown": ((92, 70, 48), ()), "sand": ((196, 172, 128), ()),
    "stone": ((150, 146, 132), ()),
}
PRIMARY = {"red": ("red", "crimson", "scarlet"), "green": ("green", "kelly_green"),
           "blue": ("blue", "royal_blue")}
NEUTRALS = ("white", "light_grey", "grey", "charcoal", "black", "beige", "khaki", "brown",
            "navy", "olive", "stone")
ACCENTS = ("orange", "yellow", "mustard", "teal", "maroon", "pink", "purple", "sky_blue", "lime")
TOP_STYLES = ("solid", "heather", "stripes", "plaid", "camo", "wide_band", "hivis", "graphic")
TROUSERS = {"blue_denim": (70, 96, 150), "dark_denim": (40, 52, 84), "black": (34, 34, 36),
            "khaki": (170, 150, 108), "grey": (112, 114, 116), "olive": (90, 94, 58),
            "brown": (96, 70, 48), "navy": (34, 42, 70), "stone": (160, 154, 138)}
# Multipliers over the light-skinned CC0 base textures, light to dark.
SKIN_TONES = ((1.00, 1.00, 1.00), (0.94, 0.88, 0.84), (0.84, 0.72, 0.62), (0.72, 0.57, 0.46),
              (0.55, 0.41, 0.32), (0.38, 0.28, 0.22))
HAIR = {"black": (30, 26, 24), "dark_brown": (64, 44, 30), "brown": (110, 76, 46),
        "auburn": (130, 60, 34), "blond": (200, 170, 110), "grey": (150, 150, 148),
        "white": (220, 218, 212)}
BODIES = {
    # Measured mesh envelope with the model facing +x (see people.FACING_YAW): forward extent
    # (back, front) in metres, half width, total height incl. the 0.02 m visual lift, head centre
    # forward offset. Scaled per actor by width (x, y) and stature (z).
    "male_a": {"height_m": 1.926, "forward_m": (-0.071, 0.252), "half_width_m": 0.27,
               "head_forward_m": 0.10, "author": "Marina Kollmitz",
               "source": "OpenRobotics Standing person v3 (CC0 1.0)"},
    "female_a": {"height_m": 1.672, "forward_m": (-0.151, 0.401), "half_width_m": 0.322,
                 "head_forward_m": 0.03, "author": "Rohit Salem",
                 "source": "OpenRobotics Casual female v4 (CC0 1.0)"},
}
TORSO_FRACTION = 0.58  # HORUS's standing torso point (1.1 m) on the 1.9 m reference body
# Seated primitive figure envelope: forward extent, half width, top.
SITTING_ENVELOPE = {"forward_m": (-0.22, 0.46), "half_width_m": 0.22, "height_m": 1.17}


@dataclass(frozen=True)
class Garment:
    style: str
    colours: tuple[str, ...]
    scale_px: int = 12

    def rgb(self, index: int) -> RGB:
        return COLOURS[self.colours[index % len(self.colours)]][0]


@dataclass(frozen=True)
class Appearance:
    body: str
    stature_scale: float
    width_scale: float
    skin_tone: int
    skin_multiplier: tuple[float, float, float]
    hair: str | None
    top: Garment
    trousers: str
    accessories: tuple[tuple[str, str], ...]
    shirt_colour_attribute: str | None
    possible_shirt_colours: tuple[str, ...]

    def envelope(self, posture: str) -> dict[str, float]:
        """Model-frame box that bounds the rendered body (x forward, z up) and its oracle points."""
        base = BODIES[self.body]
        s, w = self.stature_scale, self.width_scale
        height = base["height_m"] * s
        back, front = base["forward_m"]
        if posture == "standing":
            x0, x1, half, top = back * w, front * w, base["half_width_m"] * w, height
            if any(kind == "backpack" for kind, _colour in self.accessories):
                x0 = back * w - 0.18
            torso = height * TORSO_FRACTION
        elif posture == "sitting":
            env = SITTING_ENVELOPE
            x0, x1 = env["forward_m"][0] * w, env["forward_m"][1] * s
            half, top, torso = env["half_width_m"] * w, env["height_m"] * s, 0.7 * s
        elif posture == "lying":
            # Face-down along the model y axis; depth is the body's front-to-back extent.
            half_length = height / 2
            return {"x_min_m": round(-base["half_width_m"] * w, 3), "x_max_m": round(base["half_width_m"] * w, 3),
                    "y_min_m": round(-half_length, 3), "y_max_m": round(half_length, 3),
                    "top_z_m": round((front - back) * w, 3), "torso_z_m": round((front - back) * w * 0.6, 3),
                    "height_m": round(height, 3)}
        else:
            raise ValueError(f"unknown posture {posture!r}")
        return {"x_min_m": round(x0, 3), "x_max_m": round(x1, 3), "y_min_m": round(-half, 3),
                "y_max_m": round(half, 3), "top_z_m": round(top, 3), "torso_z_m": round(torso, 3),
                "height_m": round(height, 3)}

    def manifest(self) -> dict:
        record = asdict(self)
        record["top"] = {"style": self.top.style, "colours": list(self.top.colours),
                         "scale_px": self.top.scale_px}
        record["accessories"] = [{"kind": kind, "colour": colour} for kind, colour in self.accessories]
        record["skin_multiplier"] = list(self.skin_multiplier)
        record["possible_shirt_colours"] = list(self.possible_shirt_colours)
        record["body_source"] = BODIES[self.body]["source"]
        return record


def _possible(colours: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({family for name in colours for family in COLOURS[name][1]}))


def _hivis_pool(exclude: frozenset[str]) -> tuple[str, ...]:
    """Hi-vis base colours that no classifier colour in ``exclude`` can absorb."""
    pool = tuple(name for name in ("hivis_lime", "hivis_orange")
                 if not set(COLOURS[name][1]) & exclude)
    return pool or ("hivis_lime",)


def sample_garment(rng: random.Random, *, nominal: str | None = None, outdoor: bool = False,
                   exclude: frozenset[str] = frozenset(), style: str | None = None) -> Garment:
    """A top garment; ``nominal`` forces a solid saturated primary; ``exclude`` bans colours
    whose possible classifier readings intersect the given set."""
    if nominal is not None:
        return Garment("solid", (rng.choice(PRIMARY[nominal]),))

    def pick(pool: tuple[str, ...]) -> str:
        allowed = [name for name in pool if not set(COLOURS[name][1]) & exclude]
        return rng.choice(allowed or list(NEUTRALS[:5]))

    style = style or rng.choices(TOP_STYLES, (5, 3, 3, 2, 2 if outdoor else 1, 2, 1, 2))[0]
    everything = tuple(PRIMARY["red"] + PRIMARY["green"] + PRIMARY["blue"]) + NEUTRALS + ACCENTS
    if style in ("solid", "heather", "graphic"):
        colours = (pick(everything if rng.random() < 0.5 else NEUTRALS),)
        if style == "graphic":
            colours += (pick(ACCENTS + NEUTRALS),)
    elif style in ("stripes", "wide_band"):
        colours = (pick(everything), pick(NEUTRALS))
    elif style == "plaid":
        colours = (pick(("red", "navy", "green", "brown", "charcoal", "maroon", "blue")),
                   pick(NEUTRALS), pick(("black", "white", "yellow", "navy")))
    elif style == "camo":
        palette = rng.choice((("woodland_green", "woodland_brown", "black", "olive"),
                              ("sand", "khaki", "brown", "stone"), ("grey", "charcoal", "light_grey", "black")))
        colours = tuple(name for name in palette if not set(COLOURS[name][1]) & exclude) or ("grey", "charcoal")
    else:  # hivis workwear: fluorescent base with retro-reflective bands
        base = pick(("hivis_lime", "hivis_orange"))
        colours = (base, "reflective_silver")
    return Garment(style, colours, rng.randint(6, 18))


def sample_appearance(rng: random.Random, *, nominal: str | None = None, child: bool = False,
                      outdoor: bool = False, hivis: bool = False,
                      exclude: frozenset[str] = frozenset()) -> Appearance:
    body = rng.choice(("male_a", "female_a"))
    if child:
        stature = rng.uniform(0.62, 0.74)
        width = stature * rng.uniform(0.95, 1.05)
    else:
        # Scales about each base body, roughly 1.55–2.0 m (male) and 1.45–1.78 m (female).
        stature = rng.uniform(0.84, 1.04) if body == "male_a" else rng.uniform(0.88, 1.07)
        width = rng.uniform(0.9, 1.18)
    tone = rng.randrange(len(SKIN_TONES))
    blend = rng.uniform(0.0, 1.0)
    nxt = SKIN_TONES[min(tone + 1, len(SKIN_TONES) - 1)]
    skin = tuple(round(a + (b - a) * blend * 0.5, 3) for a, b in zip(SKIN_TONES[tone], nxt))
    bald = body == "male_a" and not child and rng.random() < 0.14
    hair_names = list(HAIR) if not child else ["black", "dark_brown", "brown", "blond", "auburn"]
    hair = None if bald else rng.choice(hair_names)
    top = (Garment("hivis", (rng.choice(_hivis_pool(exclude)), "reflective_silver"),
                   rng.randint(6, 18)) if hivis
           else sample_garment(rng, nominal=nominal, outdoor=outdoor, exclude=exclude))
    trousers = rng.choice(tuple(TROUSERS))
    accessories: list[tuple[str, str]] = []
    if rng.random() < (0.45 if outdoor else 0.25):
        accessories.append(("backpack", rng.choice(NEUTRALS + ("orange", "teal", "maroon"))))
    head = rng.random()
    if head < (0.18 if outdoor else 0.12):
        accessories.append(("cap", rng.choice(NEUTRALS + ("orange", "yellow"))))
    elif head < (0.3 if outdoor else 0.16):
        accessories.append(("sun_hat", rng.choice(("khaki", "beige", "olive", "stone"))))
    exclude_colours = [name for _kind, name in accessories if set(COLOURS[name][1]) & exclude]
    if exclude_colours:
        accessories = [(kind, "grey" if colour in exclude_colours else colour) for kind, colour in accessories]
    nominal_colour = nominal if top.style == "solid" and nominal else None
    return Appearance(body, round(stature, 3), round(width, 3), tone, skin, hair, top, trousers,
                      tuple(accessories), nominal_colour, _possible(top.colours))


def _noise(ix: int, iy: int, seed: int) -> float:
    value = (ix * 374761393 + iy * 668265263 + seed * 2147483647) & 0xFFFFFFFF
    value = ((value ^ (value >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((value ^ (value >> 16)) & 0xFFFF) / 65535.0


def _smooth_noise(x: float, y: float, cell: float, seed: int) -> float:
    gx, gy = x / cell, y / cell
    x0, y0 = int(math.floor(gx)), int(math.floor(gy))
    fx, fy = gx - x0, gy - y0
    fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    top = _noise(x0, y0, seed) * (1 - fx) + _noise(x0 + 1, y0, seed) * fx
    bottom = _noise(x0, y0 + 1, seed) * (1 - fx) + _noise(x0 + 1, y0 + 1, seed) * fx
    return top * (1 - fy) + bottom * fy


def garment_pixel(garment: Garment, x: int, y: int, size: int, seed: int) -> RGB:
    style, s = garment.style, max(3, garment.scale_px)
    if style == "solid":
        return garment.rgb(0)
    if style == "heather":
        k = 0.86 + 0.26 * _noise(x, y, seed)
        return tuple(min(255, round(c * k)) for c in garment.rgb(0))
    if style == "stripes":
        return garment.rgb((y // s) % 2)
    if style == "wide_band":
        return garment.rgb(1 if (y % (size // 2)) < size // 7 else 0)
    if style == "graphic":
        inside = 0.30 < x / size < 0.50 and (0.66 < y / size < 0.82 or 0.18 < y / size < 0.32)
        return garment.rgb(1 if inside else 0)
    if style == "plaid":
        a, b = (x // s) % 3 == 0, (y // s) % 3 == 0
        if a and b:
            return garment.rgb(2)
        base, stripe = garment.rgb(0), garment.rgb(1)
        return stripe if a or b else base
    if style == "camo":
        value = _smooth_noise(x, y, s * 2.2, seed) * 0.65 + _smooth_noise(x, y, s * 0.9, seed + 7) * 0.35
        index = min(len(garment.colours) - 1, int(value * len(garment.colours)))
        return garment.rgb(index)
    if style == "hivis":
        band = (y % (size // 3)) < max(4, size // 28)
        return garment.rgb(1 if band else 0)
    raise ValueError(f"unknown garment style {style!r}")


_TEMPLATES: dict[str, tuple[int, int, bytes]] = {}


def _template(path) -> tuple[int, int, bytes]:
    from sim.worldgen.png import read_png

    key = str(path)
    if key not in _TEMPLATES:
        width, height, channels, pixels = read_png(path.read_bytes())
        if channels != 4:
            raise ValueError(f"garment template {path} must be RGBA")
        _TEMPLATES[key] = (width, height, pixels)
    return _TEMPLATES[key]


def garment_texture(template_path, appearance: Appearance, seed: int) -> bytes:
    """Per-actor clothing texture over a body's garment UV template (see import script)."""
    from sim.worldgen.png import png_rgb

    width, height, pixels = _template(template_path)
    sums: dict[int, list[int]] = {}
    for i in range(0, len(pixels), 4):
        bucket = sums.setdefault(pixels[i], [0, 0])
        bucket[0] += pixels[i + 1]
        bucket[1] += 1
    means = {region: max(1.0, total / count) for region, (total, count) in sums.items()}
    trousers = TROUSERS[appearance.trousers]
    trim = appearance.top.rgb(1 if len(appearance.top.colours) > 1 else 0)
    out = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 4
            region, luminance = pixels[i], pixels[i + 1]
            relative = luminance / means[region]
            shade = min(1.15, 0.7 + 0.3 * relative)
            if region == 128:
                colour, shade = trousers, min(1.3, 0.45 + 0.55 * relative)
            elif region == 192:
                colour = trim
            else:
                colour = garment_pixel(appearance.top, x, y, width, seed)
            o = (y * width + x) * 3
            out[o:o + 3] = bytes(max(0, min(255, round(c * shade))) for c in colour)
    return png_rgb(width, height, bytes(out))
