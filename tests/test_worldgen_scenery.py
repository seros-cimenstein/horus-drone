"""Behavior tests for scenery tiles, their collision meshes and the sidecar proxies.

These tests inject a fake asset registry (the real one lives in ``sim/worldgen/assets.py``)
so the scenery writer is exercised without importing any imported meshes.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from sim.worldgen.geometry import (
    CollisionWorld,
    Primitive,
    load_collisions,
    parse_scenery_sidecar,
    scenery_sidecar_path,
    scenery_sidecar_text,
)
from sim.worldgen.materials import facade_uv, scene_textures
from sim.worldgen.png import read_png
from sim.worldgen.proxy import ProxyShape
from sim.worldgen.scenery import SceneryInstance, SceneryMesh, collision_obj, emit_scenery
from sim.worldgen.surface import CANONICAL_MAP, PROCEDURAL_MAP, MapSpec

WORLD = "horus_forest_p7"


@dataclass(frozen=True)
class FakeVisual:
    name: str
    mesh_uri: str
    pose: tuple[float, ...] = (0.0,) * 6
    submesh: str | None = None
    scale: float = 1.0
    albedo_map: str | None = None
    normal_map: str | None = None
    double_sided: bool = False
    metalness: float = 0.0
    roughness: float = 0.8
    cast_shadows: bool = True


@dataclass(frozen=True)
class FakeAsset:
    key: str
    kind: str = "tree"
    visuals: tuple[FakeVisual, ...] = ()
    native_height_m: float = 1.0
    footprint_radius_m: float = 1.0
    proxies: tuple[ProxyShape, ...] = ()
    license: str = "test"
    source: str = "test"
    model_dir: str = "test"


TREE = FakeAsset(
    "tree_test", visuals=(FakeVisual("tree", "model://trees/meshes/trunk.obj"),),
    native_height_m=10.0, footprint_radius_m=2.0,
    proxies=(ProxyShape("cylinder", (0, 0, 2.5, 0, 0, 0), (0.25, 5.0)),
             ProxyShape("sphere", (0, 0, 7.0, 0, 0, 0), (2.0,))),
)
ROCK = FakeAsset(
    "rock_test", kind="rock", visuals=(FakeVisual("rock", "model://rocks/meshes/rock.obj"),),
    native_height_m=1.0, footprint_radius_m=0.8,
    proxies=(ProxyShape("box", (0, 0, 0.4, 0, 0, 0.5), (1.2, 1.0, 0.8)),),
)
REGISTRY = {"tree_test": TREE, "rock_test": ROCK}

SHRUB = FakeAsset(
    "shrub_test", kind="tree",
    visuals=(FakeVisual("foliage", "model://shrubs/meshes/shrub.dae",
                        pose=(1.0, 0.0, 0.25, 0.0, 0.0, 0.0), submesh="Branch", scale=2.0,
                        albedo_map="model://shrubs/materials/textures/leaf.png",
                        normal_map="model://shrubs/materials/textures/leaf_n.png",
                        double_sided=True),),
    native_height_m=2.0, footprint_radius_m=1.0,
    proxies=(ProxyShape("box", (0.0, 0.0, 1.0, 0.0, 0.0, 0.0), (1.0, 1.0, 2.0)),),
)


def _quad_mesh(name: str, *, x: float = 0.0, y: float = 0.0,
               proxies: tuple[ProxyShape, ...] = (), texture: str | None = None) -> SceneryMesh:
    positions = ((x - 1, y - 1, 0.0), (x + 1, y - 1, 0.0), (x + 1, y + 1, 0.0), (x - 1, y + 1, 0.0))
    return SceneryMesh(name, "dirt_trail", positions, ((0, 0, 1),) * 4,
                       ((0, 0), (1, 0), (1, 1), (0, 1)), ((0, 1, 2), (0, 2, 3)),
                       proxies=proxies, texture=texture)


def _parse_obj(text: str):
    """(vertices, normals, face refs); a face ref is a tuple of ``(vertex, uv, normal)``."""
    vertices, normals, faces = [], [], []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "v":
            vertices.append(tuple(float(value) for value in fields[1:4]))
        elif fields[0] == "vn":
            normals.append(tuple(float(value) for value in fields[1:4]))
        elif fields[0] == "f":
            face = []
            for token in fields[1:]:
                parts = token.split("/")
                face.append((int(parts[0]),
                             int(parts[1]) if len(parts) > 1 and parts[1] else 0,
                             int(parts[2]) if len(parts) > 2 and parts[2] else 0))
            faces.append(tuple(face))
    return vertices, normals, faces


def _indices(faces):
    return [tuple(reference[0] - 1 for reference in face) for face in faces]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _write_world(directory: Path, sdf: str, files: dict[str, bytes | str]) -> Path:
    """Write a world the way ``WorldBundle.write`` does (``models/`` -> the model root)."""
    world_path = directory / f"{WORLD}.sdf"
    world_path.write_text(f'<sdf version="1.9"><world name="{WORLD}">{sdf}</world></sdf>',
                          encoding="utf-8")
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return world_path


def test_map_spec_grid_math_for_canonical_and_procedural():
    assert (CANONICAL_MAP.min, CANONICAL_MAP.size, CANONICAL_MAP.grid_side) == (-75.0, 150.0, 76)
    assert CANONICAL_MAP.min + (CANONICAL_MAP.grid_side - 1) * CANONICAL_MAP.cell_m == 75.0
    assert (PROCEDURAL_MAP.min, PROCEDURAL_MAP.size, PROCEDURAL_MAP.grid_side) == (-600.0, 1200.0, 301)
    assert PROCEDURAL_MAP.min + (PROCEDURAL_MAP.grid_side - 1) * PROCEDURAL_MAP.cell_m == 600.0
    with pytest.raises(ValueError):
        MapSpec(0.0, 2.0)
    with pytest.raises(ValueError):
        MapSpec(75.0, 0.0)


def test_tiles_hold_visuals_and_one_merged_collision_carries_every_proxy():
    # Visuals go to the tile containing the instance: y = -0.5 falls in tile row -1 and
    # x = 100.0 (exactly on the boundary) in tile column 1 because tiles are [k*100, (k+1)*100).
    bundle = emit_scenery(WORLD, [
        SceneryInstance("tree_test", 10.0, 20.0),
        SceneryInstance("tree_test", 99.0, -0.5),
        SceneryInstance("tree_test", 100.0, 0.0, yaw=math.pi / 2),
    ], [], assets=REGISTRY)
    # All proxies belong to the single merged collision; tile order (0,-1), (0,0), (1,0) is kept
    # and the owner label (asset key or mesh name) stays in each oracle name for hazard tokens.
    assert {primitive.model for primitive in bundle.proxies} == {"scenery_collision/scenery_collision"}
    assert [primitive.name for primitive in bundle.proxies] == [
        f"scenery_collision/scenery_collision/tree_test_{n}" for n in range(6)]
    assert [primitive.transform[:2] for primitive in bundle.proxies][::2] == [
        (99.0, -0.5), (10.0, 20.0), (100.0, 0.0)]
    # The tile model's pose is its 100 m corner; the instance visual sits at the local offset.
    assert '<model name="scenery_1_0"><static>true</static><pose>100 0 0 0 0 0</pose>' in bundle.sdf
    assert '<model name="scenery_0_-1"><static>true</static><pose>0 -100 0 0 0 0</pose>' in bundle.sdf
    assert '<visual name="tree_test_0"><pose>0 0 0 0 0 1.5708</pose>' in bundle.sdf
    assert '<visual name="tree_test_0"><pose>99 99.5 0 0 0 0</pose>' in bundle.sdf
    # Tiles are visual-only; one world-space trimesh on the scenery bit covers every proxy.
    assert bundle.sdf.count('collision name="scenery_collision"') == 1
    assert ('<model name="scenery_collision"><static>true</static><link name="scenery">'
            '<collision name="scenery_collision">') in bundle.sdf
    assert "<collide_bitmask>0x0010</collide_bitmask>" in bundle.sdf
    assert [name for name in bundle.files if name.endswith(".obj")] == [
        f"models/{WORLD}_scenery/meshes/scenery_collision.obj"]


def test_instance_visuals_apply_registry_scale_material_and_submesh():
    bundle = emit_scenery(WORLD, [SceneryInstance("shrub_test", 5.0, 6.0, 0.4, scale=1.5)],
                          [], assets={"shrub_test": SHRUB})
    # mesh <scale> = visual.scale * instance scale; pose translation scaled by instance scale only.
    assert ('<mesh><uri>model://shrubs/meshes/shrub.dae</uri><scale>3 3 3</scale>'
            "<submesh><name>Branch</name></submesh></mesh>") in bundle.sdf
    assert "<pose>6.5 6 0.775 0 0 0</pose>" in bundle.sdf
    assert ("<material><diffuse>1 1 1 1</diffuse><pbr><metal>"
            "<albedo_map>model://shrubs/materials/textures/leaf.png</albedo_map>"
            "<normal_map>model://shrubs/materials/textures/leaf_n.png</normal_map>"
            "<metalness>0</metalness><roughness>0.8</roughness>"
            "</metal></pbr><double_sided>true</double_sided></material>") in bundle.sdf
    # Assets without an albedo map emit no <material> (the mesh keeps its embedded material),
    # and unit visual scale at unit instance scale emits no <scale>.
    plain = emit_scenery(WORLD, [SceneryInstance("rock_test", 0.0, 0.0)], [], assets=REGISTRY)
    assert "<material>" not in plain.sdf
    assert "<scale>" not in plain.sdf
    # A non-zero yaw rotates the visual translation around the instance origin.
    rotated = emit_scenery(WORLD, [SceneryInstance("shrub_test", 0.0, 0.0, 0.0, yaw=math.pi / 2)],
                           [], assets={"shrub_test": SHRUB})
    assert "<pose>0 1 0.25 0 0 1.5708</pose>" in rotated.sdf


def test_tiles_without_proxies_emit_no_collision():
    bundle = emit_scenery(WORLD, [], [_quad_mesh("paint_strip", proxies=())], assets=REGISTRY)
    assert "scenery_collision" not in bundle.sdf
    assert '<model name="scenery_0_0">' in bundle.sdf
    assert not any(name.endswith("_collision.obj") for name in bundle.files)
    assert bundle.proxies == ()
    # A mesh in a later tile still gets its own tile model, and the sidecar is empty.
    far = emit_scenery(WORLD, [], [_quad_mesh("far_strip", x=250.0)], assets=REGISTRY)
    assert '<model name="scenery_2_0">' in far.sdf
    assert parse_scenery_sidecar(far.files[f"{WORLD}.scenery.json"]) == []


def test_collision_obj_has_per_vertex_normals_and_closed_hulls():
    bundle = emit_scenery(WORLD, [SceneryInstance("tree_test", 10.0, 20.0),
                                  SceneryInstance("rock_test", 30.0, 40.0)], [], assets=REGISTRY)
    obj = bundle.files[f"models/{WORLD}_scenery/meshes/scenery_collision.obj"]
    vertices, normals, refs = _parse_obj(obj)
    faces = _indices(refs)
    assert len(vertices) == len(normals) > 0
    assert len(faces) == 12 + 8 + 28  # box 12, sphere octahedron 8, cylinder 16 side + 12 caps
    for face in refs:
        assert len(face) == 3
        for vertex, _uv, normal in face:
            assert vertex == normal, "faces must be f a//a (per-vertex normals)"
    edges = Counter()
    for a, b, c in faces:
        assert len({a, b, c}) == 3
        for first, second in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted((first, second)))] += 1
    assert edges and all(count == 2 for count in edges.values()), "hull must be closed"
    # The hulls are outward-wound (positive signed volume).
    volume = sum(_dot(vertices[a], _cross(vertices[b], vertices[c])) for a, b, c in faces) / 6.0
    assert volume > 0.0
    # Every point inside a proxy lies inside the merged mesh's bounds (its AABB).
    low = tuple(min(vertex[axis] for vertex in vertices) for axis in range(3))
    high = tuple(max(vertex[axis] for vertex in vertices) for axis in range(3))
    for point in ((10.0, 20.0, 2.5), (10.0, 20.0, 7.0), (10.0, 20.0, 8.5), (30.0, 40.0, 0.4)):
        assert all(low[axis] - 1e-9 <= point[axis] <= high[axis] + 1e-9 for axis in range(3)), point
    assert high[2] == pytest.approx(9.0)      # canopy sphere centre 7 + radius 2
    assert high[0] < 31.5                     # the rock hull ends before x = 31.5
    assert all(math.isfinite(value) for vertex in vertices for value in vertex)


def test_single_proxy_hulls_enclosed_each_shape():
    for shape, pose, size, inside, outside in (
        ("box", (5.0, 5.0, 1.0, 0.0, 0.0, 0.4), (2.0, 1.0, 0.8), (5.1, 5.0, 1.1), (7.0, 5.0, 1.0)),
        ("cylinder", (0.0, 0.0, 0.5, 0.0, 0.0, 0.0), (0.5, 3.0), (0.1, 0.0, 1.5), (0.0, 0.0, 3.0)),
        ("sphere", (1.0, 2.0, 3.0, 0.0, 0.0, 0.4), (1.5,), (1.0, 2.0, 3.0), (4.0, 2.0, 3.0)),
    ):
        proxy = ProxyShape(shape, pose, size)
        vertices, normals, refs = _parse_obj(collision_obj([proxy]))
        faces = _indices(refs)
        assert len(vertices) == len(normals)
        assert all(vertex == normal for face in refs for vertex, _uv, normal in face)
        inside_faces = [_dot(_cross(tuple(vertices[b][i] - vertices[a][i] for i in range(3)),
                                    tuple(vertices[c][i] - vertices[a][i] for i in range(3))),
                             tuple(point[i] - vertices[a][i] for i in range(3)))
                        for a, b, c in faces for point in (inside,)]
        assert all(value < -1e-9 for value in inside_faces), shape
        outside_faces = [_dot(_cross(tuple(vertices[b][i] - vertices[a][i] for i in range(3)),
                                     tuple(vertices[c][i] - vertices[a][i] for i in range(3))),
                              tuple(outside[i] - vertices[a][i] for i in range(3)))
                         for a, b, c in faces]
        assert not all(value < -1e-9 for value in outside_faces), shape


def test_emit_scenery_is_deterministic_and_order_independent():
    meshes = [_quad_mesh("strip_a", x=10.0, texture="dirt_trail",
                         proxies=(ProxyShape("box", (10.0, 0.0, 0.01, 0, 0, 0), (8.0, 2.0, 0.02)),))]
    instances = [SceneryInstance("tree_test", 10.0, 20.0, yaw=0.25, scale=1.3),
                 SceneryInstance("rock_test", -120.0, 33.0)]
    textures = {"dirt_trail": scene_textures(3, size=32)["dirt_trail"]}
    first = emit_scenery(WORLD, instances, meshes, assets=REGISTRY, textures=textures)
    second = emit_scenery(WORLD, instances, meshes, assets=REGISTRY, textures=textures)
    assert first == second
    # Registry insertion order must not leak into the output.
    reversed_registry = {"rock_test": ROCK, "tree_test": TREE}
    third = emit_scenery(WORLD, instances, meshes, assets=reversed_registry, textures=textures)
    assert third == first
    assert first.files[f"{WORLD}.scenery.json"] == scenery_sidecar_text(first.proxies)
    with pytest.raises(KeyError):
        emit_scenery(WORLD, [SceneryInstance("missing_asset", 0.0, 0.0)], [], assets=REGISTRY)
    with pytest.raises(KeyError):
        emit_scenery(WORLD, [], [_quad_mesh("strip_b", texture="missing_texture")],
                     assets=REGISTRY, textures=textures)


def test_sidecar_roundtrip_preserves_primitive_fields():
    primitives = [Primitive("scenery_0_0/scenery_collision/0", "scenery_0_0/scenery_collision",
                            "box", (1.0, 2.0, 3.0, 0.1, 0.2, 0.3), (1.0, 2.0, 3.0), True),
                  Primitive("scenery_0_0/scenery_collision/1", "scenery_0_0/scenery_collision",
                            "cylinder", (0.0, 0.0, 0.5, 0.0, 0.0, 1.5), (0.4, 3.0), True)]
    text = scenery_sidecar_text(primitives)
    assert parse_scenery_sidecar(text) == primitives
    assert scenery_sidecar_path(Path("/tmp/x/horus_forest_p7.sdf")) == Path("/tmp/x/horus_forest_p7.scenery.json")


def test_load_collisions_reads_sidecar_and_collision_world_sees_proxies(tmp_path):
    bundle = emit_scenery(WORLD, [SceneryInstance("tree_test", 10.0, 20.0),
                                  SceneryInstance("rock_test", 130.0, 40.0)],
                          [_quad_mesh("strip_a", x=10.0,
                                      proxies=(ProxyShape("box", (10.0, 0.0, 0.01, 0, 0, 0),
                                                          (8.0, 2.0, 0.02)),))],
                          assets=REGISTRY)
    world_path = _write_world(tmp_path, bundle.sdf, bundle.files)
    primitives = load_collisions(world_path)
    assert [primitive.name for primitive in primitives] == [p.name for p in bundle.proxies]
    assert all(primitive.static for primitive in primitives)
    assert {primitive.model for primitive in primitives} == {"scenery_collision/scenery_collision"}
    world = CollisionWorld(primitives)
    clearance, nearest = world.clearance((10.0, 20.0, 2.0))
    assert clearance < 0.0 and nearest.name.startswith("scenery_collision/scenery_collision/tree_test")
    assert world.segment_clearance((10.0, 20.0, 12.0), (10.0, 20.0, 2.0))[0] < 0.0
    blocker = world.blocking((10.0, 20.0, 12.0), (10.0, 20.0, 1.0))
    assert blocker is not None and blocker.name.startswith("scenery_collision/scenery_collision/tree_test")
    assert world.max_top(10.0, 20.0, 0.5) == pytest.approx(9.0)  # canopy sphere centre 7 + radius 2
    assert world.max_top(60.0, 60.0, 1.0) == -math.inf
    # Without the sidecar the mesh collision has no primitive proxy: reject, not silently ignore.
    scenery_sidecar_path(world_path).unlink()
    with pytest.raises(ValueError, match="scenery_collision"):
        load_collisions(world_path)
    # The in-memory path used by the validator passes the proxies explicitly.
    again = load_collisions(world_path, scenery_proxies=list(bundle.proxies))
    assert [primitive.name for primitive in again] == [p.name for p in bundle.proxies]


def test_load_collisions_still_rejects_other_static_mesh_collisions(tmp_path):
    model = tmp_path / "models" / "blob"
    model.mkdir(parents=True)
    (model / "model.sdf").write_text(
        '<sdf><model name="blob"><static>true</static><link name="l">'
        '<collision name="mesh_collision"><geometry><mesh><uri>model://blob/meshes/x.obj</uri></mesh>'
        "</geometry></collision></link></model></sdf>", encoding="utf-8")
    world = tmp_path / "w.sdf"
    world.write_text('<sdf version="1.9"><world name="w">'
                     "<include><uri>model://blob</uri><name>blob</name></include>"
                     "</world></sdf>", encoding="utf-8")
    with pytest.raises(ValueError, match="no primitive proxy"):
        load_collisions(world, resource_paths=[tmp_path / "models"])


def test_procedural_size_world_emits_one_model_per_occupied_tile():
    instances = [SceneryInstance("tree_test", -570.0 + 38.0 * index, 570.0 - 38.0 * (index % 30),
                                 scale=1.0 + 0.05 * (index % 3))
                 for index in range(300)]
    meshes = [_quad_mesh("road_a", x=-560.0, y=-580.0,
                         proxies=(ProxyShape("box", (-560.0, -580.0, 0.02, 0, 0, 0), (40.0, 9.5, 0.04)),),
                         texture="dirt_trail"),
              _quad_mesh("road_b", x=560.0, y=580.0,
                         proxies=(ProxyShape("box", (560.0, 580.0, 0.02, 0, 0, 0), (40.0, 9.5, 0.04)),),
                         texture="dirt_trail")]
    bundle = emit_scenery(WORLD, instances, meshes, assets=REGISTRY, tile_m=100.0,
                          textures={"dirt_trail": scene_textures(11, size=32)["dirt_trail"]})
    models = [line for line in bundle.sdf.splitlines() if line.startswith("<model")]
    assert len(models) == len(set(models)) > 0
    assert len(bundle.proxies) == 2 * len(instances) + 2
    # PROXIES are spread over the 1.2 km map; the smallest tile index is well past the canonical map.
    assert min(primitive.transform[0] for primitive in bundle.proxies) < -500.0
    assert max(primitive.transform[0] for primitive in bundle.proxies) > 500.0
    world = CollisionWorld(bundle.proxies)
    assert world.clearance((-570.0, 570.0, 2.0))[0] < 0.0


def test_scene_textures_are_deterministic_named_and_sized():
    first = scene_textures(5, size=48)
    assert first == scene_textures(5, size=48)
    assert set(first) == {
        "facade_brick", "facade_concrete", "facade_glass", "facade_stucco", "facade_panel",
        "roof_gravel", "roof_membrane", "asphalt_plain", "asphalt_center_dashed",
        "asphalt_edge_lines", "crosswalk", "pavers", "forest_floor", "dirt_trail", "creek",
        "bark", "wood_planks", "tent_fabric", "metal_paint", "traffic_cone", "foliage", "rock",
        "plaza_stone", "park_path", "park_lawn", "bus_shelter_glass",
    }
    for name, png in first.items():
        width, height, channels, pixels = read_png(png)
        assert (width, height, channels) == (48, 48, 3), name
        assert len(pixels) == 48 * 48 * 3
    assert facade_uv(2 * 3.2, 3 * 3.2) == pytest.approx((2.0, 3.0))
    assert scene_textures(6, size=48)["facade_brick"] != first["facade_brick"]
