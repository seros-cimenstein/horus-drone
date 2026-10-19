"""Registry invariants for the imported scenery assets used by procedural worlds.

The registry (``sim/worldgen/assets.py``) hard-codes measurements taken from the committed
Fuel meshes, so these tests re-measure the meshes and check the numbers still describe them.
"""

from __future__ import annotations

import pytest

from sim.worldgen.assets import (
    ASSETS,
    MODELS,
    asset_bounds,
    mesh_bounds,
    mesh_file,
    model_sdf,
    proxy_bounds,
)

KEYS = sorted(ASSETS)
KINDS = {"tree", "rock", "lamp", "bench", "vehicle", "person_sitting"}


def test_registry_covers_the_imported_model_directories():
    for key, asset in ASSETS.items():
        assert asset.key == key
        assert asset.kind in KINDS
        assert asset.model_dir.startswith("horus_")
        directory = MODELS / asset.model_dir
        assert directory.is_dir(), f"{key}: {directory} is not imported"
        model_config = (directory / "model.config").read_text(encoding="utf-8")
        assert f"<name>{asset.model_dir}</name>" in model_config
        # <include uri="model://..."> needs the config to point at the SDF (gz-sim error 40 otherwise)
        assert "<sdf version=\"1.9\">model.sdf</sdf>" in model_config


@pytest.mark.parametrize("key", KEYS)
def test_visual_meshes_exist_and_are_readable(key):
    asset = ASSETS[key]
    assert asset.visuals
    for visual in asset.visuals:
        assert visual.mesh_uri.startswith(f"model://{asset.model_dir}/")
        path = mesh_file(visual.mesh_uri)
        assert path.is_file(), f"{key}/{visual.name}: missing {path}"
        assert visual.scale > 0
        bounds = mesh_bounds(path, visual.submesh)  # raises for an unknown submesh
        assert bounds.size[0] > 0 and bounds.size[1] > 0 and bounds.size[2] > 0


@pytest.mark.parametrize("key", KEYS)
def test_provenance_declares_the_registry_licence(key):
    asset = ASSETS[key]
    provenance = MODELS / asset.model_dir / "PROVENANCE.txt"
    assert provenance.is_file()
    text = provenance.read_text(encoding="utf-8")
    markers = {"CC0 1.0": "CC0 1.0", "CC BY 4.0": "CC BY 4.0"}
    assert asset.license in markers
    assert markers[asset.license] in text
    assert asset.source.startswith("Gazebo Fuel ")


def test_readme_attributes_every_imported_asset():
    readme = (MODELS.parents[1] / "README.md").read_text(encoding="utf-8")
    for asset in ASSETS.values():
        fuel_name = asset.source.split("Gazebo Fuel ", 1)[1].split(" v", 1)[0]
        assert fuel_name in readme, f"{asset.key}: {fuel_name} missing from README attribution"
        assert asset.license in readme


@pytest.mark.parametrize("key", KEYS)
def test_proxies_sit_inside_the_measured_mesh_bounds(key):
    asset = ASSETS[key]
    assert asset.proxies
    bounds = asset_bounds(asset)
    for proxy in asset.proxies:
        proxy_bounds_value = proxy_bounds(proxy)
        for axis, name in enumerate("xyz"):
            # Registry numbers are recorded to 0.01 mm; 0.1 mm absorbs that rounding while still
            # catching any proxy that really pokes out of the mesh.
            assert proxy_bounds_value.min[axis] >= bounds.min[axis] - 1e-4, (
                f"{key}: {proxy.shape} proxy undercuts the mesh on {name}")
            assert proxy_bounds_value.max[axis] <= bounds.max[axis] + 1e-4, (
                f"{key}: {proxy.shape} proxy exceeds the mesh on {name}")
    # Every asset stands on the ground: its proxies reach z=0.
    assert min(proxy_bounds(proxy).min[2] for proxy in asset.proxies) == pytest.approx(0.0, abs=1e-3)


@pytest.mark.parametrize("key", KEYS)
def test_declared_dimensions_match_a_fresh_measurement(key):
    asset = ASSETS[key]
    bounds = asset_bounds(asset)
    assert asset.native_height_m == pytest.approx(bounds.size[2], abs=1e-3)
    assert asset.footprint_radius_m == pytest.approx(bounds.radius_m, abs=1e-3)


@pytest.mark.parametrize("key", KEYS)
def test_standalone_model_sdf_matches_the_registry(key):
    asset = ASSETS[key]
    committed = (MODELS / asset.model_dir / "model.sdf").read_text(encoding="utf-8")
    assert committed == model_sdf(asset)
