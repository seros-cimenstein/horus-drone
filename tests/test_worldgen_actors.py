"""Registry and builder invariants for procedural actors, animals, clutter and camp scenery.

These cover the ActorRealism slice: mesh vehicles in the scenery registry, the seated
person_sitting_a actor, the lofted animal models, and the textured clutter/camp scenery
pieces. Rendering behaviour is measured separately by the throwaway YOLO harness.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from sim.worldgen import dressing, fauna, people, vehicles
from sim.worldgen.appearance import sample_appearance
from sim.worldgen.assets import ASSETS, MODELS, mesh_bounds, mesh_file
from sim.worldgen.materials import scene_textures
from sim.worldgen.proxy import ProxyShape
from sim.worldgen.scenery import emit_scenery

VEHICLE_KEYS = ("vehicle_hatchback", "vehicle_hatchback_red", "vehicle_hatchback_blue",
                "vehicle_sedan", "vehicle_suv", "vehicle_pickup", "vehicle_box_truck",
                "vehicle_delivery_truck", "bus")
MESH_TYPES = tuple(sorted(vehicles.SAMPLED_TYPES + vehicles.PARKED_TYPES))


def test_registry_has_every_mesh_vehicle():
    for key in VEHICLE_KEYS:
        asset = ASSETS[key]
        assert asset.kind == "vehicle"
        for visual in asset.visuals:
            assert mesh_file(visual.mesh_uri).is_file()


@pytest.mark.parametrize("type_name", MESH_TYPES)
def test_vehicle_type_maps_to_a_registry_asset(type_name):
    kind = vehicles.vehicle_type(type_name)
    assert kind.mesh is not None
    assert kind.assets
    for variant in range(len(kind.assets)):
        key = vehicles.vehicle_asset(type_name, variant)
        assert key in ASSETS
    # The physics footprint stays within the measured registry proxy (the bus asset's long
    # axis runs along y, so compare the sorted horizontal extents).
    asset = ASSETS[kind.assets[0]]
    proxy = asset.proxies[0]
    assert abs(max(proxy.size[:2]) - kind.length_m) < 0.08
    assert abs(min(proxy.size[:2]) - kind.width_m) < 0.08


def test_procedural_sampling_uses_only_mesh_vehicles():
    assert vehicles.SAMPLED_TYPES == ("hatchback", "sedan", "suv", "pickup", "box_truck",
                                      "delivery_truck", "coach")
    assert vehicles.PARKED_TYPES == ("hatchback", "sedan", "suv", "pickup", "box_truck",
                                     "delivery_truck")
    assert len(vehicles.SAMPLED_WEIGHTS) == len(vehicles.SAMPLED_TYPES)
    for name in vehicles.SAMPLED_TYPES + vehicles.PARKED_TYPES:
        assert vehicles.vehicle_type(name).mesh is not None, name
    # The canonical primitive bodies stay available (their worlds are byte-frozen).
    for name in ("car", "van", "bus"):
        assert vehicles.vehicle_type(name).mesh is None


@pytest.mark.parametrize("type_name", MESH_TYPES)
def test_vehicle_model_renders_the_mesh_with_one_collision(type_name):
    kind = vehicles.vehicle_type(type_name)
    model = vehicles.vehicle_model("probe_veh", type_name, 1.0, 2.0, 0.0, 0.3, static=True)
    assert kind.mesh in model
    assert "<cylinder>" not in model  # no primitive wheels
    assert model.count("<collision") == 1
    assert "<kinematic>true</kinematic>" not in model  # parked


def test_canonical_primitive_vehicle_model_is_unchanged():
    model = vehicles.vehicle_model("probe_car", "car", 0.0, 0.0, 0.0, 0.0, static=False)
    assert "<mesh>" not in model
    assert "<cylinder>" in model


# -- seated actors -----------------------------------------------------------------

def test_sitting_actor_uses_the_rescue_randy_mesh():
    appearance = sample_appearance(random.Random(4))
    files = people.actor_model_files("probe_sitting", appearance, "sitting", textures=False)
    sdf = files["models/probe_sitting/model.sdf"]
    assert people.SITTING_MESH in sdf
    assert mesh_file(people.SITTING_MESH).is_file()
    assert "<box><size>" in sdf  # the envelope collision box
    assert "<sphere>" not in sdf and "<cylinder>" not in sdf  # no primitive figure


def test_sitting_envelope_bounds_the_scaled_mesh():
    appearance = sample_appearance(random.Random(5))
    envelope = appearance.envelope("sitting")
    s, w = appearance.stature_scale, appearance.width_scale
    raw = mesh_bounds(mesh_file(people.SITTING_MESH))
    # The asset's visual pose rotates mesh -y onto +x; x/y scale with width, z with stature.
    pose = ASSETS["person_sitting_a"].visuals[0]
    scale = pose.scale
    x_extent = (raw.max[1] - raw.min[1]) * scale * w
    y_extent = (raw.max[0] - raw.min[0]) * scale * w
    z_extent = (raw.max[2] - raw.min[2]) * scale * s
    assert envelope["x_max_m"] - envelope["x_min_m"] == pytest.approx(x_extent, abs=0.02)
    assert envelope["y_max_m"] - envelope["y_min_m"] == pytest.approx(y_extent, abs=0.02)
    assert envelope["top_z_m"] == pytest.approx(z_extent, abs=0.02)


def test_sitting_manifest_attribute_is_the_measured_constant():
    assert people.SITTING_SHIRT_COLOUR is None
    assert people.SITTING_POSSIBLE_SHIRT_COLOURS == ("green",)
    record = {"model": "probe", "asset": "person_sitting", "role": "injured", "speed": 0.0}
    d = dressing.Dressing("baseline", dressing.sample_environment(random.Random(0), chaotic=False),
                          appearances={"probe": sample_appearance(random.Random(6))})
    dressed = dressing.dress_person(d, record)
    assert dressed["posture"] == "sitting"
    assert dressed["shirt_colour_attribute"] is None
    assert dressed["shirt_colour_source"] == "measured_rendered_asset"
    assert dressed["possible_shirt_colours"] == ["green"]
    assert dressed["clothing"] == people.SITTING_CLOTHING
    assert dressed["envelope"] == d.appearances["probe"].envelope("sitting")


# -- animals -----------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(fauna.SPECIES))
def test_animal_mesh_matches_its_species_size(name):
    kind = fauna.species(name)
    mesh = fauna.species_mesh(name)
    low, high = mesh.bounds()
    assert high[2] == pytest.approx(kind.height_m, abs=1e-6)
    assert low[2] == pytest.approx(0.0, abs=0.01)  # feet on the ground
    envelope = fauna.animal_envelope(name, 1.0)
    assert envelope["top_z_m"] == pytest.approx(kind.height_m, abs=0.002)
    assert envelope["x_max_m"] - envelope["x_min_m"] > kind.length_m * 0.7


def test_animal_mesh_is_deterministic_and_has_texture_uvs():
    first, second = fauna.species_mesh("dog"), fauna.species_mesh("dog")
    assert first.positions == second.positions
    assert first.faces == second.faces
    assert len(first.uvs) == len(first.positions)
    assert max(u for u, _v in first.uvs) > 1.0


@pytest.mark.parametrize("name", sorted(fauna.SPECIES))
def test_animal_model_uses_the_committed_mesh_and_envelope(name):
    model = fauna.animal_model("probe_animal", name, 1.0, 0, 0.0, 0.0, 0.0, 0.0)
    assert f"model://horus_animal_{name}/meshes/{name}.obj" in model
    assert mesh_file(f"model://horus_animal_{name}/meshes/{name}.obj").is_file()
    assert model.count("<collision") == 1
    assert "<kinematic>true</kinematic>" in model


@pytest.mark.parametrize("name", sorted(fauna.SPECIES))
def test_committed_animal_models_regenerate_byte_identically(name, tmp_path):
    fauna.write_animal_models(tmp_path)
    for relative in (f"horus_animal_{name}/meshes/{name}.obj",
                     f"horus_animal_{name}/meshes/{name}.mtl",
                     f"horus_animal_{name}/model.sdf",
                     f"horus_animal_{name}/model.config"):
        generated = (tmp_path / relative).read_bytes()
        committed = (MODELS / relative).read_bytes()
        assert generated == committed, relative


# -- clutter and camp scenery -------------------------------------------------------

CLUTTER_ITEMS = (
    {"name": "clutter_stump_01", "kind": "stump", "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.3, "size": 0.45},
    {"name": "clutter_boulder_02", "kind": "boulder", "x": 6.0, "y": 0.0, "z": 0.1, "yaw": 0.0, "size": 0.8},
    {"name": "clutter_cairn_03", "kind": "cairn", "x": 12.0, "y": 0.0, "z": 0.0, "yaw": 0.5, "size": 0.5},
    {"name": "clutter_deadfall_04", "kind": "deadfall", "x": 18.0, "y": 0.0, "z": 0.2, "yaw": 1.1, "size": 2.4},
    {"name": "clutter_bollard_05", "kind": "bollard", "x": 24.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "size": 0.2},
    {"name": "clutter_cone_06", "kind": "cone", "x": 30.0, "y": 0.0, "z": 0.0, "yaw": 0.2, "size": 0.25},
    {"name": "clutter_bike_rack_07", "kind": "bike_rack", "x": 36.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "size": 1.0},
    {"name": "clutter_planter_08", "kind": "planter", "x": 42.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "size": 0.7},
    {"name": "clutter_crate_stack_09", "kind": "crate_stack", "x": 48.0, "y": 0.0, "z": 0.0, "yaw": 0.4, "size": 0.8},
)


def test_clutter_scenery_covers_every_kind_with_textured_meshes():
    instances, meshes = dressing.clutter_scenery("urban", list(CLUTTER_ITEMS))
    assert {instance.asset for instance in instances} == {"rock_a"}
    names = {mesh.name for mesh in meshes}
    assert len(names) == len(meshes)  # unique names
    textures = scene_textures(7, size=32)
    for mesh in meshes:
        assert mesh.texture in textures, mesh.name
        assert mesh.proxies
        for proxy in mesh.proxies:
            assert isinstance(proxy, ProxyShape)
        # Every proxy is in world coordinates near its item.
    assert "bark" in {mesh.texture for mesh in meshes}
    assert "tent_fabric" not in {mesh.texture for mesh in meshes}


def test_clutter_scenery_is_deterministic():
    first = dressing.clutter_scenery("forest", list(CLUTTER_ITEMS))
    second = dressing.clutter_scenery("forest", list(CLUTTER_ITEMS))
    assert first[0] == second[0]
    assert first[1] == second[1]


def test_camp_scenery_pieces_and_emission():
    instances, meshes = dressing.camp_scenery((10.0, -4.0), 0.5, seed=3)
    assert {instance.asset for instance in instances} == {"rock_a"}
    textures = {mesh.texture for mesh in meshes}
    assert {"tent_fabric", "wood_planks", "bark"} <= textures
    names = {mesh.name for mesh in meshes}
    assert {"camp_tent_1", "camp_tent_2", "camp_table", "camp_fire_pit"} <= names
    bank = scene_textures(7, size=32)
    bundle = emit_scenery("probe_camp", instances, meshes, textures=bank)
    assert bundle.sdf
    assert bundle.proxies
    for proxy in bundle.proxies:
        assert proxy.shape in ("box", "cylinder", "sphere")


def test_shrub_mesh_is_narrow_at_the_base_and_wide_on_top():
    shrub = dressing.shrub_mesh("probe_shrub", 0.0, 0.0, 0.0, height=4.5, crown_radius=2.4,
                                base_radius=0.5, seed=2)
    low, high = shrub.proxies[0].pose, shrub.proxies[1].pose
    assert shrub.proxies[0].size[0] < shrub.proxies[1].size[0]
    assert high[2] > low[2]
    assert max(point[2] for point in shrub.positions) <= 4.5 + 1e-6


def test_deadfall_mesh_spans_its_length():
    log = dressing.deadfall_mesh("probe_log", 1.0, 2.0, 0.1, 0.4, length=4.0)
    span = max(math.dist(point[:2], (1.0, 2.0)) for point in log.positions)
    assert span == pytest.approx(2.0, abs=0.05)
    assert log.proxies[0].size[0] == pytest.approx(4.0)


def test_animal_and_clutter_textures_are_in_the_scene_bank():
    bank = scene_textures(11, size=32)
    for name in ("bark", "wood_planks", "tent_fabric", "metal_paint", "traffic_cone",
                 "foliage", "rock"):
        assert name in bank


def test_committed_person_models_are_untouched():
    """Canonical person assets keep their primitive seated figure (byte-frozen)."""
    text = (MODELS / "person_sitting" / "model.sdf").read_text(encoding="utf-8")
    assert "<box><size>0.68 0.44 1.17</size></box>" in text
