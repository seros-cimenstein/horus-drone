from __future__ import annotations

import math
import random
import time
from pathlib import Path

import pytest

from sim.worldgen.geometry import CollisionWorld, Primitive, load_collisions

ROOT = Path(__file__).resolve().parents[1]


def _rotation(pose):
    roll, pitch, yaw = pose[3:]
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _world_point(pose, local):
    rotation = _rotation(pose)
    return tuple(pose[i] + sum(rotation[i][j] * local[j] for j in range(3)) for i in range(3))


def _local_point(pose, point):
    rotation = _rotation(pose)
    delta = tuple(point[i] - pose[i] for i in range(3))
    return tuple(sum(rotation[j][i] * delta[j] for j in range(3)) for i in range(3))


def _analytic_distance(shape, size, point):
    x, y, z = point
    if shape == "sphere":
        return math.sqrt(x * x + y * y + z * z) - size[0]
    if shape == "box":
        q = tuple(abs(point[i]) - size[i] / 2 for i in range(3))
        return math.sqrt(sum(max(v, 0) ** 2 for v in q)) + min(max(q), 0)
    radial = math.hypot(x, y)
    radius, length = size
    if shape == "cylinder":
        qr, qz = radial - radius, abs(z) - length / 2
        return math.hypot(max(qr, 0), max(qz, 0)) + min(max(qr, qz), 0)
    # Brute-force distance to the cone's side and base surface in its axial
    # profile; the angular coordinate is immaterial by rotational symmetry.
    half = length / 2
    ax, ay = -radius, length
    t = max(0.0, min(1.0, ((radial - radius) * ax + (z + half) * ay) / (ax * ax + ay * ay)))
    side_r, side_z = radius + t * ax, -half + t * ay
    side_distance = math.hypot(radial - side_r, z - side_z)
    base_distance = math.hypot(max(radial - radius, 0), z + half)
    distance = min(side_distance, base_distance)
    inside = -half <= z <= half and radial <= radius * (half - z) / length
    return -distance if inside else distance


def _inside(primitive, point):
    local = _local_point(primitive.transform, point)
    x, y, z = local
    if primitive.shape == "sphere":
        return x * x + y * y + z * z <= primitive.size[0] ** 2
    if primitive.shape == "box":
        return all(abs(local[i]) <= primitive.size[i] / 2 for i in range(3))
    radius, length = primitive.size
    if primitive.shape == "cylinder":
        return x * x + y * y <= radius * radius and abs(z) <= length / 2
    return abs(z) <= length / 2 and math.hypot(x, y) <= radius * (length / 2 - z) / length


def _sample_segment_hit(primitive, a, b, samples=1800):
    for i in range(1, samples):  # open segment; endpoint contacts do not count
        t = i / samples
        point = tuple(a[j] + t * (b[j] - a[j]) for j in range(3))
        if _inside(primitive, point):
            return True
    return False


@pytest.mark.parametrize("shape", ["box", "sphere", "cylinder", "cone"])
def test_random_oriented_clearances_match_analytic_oracle(shape):
    rng = random.Random(73021 + ("box", "sphere", "cylinder", "cone").index(shape))
    primitives = []
    local_queries = []
    for i in range(500):
        if shape == "box":
            size = tuple(rng.uniform(0.3, 4.0) for _ in range(3))
            extent = max(size) / 2 + 1
        elif shape == "sphere":
            size = (rng.uniform(0.3, 2.0),)
            extent = size[0] + 1
        else:
            size = (rng.uniform(0.3, 2.0), rng.uniform(0.3, 4.0))
            extent = math.hypot(size[0], size[1] / 2) + 1
        # Sparse grid placement keeps each oracle query nearest to its shape.
        pose = (i % 25 * 30.0, i // 25 * 30.0, rng.uniform(-3, 3), *(rng.uniform(-math.pi, math.pi) for _ in range(3)))
        primitives.append(Primitive(f"m{i}/c", f"m{i}", shape, pose, size, True))
        local_queries.append(tuple(rng.uniform(-extent, extent) for _ in range(3)))
    world = CollisionWorld(primitives)
    for primitive, local in zip(primitives, local_queries):
        point = _world_point(primitive.transform, local)
        actual, nearest = world.clearance(point)
        expected = _analytic_distance(shape, primitive.size, local)
        assert nearest is primitive
        assert actual == pytest.approx(expected, abs=1e-8)


def test_random_blocking_matches_dense_inside_sampling_and_first_hit():
    rng = random.Random(87311)
    for shape in ("box", "sphere", "cylinder", "cone"):
        for i in range(80):
            if shape == "box":
                size = tuple(rng.uniform(0.8, 3.0) for _ in range(3))
            elif shape == "sphere":
                size = (rng.uniform(0.6, 1.8),)
            else:
                size = (rng.uniform(0.6, 1.8), rng.uniform(0.8, 3.0))
            pose = (rng.uniform(-2, 2), rng.uniform(-2, 2), rng.uniform(-1, 1),
                    rng.uniform(-math.pi, math.pi), rng.uniform(-math.pi, math.pi),
                    rng.uniform(-math.pi, math.pi))
            primitive = Primitive(f"{shape}{i}/collision", f"{shape}{i}", shape, pose, size, True)
            local_a = tuple(rng.uniform(-5, 5) for _ in range(3))
            local_b = tuple(rng.uniform(-5, 5) for _ in range(3))
            a, b = _world_point(pose, local_a), _world_point(pose, local_b)
            world = CollisionWorld([primitive])
            actual = world.blocking(a, b)
            sampled = _sample_segment_hit(primitive, a, b)
            if shape == "cone":
                # The documented cone ray test may conservatively return false positives.
                assert not sampled or actual is not None
            else:
                assert (actual is not None) == sampled

    near = Primitive("near/c", "near", "sphere", (2, 0, 0, 0, 0, 0), (0.5,), True)
    far = Primitive("far/c", "far", "sphere", (5, 0, 0, 0, 0, 0), (0.5,), True)
    world = CollisionWorld([far, near])
    assert world.blocking((0, 0, 0), (8, 0, 0)) is near
    assert world.blocking((0, 0, 0), (8, 0, 0), ignore=frozenset({"near"})) is far


def test_segment_clearance_is_within_half_step_of_dense_minimum():
    rng = random.Random(30303)
    obstacles = [
        Primitive(f"m{i}/c", f"m{i}", shape, (rng.uniform(-3, 3), rng.uniform(-3, 3), rng.uniform(-2, 2),
                  rng.uniform(-1.2, 1.2), rng.uniform(-1.2, 1.2), rng.uniform(-math.pi, math.pi)), size, True)
        for i, (shape, size) in enumerate([
            ("box", (2.0, 1.5, 2.4)), ("sphere", (1.5,)), ("cylinder", (1.0, 2.0)),
            ("cone", (1.1, 2.5)), ("box", (1.3, 2.1, 1.4)),
        ])
    ]
    world = CollisionWorld(obstacles)
    for _ in range(30):
        a = tuple(rng.uniform(-5, 5) for _ in range(3))
        b = tuple(rng.uniform(-5, 5) for _ in range(3))
        radius, step = rng.uniform(0, 0.4), 0.25
        value, _ = world.segment_clearance(a, b, radius=radius, step=step)
        length = math.dist(a, b)
        samples = max(1000, math.ceil(length / 0.002))
        dense_min = min(
            world.clearance(tuple(a[j] + i / samples * (b[j] - a[j]) for j in range(3)))[0] - radius
            for i in range(samples + 1)
        )
        assert dense_min - 1e-6 <= value <= dense_min + step / 2 + 0.003


def test_load_collisions_composes_models_links_includes_and_filters_dynamic(tmp_path):
    assets = tmp_path / "models"
    included = assets / "crate"
    included.mkdir(parents=True)
    (included / "model.sdf").write_text(
        '<sdf><model name="crate"><static>true</static><link name="l">'
        '<collision name="c"><pose>0 0 1 0 0 0</pose><geometry><box><size>2 2 2</size></box>'
        '</geometry></collision></link></model></sdf>', encoding="utf-8"
    )
    world_file = tmp_path / "nested.sdf"
    world_file.write_text('''<sdf><world name="w">
      <model name="outer"><static>true</static><pose degrees="true">10 0 0 18 -25 70</pose>
        <link name="link"><pose>1 0 0 0 0 0</pose><collision name="inline">
          <pose>0 0 0 0 0 0</pose><geometry><sphere><radius>1</radius></sphere></geometry>
        </collision></link>
        <include><uri>model://crate</uri><name>included</name><pose>2 0 0 0 0 0</pose></include>
        <model name="moving"><static>false</static><link name="l"><collision name="d">
          <geometry><sphere><radius>1</radius></sphere></geometry>
        </collision></link></model>
      </model>
    </world></sdf>''', encoding="utf-8"
    )
    static = load_collisions(world_file, resource_paths=[assets])
    assert [item.name for item in static] == ["outer/inline", "outer/included/c"]
    outer_pose = (10, 0, 0, math.radians(18), math.radians(-25), math.radians(70))
    assert static[0].transform[:3] == pytest.approx(_world_point(outer_pose, (1, 0, 0)))
    assert static[1].transform[:3] == pytest.approx(_world_point(outer_pose, (2, 0, 1)))
    expected_rotation = [value for row in _rotation(outer_pose) for value in row]
    for primitive in static:
        assert [value for row in _rotation(primitive.transform) for value in row] == pytest.approx(expected_rotation)
    assert all(item.static for item in static)
    dynamic = load_collisions(world_file, resource_paths=[assets], include_dynamic=True)
    assert [item.name for item in dynamic] == ["outer/inline", "outer/included/c", "outer/moving/d"]
    assert not dynamic[-1].static


def test_max_top_uses_footprint_and_disc_radius():
    yaw_box = Primitive("box/c", "box", "box", (0, 0, 5, 0, 0, math.pi / 4), (2, 2, 4), True)
    box_world = CollisionWorld([yaw_box])
    assert box_world.max_top(0, 0, 0) == pytest.approx(7.0)
    assert box_world.max_top(1.7, 0, 0) == -math.inf
    assert box_world.max_top(1.7, 0, 0.3) == pytest.approx(7.0)

    sphere_world = CollisionWorld([Primitive("sphere/c", "sphere", "sphere", (5, 0, 4, 0, 0, 0), (1,), True)])
    assert sphere_world.max_top(5.5, 0, 0.2) == pytest.approx(4 + math.sqrt(1 - 0.3 ** 2))
    cylinder_world = CollisionWorld([Primitive("cylinder/c", "cylinder", "cylinder", (0, 0, 2, 0, 0, 0), (1, 4), True)])
    assert cylinder_world.max_top(1.5, 0, 0.6) == pytest.approx(4.0)
    cone_world = CollisionWorld([Primitive("cone/c", "cone", "cone", (0, 0, 0, 0, 0, 0), (2, 4), True)])
    assert cone_world.max_top(1, 0, 0) == pytest.approx(0.0)


def test_canonical_worlds_load_quickly_and_have_open_home_column():
    models = ROOT / "sim/models"
    worlds = ROOT / "sim/worlds"
    start = time.perf_counter()
    forest = CollisionWorld(load_collisions(worlds / "horus_forest.sdf", resource_paths=[models, worlds]))
    urban = CollisionWorld(load_collisions(worlds / "horus_urban.sdf", resource_paths=[models, worlds]))
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    assert len(forest.primitives) > 0
    assert len(urban.primitives) > 0
    for collision_world in (forest, urban):
        column_clearance, _ = collision_world.segment_clearance((0, 0, 3.5), (0, 0, 40), step=0.2)
        # Sampling may overestimate the true minimum by at most step/2.
        assert column_clearance >= 3.1
    building = next(p for p in urban.primitives if p.model == "urban_building_03")
    distance, nearest = urban.clearance((-2, -32, 5))
    assert nearest is building
    assert distance < 0
    assert elapsed < 2.0
    forest_dynamic = load_collisions(worlds / "horus_forest.sdf", resource_paths=[models, worlds], include_dynamic=True)
    assert len(forest_dynamic) > len(forest.primitives)
    assert any(primitive.model == "civ_hiker_01" for primitive in forest_dynamic)
