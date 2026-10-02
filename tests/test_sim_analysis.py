from __future__ import annotations

import json

from sim.scripts.gt_trace import pose_info_topic
from sim.scripts.s6_analyze import analyze


def _write_trace(prefix, samples):
    path = prefix.with_name(f"{prefix.name}_groundtruth.jsonl")
    path.write_text("".join(json.dumps({"models": models}) + "\n" for models in samples), encoding="utf-8")


def test_s6_analyzer_retains_indoor_target_and_success_defaults(tmp_path):
    prefix = tmp_path / "indoor"
    _write_trace(
        prefix,
        [
            {
                "horus_iris": [0.0, 0.0, 2.0, 0.0],
                "person_red": [3.0, 0.0, 0.0, 0.0],
                "person_blue": [5.0, 0.0, 0.0, 0.0],
                "person_green": [8.0, 0.0, 0.0, 0.0],
                "person_laying": [12.0, 0.0, 0.0, 0.0],
            },
            {
                "horus_iris": [1.5, 0.0, 2.0, 0.0],
                "person_red": [3.0, 0.0, 0.0, 0.0],
                "person_blue": [5.0, 0.0, 0.0, 0.0],
                "person_green": [8.0, 0.0, 0.0, 0.0],
                "person_laying": [12.0, 0.0, 0.0, 0.0],
            },
            {
                "horus_iris": [0.0, 0.0, 0.0, 0.0],
                "home_apriltag_pad": [0.0, 0.0, 0.0, 0.0],
            },
        ],
    )
    (tmp_path / "indoor_final_state.json").write_text(
        json.dumps({"vehicle": {"armed": False}}), encoding="utf-8"
    )

    summary = analyze(prefix)

    assert summary["approached_person"] == "person_red"
    assert summary["success_correct_target"] is True
    assert summary["approach_distance_m"] == 1.5
    assert summary["success_distance_m"] == 2.0
    assert summary["landed_disarmed"] is True


def test_s6_analyzer_uses_outdoor_model_names_and_configured_distance(tmp_path):
    prefix = tmp_path / "urban"
    _write_trace(
        prefix,
        [
            {
                "horus_iris": [0.0, 0.0, 5.0, 0.0],
                "civ_red_shirt_01": [10.0, 0.0, 0.0, 0.0],
                "civ_plaza_crowd_01": [30.0, 0.0, 0.0, 0.0],
                "civ_plaza_crowd_02": [50.0, 0.0, 0.0, 0.0],
            },
            {
                "horus_iris": [9.0, 0.0, 5.0, 0.0],
                "civ_red_shirt_01": [10.0, 0.0, 0.0, 0.0],
                "civ_plaza_crowd_01": [30.0, 0.0, 0.0, 0.0],
                "civ_plaza_crowd_02": [50.0, 0.0, 0.0, 0.0],
            },
        ],
    )

    summary = analyze(
        prefix,
        target_model="civ_red_shirt_01",
        distractor_models=("civ_plaza_crowd_01", "civ_plaza_crowd_02"),
        success_distance_m=1.0,
    )

    assert summary["approached_person"] == "civ_red_shirt_01"
    assert summary["success_correct_target"] is True
    assert summary["closest_airborne_distance_m"]["civ_red_shirt_01"] == 1.0
    assert summary["success_distance_m"] == 1.0


def test_s6_analyzer_rejects_a_nearer_distractor_as_the_success_target(tmp_path):
    prefix = tmp_path / "distractor"
    _write_trace(
        prefix,
        [
            {"horus_iris": [0.0, 0.0, 5.0, 0.0]},
            {
                "horus_iris": [1.0, 0.0, 5.0, 0.0],
                "civ_collapsed_01": [2.0, 0.0, 0.0, 0.0],
                "civ_bystander_01": [1.5, 0.0, 0.0, 0.0],
            },
        ],
    )

    summary = analyze(
        prefix,
        target_model="civ_collapsed_01",
        distractor_models=("civ_bystander_01",),
        success_distance_m=2.0,
    )

    assert summary["approached_person"] == "civ_bystander_01"
    assert summary["success_correct_target"] is False


def test_pose_info_topic_defaults_to_indoor_and_resolves_world_or_override():
    assert pose_info_topic() == "/world/horus_indoor/pose/info"
    assert pose_info_topic("horus_forest") == "/world/horus_forest/pose/info"
    assert pose_info_topic("horus_urban", "/custom/poses") == "/custom/poses"
