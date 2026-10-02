from __future__ import annotations

import math
from pathlib import Path

from sim.scripts.procedural_campaign import (
    _parse_cli,
    aggregate_campaign,
    candidate_bbox,
    match_candidates_to_people,
    render_summary_markdown,
    score_detection_rows,
    select_largest_attribute_candidate,
)
from sim.scripts.campaign_episode import (
    Episode,
    _arguments,
    _clearance_below_threshold,
    _exception_summary,
    _hazard_path_is_collision_course,
    _mission_id_matches,
    _parse_camera_info,
    _parse_pose_info,
    _projection_validation,
    _static_gt_people,
    _topic_messages,
    compose_camera_pose,
)


def test_clearance_threshold_accepts_rounding_but_keeps_real_shortfall():
    assert not _clearance_below_threshold(1.498)
    assert not _clearance_below_threshold(1.5)
    assert _clearance_below_threshold(1.494)



def test_forest_scan_planner_falls_back_to_a_clear_inner_rectangle():
    from sim.worldgen.geometry import CollisionWorld, Primitive

    episode = object.__new__(Episode)
    episode.kind = "forest"
    episode.manifest = {"areas": {"clearing": {"center": [0.0, 0.0], "radius": 15.0}}}
    episode.world = CollisionWorld([
        Primitive("tree", "tree", "box", (12.0, 0.0, 10.0, 0.0, 0.0, 0.0),
                  (2.0, 2.0, 20.0), True)
    ])

    plan = episode._plan_scan_area()

    assert plan is not None
    rect, altitude, clearance = plan
    assert rect[4] == "clearing-core-80pct"
    assert altitude == 10.0
    assert clearance >= 2.0
    assert episode._scan_clearance(episode._scan_rectangles()[0], altitude) < 2.0


def test_forest_scan_skips_when_the_clearing_has_no_person():
    # A clear rectangle with nobody inside can never score detection coverage; the campaign
    # must say so instead of flying it and reporting a bare coverage failure.
    import asyncio

    from sim.worldgen.geometry import CollisionWorld

    episode = object.__new__(Episode)
    episode.kind = "forest"
    episode.manifest = {"areas": {"clearing": {"center": [0.0, 0.0], "radius": 15.0}},
                        "people": [{"model": "civ_far_away", "position": [60.0, 60.0]}]}
    episode.people = list(episode.manifest["people"])
    episode.world = CollisionWorld([])

    async def airborne() -> bool:
        return True

    episode._ensure_airborne = airborne

    result = asyncio.run(episode.test_scan())

    assert result["passed"] is False and result["status"] == "skipped"
    assert result["reason"] == "no manifest person inside the planned scan area"

def test_camera_pose_uses_model_pose_order_and_outdoor_sensor_offset():
    models = {
        "horus_iris": [2.0, -3.0, 10.0, 0.7, 0.0, 0.0],
        "iris_with_gimbal": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "gimbal": [0.0, -0.01, -0.1249, math.pi / 2, math.pi / 2, 0.0],
        "pitch_link": [0.0, 0.0147, 0.0043, -0.0015, 0.7854, 0.00005],
    }

    camera = compose_camera_pose(models, (0.0, 0.0, 0.0, -1.57, -1.57, 0.0))

    assert camera is not None
    assert camera["pose_chain"] == [
        "horus_iris", "iris_with_gimbal", "gimbal", "pitch_link", "camera_sensor",
    ]
    assert all(math.isclose(actual, expected, abs_tol=1e-4)
               for actual, expected in zip(camera["position"], (2.009731, -3.004878, 9.8898)))
    assert math.isclose(camera["yaw_deg"], math.degrees(0.7), abs_tol=0.1)
    assert math.isclose(camera["yaw_offset_deg"], 0.0, abs_tol=0.1)
    assert math.isclose(camera["pitch_deg"], -45.0, abs_tol=0.1)
    assert math.isclose(camera["roll_deg"], 0.0, abs_tol=0.1)


def test_camera_info_uses_runtime_intrinsics_for_horizontal_fov():
    parsed = _parse_camera_info(
        """
        width: 640
        height: 480
        intrinsics {
          k: [400, 0, 300, 0, 410, 240, 0, 0, 1]
        }
        """,
        "/world/horus_urban/model/horus_iris/link/camera/image/camera_info",
    )

    assert parsed["width"] == 640
    assert parsed["height"] == 480
    assert parsed["fx_px"] == 400
    assert parsed["fy_px"] == 410
    assert parsed["cx_px"] == 300
    assert parsed["cy_px"] == 240
    expected_hfov = math.degrees(math.atan2(300, 400) + math.atan2(340, 400))
    assert math.isclose(parsed["horizontal_fov_deg"], expected_hfov, abs_tol=1e-10)
    assert parsed["intrinsics_source"] == "intrinsics.k"


def test_camera_info_falls_back_to_projection_matrix():
    parsed = _parse_camera_info(
        """
        width: 640
        height: 480
        projection {
          p: [400, 0, 300, 0, 0, 410, 240, 0, 0, 0, 1, 0]
        }
        """,
        "/world/horus_urban/model/horus_iris/link/camera/image/camera_info",
    )

    assert parsed["fx_px"] == 400
    assert parsed["fy_px"] == 410
    assert parsed["cx_px"] == 300
    assert parsed["cy_px"] == 240
    assert parsed["intrinsics_source"] == "projection.p"
    expected_hfov = math.degrees(math.atan2(300, 400) + math.atan2(340, 400))
    assert math.isclose(parsed["horizontal_fov_deg"], expected_hfov, abs_tol=1e-10)


def test_project_uses_camera_info_intrinsics():
    from sim.worldgen.plan import Camera

    episode = object.__new__(Episode)
    episode.camera = Camera(hfov_deg=84.0, width=640, height=480)
    episode.camera_info = {"fx_px": 400.0, "fy_px": 410.0, "cx_px": 300.0, "cy_px": 240.0}
    pose = {"position": (0.0, 0.0, 0.0), "quaternion_xyzw": (0.0, 0.0, 0.0, 1.0)}

    lateral = episode._project(pose, (10.0, -1.0, 0.0))
    vertical = episode._project(pose, (10.0, 0.0, 1.0))

    assert lateral is not None and math.isclose(lateral[0], 340.0, abs_tol=1e-9)
    assert math.isclose(lateral[1], 240.0, abs_tol=1e-9)
    assert vertical is not None and math.isclose(vertical[0], 300.0, abs_tol=1e-9)
    assert math.isclose(vertical[1], 199.0, abs_tol=1e-9)


def test_outdoor_gimbal_camera_loads_no_hfov_overriding_plugin():
    import xml.etree.ElementTree as ET

    from sim.worldgen.plan import Camera

    sdf = Path(__file__).resolve().parents[1] / "sim" / "models" / "horus_gimbal_small_3d_outdoor" / "model.sdf"
    sensor = ET.parse(sdf).getroot().find("./model/link/sensor[@name='camera']")

    assert sensor is not None
    declared = float(sensor.findtext("./camera/horizontal_fov"))
    assert math.isclose(declared, math.radians(Camera().hfov_deg), rel_tol=1e-12)
    plugins = {plugin.get("name") for plugin in sensor.findall("./plugin")}
    # The upstream CameraZoomPlugin would force the render back to 2.0 rad while the
    # published CameraInfo keeps these SDF optics, so it must stay out of this sensor.
    assert "CameraZoomPlugin" not in plugins
    assert "GstCameraPlugin" in plugins


def test_topic_stream_keeps_nested_headers_in_one_message():
    message = (
        'header {\n  stamp {\n    sec: 1\n  }\n}\n'
        'pose {\n  header {\n    stamp {\n      sec: 1\n    }\n  }\n'
        '  name: "civ_a"\n  id: 1\n  position {\n    x: 1.0\n  }\n}\n'
        'pose {\n  header {\n    stamp {\n      sec: 1\n    }\n  }\n'
        '  name: "civ_b"\n  id: 2\n  position {\n    x: 2.0\n  }\n}\n'
    )
    stream = message.splitlines(keepends=True) + ["\n", "---\n", message]

    messages = list(_topic_messages(stream))

    assert [truncated for _text, truncated in messages] == [False, False]
    first, second = (text for text, _truncated in messages)
    assert first == message
    assert second == message
    assert first.count("header {") == 3
    assert second.count("pose {") == 2

    # A consecutive message that starts with its own top-level header must split too,
    # even though every entity inside the message also carries a nested header.
    header_split = list(_topic_messages(message.splitlines(keepends=True) * 2))
    assert [text for text, _truncated in header_split] == [message, message]


def test_topic_stream_flags_oversized_messages_without_dropping_lines():
    stream = ['header {\n', '  field: "x"\n', '}\n', 'next {\n', '  field: "y"\n', '}\n']

    messages = list(_topic_messages(stream, max_message_bytes=25))

    assert [truncated for _text, truncated in messages] == [True, False]
    assert all(text for text, _truncated in messages)
    assert "".join(text for text, _truncated in messages) == "".join(stream)
    assert messages[0][0].startswith("header {")


def test_parse_pose_info_reads_every_model_in_a_large_message():
    blocks = [
        f'pose {{\n  name: "civ_{index:04d}"\n  id: {index}\n'
        f'  position {{\n    x: {index}.0\n    y: 2.0\n    z: 3.0\n  }}\n'
        f'  orientation {{\n    x: 0.0\n    y: 0.0\n    z: 0.0\n    w: 1.0\n  }}\n}}\n'
        for index in range(4000)
    ]
    text = "".join(blocks)

    parsed = _parse_pose_info(text)

    assert len(text) > 200_000
    assert parsed is not None
    assert len(parsed["models"]) == 4000
    assert parsed["models"]["civ_3999"][:3] == [3999.0, 2.0, 3.0]


def test_camera_pose_fills_missing_rigid_chain_models():
    # The sparse pose topic can omit nested models that never moved; their SDF include poses
    # are parent-relative, exactly like the published Pose_V entries.
    episode = object.__new__(Episode)
    episode.camera_sensor_pose = (0.0, 0.0, 0.0, -1.57, -1.57, 0.0)
    # SDF order: (x, y, z, roll, pitch, yaw); the fallback converts to the pose-topic order.
    episode.camera_chain_relative = {
        "iris_with_gimbal": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "gimbal": (0.0, -0.01, -0.124923, math.pi / 2, 0.0, math.pi / 2),
    }
    models = {
        "horus_iris": [2.0, -3.0, 10.0, 0.7, 0.0, 0.0],
        "pitch_link": [0.0, 0.0147, 0.0043, -0.0015, 0.7854, 0.00005],
    }

    camera = episode._camera_pose({"models": models})

    assert camera is not None
    assert camera["pose_chain"] == [
        "horus_iris", "iris_with_gimbal", "gimbal", "pitch_link", "camera_sensor",
    ]
    assert all(math.isclose(actual, expected, abs_tol=1e-4)
               for actual, expected in zip(camera["position"], (2.009731, -3.004878, 9.8898)))
    assert math.isclose(camera["pitch_deg"], -45.0, abs_tol=0.1)


def test_camera_chain_relative_poses_come_from_the_outdoor_sdfs():
    from sim.scripts.campaign_episode import ROOT, _include_pose_and_sdf

    roots = (ROOT / "sim" / "models",)
    identity, child = _include_pose_and_sdf(
        ROOT / "sim" / "models" / "horus_iris_outdoor" / "model.sdf", "iris_with_gimbal", roots)
    assert identity == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert child is not None and child.parent.name == "horus_iris_with_gimbal_outdoor"

    gimbal, child_sdf = _include_pose_and_sdf(
        ROOT / "sim" / "models" / "horus_iris_with_gimbal_outdoor" / "model.sdf", "gimbal", roots)
    assert child_sdf is not None and child_sdf.parent.name == "horus_gimbal_small_3d_outdoor"
    assert math.isclose(gimbal[1], -0.01, abs_tol=1e-9)
    assert math.isclose(gimbal[3], math.pi / 2, abs_tol=1e-9)  # roll, degrees="true" honoured
    assert math.isclose(gimbal[4], 0.0, abs_tol=1e-9)          # pitch
    assert math.isclose(gimbal[5], math.pi / 2, abs_tol=1e-9)  # yaw


def test_scaled_ttl_applies_the_episode_scale():
    episode = object.__new__(Episode)
    episode.ttl_scale = 6.0
    assert episode._scaled_ttl(180.0) == 1080.0

    episode.ttl_scale = 1.0
    assert episode._scaled_ttl(120.0) == 120.0


def test_ttl_scale_rewrites_mission_commands_only_when_needed():
    from sim.scripts.campaign_episode import _scale_mission_ttls

    mission = {"steps": [
        {"tool": "take_off", "arguments": {"altitude_m": 34.0, "ttl_s": 180.0}, "timeout_s": 185.0},
        {"select_candidate": {"attribute": "shirt_colour", "equals": "red", "choose": "largest",
                              "store_as": "target", "timeout_s": 15.0}},
    ]}

    _scale_mission_ttls(mission, 1.0)
    assert mission["steps"][0]["arguments"]["ttl_s"] == 180.0
    assert mission["steps"][0]["timeout_s"] == 185.0

    _scale_mission_ttls(mission, 4.0)
    assert mission["steps"][0]["arguments"]["ttl_s"] == 720.0
    assert mission["steps"][0]["timeout_s"] == 740.0
    # Selection waits are wall-clock UI waits, not sim-bound commands, so they stay fixed.
    assert mission["steps"][1]["select_candidate"]["timeout_s"] == 15.0


def test_episode_cli_accepts_a_ttl_scale():
    args = _arguments([
        "--kind", "forest", "--seed", "1", "--manifest", "manifest.json",
        "--sdf", "world.sdf", "--episode-dir", "episode", "--tests", "perception",
        "--ttl-scale", "6",
    ])
    assert args.ttl_scale == 6.0
    default = _arguments([
        "--kind", "forest", "--seed", "1", "--manifest", "manifest.json",
        "--sdf", "world.sdf", "--episode-dir", "episode", "--tests", "perception",
    ])
    assert default.ttl_scale == 1.0


def test_civilian_check_uses_the_declared_max_speed_not_the_nominal_one(tmp_path):
    import json

    import yaml

    from sim.scripts.campaign_episode import Episode

    scenario = {
        "world": "test_world", "rate_hz": 10, "seed": 1,
        "agents": [
            # Startled deer: the declared maximum is the flee speed; the nominal walk speed is not.
            {"model": "ani_deer_01", "kind": "animal", "species": "deer", "group": "ambient",
             "behavior": "wander", "speed_m_s": 1.22, "max_speed_m_s": 2.5, "accel_m_s2": 1.0,
             "pause_s": [1, 2], "area": [[-5.0, -5.0], [5.0, -5.0], [5.0, 5.0], [-5.0, 5.0]]},
            {"model": "ani_crow_01", "kind": "animal", "species": "crow", "group": "ambient",
             "behavior": "wander", "speed_m_s": 1.0, "max_speed_m_s": 2.0, "accel_m_s2": 1.0,
             "pause_s": [1, 2], "area": [[-5.0, -5.0], [5.0, -5.0], [5.0, 5.0], [-5.0, 5.0]]},
        ],
    }
    (tmp_path / "scenario.yaml").write_text(yaml.safe_dump(scenario), encoding="utf-8")
    trace = [
        {"t": 0.0, "agents": {"ani_deer_01": [0.0, 0.0, 0.0, 0.0], "ani_crow_01": [0.0, 0.0, 0.0, 0.0]}},
        {"t": 1.0, "agents": {"ani_deer_01": [2.4, 0.0, 0.0, 0.0], "ani_crow_01": [2.4, 0.0, 0.0, 0.0]}},
        {"t": 2.0, "agents": {"ani_deer_01": [4.8, 0.0, 0.0, 0.0], "ani_crow_01": [4.8, 0.0, 0.0, 0.0]}},
    ]
    (tmp_path / "civilians.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in trace), encoding="utf-8")

    episode = object.__new__(Episode)
    episode.manifest_path = tmp_path / "world.layout.json"
    episode.manifest = {"scenario": "scenario.yaml", "bounds": {"x": [-50.0, 50.0], "y": [-50.0, 50.0]}}
    episode.civilian_trace_path = tmp_path / "civilians.jsonl"

    check = episode._civilian_check()

    assert check["checked_poses"] == 6 and check["agents"] == 2
    # 2.4 m/s is below the deer's declared cap (2.5 * 1.05) but above its nominal speed, so the
    # deer passes while the crow (declared cap 2.1) is the only speed violation.
    assert [v["model"] for v in check["violations"]] == ["ani_crow_01", "ani_crow_01"]
    assert all(v["type"] == "speed_limit" for v in check["violations"])
    assert all(math.isclose(v["maximum_m_s"], 2.1, abs_tol=1e-9) for v in check["violations"])
    assert all(math.isclose(v["speed_m_s"], 2.4, abs_tol=1e-9) for v in check["violations"])
    assert math.isclose(check["max_speed_ratio"], 2.4 / 2.1, rel_tol=1e-9)
    assert check["passed"] is False

    # Without the offending agent the declared-cap world passes (nominal-only cap would fail).
    scenario["agents"] = scenario["agents"][:1]
    (tmp_path / "scenario.yaml").write_text(yaml.safe_dump(scenario), encoding="utf-8")
    (tmp_path / "civilians.jsonl").write_text(
        "".join(json.dumps({"t": row["t"], "agents": {"ani_deer_01": row["agents"]["ani_deer_01"]}}) + "\n"
                for row in trace), encoding="utf-8")
    assert episode._civilian_check()["passed"] is True


def test_ground_truth_history_records_target_people_for_orbit_scoring():
    from sim.scripts.campaign_episode import Episode

    class FakeReader:
        def latest(self):
            return {"wall_time": 10.0, "sim_time": 5.0, "models": {
                "horus_iris": [0.0, 0.0, 9.0, 0.0, 0.0, 0.0],
                "civ_target": [3.0, 4.0, 0.2, 0.0, 0.0, 0.0],
                "forest_trees": [1.0, 2.0, 0.0, 0.0, 0.0, 0.0],
            }}

    episode = object.__new__(Episode)
    episode._gt = FakeReader()
    episode._person_models = {"civ_target"}
    episode.path_history = []
    episode.start_sim = None

    episode._record_gt()

    entry = episode.path_history[0]
    assert set(entry["models"]) == {"horus_iris", "civ_target"}
    assert entry["models"]["civ_target"][:3] == [3.0, 4.0, 0.2]
    assert episode.start_sim == 5.0


def test_projection_validation_uses_all_episode_matches_and_both_limits():
    assert _projection_validation([1.0, 10.0, 15.0]) == {
        "confident_match_count": 3, "median_pixel_error": 10.0,
        "minimum_confident_matches": 3, "threshold_px": 15.0, "passed": True,
    }
    assert not _projection_validation([1.0, 10.0])["passed"]
    assert not _projection_validation([16.0, 17.0, 18.0])["passed"]


def test_episode_command_line_parses_core_arguments():
    args = _arguments([
        "--kind", "forest", "--seed", "1", "--manifest", "manifest.json",
        "--sdf", "world.sdf", "--episode-dir", "episode", "--tests", "perception,orbit",
    ])
    assert args.kind == "forest" and args.seed == 1
    assert args.tests == ("perception", "orbit")
    assert not hasattr(args, "camera_hfov_deg")


def test_campaign_cli_has_no_camera_hfov_override():
    args = _parse_cli(["--kinds", "forest", "--seeds", "1", "--tests", "perception"])
    assert args.tests == ("perception",)
    assert not hasattr(args, "camera_hfov_deg")


def test_campaign_cli_supports_random_seed_mode():
    import pytest

    from sim.scripts.procedural_campaign import random_seeds

    default = _parse_cli(["--kinds", "forest", "--tests", "perception"])
    assert default.seeds == [1, 2, 3]
    assert default.random is None

    drawn = _parse_cli(["--kinds", "forest", "--random", "4", "--tests", "perception"])
    assert drawn.random == 4
    assert len(drawn.seeds) == 4 and len(set(drawn.seeds)) == 4
    assert all(1 <= seed <= 99999 for seed in drawn.seeds)

    for invalid in (["--random", "0"], ["--random", "2", "--seeds", "1", "2"], ["--seeds", "0", "3"]):
        with pytest.raises(SystemExit):
            _parse_cli(invalid)

    sample = random_seeds(5)
    assert len(sample) == 5 and len(set(sample)) == 5


def test_exception_summary_unwraps_taskgroup_sub_exceptions():
    class _FakeGroup(Exception):
        def __init__(self, message, exceptions):
            super().__init__(message)
            self.exceptions = tuple(exceptions)

    summary = _exception_summary(_FakeGroup(
        "unhandled errors in a TaskGroup (1 sub-exception)",
        [ValueError("Connection reset by peer")],
    ))

    assert "unhandled errors in a TaskGroup" in summary
    assert "ValueError: Connection reset by peer" in summary


def test_hazard_gate_rejects_grazing_paths_and_requires_real_penetration():
    assert _hazard_path_is_collision_course(-1.2)
    assert _hazard_path_is_collision_course(-0.3)
    assert not _hazard_path_is_collision_course(-0.05)
    assert not _hazard_path_is_collision_course(0.06)
    assert not _hazard_path_is_collision_course(math.nan)
    assert not _hazard_path_is_collision_course(math.inf)


def test_mission_id_match_requires_both_ids():
    assert _mission_id_matches(1773, 1773)
    assert not _mission_id_matches(1773, 1774)
    assert not _mission_id_matches(None, None)
    assert not _mission_id_matches(1773, None)
    assert not _mission_id_matches(None, 1773)


def test_static_gt_people_cover_the_follow_group_only():
    people = [
        {"model": "civ_ambient", "group": "ambient", "position": [1.0, 2.0]},
        {"model": "civ_follow", "group": "follow_hiker", "position": [3.0, 4.0]},
        {"model": "civ_unknown"},
    ]

    assert [person["model"] for person in _static_gt_people(people)] == ["civ_follow"]


def test_follow_results_dir_is_relative_to_the_mission_file(tmp_path):
    episode = object.__new__(Episode)
    episode.directory = tmp_path

    assert episode._follow_results_dir() == tmp_path / "mission" / "follow_results"


def candidate(candidate_id, box, colour="red", confidence=0.9):
    return {
        "id": candidate_id,
        "bbox": box,
        "class_name": "person",
        "confidence": confidence,
        "attributes": {"shirt_colour": colour},
    }


def test_candidate_bbox_accepts_both_public_shapes_and_rejects_invalid_values():
    assert candidate_bbox({"bbox": {"left": 10, "top": 20, "right": 30, "bottom": 60}}) == (
        10.0,
        20.0,
        30.0,
        60.0,
    )
    assert candidate_bbox({"box": [1, 2, 4, 8]}) == (1.0, 2.0, 4.0, 8.0)
    assert candidate_bbox({"bbox": [0, 0, 0, 4]}) is None
    assert candidate_bbox({"bbox": [0, 0, float("nan"), 4]}) is None


def test_select_candidate_uses_exact_attribute_then_largest_box_and_low_id_tie_break():
    rows = [
        candidate(2, [0, 0, 20, 20], "red"),
        candidate(9, [0, 0, 30, 30], "red"),
        candidate(4, [0, 0, 30, 30], "red"),
        candidate(1, [0, 0, 90, 90], "green"),
    ]
    assert select_largest_attribute_candidate(rows, equals="red") == rows[2]
    assert select_largest_attribute_candidate(rows, equals="blue") is None


def test_select_candidate_applies_the_runner_confidence_floor():
    from sim.scripts.procedural_campaign import selection_min_confidence

    floor = selection_min_confidence()
    big_artifact = candidate(3, [0, 0, 90, 90], "red", confidence=floor - 0.01)
    small_match = candidate(4, [0, 0, 20, 20], "red", confidence=floor)
    rows = [big_artifact, small_match]

    # The larger low-confidence match is exactly what the floor exists to reject.
    assert select_largest_attribute_candidate(rows, equals="red") == small_match
    # A candidate table without a confidence value is not gated (synthetic/partial tables).
    assert select_largest_attribute_candidate(
        [{"id": 5, "attributes": {"shirt_colour": "red"}, "bbox": [0, 0, 10, 10]}], equals="red"
    ) == {"id": 5, "attributes": {"shirt_colour": "red"}, "bbox": [0, 0, 10, 10]}
    assert select_largest_attribute_candidate([big_artifact], equals="red") is None


def test_follow_mission_selection_floor_is_pinned_explicitly():
    from sim.scripts.campaign_episode import _pin_selection_confidence
    from sim.scripts.procedural_campaign import selection_min_confidence

    mission = {"steps": [
        {"select_candidate": {"attribute": "shirt_colour", "equals": "red", "choose": "largest",
                              "store_as": "target", "timeout_s": 15.0}},
        {"tool": "follow", "arguments": {"id": "$target.id"}},
    ]}

    _pin_selection_confidence(mission)

    assert mission["steps"][0]["select_candidate"]["min_confidence"] == selection_min_confidence()
    assert mission["steps"][1] == {"tool": "follow", "arguments": {"id": "$target.id"}}


def test_match_prefers_expanded_box_then_uses_nearest_centre_and_tracks_false_positives():
    rows = [
        candidate(11, [90, 90, 110, 110]),  # torso at 86,86 is inside the 15 px expansion
        candidate(12, [136, 76, 156, 96]),  # nearest centre to person two, within 40 px
        candidate(13, [300, 300, 320, 320]),
    ]
    match = match_candidates_to_people(rows, {"expanded": (86, 86), "nearest": (120, 86)})
    assert match["matches"] == {"expanded": rows[0], "nearest": rows[1]}
    assert match["unmatched_candidates"] == [rows[2]]
    assert match["center_error_px"]["expanded"] > 15


def test_match_is_one_to_one_and_nearest_fallback_has_a_strict_40_pixel_limit():
    row = candidate(5, [95, 95, 105, 105])
    match = match_candidates_to_people(
        [row],
        {"closer": (103, 100), "farther": (110, 100), "outside": (146, 100)},
    )
    assert match["matches"] == {"closer": row}
    assert match["unmatched_candidates"] == []
    assert match["center_error_px"]["outside"] is None


def test_detection_metrics_are_stratified_and_confusion_counts_reported_attributes():
    rows = [
        {"detected": True, "slant_bin": "≤6", "posture": "standing", "clothing": "orange",
         "los_blocked": False, "shirt_colour": "red"},
        {"detected": False, "slant_bin": "6–9", "posture": "lying", "clothing": "dark",
         "los_blocked": True, "shirt_colour": None},
    ]
    metrics = score_detection_rows(rows)
    assert metrics["seen"] == 2
    assert metrics["detected"] == 1
    assert metrics["recall"] == 0.5
    assert metrics["strata"]["posture"] == {
        "standing": {"seen": 1, "detected": 1},
        "lying": {"seen": 1, "detected": 0},
    }
    assert metrics["strata"]["los_blocked"]["True"]["detected"] == 0
    assert metrics["shirt_colour_confusion"] == {"orange": {"red": 1}}
    assert score_detection_rows([])["recall"] is None


def test_campaign_aggregation_counts_completed_runs_and_preserves_seed_rows():
    episodes = [
        {"kind": "urban", "seed": 1, "tests": {
            "perception": {"passed": True, "person_observations": [
                {"detected": True, "slant_bin": "≤6", "posture": "standing", "clothing": "red",
                 "los_blocked": False, "shirt_colour": "red"},
            ]},
            "scan": {"passed": False, "status": "failed"},
        }},
        {"kind": "urban", "seed": 2, "tests": {
            "perception": {"passed": False, "person_observations": []},
            "scan": {"passed": False, "status": "skipped"},
        }},
    ]
    summary = aggregate_campaign(episodes)
    urban = summary["by_kind"]["urban"]
    assert summary["episodes"] == 2
    assert urban["tests"]["perception"] == {"total": 2, "passed": 1, "seeds": [1, 2], "pass_rate": 0.5}
    assert urban["tests"]["scan"] == {"total": 1, "passed": 0, "seeds": [1, 2], "pass_rate": 0.0}
    assert urban["detection"]["recall"] == 1.0


def test_operational_metrics_aggregate_and_render_in_campaign_report():
    episode = {
        "kind": "forest",
        "seed": 7,
        "world": "horus_forest_p7",
        "status": "passed",
        "wall_duration_s": 123.0,
        "civilian_check": {"passed": True, "checked": 4},
        "tests": {
            "select": {"passed": True, "selected_matches_intended": True},
            "approach": {"trials": [{"distance_error_vs_5m_m": -0.25},
                                    {"distance_error_vs_5m_m": 0.5}]},
            "orbit": {"gt_radius_error_m": 0.4, "gt_radius_std_m": 0.2,
                      "target_in_frame_fraction": 0.75},
            "scan": {"coverage": {"fraction": 0.5}},
            "follow": {"passed": True, "reacquisition_result": {
                "reacquisition": {"time_to_reacquire_s": 2.3}}},
            "rth": {"landing_error_to_pad_m": 0.8},
            "hazard": {"passed": False, "crashed": True},
        },
        "rtf": {"mean": 0.4, "p10": 0.32},
    }
    summary = aggregate_campaign([episode])
    forest = summary["by_kind"]["forest"]
    assert forest["metrics"]["select_correctness"] == {"correct": 1, "total": 1, "rate": 1.0}
    assert forest["metrics"]["approach_distance_error_m"]["mean"] == 0.125
    assert forest["metrics"]["orbit_target_in_frame_fraction"]["mean"] == 0.75
    assert forest["metrics"]["orbit_radius_std_m"]["mean"] == 0.2
    assert forest["metrics"]["scan_coverage_fraction"]["mean"] == 0.5
    assert forest["metrics"]["follow_reacquire_time_s"]["mean"] == 2.3
    assert forest["metrics"]["landing_error_m"]["mean"] == 0.8
    assert forest["metrics"]["hazard"] == {"attempts": 1, "safe_outcomes": 0, "crashes": 1,
                                           "skipped_invalid_trials": 0}
    markdown = render_summary_markdown({
        "episode_count": 1,
        "episodes": [episode],
        "aggregate": summary,
    })
    assert "| forest | select correctness | 1/1 (100.0%) |" in markdown
    assert "| forest | orbit radius standard deviation, m | 0.200 (n=1) |" in markdown
    assert "| forest | scan coverage fraction | 0.500 (n=1) |" in markdown
    assert "| horus_forest_p7 | 7 | 123.0s (" in markdown
    assert " | passed |" in markdown


def test_skipped_hazard_trials_are_not_counted_as_product_attempts():
    episode = {
        "kind": "urban",
        "seed": 1,
        "world": "horus_urban_p1",
        "status": "failed",
        "civilian_check": {"passed": True},
        "tests": {
            "hazard": {"passed": False, "status": "skipped", "crashed": False,
                       "reason": "direct path static clearance 0.06 m is not a collision course"},
        },
        "rtf": {"mean": 0.5, "p10": 0.4},
    }
    report = render_summary_markdown({
        "episode_count": 1,
        "episodes": [episode],
        "aggregate": aggregate_campaign([episode]),
    })

    assert aggregate_campaign([episode])["by_kind"]["urban"]["metrics"]["hazard"] == {
        "attempts": 0, "safe_outcomes": 0, "crashes": 0, "skipped_invalid_trials": 1,
    }
    assert "| urban | hazard invalid trials skipped | 1 |" in report


def test_camera_pose_aggregates_measured_per_vantage_samples_and_renders_summary():
    episode = {
        "kind": "urban",
        "seed": 4,
        "world": "horus_urban",
        "camera_info": {"horizontal_fov_deg": 83.0},
        "tests": {
            "perception": {
                "vantage_frames": [
                    {
                        "vantage": 1,
                        "target": "red_target",
                        "actual_gt_camera_pose": {
                            "pitch_deg": -44.0, "yaw_offset_deg": 2.0,
                            "yaw_deg": 30.0, "roll_deg": 1.0,
                        },
                    },
                    {
                        "vantage": 2,
                        "target": "blue_target",
                        "actual_gt_camera_pose": {
                            "pitch_deg": -46.0, "yaw_offset_deg": 6.0,
                            "yaw_deg": 31.0, "roll_deg": 3.0,
                        },
                    },
                ],
            },
        },
    }
    aggregate = aggregate_campaign([episode])
    metrics = aggregate["by_kind"]["urban"]["metrics"]
    pose = metrics["camera_pose"]
    assert pose["vantage_count"] == 2
    assert pose["pitch_deg"]["mean"] == -45.0
    assert pose["pitch_deg"]["std"] == 1.0
    assert pose["yaw_offset_deg"]["mean"] == 4.0
    assert pose["yaw_offset_deg"]["std"] == 2.0
    assert pose["roll_deg"]["mean"] == 2.0
    assert metrics["camera_info_hfov_deg"]["mean"] == 83.0
    assert pose["vantages"][0] == {
        "world": "horus_urban", "seed": 4, "vantage": 1, "target": "red_target",
        "pitch_deg": -44.0, "yaw_offset_deg": 2.0, "yaw_deg": 30.0, "roll_deg": 1.0,
    }

    report = render_summary_markdown({
        "episode_count": 1,
        "episodes": [episode],
        "aggregate": aggregate,
    })
    assert ("| urban | 83.0 ± 0.0 (n=1) | 2 | "
            "-45.0 ± 1.0 (n=2) | 4.0 ± 2.0 (n=2) | 2.0 ± 1.0 (n=2) |") in report
    assert "| urban | horus_urban | 4 | 1 | red_target | -44.0 | +30.0 | +2.0 | +1.0 |" in report
    assert "| urban | horus_urban | 4 | 2 | blue_target | -46.0 | +31.0 | +6.0 | +3.0 |" in report


def test_rth_skips_a_grounded_vehicle_but_runs_when_airborne():
    # RTH on a grounded vehicle disarms trivially; without the airborne gate the test reports
    # a pass for an episode whose takeoff never succeeded.
    import asyncio

    from sim.scripts.campaign_episode import Episode

    class FakeReader:
        def __init__(self, z: float) -> None:
            self._z = z

        def latest(self):
            return {"wall_time": 1.0, "sim_time": 1.0,
                    "models": {"horus_iris": [0.0, 0.0, self._z, 0.0, 0.0, 0.0]}}

    class FakeGround:
        def height(self, x: float, y: float) -> float:
            return 0.0

    grounded = object.__new__(Episode)
    grounded._gt = FakeReader(0.21)
    grounded.ground = FakeGround()
    requested: list[str] = []

    async def grounded_tool(name, arguments=None):
        requested.append(name)
        return {}

    grounded._tool = grounded_tool

    result = asyncio.run(grounded._rth(require_airborne=True))

    assert result["passed"] is False and result["status"] == "skipped"
    assert result["reason"] == "vehicle was not airborne before RTH"
    assert result["airborne_altitude_m"] == 0.21
    assert requested == []  # a grounded vehicle must not even be asked to RTH

    flying = object.__new__(Episode)
    flying._gt = FakeReader(9.0)
    flying.ground = FakeGround()
    flying.path_history = []
    flying._person_models = set()
    flying.start_sim = None
    flying._sim_now = lambda: 5.0

    async def flying_tool(name, arguments=None):
        return {"state": "RTH"}

    async def wait_disarmed(limit_sim_s=240.0):
        return {"disarmed": True, "elapsed_sim_s": 1.0}

    flying._tool = flying_tool
    flying._wait_disarmed = wait_disarmed

    result = asyncio.run(flying._rth(require_airborne=True))

    assert result["passed"] is True and result["landing_error_to_pad_m"] == 0.0


def test_missing_result_failure_names_the_exit_code_and_container_state():
    from sim.scripts.procedural_campaign import _missing_result_failure, container_state

    assert _missing_result_failure(0, {}) == "episode wrote no result file; exit code 0; see episode.log"
    oom = {"status": "exited", "exit_code": 137, "oom_killed": True, "error": ""}
    assert "OOM-killed" in _missing_result_failure(137, oom)
    assert "exit 137" in _missing_result_failure(137, oom)
    running = {"status": "running", "exit_code": 0, "oom_killed": False, "error": ""}
    assert _missing_result_failure(None, running) == "episode wrote no result file; exit code None; see episode.log"
    # docker may be absent or the container already purged: the helper stays best-effort.
    assert container_state("horus-sim-does-not-exist") == {}


def test_takeoff_failure_payload_keeps_attempts_and_status_texts():
    from sim.scripts.campaign_episode import Episode

    episode = object.__new__(Episode)
    episode._takeoff_attempts = [Episode._skill_summary({
        "task_id": "t1", "skill": "take_off", "status": "expired",
        "result": {"status": "expired", "error": "command TTL expired", "_image_data": ["jpeg"]},
    })]
    episode._takeoff_statustext = ["PreArm: GPS: waiting for home"]

    payload = episode._takeoff_failure("vehicle could not take off")

    assert payload["passed"] is False and payload["reason"] == "vehicle could not take off"
    assert payload["takeoff_attempts"] == [{"task_id": "t1", "skill": "take_off", "status": "expired",
                                            "error": "command TTL expired", "result_status": "expired",
                                            "clamp_reason": None}]
    assert payload["vehicle_statustext"] == ["PreArm: GPS: waiting for home"]


def test_ensure_airborne_retries_once_and_reports_the_attempts():
    import asyncio

    from sim.scripts.campaign_episode import Episode

    class FakeReader:
        def latest(self):
            return {"wall_time": 1.0, "sim_time": 1.0,
                    "models": {"horus_iris": [0.0, 0.0, 0.2, 0.0, 0.0, 0.0]}}

    def build(statuses):
        episode = object.__new__(Episode)
        episode.transit_alt = 30.0
        episode.ttl_scale = 1.0
        episode._gt = FakeReader()
        episode.path_history = []
        episode._person_models = set()
        episode.start_sim = None
        calls: list[str] = []

        async def tool(name, arguments=None):
            if name == "get_state":
                return {"vehicle": {"untrusted_statustext": ["PreArm: GPS: waiting for home"]}}
            return {}

        async def skill(name, arguments, timeout_s=None, observer=None):
            index = len(calls)
            calls.append(name)
            status = statuses[min(index, len(statuses) - 1)]
            return {"task_id": f"t{index}", "skill": "take_off", "status": status}

        async def wait_gt(point, tolerance=0.5, settle_sim_s=1.0, timeout_s=300.0):
            return True

        episode._tool = tool
        episode._skill = skill
        episode._wait_gt = wait_gt
        return episode, calls

    episode, calls = build(["expired", "succeeded"])
    assert asyncio.run(episode._ensure_airborne()) is True
    assert calls == ["take_off", "take_off"]
    assert [attempt["status"] for attempt in episode._takeoff_attempts] == ["expired", "succeeded"]

    failing, calls = build(["expired", "expired"])
    assert asyncio.run(failing._ensure_airborne()) is False
    assert len(calls) == 2
    payload = failing._takeoff_failure("vehicle could not take off")
    assert [attempt["status"] for attempt in payload["takeoff_attempts"]] == ["expired", "expired"]
    assert payload["vehicle_statustext"] == ["PreArm: GPS: waiting for home"]


def test_sitl_console_is_copied_beside_the_episode_result(tmp_path, monkeypatch):
    from sim.scripts import campaign_episode

    source = tmp_path / "ArduCopter.log"
    source.write_text("PreArm: GPS: waiting for home\n", encoding="utf-8")
    monkeypatch.setattr(campaign_episode, "SITL_CONSOLE_LOG", source)

    assert campaign_episode._copy_sitl_console(tmp_path) == "sitl-console.log"
    assert (tmp_path / "sitl-console.log").read_text(encoding="utf-8").startswith("PreArm:")

    monkeypatch.setattr(campaign_episode, "SITL_CONSOLE_LOG", tmp_path / "missing.log")
    assert campaign_episode._copy_sitl_console(tmp_path) is None


def test_campaign_wall_timeout_defaults_to_the_ttl_scale():
    # A host that needs TTL scaling needs proportionally more wall time; without this the
    # forest episode is truncated mid-suite (rth/hazard report "episode received termination").
    default = _parse_cli(["--kinds", "forest", "--seeds", "1", "--tests", "perception"])
    assert default.timeout_minutes == 45.0

    local = _parse_cli(["--kinds", "forest", "--seeds", "1", "--tests", "perception",
                        "--ttl-scale", "6"])
    assert local.timeout_minutes == 270.0

    explicit = _parse_cli(["--kinds", "forest", "--seeds", "1", "--tests", "perception",
                           "--ttl-scale", "6", "--timeout-minutes", "30"])
    assert explicit.timeout_minutes == 30.0


def test_hazard_trial_searches_for_a_real_collision_course():
    # urban seed 15178 skipped hazard because the single chosen obstacle path was tangent
    # (+0.06 m). The search must try other obstacles/headings, keep only real collision courses
    # with a safe approach leg, and skip with that reason only when none exist.
    import asyncio

    from sim.scripts.campaign_episode import Episode
    from sim.worldgen.geometry import CollisionWorld, Primitive

    def box(name: str, model: str, x: float, y: float, z: float,
            hx: float, hy: float, hz: float) -> Primitive:
        return Primitive(name, model, "box", (x, y, z, 0.0, 0.0, 0.0), (hx, hy, hz), True)

    class FakeReader:
        def latest(self):
            return {"wall_time": 1.0, "sim_time": 1.0,
                    "models": {"horus_iris": [0.0, 0.0, 34.0, 0.0, 0.0, 0.0]}}

    def episode_with(*primitives: Primitive) -> Episode:
        episode = object.__new__(Episode)
        episode._gt = FakeReader()
        episode.transit_alt = 34.0
        episode.world = CollisionWorld(list(primitives))
        return episode

    # A low bush is under the 3 m trial altitude for every heading: never a collision course.
    low = box("tree_low", "forest_trees", 10.0, 0.0, 0.5, 0.5, 0.5, 0.5)
    crossing = box("building_crossing", "urban_buildings", 30.0, 0.0, 3.0, 0.5, 0.5, 3.0)

    trial = episode_with(low, crossing)._hazard_trial()
    assert trial is not None and trial["obstacle"] == "building_crossing"
    assert trial["direct_path_static_clearance_m"] <= -0.3
    assert trial["approach_min_clearance_m"] >= 1.5

    assert episode_with(low)._hazard_trial() is None

    # A wall across the approach (cruise altitude included) makes the only crossing obstacle
    # unrunnable, so the search must not fall back to it.
    # Primitive sizes are full extents, so this wall spans x 19-21 and z -20..60: the cruise
    # leg at 34 m cannot avoid it.
    wall = box("blocking_wall", "walls", 20.0, 0.0, 20.0, 2.0, 40.0, 80.0)
    assert episode_with(crossing, wall)._hazard_trial() is None

    episode = episode_with(low)

    async def airborne() -> bool:
        return True

    episode._ensure_airborne = airborne
    result = asyncio.run(episode.test_hazard())

    assert result["passed"] is False and result["status"] == "skipped"
    assert result["reason"].startswith("no static obstacle offers a collision course")
    assert result["obstacles_considered"] == ["tree_low"]
