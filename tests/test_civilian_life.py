from __future__ import annotations

import math
import random
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml

from sim.campaign.civilian_spec import load_scenario, point_in_polygon, select_agents
from sim.scripts.civilian_life import MotionEngine


def _load(tmp_path: Path, agents, **fields):
    tmp_path.mkdir(parents=True, exist_ok=True)
    scenario_path = tmp_path / "scenario.yaml"
    scenario_path.write_text(
        yaml.safe_dump({"world": "test_world", "agents": agents, **fields}), encoding="utf-8"
    )
    return load_scenario(scenario_path)


def _route(model="walker", *, mode="once", waypoints=None, **fields):
    return {
        "model": model,
        "kind": "pedestrian",
        "behavior": "route",
        "speed_m_s": 1.0,
        "mode": mode,
        "waypoints": ([{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0}] if waypoints is None else waypoints),
        **fields,
    }


def _route_nodes(count: int, mode: str) -> list[int]:
    if mode == "loop":
        return list(range(count))
    if mode == "pingpong":
        return list(range(count)) + list(range(count - 2, 0, -1))
    return list(range(count))


def _route_edges(agent):
    points = agent["waypoints"]
    nodes = _route_nodes(len(points), agent["mode"])
    edge_count = len(nodes) if agent["mode"] in ("loop", "pingpong") else len(nodes) - 1
    return [
        (nodes[index], nodes[(index + 1) % len(nodes)])
        for index in range(edge_count)
    ]


def _route_position(agent, elapsed_s):
    """Independent continuous-time route oracle; generated segment and hold times are 50 ms multiples."""
    points = agent["waypoints"]
    mode = agent["mode"]
    speed = agent["speed_m_s"]
    nodes = _route_nodes(len(points), mode)
    edge_count = len(nodes) if mode in ("loop", "pingpong") else len(nodes) - 1
    edges = [(nodes[index], nodes[(index + 1) % len(nodes)]) for index in range(edge_count)]
    lengths = [
        math.hypot(
            points[end]["x"] - points[start]["x"],
            points[end]["y"] - points[start]["y"],
        )
        for start, end in edges
    ]
    total_length = sum(lengths)
    distance = speed * agent.get("start_offset_s", 0.0)
    if mode == "once":
        if distance >= total_length - 1e-12:
            return points[-1]["x"], points[-1]["y"]
    else:
        distance %= total_length

    edge_index = 0
    progress = 0.0
    for index, length in enumerate(lengths):
        if distance < length - 1e-12:
            edge_index, progress = index, distance
            break
        distance -= length
        edge_index = index + 1
        if distance <= 1e-12:
            if edge_index == edge_count:
                if mode == "once":
                    return points[-1]["x"], points[-1]["y"]
                edge_index = 0
            break
    else:
        if mode == "once":
            return points[-1]["x"], points[-1]["y"]
        edge_index, progress = 0, 0.0

    def position():
        start_index, end_index = edges[edge_index]
        fraction = min(1.0, progress / lengths[edge_index])
        start, end = points[start_index], points[end_index]
        return (
            start["x"] + (end["x"] - start["x"]) * fraction,
            start["y"] + (end["y"] - start["y"]) * fraction,
        )

    remaining_s = elapsed_s
    hold_remaining_s = 0.0
    while remaining_s > 1e-12:
        if hold_remaining_s > 0.0:
            spent = min(remaining_s, hold_remaining_s)
            remaining_s -= spent
            hold_remaining_s -= spent
            if remaining_s <= 1e-12:
                return position()
        travel_s = (lengths[edge_index] - progress) / speed
        if remaining_s < travel_s - 1e-12:
            progress += speed * remaining_s
            return position()
        remaining_s = max(0.0, remaining_s - travel_s)
        _start, target_index = edges[edge_index]
        progress = 0.0
        edge_index += 1
        if edge_index == edge_count:
            if mode == "once":
                return points[-1]["x"], points[-1]["y"]
            edge_index = 0
        hold_remaining_s = points[target_index].get("hold_s", 0.0)
        if remaining_s <= 1e-12:
            return position()
    return position()


def _distance_to_route(point, agent):
    points = agent["waypoints"]
    minimum = math.inf
    for start_index, end_index in _route_edges(agent):
        start, end = points[start_index], points[end_index]
        dx, dy = end["x"] - start["x"], end["y"] - start["y"]
        length_squared = dx * dx + dy * dy
        fraction = max(
            0.0,
            min(1.0, ((point[0] - start["x"]) * dx + (point[1] - start["y"]) * dy) / length_squared),
        )
        closest = (start["x"] + fraction * dx, start["y"] + fraction * dy)
        minimum = min(minimum, math.hypot(point[0] - closest[0], point[1] - closest[1]))
    return minimum


def _random_polygon(rng):
    count = rng.randint(4, 7)
    phase = rng.uniform(0.0, 2.0 * math.pi)
    step = 2.0 * math.pi / count
    scale = rng.uniform(2.0, 4.5)
    return [
        [
            scale * rng.uniform(0.96, 1.04) * math.cos(
                phase + index * step + rng.uniform(-0.025, 0.025) * step
            ),
            scale * rng.uniform(0.96, 1.04) * math.sin(
                phase + index * step + rng.uniform(-0.025, 0.025) * step
            ),
        ]
        for index in range(count)
    ]


def _random_route(rng, model, *, mode, kind="pedestrian", holds=True):
    speed = rng.uniform(0.55, 2.3) if kind == "pedestrian" else rng.uniform(1.4, 4.0)
    count = rng.randint(3, 4)
    duration_ticks = rng.randint(8, 10)
    segment_length = speed * duration_ticks * 0.05
    center_x, center_y = rng.uniform(-2.0, 2.0), rng.uniform(-2.0, 2.0)
    if kind == "vehicle":
        # Vehicles drive curvature-bounded filleted paths (5 m default turn radius), so a
        # metre-scale random polygon is an undrivable route that the engine must reject.
        # Fuzz vehicles on a straight route: with no corners to fillet the driven path is
        # exactly the declared polyline (the oracle below stays exact) while speed,
        # acceleration limits and update-cadence independence are still exercised.
        heading = rng.uniform(-math.pi, math.pi)
        travelled = 0.0
        waypoints = []
        for index in range(rng.randint(2, 4)):
            if index:
                travelled += max(6.0, segment_length) * rng.uniform(0.8, 1.25)
            waypoints.append(
                {
                    "x": center_x + travelled * math.cos(heading),
                    "y": center_y + travelled * math.sin(heading),
                    "hold_s": 0.0,
                }
            )
    else:
        radius = segment_length / (2.0 * math.sin(math.pi / count))
        phase = rng.uniform(-math.pi, math.pi)
        waypoints = []
        for index in range(count):
            angle = phase + index * 2.0 * math.pi / count
            hold_ticks = rng.randint(0, 2) if holds and index else 0
            waypoints.append(
                {
                    "x": center_x + radius * math.cos(angle),
                    "y": center_y + radius * math.sin(angle),
                    "hold_s": hold_ticks * 0.05,
                }
            )
    agent = {
        "model": model,
        "kind": kind,
        "behavior": "route",
        "speed_m_s": speed,
        "mode": mode,
        "start_offset_s": rng.randint(1, 6) * 0.05,
        "waypoints": waypoints,
        "z_offset": rng.uniform(-0.5, 1.5),
    }
    if kind == "vehicle":
        agent["accel_m_s2"] = rng.uniform(0.6, 2.5)
    return agent


def _random_scenario(tmp_path, seed):
    rng = random.Random(seed)
    agents = [
        _random_route(rng, f"{mode}_{seed}", mode=mode)
        for mode in ("loop", "pingpong", "once")
    ]
    agents.append(_random_route(rng, f"vehicle_{seed}", mode="once", kind="vehicle", holds=False))
    for index in range(2):
        agents.append(
            {
                "model": f"wander_{index}_{seed}",
                "kind": "pedestrian",
                "behavior": "wander",
                "speed_m_s": rng.uniform(0.4, 1.4),
                "area": _random_polygon(rng),
                "pause_s": [rng.randint(0, 3) * 0.05, rng.randint(4, 10) * 0.05],
                "z_offset": rng.uniform(-0.5, 1.5),
            }
        )
    agents.append(
        {
            "model": f"idle_{seed}",
            "kind": "pedestrian",
            "behavior": "idle",
            "pose": {
                "x": rng.uniform(-3.0, 3.0),
                "y": rng.uniform(-3.0, 3.0),
                "yaw_deg": rng.uniform(-180.0, 180.0),
            },
            "fidget_deg": rng.uniform(0.0, 12.0),
            "fidget_period_s": rng.uniform(2.0, 10.0),
            "z_offset": rng.uniform(-0.5, 1.5),
        }
    )
    heights = np.asarray(
        [[rng.uniform(-4.0, 4.0) for _ in range(21)] for _ in range(21)],
        dtype=float,
    )
    scenario_dir = tmp_path / f"seed-{seed}"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    np.save(scenario_dir / "ground.npy", heights)
    ground_origin = [rng.uniform(-30.0, -20.0), rng.uniform(-30.0, -20.0)]
    ground_cell_m = rng.uniform(1.5, 3.5)
    scenario = _load(
        scenario_dir,
        agents,
        rate_hz=rng.choice([5.0, 10.0, 15.0, 20.0, 30.0]),
        seed=seed,
        ground={"origin_xy": ground_origin, "cell_m": ground_cell_m, "file": "ground.npy"},
    )
    return scenario, agents


def _angle_delta(after, before):
    return (after - before + math.pi) % (2.0 * math.pi) - math.pi


def test_seeded_procedural_scenarios_preserve_motion_invariants(tmp_path):
    for seed in range(200):
        scenario, raw_agents = _random_scenario(tmp_path, seed)
        raw_by_model = {agent["model"]: agent for agent in raw_agents}
        engine = MotionEngine(scenario)
        reordered = None
        if seed < 50:
            shuffled_agents = list(scenario.agents)
            random.Random(seed).shuffle(shuffled_agents)
            if shuffled_agents == list(scenario.agents):
                shuffled_agents.reverse()
            reordered = MotionEngine(replace(scenario, agents=tuple(shuffled_agents)))
        previous_poses = None
        previous_speeds = {agent.model: 0.0 for agent in scenario.agents}

        # Irregular queries still match the continuous oracle because route event times align to 50 ms.
        sample_rng = random.Random(seed + 0xA11CE)
        sample_times = [0.0]
        while sample_times[-1] < 3.5:
            sample_times.append(
                min(3.5, sample_times[-1] + sample_rng.uniform(0.013, 0.17))
            )
        for tick, sim_time in enumerate(sample_times):
            poses = engine.update(sim_time)
            context = f"seed={seed} t={sim_time:.3f}"
            if tick % 5 == 0 and reordered is not None:
                reverse_poses = reordered.update(sim_time)
                assert poses == reverse_poses, f"agent-order determinism failed: {context}"

            dt = sim_time - sample_times[tick - 1] if tick else 0.0
            for spec in scenario.agents:
                pose = poses[spec.model]
                raw = raw_by_model[spec.model]
                assert pose[2] == pytest.approx(
                    scenario.ground.height(pose[0], pose[1]) + spec.z_offset,
                    abs=1e-6,
                ), f"ground height mismatch for {spec.model}: {context}"
                if spec.behavior == "route":
                    assert _distance_to_route(pose[:2], raw) <= 1e-6, (
                        f"route left polyline for {spec.model}: {context}"
                    )
                    if spec.kind == "pedestrian":
                        expected = _route_position(raw, sim_time)
                        assert pose[:2] == pytest.approx(expected, abs=1e-6), (
                            f"route phase/hold mismatch for {spec.model}: {context}"
                        )
                elif spec.behavior == "wander":
                    assert point_in_polygon(pose[:2], spec.area), (
                        f"wander left polygon for {spec.model}: {context}"
                    )

                state = engine._output_states[spec.model]
                if spec.kind == "vehicle":
                    assert state.speed_m_s <= spec.speed_m_s + 1e-6, (
                        f"vehicle exceeded route speed for {spec.model}: {context}"
                    )
                    if tick:
                        # The driver accelerates within accel_m_s2 and brakes within
                        # decel_m_s2 (defaulting to max(accel, 1.5)), so the per-step speed
                        # change is bounded by the larger of the two.
                        limit = max(spec.accel_m_s2, spec.decel_m_s2 or 0.0)
                        assert abs(state.speed_m_s - previous_speeds[spec.model]) <= (
                            limit * dt + 1e-6
                        ), f"vehicle acceleration limit failed for {spec.model}: {context}"
                if tick:
                    yaw_rate = abs(
                        _angle_delta(pose[3], previous_poses[spec.model][3])
                    ) / dt
                    if spec.kind == "vehicle":
                        max_speed = min(
                            spec.speed_m_s,
                            max(state.speed_m_s, previous_speeds[spec.model])
                            + spec.accel_m_s2 * dt,
                        )
                        max_yaw_rate = max_speed / 5.0
                    else:
                        max_yaw_rate = math.pi
                    assert yaw_rate <= max_yaw_rate + 1e-6, (
                        f"yaw-rate limit failed for {spec.model}: {context}"
                    )
                if tick and spec.behavior == "route":
                    distance = math.dist(
                        pose[:2],
                        previous_poses[spec.model][:2],
                    )
                    assert distance <= spec.speed_m_s * dt + 1e-6, (
                        f"route exceeded speed for {spec.model}: {context}"
                    )
                previous_speeds[spec.model] = state.speed_m_s
            previous_poses = poses

        once_agent = raw_by_model[f"once_{seed}"]
        once_pose = previous_poses[once_agent["model"]]
        once_last = once_agent["waypoints"][-1]
        assert once_pose[:2] == pytest.approx(
            (once_last["x"], once_last["y"]), abs=1e-6
        ), f"once route did not finish: seed={seed}"


def test_seeded_random_scenarios_are_cadence_independent(tmp_path):
    checkpoints = [0.5 * index for index in range(1, 7)]
    for seed in range(50):
        scenario, _agents = _random_scenario(tmp_path, 10_000 + seed)
        regular = MotionEngine(scenario)
        irregular = MotionEngine(scenario)
        regular.update(0.0)
        irregular.update(0.0)
        irregular_rng = random.Random(seed)
        current_time = 0.0
        irregular_common_poses = {}
        for checkpoint in checkpoints:
            while current_time < checkpoint:
                next_time = min(checkpoint, current_time + irregular_rng.uniform(0.013, 0.17))
                irregular.update(next_time)
                current_time = next_time
            irregular_common_poses[checkpoint] = irregular.update(checkpoint)

        for tick in range(1, 31):
            sim_time = tick * 0.1
            regular_poses = regular.update(sim_time)
            if tick % 5 == 0:
                expected = irregular_common_poses[sim_time]
                for model, pose in regular_poses.items():
                    assert pose == pytest.approx(expected[model], abs=1e-6), (
                        f"cadence changed {model} pose at t={sim_time}: seed={seed}"
                    )


def test_crosswalk_top_level_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="crosswalks"):
        _load(tmp_path, [_route()], crosswalks=[])


def test_randomized_agent_validation_errors_name_agent_and_field(tmp_path):
    mutation_names = [
        "missing_speed",
        "unknown_key",
        "wrong_type",
        "negative_speed",
        "empty_waypoints",
        "unknown_behavior",
        "unknown_kind",
        "unknown_mode",
    ]
    for seed in range(20):
        rng = random.Random(seed)
        for mutation in mutation_names:
            model = f"fuzz_{seed}_{mutation}"
            agent = _random_route(rng, model, mode="once")
            if mutation == "missing_speed":
                agent.pop("speed_m_s")
                field = "speed_m_s"
            elif mutation == "unknown_key":
                agent["unexpected"] = rng.uniform(-1.0, 1.0)
                field = "unexpected"
            elif mutation == "wrong_type":
                agent["speed_m_s"] = "fast"
                field = "speed_m_s"
            elif mutation == "negative_speed":
                agent["speed_m_s"] = -rng.uniform(0.1, 5.0)
                field = "speed_m_s"
            elif mutation == "empty_waypoints":
                agent["waypoints"] = []
                field = "waypoints"
            elif mutation == "unknown_behavior":
                agent["behavior"] = "teleport"
                field = "behavior"
            elif mutation == "unknown_kind":
                agent["kind"] = "drone"
                field = "kind"
            else:
                agent["mode"] = "random"
                field = "mode"

            with pytest.raises(ValueError) as error:
                _load(tmp_path / f"validation-{seed}-{mutation}", [agent])
            message = str(error.value)
            assert f"agent {model!r}" in message and field in message, (
                f"validation omitted agent/field for seed={seed} mutation={mutation}: {message}"
            )


def test_group_selection_defaults_to_ambient_and_can_select_multiple_groups(tmp_path):
    scenario = _load(
        tmp_path,
        [
            _route("ambient_agent"),
            _route("commuter", group="commuters"),
            _route("traffic", group="traffic"),
        ],
    )
    assert [agent.model for agent in select_agents(scenario)] == ["ambient_agent"]
    assert [agent.model for agent in select_agents(scenario, ["commuters", "traffic"])] == [
        "commuter",
        "traffic",
    ]
    assert list(MotionEngine(scenario, ["commuters"]).update(0.0)) == ["commuter"]


def test_wander_startle_flees_within_the_declared_speed_cap(tmp_path):
    """A startle makes a wanderer flee above its walk speed but within max_speed_m_s and
    accel_m_s2. The declared cap is max_speed_m_s, not the nominal walk speed, so legitimate
    flee motion must stay inside it (and the civilian check must bound by it)."""
    meadow = [[21.6757, -12.3315], [19.0454, -5.9814], [12.6953, -3.3511], [6.3452, -5.9814],
              [3.7149, -12.3315], [6.3452, -18.6816], [12.6953, -21.3119], [19.0454, -18.6816]]
    scenario = _load(
        tmp_path,
        [{
            "model": "ani_deer_01", "kind": "animal", "species": "deer", "behavior": "wander",
            "speed_m_s": 1.22, "max_speed_m_s": 4.0, "accel_m_s2": 2.5,
            "area": meadow, "pause_s": [1.0, 6.0], "avoid_agents": True, "z_offset": 0.0,
        }],
        seed=12648,
        events=[{"t_s": 228.86, "model": "ani_deer_01", "type": "startle", "duration_s": 2.46}],
    )
    spec = scenario.agents[0]
    engine = MotionEngine(scenario)
    previous = None
    fastest = 0.0
    sim_time = 0.0
    while sim_time <= 233.0:
        pose = engine.update(sim_time)["ani_deer_01"]
        state = engine._output_states["ani_deer_01"]
        if previous is not None:
            dt = sim_time - previous[0]
            speed = math.dist(pose[:2], previous[1][:2]) / dt
            fastest = max(fastest, speed)
            assert state.speed_m_s <= spec.max_speed_m_s + 1e-6, f"cap exceeded at t={sim_time}"
            assert abs(state.speed_m_s - previous[2]) <= spec.accel_m_s2 * dt + 1e-6, (
                f"accel limit exceeded at t={sim_time}"
            )
        previous = (sim_time, pose, state.speed_m_s)
        sim_time += 0.5
    assert spec.speed_m_s < fastest <= spec.max_speed_m_s + 1e-6, (
        f"the startle flee should exceed the walk speed {spec.speed_m_s} but stay under "
        f"max_speed_m_s {spec.max_speed_m_s}; measured {fastest:.2f}"
    )
