from __future__ import annotations

import math
import random
import struct
from typing import Sequence

from sim.worldgen.png import png_rgb, solid_png
from sim.worldgen.layout import distance_to_path
from sim.worldgen.sdf import TERRAIN_COLLIDE_BITMASK, collide_surface, f
from sim.worldgen.surface import CANONICAL_MAP, MapSpec


def write_npy(rows: Sequence[Sequence[float]]) -> bytes:
    shape = (len(rows), len(rows[0]))
    header = f"{{'descr': '<f4', 'fortran_order': False, 'shape': {shape}, }}"
    padding = (16 - ((10 + len(header) + 1) % 16)) % 16
    header_bytes = (header + " " * padding + "\n").encode("latin1")
    output = bytearray(b"\x93NUMPY\x01\x00")
    output.extend(struct.pack("<H", len(header_bytes)))
    output.extend(header_bytes)
    for row in rows:
        output.extend(struct.pack(f"<{len(row)}f", *row))
    return bytes(output)


def noise_grid(rng: random.Random, width: int, height: int) -> list[list[float]]:
    return [[rng.uniform(-1.0, 1.0) for _ in range(width)] for _ in range(height)]


def sample_noise(grid: list[list[float]], u: float, v: float) -> float:
    height, width = len(grid), len(grid[0])
    gx, gy = u * (width - 1), v * (height - 1)
    x0, y0 = int(gx), int(gy)
    x1, y1 = min(width - 1, x0 + 1), min(height - 1, y0 + 1)
    tx, ty = gx - x0, gy - y0
    top = grid[y0][x0] * (1.0 - tx) + grid[y0][x1] * tx
    bottom = grid[y1][x0] * (1.0 - tx) + grid[y1][x1] * tx
    return top * (1.0 - ty) + bottom * ty


def noise_texture(width: int, height: int, base: tuple[int, int, int], seed: int,
                  variation: int, *, style: str = "mottled",
                  trail_paths: Sequence[tuple[float, float]] | None = None,
                  clearing: tuple[float, float] | None = None,
                  clearing_radius: float = 17.0,
                  map_spec: MapSpec = CANONICAL_MAP) -> bytes:
    rng = random.Random(seed)
    broad_noise = noise_grid(rng, 12, 12)
    detail_noise = noise_grid(rng, 64, 64)
    pixels = bytearray()
    for row in range(height):
        v = 1.0 - row / max(1, height - 1)
        for col in range(width):
            u = col / max(1, width - 1)
            broad = sample_noise(broad_noise, u, v)
            detail = sample_noise(detail_noise, u, v)
            grain = broad * (0.50 if style == "foliage" else 0.55) + detail * (0.50 if style == "foliage" else 0.45)
            fleck = rng.randint(-5, 5)
            color = [max(0, min(255, round(channel + grain * variation + fleck)))
                     for channel in base]
            if trail_paths is not None and clearing is not None:
                x = map_spec.min + u * map_spec.size
                y = map_spec.min + v * map_spec.size
                litter = max(0.0, min(0.42, (broad + 0.25) * 0.28))
                clearing_distance = math.hypot(x - clearing[0], y - clearing[1])
                clearing_mix = max(0.0, min(1.0, (clearing_radius - clearing_distance) / 8.0))
                litter *= 1.0 - clearing_mix
                grass = tuple(color)
                leaf_mix = (102 + fleck, 82 + fleck, 51 + fleck)
                color = [round(grass[channel] * (1.0 - litter) + leaf_mix[channel] * litter)
                         for channel in range(3)]
                clear_grass = (112 + fleck, 128 + fleck, 76 + fleck)
                color = [round(color[channel] * (1.0 - clearing_mix) + clear_grass[channel] * clearing_mix)
                         for channel in range(3)]
                distance = (distance_to_path((x, y), trail_paths)
                            if len(trail_paths) > 1 else math.inf)
                trail_mix = max(0.0, min(1.0, (1.95 - distance) / 0.78))
                if trail_mix:
                    dirt_noise = round(detail * 11 + fleck)
                    dirt = (122 + dirt_noise, 91 + dirt_noise, 56 + dirt_noise)
                    color = [round(color[channel] * (1.0 - trail_mix) + dirt[channel] * trail_mix)
                             for channel in range(3)]
            pixels.extend(max(0, min(255, value)) for value in color)
    return png_rgb(width, height, bytes(pixels))




def _require_square_grid(rows: Sequence[Sequence[float]], map_spec: MapSpec) -> int:
    """Validate a square height grid against ``map_spec`` and return its side length."""
    side = len(rows)
    if side != map_spec.grid_side or any(len(row) != side for row in rows):
        raise ValueError(f"height grid must be {map_spec.grid_side} x {map_spec.grid_side} for "
                         f"{map_spec!r}, got {side} rows")
    return side


def terrain_dae(rows: Sequence[Sequence[float]], map_spec: MapSpec = CANONICAL_MAP, *,
                uv_tile_m: float | None = None) -> str:
    side = _require_square_grid(rows, map_spec)
    points: list[str] = []
    normals: list[str] = []
    texcoords: list[str] = []
    for row, values in enumerate(rows):
        y = map_spec.min + row * map_spec.cell_m
        lower_row, upper_row = max(0, row - 1), min(side - 1, row + 1)
        for col, z in enumerate(values):
            x = map_spec.min + col * map_spec.cell_m
            points.extend((f(x), f(y), f(z)))
            if uv_tile_m is None:
                texcoords.extend((f(col / (side - 1)), f(row / (side - 1))))
            else:
                # Generated 1.2 km maps tile a seamless ground texture every ``uv_tile_m``
                # metres instead of stretching one 512 px image over the whole map.
                texcoords.extend((f(col * map_spec.cell_m / uv_tile_m),
                                  f(row * map_spec.cell_m / uv_tile_m)))
            left_col, right_col = max(0, col - 1), min(side - 1, col + 1)
            dz_dx = (values[right_col] - values[left_col]) / ((right_col - left_col) * map_spec.cell_m)
            dz_dy = (rows[upper_row][col] - rows[lower_row][col]) / ((upper_row - lower_row) * map_spec.cell_m)
            nx, ny, nz = -dz_dx, -dz_dy, 1.0
            magnitude = math.sqrt(nx * nx + ny * ny + nz * nz)
            normals.extend(f(value / magnitude) for value in (nx, ny, nz))
    triangles: list[str] = []
    for row in range(side - 1):
        for col in range(side - 1):
            a = row * side + col
            b, d = a + 1, a + side
            c = d + 1
            for vertex in (a, b, c, a, c, d):
                triangles.extend((str(vertex), str(vertex), str(vertex)))
    return f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><contributor><authoring_tool>HORUS deterministic outdoor generator</authoring_tool></contributor><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
  <library_images><image id="forest-ground-image" name="forest_ground.png"><init_from>../materials/textures/forest_ground.png</init_from></image></library_images>
  <library_effects><effect id="forest-ground-effect"><profile_COMMON>
    <newparam sid="forest-ground-surface"><surface type="2D"><init_from>forest-ground-image</init_from></surface></newparam>
    <newparam sid="forest-ground-sampler"><sampler2D><source>forest-ground-surface</source></sampler2D></newparam>
    <technique sid="common"><lambert><diffuse><texture texture="forest-ground-sampler" texcoord="UVMap"/></diffuse></lambert></technique>
  </profile_COMMON></effect></library_effects>
  <library_materials><material id="forest-ground-material" name="forest ground"><instance_effect url="#forest-ground-effect"/></material></library_materials>
  <library_geometries><geometry id="terrain" name="forest terrain"><mesh>
    <source id="terrain-positions"><float_array id="terrain-positions-array" count="{len(points)}">{' '.join(points)}</float_array><technique_common><accessor source="#terrain-positions-array" count="{side * side}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
    <source id="terrain-normals"><float_array id="terrain-normals-array" count="{len(normals)}">{' '.join(normals)}</float_array><technique_common><accessor source="#terrain-normals-array" count="{side * side}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
    <source id="terrain-uv"><float_array id="terrain-uv-array" count="{len(texcoords)}">{' '.join(texcoords)}</float_array><technique_common><accessor source="#terrain-uv-array" count="{side * side}" stride="2"><param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>
    <vertices id="terrain-vertices"><input semantic="POSITION" source="#terrain-positions"/></vertices>
    <triangles material="forest-ground-material" count="{(side - 1) * (side - 1) * 2}"><input semantic="VERTEX" source="#terrain-vertices" offset="0"/><input semantic="NORMAL" source="#terrain-normals" offset="1"/><input semantic="TEXCOORD" source="#terrain-uv" offset="2" set="0"/><p>{' '.join(triangles)}</p></triangles>
  </mesh></geometry></library_geometries>
  <library_visual_scenes><visual_scene id="Scene" name="Scene"><node id="Terrain" name="Terrain"><instance_geometry url="#terrain"><bind_material><technique_common><instance_material symbol="forest-ground-material" target="#forest-ground-material"><bind_vertex_input semantic="UVMap" input_semantic="TEXCOORD" input_set="0"/></instance_material></technique_common></bind_material></instance_geometry></node></visual_scene></library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
'''


def terrain_model_files(rows: Sequence[Sequence[float]], seed: int, model_name: str,
                        trail: Sequence[tuple[float, float]], clearing: tuple[float, float],
                        *, textures: bool = True, clearing_radius: float = 17.0,
                        tint: tuple[float, float, float] = (1.0, 1.0, 1.0),
                        map_spec: MapSpec = CANONICAL_MAP,
                        uv_tile_m: float | None = None) -> dict[str, bytes | str]:
    base = tuple(max(0, min(255, round(channel * factor))) for channel, factor in zip((92, 108, 64), tint))
    files: dict[str, bytes | str] = {
        f"models/{model_name}/meshes/terrain.dae": terrain_dae(rows, map_spec, uv_tile_m=uv_tile_m),
        f"models/{model_name}/model.sdf": f'''<?xml version="1.0"?>
<sdf version="1.9"><model name="{model_name}"><static>true</static><link name="ground">
  <collision name="terrain_collision"><geometry><mesh><uri>model://{model_name}/meshes/terrain.dae</uri></mesh></geometry>{collide_surface(TERRAIN_COLLIDE_BITMASK)}</collision>
  <visual name="terrain_visual"><geometry><mesh><uri>model://{model_name}/meshes/terrain.dae</uri></mesh></geometry></visual>
</link></model></sdf>
''',
        f"models/{model_name}/model.config": f'''<?xml version="1.0"?>
<model><name>{model_name}</name><version>1.0</version><sdf version="1.9">model.sdf</sdf><author><name>HORUS</name></author><description>Generated {map_spec.size:g} m forest terrain mesh with a procedural trail-blended ground texture.</description></model>
''',
    }
    if textures:
        if uv_tile_m is None:
            files[f"models/{model_name}/materials/textures/forest_ground.png"] = noise_texture(
                512, 512, base, seed, 24,
                trail_paths=trail, clearing=clearing,
                clearing_radius=clearing_radius, map_spec=map_spec)
        else:
            # Tiled ground: one seamless forest-floor repeat every ``uv_tile_m`` metres.
            from sim.worldgen.materials import forest_floor_texture

            files[f"models/{model_name}/materials/textures/forest_ground.png"] = \
                forest_floor_texture(seed, size=256)
    else:
        files[f"models/{model_name}/materials/textures/forest_ground.png"] = solid_png(base)
    return files


