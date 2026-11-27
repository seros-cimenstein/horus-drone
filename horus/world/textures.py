"""Fast vectorized procedural textures and cubemap skyboxes for Built-in 3D worlds."""

from __future__ import annotations

import math
from pathlib import Path
import struct
import zlib

import numpy as np


def write_png(path: Path, rgb: np.ndarray) -> None:
    """Write an RGB uint8 array as a PNG file (OpenCV fast path or stdlib zlib fallback)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import cv2

        if cv2.imwrite(str(path), np.ascontiguousarray(rgb[:, :, ::-1])):
            return
    except Exception:
        pass

    height, width, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def _tileable_fbm(
    size: int,
    *,
    octaves: tuple[tuple[int, float], ...] = ((4, 0.55), (8, 0.25), (16, 0.13), (32, 0.07)),
    seed: int = 0,
) -> np.ndarray:
    """Generate a seamless tileable 2D fractional Brownian noise map in [0, 1] using FFT/bilinear value noise."""
    rng = np.random.default_rng(seed)
    acc = np.zeros((size, size), dtype=np.float32)
    weight_sum = 0.0
    yy = np.arange(size, dtype=np.float32)[:, None]
    xx = np.arange(size, dtype=np.float32)[None, :]
    for freq, weight in octaves:
        grid = rng.random((freq, freq), dtype=np.float32)
        gy = yy * (freq / size)
        gx = xx * (freq / size)
        y0 = np.floor(gy).astype(np.int32) % freq
        x0 = np.floor(gx).astype(np.int32) % freq
        y1 = (y0 + 1) % freq
        x1 = (x0 + 1) % freq
        fy = gy - np.floor(gy)
        fx = gx - np.floor(gx)
        # Quintic smoothstep
        sy = fy * fy * fy * (fy * (fy * 6.0 - 15.0) + 10.0)
        sx = fx * fx * fx * (fx * (fx * 6.0 - 15.0) + 10.0)
        c00 = grid[y0, x0]
        c10 = grid[y0, x1]
        c01 = grid[y1, x0]
        c11 = grid[y1, x1]
        nx0 = c00 * (1.0 - sx) + c10 * sx
        nx1 = c01 * (1.0 - sx) + c11 * sx
        acc += (nx0 * (1.0 - sy) + nx1 * sy) * weight
        weight_sum += weight
    out = acc / max(weight_sum, 1e-6)
    return np.clip(out, 0.0, 1.0)


def _to_rgb_u8(arr: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(arr), 0.0, 255.0).astype(np.uint8)


def _render_skybox_strip(
    face_px: int = 256,
    *,
    zenith: tuple[float, float, float],
    horizon: tuple[float, float, float],
    ground: tuple[float, float, float],
    sun_dir: tuple[float, float, float] = (-0.48, 0.38, 0.78),
    sun_tint: tuple[float, float, float] = (255.0, 245.0, 218.0),
    cloud_strength: float = 0.28,
    seed: int = 11,
) -> np.ndarray:
    """Build a 6x1 vertical cubemap strip (UDLRFB) for MuJoCo ``type='skybox'``."""
    # MuJoCo gridlayout="UDLRFB": +Z (Up), -Z (Down), -X (Left), +X (Right), +Y (Front), -Y (Back)
    u = np.linspace(-1.0, 1.0, face_px, dtype=np.float32)[None, :]
    v = np.linspace(1.0, -1.0, face_px, dtype=np.float32)[:, None]
    uu = np.broadcast_to(u, (face_px, face_px))
    vv = np.broadcast_to(v, (face_px, face_px))

    sn = np.array(sun_dir, dtype=np.float32)
    sn = sn / max(float(np.linalg.norm(sn)), 1e-6)
    z_arr = np.array(zenith, dtype=np.float32)
    h_arr = np.array(horizon, dtype=np.float32)
    g_arr = np.array(ground, dtype=np.float32)
    s_arr = np.array(sun_tint, dtype=np.float32)

    cloud_map = _tileable_fbm(
        face_px,
        octaves=((3, 0.55), (6, 0.28), (12, 0.12), (24, 0.05)),
        seed=seed,
    )

    # Basis vectors per face: (forward, right, up) in world ENU
    faces_basis = (
        ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),   # U (+Z)
        ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),   # D (-Z)
        ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),   # L (-X)
        ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),   # R (+X)
        ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),    # F (+Y)
        ((0.0, -1.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),  # B (-Y)
    )

    strips: list[np.ndarray] = []
    for fwd, right, up in faces_basis:
        dx = fwd[0] + uu * right[0] + vv * up[0]
        dy = fwd[1] + uu * right[1] + vv * up[1]
        dz = fwd[2] + uu * right[2] + vv * up[2]
        inv_len = 1.0 / np.sqrt(dx * dx + dy * dy + dz * dz)
        dx *= inv_len
        dy *= inv_len
        dz *= inv_len

        sky_t = np.clip(dz, 0.0, 1.0) ** 0.55
        gnd_t = np.clip(-dz * 3.2, 0.0, 1.0)
        rgb = h_arr[None, None, :] * (1.0 - sky_t[:, :, None]) + z_arr[None, None, :] * sky_t[:, :, None]
        rgb = rgb * (1.0 - gnd_t[:, :, None]) + g_arr[None, None, :] * gnd_t[:, :, None]

        # Clouds projected onto upper hemisphere
        if cloud_strength > 0.0:
            up_mask = np.clip((dz - 0.05) / 0.25, 0.0, 1.0)
            cx = (dx / np.maximum(dz + 0.35, 0.25)) * 0.45 + 0.5
            cy = (dy / np.maximum(dz + 0.35, 0.25)) * 0.45 + 0.5
            ix = np.mod(np.rint(cx * face_px).astype(np.int32), face_px)
            iy = np.mod(np.rint(cy * face_px).astype(np.int32), face_px)
            cval = np.clip((cloud_map[iy, ix] - 0.50) / 0.32, 0.0, 1.0) * up_mask * cloud_strength
            cloud_col = np.array([250.0, 250.0, 252.0], dtype=np.float32)
            rgb = rgb * (1.0 - cval[:, :, None]) + cloud_col[None, None, :] * cval[:, :, None]

        # Sun glow halo
        dot = np.clip(dx * sn[0] + dy * sn[1] + dz * sn[2], 0.0, 1.0)
        halo = (dot ** 28) * 0.45 + (dot ** 260) * 0.65
        rgb = np.clip(rgb + s_arr[None, None, :] * halo[:, :, None] * 0.65, 0.0, 255.0)
        strips.append(_to_rgb_u8(rgb))

    return np.vstack(strips)


def _tex_terrazzo_floor(size: int = 256) -> np.ndarray:
    base = _tileable_fbm(size, octaves=((4, 0.45), (12, 0.35), (32, 0.20)), seed=101)
    speck = _tileable_fbm(size, octaves=((32, 0.6), (64, 0.4)), seed=102)
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 194.0 + base * 22.0
    rgb[:, :, 1] = 196.0 + base * 22.0
    rgb[:, :, 2] = 198.0 + base * 20.0
    dark_chips = speck > 0.72
    warm_chips = speck < 0.24
    rgb[dark_chips] -= 26.0
    rgb[warm_chips] += np.array([14.0, 10.0, 4.0], dtype=np.float32)
    # 2x2 architectural tile grout lines
    step = size // 2
    yy, xx = np.ogrid[:size, :size]
    grout = ((yy % step) < 3) | ((xx % step) < 3)
    rgb[grout] -= 28.0
    return _to_rgb_u8(rgb)


def _tex_plaster_wall(size: int = 256) -> np.ndarray:
    noise = _tileable_fbm(size, octaves=((4, 0.5), (16, 0.3), (48, 0.2)), seed=103)
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 216.0 + (noise - 0.5) * 18.0
    rgb[:, :, 1] = 213.0 + (noise - 0.5) * 18.0
    rgb[:, :, 2] = 206.0 + (noise - 0.5) * 16.0
    return _to_rgb_u8(rgb)


def _tex_wood_oak(size: int = 256) -> np.ndarray:
    y = np.linspace(0.0, math.tau * 3.0, size, dtype=np.float32)[:, None]
    x = np.linspace(0.0, math.tau, size, dtype=np.float32)[None, :]
    n = _tileable_fbm(size, octaves=((4, 0.55), (16, 0.30), (32, 0.15)), seed=104)
    rings = 0.5 + 0.5 * np.sin(y * 2.5 + np.sin(x) * 0.8 + n * 7.5)
    fine = _tileable_fbm(size, octaves=((32, 0.7), (64, 0.3)), seed=105)
    grain = 0.72 * rings + 0.28 * fine
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 108.0 + grain * 66.0
    rgb[:, :, 1] = 74.0 + grain * 48.0
    rgb[:, :, 2] = 44.0 + grain * 30.0
    return _to_rgb_u8(rgb)


def _tex_crate_wood(size: int = 256) -> np.ndarray:
    rgb = _tex_wood_oak(size).astype(np.float32) * np.array([1.05, 0.96, 0.82], dtype=np.float32)
    yy, xx = np.ogrid[:size, :size]
    plank_seam = ((yy % (size // 4)) < 3)[:, 0]
    frame = (xx < 18) | (xx >= size - 18) | (yy < 18) | (yy >= size - 18)
    rgb[plank_seam, :] *= 0.65
    rgb[frame] *= 0.86
    border = (xx < 4) | (xx >= size - 4) | (yy < 4) | (yy >= size - 4)
    rgb[border] *= 0.65
    return _to_rgb_u8(rgb)


def _tex_brushed_metal(size: int = 256) -> np.ndarray:
    streaks = np.repeat(
        np.random.default_rng(106).normal(0.0, 9.0, (size, 1)).astype(np.float32),
        size,
        axis=1,
    )
    fine = _tileable_fbm(size, octaves=((8, 0.5), (32, 0.5)), seed=107)
    val = 148.0 + streaks + (fine - 0.5) * 20.0
    rgb = np.stack([val * 0.96, val * 0.99, val * 1.03], axis=2)
    return _to_rgb_u8(rgb)


def _tex_fabric_rug(size: int = 256) -> np.ndarray:
    yy, xx = np.ogrid[:size, :size]
    weave = (((xx // 2) + (yy // 2)) % 2).astype(np.float32) * 10.0
    noise = _tileable_fbm(size, octaves=((8, 0.5), (32, 0.5)), seed=108)
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 44.0 + weave + noise * 18.0
    rgb[:, :, 1] = 60.0 + weave + noise * 20.0
    rgb[:, :, 2] = 82.0 + weave + noise * 22.0
    return _to_rgb_u8(rgb)


def _tex_forest_grass(size: int = 256) -> np.ndarray:
    macro = _tileable_fbm(size, octaves=((3, 0.5), (8, 0.35), (16, 0.15)), seed=201)
    blades = _tileable_fbm(size, octaves=((24, 0.5), (64, 0.5)), seed=202)
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 54.0 + macro * 36.0 + blades * 22.0
    rgb[:, :, 1] = 82.0 + macro * 42.0 + blades * 28.0
    rgb[:, :, 2] = 36.0 + macro * 20.0 + blades * 14.0
    dry_patch = macro < 0.32
    rgb[dry_patch] += np.array([26.0, 12.0, -4.0], dtype=np.float32)
    return _to_rgb_u8(rgb)


def _tex_dirt_trail(size: int = 256) -> np.ndarray:
    macro = _tileable_fbm(size, octaves=((4, 0.5), (12, 0.3), (32, 0.2)), seed=203)
    gravel = _tileable_fbm(size, octaves=((32, 0.6), (64, 0.4)), seed=204)
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 104.0 + macro * 38.0 + (gravel - 0.5) * 32.0
    rgb[:, :, 1] = 84.0 + macro * 30.0 + (gravel - 0.5) * 26.0
    rgb[:, :, 2] = 62.0 + macro * 22.0 + (gravel - 0.5) * 20.0
    return _to_rgb_u8(rgb)


def _tex_tree_bark(size: int = 256) -> np.ndarray:
    x = np.linspace(0.0, math.tau * 10.0, size, dtype=np.float32)[None, :]
    n = _tileable_fbm(size, octaves=((4, 0.45), (16, 0.35), (48, 0.20)), seed=205)
    furrows = 0.5 + 0.5 * np.sin(x + n * 6.0)
    val = 0.65 * furrows + 0.35 * n
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 64.0 + val * 58.0
    rgb[:, :, 1] = 46.0 + val * 42.0
    rgb[:, :, 2] = 32.0 + val * 28.0
    return _to_rgb_u8(rgb)


def _tex_foliage_pine(size: int = 256) -> np.ndarray:
    macro = _tileable_fbm(size, octaves=((4, 0.4), (16, 0.35), (48, 0.25)), seed=206)
    needles = _tileable_fbm(size, octaves=((32, 0.6), (64, 0.4)), seed=207)
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 24.0 + macro * 28.0 + needles * 18.0
    rgb[:, :, 1] = 56.0 + macro * 52.0 + needles * 32.0
    rgb[:, :, 2] = 24.0 + macro * 22.0 + needles * 14.0
    return _to_rgb_u8(rgb)


def _tex_rock_granite(size: int = 256) -> np.ndarray:
    macro = _tileable_fbm(size, octaves=((4, 0.45), (16, 0.35), (48, 0.20)), seed=208)
    speck = _tileable_fbm(size, octaves=((32, 0.5), (64, 0.5)), seed=209)
    val = 96.0 + macro * 48.0 + (speck - 0.5) * 44.0
    rgb = np.stack([val * 1.02, val * 1.00, val * 0.98], axis=2)
    moss = (macro > 0.68) & (speck > 0.55)
    rgb[moss] += np.array([-16.0, 22.0, -18.0], dtype=np.float32)
    return _to_rgb_u8(rgb)


def _tex_asphalt_road(size: int = 256) -> np.ndarray:
    macro = _tileable_fbm(size, octaves=((4, 0.5), (16, 0.3), (48, 0.2)), seed=301)
    agg = _tileable_fbm(size, octaves=((32, 0.5), (64, 0.5)), seed=302)
    val = 48.0 + macro * 20.0 + (agg - 0.5) * 28.0
    rgb = np.stack([val * 0.98, val * 1.00, val * 1.04], axis=2)
    return _to_rgb_u8(rgb)


def _tex_paver_plaza(size: int = 256) -> np.ndarray:
    noise = _tileable_fbm(size, octaves=((8, 0.5), (32, 0.5)), seed=303)
    cell = size // 8
    yy, xx = np.ogrid[:size, :size]
    row = yy // cell
    col = (xx + (row % 2) * (cell // 2)) // cell
    tile_tint = ((row * 37 + col * 19) % 7).astype(np.float32) * 4.0
    grout = ((yy % cell) < 2) | (((xx + (row % 2) * (cell // 2)) % cell) < 2)
    val = 158.0 + tile_tint + (noise - 0.5) * 22.0
    rgb = np.stack([val * 1.03, val * 1.00, val * 0.96], axis=2)
    rgb[grout] -= 42.0
    return _to_rgb_u8(rgb)


def _tex_facade_glass(size: int = 256) -> np.ndarray:
    noise = _tileable_fbm(size, octaves=((4, 0.5), (16, 0.5)), seed=304)
    yy, xx = np.ogrid[:size, :size]
    cw, ch = size // 4, size // 4
    mullion = ((xx % cw) < 5) | ((yy % ch) < 7)
    spandrel = ((yy % ch) < 16)[:, 0]
    rgb = np.empty((size, size, 3), dtype=np.float32)
    # Reflective sky-tinted architectural curtain wall glass
    vgrad = ((yy % ch) / float(ch)).astype(np.float32)
    rgb[:, :, 0] = 68.0 + (1.0 - vgrad) * 46.0 + noise * 22.0
    rgb[:, :, 1] = 104.0 + (1.0 - vgrad) * 52.0 + noise * 24.0
    rgb[:, :, 2] = 142.0 + (1.0 - vgrad) * 58.0 + noise * 24.0
    rgb[spandrel, :] = np.array([62.0, 68.0, 78.0], dtype=np.float32)
    rgb[mullion] = np.array([38.0, 42.0, 48.0], dtype=np.float32)
    return _to_rgb_u8(rgb)


def _tex_facade_brick(size: int = 256) -> np.ndarray:
    noise = _tileable_fbm(size, octaves=((8, 0.5), (32, 0.5)), seed=305)
    yy, xx = np.ogrid[:size, :size]
    bh, bw = 16, 32
    row = yy // bh
    col = (xx + (row % 2) * (bw // 2)) // bw
    tint = (((row * 29 + col * 13) % 5) - 2).astype(np.float32) * 9.0
    mortar = ((yy % bh) < 2) | (((xx + (row % 2) * (bw // 2)) % bw) < 2)
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 152.0 + tint + (noise - 0.5) * 24.0
    rgb[:, :, 1] = 92.0 + tint * 0.6 + (noise - 0.5) * 18.0
    rgb[:, :, 2] = 74.0 + tint * 0.4 + (noise - 0.5) * 16.0
    rgb[mortar] = np.array([186.0, 182.0, 174.0], dtype=np.float32)
    # Recessed dark window bays every 64x64 block
    win_x = (xx % 64 >= 14) & (xx % 64 < 50)
    win_y = (yy % 64 >= 12) & (yy % 64 < 48)
    win = win_x & win_y
    rgb[win] = np.array([48.0, 68.0, 92.0], dtype=np.float32)
    sill = win_x & (yy % 64 >= 48) & (yy % 64 < 53)
    rgb[sill] = np.array([175.0, 172.0, 166.0], dtype=np.float32)
    return _to_rgb_u8(rgb)


def _tex_facade_concrete(size: int = 256) -> np.ndarray:
    noise = _tileable_fbm(size, octaves=((4, 0.5), (16, 0.3), (48, 0.2)), seed=306)
    yy, xx = np.ogrid[:size, :size]
    seam = ((yy % 64) < 2) | ((xx % 64) < 2)
    val = 166.0 + (noise - 0.5) * 26.0
    rgb = np.stack([val * 1.01, val * 1.00, val * 0.97], axis=2)
    rgb[seam] -= 24.0
    win = (xx % 64 >= 12) & (xx % 64 < 52) & (yy % 64 >= 14) & (yy % 64 < 48)
    rgb[win] = np.array([56.0, 76.0, 102.0], dtype=np.float32)
    return _to_rgb_u8(rgb)


def _tex_water_pool(size: int = 256) -> np.ndarray:
    ripples = _tileable_fbm(size, octaves=((6, 0.5), (16, 0.35), (32, 0.15)), seed=307)
    caustics = np.clip((ripples - 0.55) / 0.35, 0.0, 1.0)
    rgb = np.empty((size, size, 3), dtype=np.float32)
    rgb[:, :, 0] = 36.0 + caustics * 85.0
    rgb[:, :, 1] = 128.0 + caustics * 75.0
    rgb[:, :, 2] = 168.0 + caustics * 65.0
    return _to_rgb_u8(rgb)


_SCENE_TEXTURE_BUILDERS: dict[str, dict[str, tuple[callable, dict]]] = {
    "indoor_sar": {
        "sky_indoor.png": (
            _render_skybox_strip,
            {
                "face_px": 128,
                "zenith": (212.0, 210.0, 204.0),
                "horizon": (224.0, 222.0, 218.0),
                "ground": (110.0, 112.0, 116.0),
                "cloud_strength": 0.0,
            },
        ),
        "terrazzo_floor.png": (_tex_terrazzo_floor, {}),
        "plaster_wall.png": (_tex_plaster_wall, {}),
        "wood_oak.png": (_tex_wood_oak, {}),
        "crate_wood.png": (_tex_crate_wood, {}),
        "brushed_metal.png": (_tex_brushed_metal, {}),
        "fabric_rug.png": (_tex_fabric_rug, {}),
    },
    "forest_sar": {
        "sky_daylight.png": (
            _render_skybox_strip,
            {
                "face_px": 256,
                "zenith": (66.0, 134.0, 214.0),
                "horizon": (194.0, 220.0, 240.0),
                "ground": (52.0, 74.0, 42.0),
                "cloud_strength": 0.30,
                "seed": 21,
            },
        ),
        "forest_grass.png": (_tex_forest_grass, {}),
        "dirt_trail.png": (_tex_dirt_trail, {}),
        "tree_bark.png": (_tex_tree_bark, {}),
        "foliage_pine.png": (_tex_foliage_pine, {}),
        "rock_granite.png": (_tex_rock_granite, {}),
        "wood_oak.png": (_tex_wood_oak, {}),
    },
    "urban_plaza": {
        "sky_urban.png": (
            _render_skybox_strip,
            {
                "face_px": 256,
                "zenith": (72.0, 138.0, 212.0),
                "horizon": (218.0, 222.0, 228.0),
                "ground": (88.0, 90.0, 96.0),
                "sun_dir": (-0.55, 0.35, 0.72),
                "cloud_strength": 0.24,
                "seed": 31,
            },
        ),
        "asphalt_road.png": (_tex_asphalt_road, {}),
        "paver_plaza.png": (_tex_paver_plaza, {}),
        "facade_glass.png": (_tex_facade_glass, {}),
        "facade_brick.png": (_tex_facade_brick, {}),
        "facade_concrete.png": (_tex_facade_concrete, {}),
        "water_pool.png": (_tex_water_pool, {}),
        "foliage_pine.png": (_tex_foliage_pine, {}),
        "tree_bark.png": (_tex_tree_bark, {}),
        "wood_oak.png": (_tex_wood_oak, {}),
    },
    "procedural": {
        "sky_daylight.png": (
            _render_skybox_strip,
            {
                "face_px": 256,
                "zenith": (68.0, 138.0, 216.0),
                "horizon": (198.0, 222.0, 240.0),
                "ground": (58.0, 78.0, 46.0),
                "cloud_strength": 0.32,
                "seed": 41,
            },
        ),
        "forest_grass.png": (_tex_forest_grass, {}),
        "dirt_trail.png": (_tex_dirt_trail, {}),
        "tree_bark.png": (_tex_tree_bark, {}),
        "foliage_pine.png": (_tex_foliage_pine, {}),
        "rock_granite.png": (_tex_rock_granite, {}),
        "wood_oak.png": (_tex_wood_oak, {}),
    },
}


def generate_scene_textures(scene_name: str, asset_dir: Path) -> dict[str, Path]:
    """Generate only the procedural textures required by ``scene_name`` into ``asset_dir``."""
    specs = _SCENE_TEXTURE_BUILDERS.get(scene_name, {})
    paths: dict[str, Path] = {}
    for filename, (builder, kwargs) in specs.items():
        out_path = asset_dir / filename
        rgb = builder(**kwargs)
        write_png(out_path, rgb)
        paths[filename] = out_path
    return paths
