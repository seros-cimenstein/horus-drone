"""Deterministic procedural PNG textures for generated scenery (pure standard library).

Everything here is drawn from ``seeding.substream`` streams keyed by the world seed and the
texture name, so a world seed always yields byte-identical PNGs and adding one texture never
shifts another. Nothing uses the global ``random`` module, wall-clock time or NumPy/Pillow.

UV conventions (world metres per texture repeat)
------------------------------------------------
* Facades (``facade_texture``): one repeat covers exactly one facade bay wide by one storey
  tall, i.e. ``FACADE_BAY_M`` (3.2 m) x ``FLOOR_HEIGHT_M`` (3.2 m) — the generators model
  floors at 3.2 m. A wall of width W and height H therefore repeats the texture
  ``u = W / FACADE_BAY_M`` times horizontally and ``v = H / FLOOR_HEIGHT_M`` times vertically
  (``facade_uv``); the window grid visible in the world emerges from tiling.
* Roof (``roof_texture``): one repeat is ``ROOF_TILE_M`` (4 m) square.
* Asphalt (``asphalt_texture``): u spans the carriageway, one repeat = ``ROAD_TILE_M`` (8 m)
  across; v runs along the road, one repeat = ``ROAD_TILE_M``. Lane markings sit at fixed u.
* Crosswalk (``crosswalk_texture``): u runs across the crossing, one repeat = one stripe pair
  (``CROSSWALK_PITCH_M`` = 1.2 m: 0.6 m paint + 0.6 m gap); v runs along the walking
  direction and repeats every 1 m (the stripes are continuous along v).
* Pavers (``pavers_texture``): one repeat is 2 x 2 slabs of ``PAVER_SLAB_M`` (0.6 m), i.e.
  1.2 m square.
* Plaza stone (``plaza_stone_texture``): one repeat is 2 x 2 large slabs, i.e. ``PLAZA_TILE_M``
  (2.4 m) square.
* Park path (``park_path_texture``): u spans the path width (``PARK_PATH_WIDTH_M`` = 2.4 m)
  and blends into lawn at both u edges; v repeats every 1 m along the walk.
* Park lawn (``park_lawn_texture``): seamless mown grass, ``GROUND_TILE_M`` (8 m) per repeat.
* Bus-shelter glass (``bus_shelter_glass_texture``): one repeat is one 1.2 m pane with a dark
  frame; walls repeat it with u along the wall and v up the pane.
* Forest floor (``forest_floor_texture``): one repeat is ``GROUND_TILE_M`` (8 m) square and is
  seamless, so a terrain strip tiles it in both axes.
* Dirt trail (``dirt_trail_texture``): u spans the trail width (``TRAIL_WIDTH_M`` = 2.4 m),
  v repeats every 1 m along the trail; the trail blends into forest floor at both u edges.
* Creek (``creek_texture``): u spans the creek width (``CREEK_WIDTH_M`` = 2.6 m) with stony
  banks at the edges and water in the middle; v repeats every 1 m along the flow.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

from sim.worldgen.png import png_rgb
from sim.worldgen.seeding import substream

MAX_TEXTURE_PX = 512
DEFAULT_TEXTURE_PX = 256

FACADE_BAY_M = 3.2
FLOOR_HEIGHT_M = 3.2
ROOF_TILE_M = 4.0
ROAD_TILE_M = 8.0
CROSSWALK_PITCH_M = 1.2
PAVER_SLAB_M = 0.6
PLAZA_TILE_M = 2.4
PARK_PATH_WIDTH_M = 2.4
GROUND_TILE_M = 8.0
TRAIL_WIDTH_M = 2.4
CREEK_WIDTH_M = 2.6

FACADE_STYLES = ("brick", "concrete", "glass", "stucco", "panel")
ROOF_VARIANTS = ("gravel", "membrane")
ASPHALT_VARIANTS = ("plain", "center_dashed", "edge_lines")

# Window openings per style, as (u0, u1, v0, v1) fractions of one bay x storey repeat
# (v is measured from the bottom of the repeat).
_FACADE_WINDOWS = {
    "brick": (0.22, 0.78, 0.30, 0.84),
    "concrete": (0.20, 0.80, 0.28, 0.76),
    "glass": (0.03, 0.97, 0.06, 0.94),
    "stucco": (0.24, 0.76, 0.30, 0.84),
    "panel": (0.10, 0.90, 0.34, 0.80),
}


def facade_uv(width_m: float, height_m: float) -> tuple[float, float]:
    """UV repeat counts for a facade wall of ``width_m`` x ``height_m`` metres.

    Returns ``(u, v)`` such that a mesh quad of that size shows exactly
    ``width_m / FACADE_BAY_M`` bays horizontally and ``height_m / FLOOR_HEIGHT_M`` storeys
    vertically (each texture repeat is one bay x one floor).
    """
    if width_m <= 0.0 or height_m <= 0.0:
        raise ValueError("facade dimensions must be positive")
    return width_m / FACADE_BAY_M, height_m / FLOOR_HEIGHT_M


def _check_size(size: int) -> int:
    if not 16 <= size <= MAX_TEXTURE_PX or size % 8:
        raise ValueError(f"texture size must be a multiple of 8 in 16..{MAX_TEXTURE_PX}, got {size!r}")
    return int(size)


def _render(size: int, pixel: Callable[[float, float], tuple[float, float, float]]) -> bytes:
    """Encode one texel callback as an RGB PNG; ``u`` runs +x, ``v`` runs up from the bottom."""
    out = bytearray(size * size * 3)
    index = 0
    for row in range(size):
        v = 1.0 - (row + 0.5) / size
        for col in range(size):
            u = (col + 0.5) / size
            red, green, blue = pixel(u, v)
            out[index] = min(255, max(0, int(red)))
            out[index + 1] = min(255, max(0, int(green)))
            out[index + 2] = min(255, max(0, int(blue)))
            index += 3
    return png_rgb(size, size, bytes(out))


def _lattice(rng, cells: int) -> list[list[float]]:
    return [[rng.random() for _ in range(cells)] for _ in range(cells)]


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _sample(grid: list[list[float]], u: float, v: float) -> float:
    """Seamless bilinear sample of a tiling ``cells x cells`` lattice (u, v in [0, 1])."""
    cells = len(grid)
    gx, gy = u * cells, v * cells
    x0, y0 = int(gx) % cells, int(gy) % cells
    x1, y1 = (x0 + 1) % cells, (y0 + 1) % cells
    tx, ty = _smoothstep(gx - int(gx)), _smoothstep(gy - int(gy))
    top = grid[y0][x0] * (1.0 - tx) + grid[y0][x1] * tx
    bottom = grid[y1][x0] * (1.0 - tx) + grid[y1][x1] * tx
    return top * (1.0 - ty) + bottom * ty


def _fbm(grids: Sequence[list[list[float]]], u: float, v: float) -> float:
    """Weighted seamless fractal noise in [0, 1]; weights halve per octave."""
    total, weight, score = 0.0, 1.0, 0.0
    for grid in grids:
        score += weight * _sample(grid, u, v)
        total += weight
        weight *= 0.5
    return score / total


def facade_texture(style: str, seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """One bay x one storey of a building facade, with the style's window grid.

    UV convention: a wall of width W and height H uses ``u = W / FACADE_BAY_M`` and
    ``v = H / FLOOR_HEIGHT_M`` (see ``facade_uv``), so the window grid tiles once per bay and
    once per floor. Styles: ``brick``, ``concrete``, ``glass``, ``stucco``, ``panel``.
    """
    if style not in FACADE_STYLES:
        raise ValueError(f"unknown facade style {style!r}; expected one of {FACADE_STYLES}")
    _check_size(size)
    rng = substream(seed, "scenery_texture", f"facade_{style}")
    coarse = _lattice(rng, 8)
    fine = _lattice(rng, 32)
    pattern = _lattice(rng, 16)
    bricks = [[rng.random() for _ in range(9)] for _ in range(15)]
    win_x0, win_x1, win_y0, win_y1 = _FACADE_WINDOWS[style]
    frame = 0.035
    sill_top, sill_bottom = win_y0 + 0.015, win_y0 - 0.045

    def wall(u: float, v: float) -> tuple[float, float, float]:
        noise = _fbm((coarse, fine), u, v)
        if style == "brick":
            courses, per_course = 14, 7
            row = int(v * courses)
            shifted = u * per_course + (0.5 if row % 2 else 0.0)
            if (v * courses) % 1.0 < 0.06 or shifted % 1.0 < 0.05:
                shade = 178 + 22 * pattern[row % 16][int(shifted * 16) % 16]
                return shade, shade - 8, shade - 18
            tint = bricks[row % 15][int(shifted) % 9]
            return 132 + 34 * tint + 8 * noise, 76 + 20 * tint + 6 * noise, 58 + 16 * tint + 5 * noise
        if style == "concrete":
            shade = 158 + 34 * noise
            if (v * 6.0) % 1.0 < 0.04:
                shade -= 16
            streak = _sample(fine, u * 7.0 % 1.0, v * 0.35 % 1.0)
            shade -= 10 * streak
            return shade, shade + 1.5, shade + 1.5
        if style == "glass":
            if v < 0.12:  # spandrel band between storeys
                shade = 52 + 14 * noise
                return shade, shade + 4, shade + 8
            glass = 96 + 42 * v + 24 * noise
            if abs(u - 0.5) < 0.012:
                glass -= 34
            return glass * 0.72, glass * 0.9, glass
        if style == "stucco":
            shade = 206 + 24 * noise + 14 * (_sample(fine, u * 5.0 % 1.0, v) - 0.5)
            if _sample(fine, u, v * 0.5) < 0.06:
                shade -= 26
            return shade, shade - 6, shade - 20
        shade = 138 + 18 * noise
        if (u * 6.0) % 1.0 < 0.03 or (v * 3.0) % 1.0 < 0.02:
            shade -= 30
        return shade, shade + 5, shade + 12

    def glass(u: float, v: float) -> tuple[float, float, float]:
        rise = (v - win_y0) / max(1e-6, win_y1 - win_y0)
        shade = 44 + 34 * rise + 16 * _fbm((coarse, fine), u, v)
        streak = (u * 2.3 + (1.0 - rise) * 1.7) % 1.0
        if streak < 0.11:
            shade += 34
        if abs(u - 0.5) < 0.014 or abs(rise - 0.5) < 0.025:  # mullions
            shade -= 22
        return shade * 0.82, shade * 0.98, shade + 10

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        if win_x0 <= u <= win_x1 and win_y0 <= v <= win_y1:
            return glass(u, v)
        if (win_x0 - frame <= u <= win_x1 + frame and win_y0 - frame <= v <= win_y1 + frame):
            if style == "glass":
                return 62.0, 66.0, 72.0
            return 224.0, 222.0, 214.0
        if style in ("brick", "concrete", "stucco") and win_x0 - 0.05 <= u <= win_x1 + 0.05 \
                and sill_bottom <= v <= sill_top:
            return 196.0, 190.0, 178.0  # sill
        return wall(u, v)

    return _render(size, pixel)


def roof_texture(seed: int, *, variant: str = "gravel", size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """A flat roof, ``ROOF_TILE_M`` (4 m) per repeat: ``gravel`` ballast or ``membrane``."""
    if variant not in ROOF_VARIANTS:
        raise ValueError(f"unknown roof variant {variant!r}; expected one of {ROOF_VARIANTS}")
    _check_size(size)
    rng = substream(seed, "scenery_texture", f"roof_{variant}")
    coarse = _lattice(rng, 10)
    fine = _lattice(rng, 40)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        noise = _fbm((coarse, fine), u, v)
        if variant == "gravel":
            shade = 96 + 76 * noise
            if _sample(fine, u * 3.0 % 1.0, v * 3.0 % 1.0) > 0.86:
                shade += 26
            return shade, shade - 3, shade - 9
        shade = 68 + 16 * noise
        if (u * 4.0) % 1.0 < 0.02 or (v * 2.0) % 1.0 < 0.015:  # membrane seams
            shade -= 16
        return shade, shade + 1, shade + 4

    return _render(size, pixel)


def asphalt_texture(seed: int, *, variant: str = "plain", size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Asphalt with optional lane markings.

    ``variant``: ``plain`` (no paint), ``center_dashed`` (a dashed centre line) or
    ``edge_lines`` (solid lines near both edges). UV convention: u spans the carriageway,
    one repeat is ``ROAD_TILE_M`` (8 m) across; v runs along the road, one repeat is 8 m.
    """
    if variant not in ASPHALT_VARIANTS:
        raise ValueError(f"unknown asphalt variant {variant!r}; expected one of {ASPHALT_VARIANTS}")
    _check_size(size)
    rng = substream(seed, "scenery_texture", f"asphalt_{variant}")
    coarse = _lattice(rng, 12)
    fine = _lattice(rng, 48)
    grit = _lattice(rng, 96)

    def paint(u: float, v: float) -> bool:
        if variant == "center_dashed":
            return abs(u - 0.5) < 0.016 and (v % 1.0) < 0.375
        if variant == "edge_lines":
            return (abs(u - 0.06) < 0.013 or abs(u - 0.94) < 0.013) and (v % 1.0) > 0.01
        return False

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        noise = _fbm((coarse, fine), u, v)
        shade = 52 + 26 * noise
        wheel = _sample(coarse, u * 2.0 % 1.0, v * 0.25 % 1.0)
        if abs(u - 0.22) < 0.08 or abs(u - 0.78) < 0.08:  # polished wheel tracks
            shade += 6 * wheel
        if paint(u, v):
            wear = _sample(grit, u, v)
            if wear > 0.12:
                paint_shade = 196 + 40 * wear
                return paint_shade, paint_shade - 4, paint_shade - 18
        return shade, shade, shade + 3

    return _render(size, pixel)


def crosswalk_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Crosswalk stripes on asphalt.

    UV convention: u runs across the crossing (one repeat is one stripe pair,
    ``CROSSWALK_PITCH_M`` = 1.2 m: 0.6 m paint + 0.6 m gap), v runs along the walking
    direction and repeats every 1 m; stripes are continuous along v and slightly worn.
    """
    _check_size(size)
    rng = substream(seed, "scenery_texture", "crosswalk")
    fine = _lattice(rng, 40)
    grit = _lattice(rng, 96)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        base = 50 + 22 * _fbm((fine,), u, v)
        if u < 0.5:
            wear = _sample(grit, u, v)
            if wear > 0.1:
                shade = 190 + 46 * wear
                if abs(u - 0.5) < 0.03 or u < 0.02:  # worn stripe edges
                    shade -= 60 * (0.03 - abs(u - 0.5) if abs(u - 0.5) < 0.03 else 0.02 - u)
                return shade, shade - 3, shade - 16
        return base, base, base + 2

    return _render(size, pixel)


def pavers_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Concrete sidewalk pavers: one repeat is 2 x 2 slabs of ``PAVER_SLAB_M`` (0.6 m)."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "pavers")
    slabs = [[rng.random() for _ in range(2)] for _ in range(2)]
    fine = _lattice(rng, 32)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        joint = (u * 2.0) % 1.0 < 0.025 or (v * 2.0) % 1.0 < 0.025
        if joint:
            shade = 132 + 14 * _sample(fine, u, v)
            return shade, shade - 2, shade - 6
        tint = slabs[int(v * 2.0) % 2][int(u * 2.0) % 2]
        shade = 158 + 26 * tint + 14 * (_sample(fine, u, v) - 0.5)
        return shade, shade - 1, shade - 4

    return _render(size, pixel)


def forest_floor_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Seamless forest floor: grass, leaf litter and soil mixed; ``GROUND_TILE_M`` per repeat."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "forest_floor")
    grass = _lattice(rng, 16)
    litter = _lattice(rng, 24)
    soil = _lattice(rng, 6)
    specks = _lattice(rng, 64)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        grass_noise = _fbm((grass, litter), u, v)
        red, green, blue = 64 + 34 * grass_noise, 84 + 34 * grass_noise, 42 + 22 * grass_noise
        litter_mix = max(0.0, min(1.0, (_sample(litter, u * 0.7 % 1.0, v * 0.7 % 1.0) - 0.52) * 4.0))
        if litter_mix:
            leaf = _sample(specks, u, v)
            red += (78 + 60 * leaf - red / 3.0) * litter_mix * 0.5
            green += (56 + 40 * leaf - green / 3.0) * litter_mix * 0.5
            blue += (28 + 20 * leaf - blue / 3.0) * litter_mix * 0.5
        soil_mix = max(0.0, min(1.0, (0.34 - _sample(soil, u, v)) * 3.0))
        if soil_mix:
            red += (96 - red / 2.0) * soil_mix * 0.6
            green += (74 - green / 2.0) * soil_mix * 0.6
            blue += (50 - blue / 2.0) * soil_mix * 0.6
        if _sample(specks, u * 1.3 % 1.0, v * 1.3 % 1.0) > 0.965:  # twigs and stones
            return 128.0, 118.0, 96.0
        return red, green, blue

    return _render(size, pixel)


def dirt_trail_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """A worn dirt trail blending into forest floor at both u edges (``TRAIL_WIDTH_M`` wide)."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "dirt_trail")
    coarse = _lattice(rng, 10)
    fine = _lattice(rng, 40)
    pebbles = _lattice(rng, 80)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        noise = _fbm((coarse, fine), u, v)
        red, green, blue = 104 + 52 * noise, 78 + 40 * noise, 50 + 26 * noise
        if _sample(pebbles, u, v) > 0.9:
            return 128.0, 122.0, 110.0
        if abs(u - 0.28) < 0.09 or abs(u - 0.72) < 0.09:  # wheel ruts
            red -= 10
            green -= 8
            blue -= 6
        edge = max(0.0, min(1.0, (0.09 - min(u, 1.0 - u)) / 0.09))
        if edge:
            mix = _sample(coarse, u, v)
            return (red * (1.0 - edge) + (70 + 26 * mix) * edge,
                    green * (1.0 - edge) + (88 + 26 * mix) * edge,
                    blue * (1.0 - edge) + (44 + 18 * mix) * edge)
        return red, green, blue

    return _render(size, pixel)


def creek_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Water with ripples down the middle and stony banks at the u edges (``CREEK_WIDTH_M``)."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "creek")
    stones = _lattice(rng, 24)
    grit = _lattice(rng, 64)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        if 0.16 <= u <= 0.84:
            depth = min(u - 0.16, 0.84 - u) / 0.34
            ripple = 0.5 + 0.5 * math.sin(2.0 * math.pi * (2.5 * u + 2.0 * v))
            shade = 40 + 34 * depth + 26 * ripple * _sample(grit, u, v)
            if _sample(stones, u * 1.5 % 1.0, v * 1.5 % 1.0) > 0.94:  # stones breaking surface
                return 132.0, 128.0, 118.0
            return shade * 0.7, shade + 26, shade + 30
        bank = 116 + 40 * _sample(stones, u, v) + 16 * (_sample(grit, u, v) - 0.5)
        if _sample(grit, u * 2.0 % 1.0, v * 2.0 % 1.0) > 0.9:
            bank += 30
        return bank, bank - 4, bank - 14

    return _render(size, pixel)


def bark_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Rough brown bark: vertical ridges and cracks that tile around a trunk (u wraps)."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "bark")
    coarse = _lattice(rng, 10)
    ridges = _lattice(rng, 24)
    cracks = _lattice(rng, 48)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        wiggle = 0.16 * (_sample(coarse, u, v * 0.25 % 1.0) - 0.5)
        ridge = math.sin(2.0 * math.pi * ((u * 7.0 + wiggle) % 1.0))
        shade = 82 + 30 * _fbm((coarse, ridges), u, v) + 14 * ridge
        crack = _sample(cracks, u * 2.0 % 1.0, v * 0.5 % 1.0)
        if abs(((u * 9.0 + wiggle * 2.0) % 1.0) - 0.5) < 0.035 and crack > 0.25:
            shade -= 46  # deep fissure
        if _sample(ridges, u, v * 3.0 % 1.0) > 0.93 and v > 2.0 / 3.0:  # moss flecks
            return shade * 0.72, shade * 0.86, shade * 0.55
        return shade, shade * 0.68, shade * 0.46

    return _render(size, pixel)


def wood_planks_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Planed wood planks with grain along u; four planks per repeat (v)."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "wood_planks")
    grain = _lattice(rng, 32)
    knots = _lattice(rng, 12)
    tint = _lattice(rng, 4)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        plank = int(v * 4.0) % 4
        if (v * 4.0) % 1.0 < 0.045:  # gap between planks
            shade = 58 + 20 * _sample(grain, u, v)
            return shade, shade * 0.72, shade * 0.5
        base = 148 + 26 * tint[plank][int(u * 4.0) % 4]
        streak = _sample(grain, u * 0.35 % 1.0, (v * 4.0 % 1.0) * 0.6 % 1.0)
        shade = base + 26 * (streak - 0.5) + 10 * (_fbm((grain, knots), u, v) - 0.5)
        if _sample(knots, u, v * 2.0 % 1.0) > 0.965:  # knot
            shade -= 52
        return shade, shade * 0.76, shade * 0.5

    return _render(size, pixel)


def tent_fabric_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Woven tent canvas (olive) with a light seam grid; one repeat is about 1 m."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "tent_fabric")
    weave = _lattice(rng, 64)
    coarse = _lattice(rng, 8)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        warp = 1.0 if (int(u * 48.0) + int(v * 48.0)) % 2 == 0 else 0.0
        shade = 118 + 16 * warp + 14 * (_fbm((coarse,), u, v) - 0.5) + 8 * (_sample(weave, u, v) - 0.5)
        seam = (u * 2.0) % 1.0 < 0.02 or (v * 2.0) % 1.0 < 0.02
        if seam:
            shade -= 18
        return shade * 0.86, shade * 0.94, shade * 0.7

    return _render(size, pixel)


def metal_paint_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Painted metal with fine scratches and grime; tiles per metre."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "metal_paint")
    coarse = _lattice(rng, 8)
    fine = _lattice(rng, 48)
    scratch = _lattice(rng, 96)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        shade = 104 + 22 * _fbm((coarse, fine), u, v)
        if _sample(scratch, (u + v * 0.3) % 1.0, v * 6.0 % 1.0) > 0.965:
            shade += 42  # bright scratch
        if _sample(fine, u * 3.0 % 1.0, v * 3.0 % 1.0) < 0.06:
            shade -= 18  # grime patch
        return shade * 0.92, shade * 0.96, shade

    return _render(size, pixel)


def traffic_cone_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Orange cone plastic with a white reflective band across the middle (v)."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "traffic_cone")
    grit = _lattice(rng, 48)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        noise = 10 * (_fbm((grit,), u, v) - 0.5)
        if 0.44 <= v <= 0.62:
            speck = _sample(grit, u, v)
            shade = 208 + 30 * speck + noise
            return shade, shade, shade - 10
        shade = 226 + noise
        return shade, 92 + 0.4 * noise, 26 + 0.3 * noise

    return _render(size, pixel)


def foliage_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Dense leaf clumps in mixed greens; one repeat is about 1.5 m of foliage."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "foliage")
    clumps = _lattice(rng, 12)
    leaves = _lattice(rng, 32)
    specks = _lattice(rng, 64)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        cover = _fbm((clumps, leaves), u, v)
        if cover > 0.62:
            base = (58 + 34 * cover, 104 + 44 * cover, 40 + 22 * cover)
        elif cover > 0.4:
            base = (38 + 26 * cover, 74 + 34 * cover, 32 + 16 * cover)
        else:
            base = (28 + 18 * cover, 52 + 24 * cover, 26 + 12 * cover)
        if _sample(specks, u, v) > 0.93:
            return base[0] * 1.5, base[1] * 1.45, base[2] * 1.3
        if _sample(specks, u * 0.7 % 1.0, v * 0.7 % 1.0) < 0.05:
            return base[0] * 0.6, base[1] * 0.65, base[2] * 0.6
        return base

    return _render(size, pixel)


def rock_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Grey weathered stone with speckles and hairline cracks; tiles per metre."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "rock")
    coarse = _lattice(rng, 8)
    fine = _lattice(rng, 40)
    specks = _lattice(rng, 96)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        shade = 128 + 34 * _fbm((coarse, fine), u, v)
        speck = _sample(specks, u, v)
        if speck > 0.96:
            shade += 34
        elif speck < 0.05:
            shade -= 30
        crack = _sample(fine, (u * 3.0 + v) % 1.0, v * 0.5 % 1.0)
        if abs(((u * 5.0 + 0.2 * _sample(coarse, u, v)) % 1.0) - 0.5) < 0.012 and crack > 0.35:
            shade -= 44
        return shade, shade * 0.99, shade * 0.95

    return _render(size, pixel)


def fur_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Neutral grey fur (directional strands); tint it with the material diffuse colour.

    The mean is kept near 0.72 so a coat-colour tint multiplies to the intended shade.
    """
    _check_size(size)
    rng = substream(seed, "scenery_texture", "fur")
    strands = _lattice(rng, 64)
    coarse = _lattice(rng, 10)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        strand = _sample(strands, (u * 6.0 + v * 1.5) % 1.0, v * 3.0 % 1.0)
        clump = _fbm((coarse, strands), u, v)
        shade = 168 + 62 * (strand - 0.5) + 52 * (clump - 0.5)
        if clump < 0.30:
            shade -= 30  # shaded undercoat
        elif clump > 0.74:
            shade += 24  # sunlit tip
        return shade, shade * 0.98, shade * 0.94

    return _render(size, pixel)


def feather_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Neutral grey feather scales (overlapping barb rows); tint per bird species."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "feather")
    noise = _lattice(rng, 32)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        rows = 9
        row = int(v * rows)
        offset = 0.5 if row % 2 else 0.0
        cu = ((u * 7.0 + offset) % 1.0) - 0.5
        cv = ((v * rows) % 1.0) - 0.5
        edge = math.hypot(cu, cv * 0.65)
        shade = 164 + 46 * (_sample(noise, u, v) - 0.5)
        if edge > 0.36:
            shade -= 48  # dark feather edge
        elif edge < 0.12:
            shade += 28  # quill highlight
        return shade, shade * 0.99, shade * 0.97

    return _render(size, pixel)


def plaza_stone_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """City plaza stone: 2 x 2 large slabs of ``PLAZA_TILE_M / 2`` (1.2 m) per repeat."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "plaza_stone")
    slabs = [[rng.random() for _ in range(2)] for _ in range(2)]
    fine = _lattice(rng, 48)
    mottle = _lattice(rng, 12)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        joint = (u * 2.0) % 1.0 < 0.02 or (v * 2.0) % 1.0 < 0.02
        flank = (u * 2.0) % 1.0 < 0.045 or (v * 2.0) % 1.0 < 0.045
        if joint:
            shade = 118 + 12 * _sample(fine, u, v)
            return shade, shade - 1, shade - 5
        if flank:
            shade = 176 + 12 * _sample(fine, u, v)
            return shade, shade - 2, shade - 8
        tint = slabs[int(v * 2.0) % 2][int(u * 2.0) % 2]
        speck = _sample(mottle, u * 1.7 % 1.0, v * 1.7 % 1.0)
        shade = 150 + 26 * tint + 16 * (_sample(fine, u, v) - 0.5) - 10 * speck
        return shade + 4, shade + 1, shade - 8

    return _render(size, pixel)


def park_path_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """A park walk blending into lawn at both u edges (``PARK_PATH_WIDTH_M`` = 2.4 m wide).

    One repeat spans the path width in u and 1 m along the walk in v, like the dirt trail.
    """
    _check_size(size)
    rng = substream(seed, "scenery_texture", "park_path")
    coarse = _lattice(rng, 10)
    fine = _lattice(rng, 40)
    grit = _lattice(rng, 80)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        noise = _fbm((coarse, fine), u, v)
        red, green, blue = 152 + 40 * noise, 136 + 34 * noise, 112 + 26 * noise
        if _sample(grit, u, v) > 0.9:
            return 168.0, 158.0, 138.0
        if abs(u - 0.3) < 0.1 or abs(u - 0.7) < 0.1:  # worn wheel/foot lines
            red -= 12
            green -= 10
            blue -= 8
        edge = max(0.0, min(1.0, (0.1 - min(u, 1.0 - u)) / 0.1))
        if edge:
            mix = _sample(coarse, u, v)
            return (red * (1.0 - edge) + (74 + 24 * mix) * edge,
                    green * (1.0 - edge) + (104 + 30 * mix) * edge,
                    blue * (1.0 - edge) + (52 + 18 * mix) * edge)
        return red, green, blue

    return _render(size, pixel)


def park_lawn_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Mown park lawn: seamless ``GROUND_TILE_M`` (8 m) repeat, brighter than the forest floor."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "park_lawn")
    blades = _lattice(rng, 24)
    patches = _lattice(rng, 8)
    specks = _lattice(rng, 64)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        lawn = _fbm((blades, patches), u, v)
        stripes = 0.5 + 0.5 * math.sin(2.0 * math.pi * (v * 4.0 + 0.5 * math.sin(2.0 * math.pi * u)))
        shade = 88 + 46 * lawn + 10 * (stripes - 0.5)
        if _sample(specks, u * 1.4 % 1.0, v * 1.4 % 1.0) > 0.94:  # daisies and dry grass
            fleck = _sample(specks, u, v)
            return shade + 40 * fleck, shade + 52 * fleck, shade * 0.7
        return shade * 0.78, shade + 26, shade * 0.5

    return _render(size, pixel)


def bus_shelter_glass_texture(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> bytes:
    """Shelter glass: a pale bluish pane with a dark frame and a diagonal sheen; 1.2 m panel."""
    _check_size(size)
    rng = substream(seed, "scenery_texture", "bus_shelter_glass")
    streaks = _lattice(rng, 16)
    grit = _lattice(rng, 48)

    def pixel(u: float, v: float) -> tuple[float, float, float]:
        frame = min(u, 1.0 - u, v, 1.0 - v) < 0.06
        if frame:
            shade = 62 + 14 * _sample(grit, u, v)
            return shade, shade + 3, shade + 7
        pane = 138 + 26 * _fbm((streaks, grit), u, v)
        sheen = (u * 0.8 + v * 1.6) % 1.0
        if sheen < 0.18:
            pane += 36 * (0.18 - sheen) / 0.18
        if abs(u - 0.5) < 0.02 or abs(v - 0.5) < 0.02:  # mullions
            pane -= 30
        return pane * 0.84, pane * 0.94, pane

    return _render(size, pixel)


def scene_textures(seed: int, *, size: int = DEFAULT_TEXTURE_PX) -> dict[str, bytes]:
    """The standard texture set for generated scenery: name -> PNG bytes.

    Names double as OBJ material names, so a ``SceneryMesh`` whose ``texture`` is one of these
    keys renders with the matching map. All textures are deterministic in ``seed``.
    """
    _check_size(size)
    textures = {f"facade_{style}": facade_texture(style, seed, size=size)
                for style in FACADE_STYLES}
    textures.update((f"roof_{variant}", roof_texture(seed, variant=variant, size=size))
                    for variant in ROOF_VARIANTS)
    textures.update((f"asphalt_{variant}", asphalt_texture(seed, variant=variant, size=size))
                    for variant in ASPHALT_VARIANTS)
    textures["crosswalk"] = crosswalk_texture(seed, size=size)
    textures["pavers"] = pavers_texture(seed, size=size)
    textures["forest_floor"] = forest_floor_texture(seed, size=size)
    textures["dirt_trail"] = dirt_trail_texture(seed, size=size)
    textures["creek"] = creek_texture(seed, size=size)
    textures["bark"] = bark_texture(seed, size=size)
    textures["wood_planks"] = wood_planks_texture(seed, size=size)
    textures["tent_fabric"] = tent_fabric_texture(seed, size=size)
    textures["metal_paint"] = metal_paint_texture(seed, size=size)
    textures["traffic_cone"] = traffic_cone_texture(seed, size=size)
    textures["foliage"] = foliage_texture(seed, size=size)
    textures["rock"] = rock_texture(seed, size=size)
    textures["plaza_stone"] = plaza_stone_texture(seed, size=size)
    textures["park_path"] = park_path_texture(seed, size=size)
    textures["park_lawn"] = park_lawn_texture(seed, size=size)
    textures["bus_shelter_glass"] = bus_shelter_glass_texture(seed, size=size)
    return textures


__all__ = [
    "ASPHALT_VARIANTS", "CREEK_WIDTH_M", "CROSSWALK_PITCH_M", "DEFAULT_TEXTURE_PX",
    "FACADE_BAY_M", "FACADE_STYLES", "FLOOR_HEIGHT_M", "GROUND_TILE_M", "MAX_TEXTURE_PX",
    "PAVER_SLAB_M", "PLAZA_TILE_M", "PARK_PATH_WIDTH_M", "ROAD_TILE_M", "ROOF_TILE_M",
    "ROOF_VARIANTS", "TRAIL_WIDTH_M",
    "asphalt_texture", "bark_texture", "bus_shelter_glass_texture", "creek_texture",
    "crosswalk_texture", "dirt_trail_texture", "facade_texture", "facade_uv", "feather_texture",
    "foliage_texture", "forest_floor_texture", "fur_texture", "metal_paint_texture",
    "park_lawn_texture", "park_path_texture", "pavers_texture", "plaza_stone_texture",
    "rock_texture", "roof_texture", "scene_textures", "tent_fabric_texture",
    "traffic_cone_texture", "wood_planks_texture",
]
