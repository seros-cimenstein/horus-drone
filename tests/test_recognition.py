from __future__ import annotations

import math
import time

import pytest

from horus.perception import (
    Detection,
    PerceptionSystem,
    PersistentTracker,
    TinyEmbeddingClassifier,
)
from horus.perception.geometry import iou

from tests.fakes import FixedDetector, feed_frame


class _AppearanceImage:
    """Frame stand-in whose every crop embeds to ``vector``."""

    shape = (480, 640)

    def __init__(self, *vector: float) -> None:
        self.vector = vector


class _CountingAppearanceEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, image, bbox):
        self.calls += 1
        return image.vector


class _PersonDetector:
    def detect(self, image):
        return [Detection((10, 10, 40, 60), class_name="person")]


def _person_system() -> tuple[PerceptionSystem, _CountingAppearanceEmbedder]:
    embedder = _CountingAppearanceEmbedder()
    return PerceptionSystem(_PersonDetector(), PersistentTracker(embedder=embedder)), embedder


_TRAINED_RED_PROFILE = {
    "target_class": "red",
    "training_embeddings": [
        {"embedding": [1, 0], "label": "red"},
        {"embedding": [0.9, 0.1], "label": "red"},
        {"embedding": [0, 1], "label": "blue"},
        {"embedding": [0.1, 0.9], "label": "blue"},
    ],
}

_REJECTED_PROFILES = {
    "undecodable reference image": {"class_name": "person", "reference_images": ["not base64!"]},
    "labels do not match examples": {
        "class_name": "person",
        "training_embeddings": [[1, 0], [0, 1]],
        "labels": ["red"],
    },
    "too few labeled examples": {
        "class_name": "person",
        "training_embeddings": [
            {"embedding": [1, 0], "label": "red"},
            {"embedding": [0, 1], "label": "blue"},
        ],
    },
    "unknown key": {"class_name": "person", "embedding": [1, 0]},
}


def _score(row) -> float | None:
    return row["target_match_confidence"]


# Tiny classifier head


def test_linear_head_learns_labeled_reference_embedding_classes() -> None:
    classifier = TinyEmbeddingClassifier(epochs=250)
    classifier.fit(
        [[1, 0], [0.9, 0.1], [0, 1], [0.1, 0.9]],
        ["red", "red", "blue", "blue"],
    )

    label, confidence = classifier.predict([1, 0])

    assert label == "red"
    assert confidence > 0.9


def test_profile_trains_classifier_and_exposes_target_match_confidence() -> None:
    system = PerceptionSystem(
        FixedDetector(Detection((10, 10, 40, 60), class_name="object")),
        PersistentTracker(embedder=_CountingAppearanceEmbedder()),
    )
    profile = {
        "target_class": "red",
        "training_embeddings": [
            {"embedding": [1, 0], "label": "red"},
            {"embedding": [0.9, 0.1], "label": "red"},
            {"embedding": [0, 1], "label": "blue"},
            {"embedding": [0.1, 0.9], "label": "blue"},
        ],
    }

    result = system.set_target_profile(profile)
    assert result["classifier_trained"]
    assert system.set_mode("step_2") == "classifier"
    feed_frame(system, _AppearanceImage(1.0, 0.0), 10.0)

    candidate = system.list_candidates()[0]
    assert candidate["recognized_class"] == "red"
    assert candidate["target_match_confidence"] > 0.9


# Target-profile scoring


def test_embedding_profile_scores_current_appearance_on_every_frame_of_a_track() -> None:
    system, embedder = _person_system()
    system.set_target_profile({"class_name": "person", "reference_embeddings": [[1, 0]]})

    track_id = None
    for frame_index in range(3):
        feed_frame(system, _AppearanceImage(0.0, 1.0), 10.0 + 0.1 * frame_index)
        [row] = system.list_candidates()
        track_id = row["id"] if track_id is None else track_id
        assert row["id"] == track_id
        assert _score(row) == pytest.approx(0.0)
        assert _score(system.get_candidate(track_id)) == pytest.approx(0.0)
        if frame_index:
            validated = system.validate_candidate(track_id, max_age_seconds=60.0)
            assert _score(validated) == pytest.approx(0.0)
    # List, get and executor validation share one lazy embedding per track per frame.
    assert embedder.calls == 3

    feed_frame(system, _AppearanceImage(0.6, 0.8), 10.3)
    [row] = system.list_candidates()
    assert row["id"] == track_id
    assert _score(row) == pytest.approx(0.6)

    system.set_target_profile({"class_name": "person", "reference_embeddings": [[0, 1]]})
    assert _score(system.get_candidate(track_id)) == pytest.approx(0.8)


def test_class_only_profile_matches_by_class_without_embedding_tracks() -> None:
    system, embedder = _person_system()
    feed_frame(system, _AppearanceImage(0.0, 1.0), 10.0)
    feed_frame(system, _AppearanceImage(0.0, 1.0), 10.1)

    system.set_target_profile({"class_name": "person"})
    [row] = system.list_candidates()
    assert _score(row) == 1.0
    system.set_target_profile({"class_name": "car"})
    assert _score(system.get_candidate(row["id"])) == 0.0
    assert embedder.calls == 1


def test_classifier_profile_recognizes_tracks_after_their_first_frame() -> None:
    system, _ = _person_system()
    system.set_target_profile(_TRAINED_RED_PROFILE)
    feed_frame(system, _AppearanceImage(1.0, 0.0), 10.0)
    feed_frame(system, _AppearanceImage(0.0, 1.0), 10.1)

    [row] = system.list_candidates()
    assert row["recognized_class"] == "blue"
    assert _score(row) == 0.0


def test_recognition_mode_switches_scoring_between_gallery_and_trained_head() -> None:
    system, _ = _person_system()
    assert system.set_target_profile(_TRAINED_RED_PROFILE)["recognition_mode"] == "classifier"
    feed_frame(system, _AppearanceImage(0.6, 0.8), 10.0)
    [row] = system.list_candidates()
    assert row["recognized_class"] == "blue"
    assert _score(row) == 0.0  # head: blue is not the red target

    assert system.set_mode("embedding") == "embedding"
    row = system.get_candidate(row["id"])
    assert _score(row) == pytest.approx(
        max(
            (0.6 * x + 0.8 * y) / math.hypot(x, y)
            for x, y in ((1, 0), (0.9, 0.1), (0, 1), (0.1, 0.9))
        )
    )
    assert _score(row) > 0.7
    assert row["recognized_class"] == "blue"  # the head still reports, it only stops scoring

    assert system.set_mode("classifier") == "classifier"
    assert _score(system.get_candidate(row["id"])) == 0.0


def test_rejected_profile_update_keeps_embedding_profile_scores() -> None:
    for rejected in _REJECTED_PROFILES.values():
        system, _ = _person_system()
        system.set_target_profile({"class_name": "person", "reference_embeddings": [[1, 0]]})
        feed_frame(system, _AppearanceImage(0.0, 1.0), 10.0)
        feed_frame(system, _AppearanceImage(0.0, 1.0), 10.1)

        with pytest.raises(ValueError):
            system.set_target_profile(rejected)

        [row] = system.list_candidates()
        assert _score(row) == pytest.approx(0.0)
        validated = system.validate_candidate(row["id"], max_age_seconds=60.0)
        assert _score(validated) == pytest.approx(0.0)


def test_rejected_profile_update_keeps_trained_classifier_and_mode() -> None:
    for rejected in _REJECTED_PROFILES.values():
        system, _ = _person_system()
        system.set_target_profile(_TRAINED_RED_PROFILE)
        assert system.set_mode("classifier") == "classifier"
        feed_frame(system, _AppearanceImage(1.0, 0.0), 10.0)
        before = system.list_candidates()

        with pytest.raises(ValueError):
            system.set_target_profile(rejected)

        assert system.list_candidates() == before
        assert before[0]["recognized_class"] == "red"
        assert _score(before[0]) > 0.9
        assert system.set_mode("step_2") == "classifier"


class _PatchImage:
    """Frame stand-in: each ``(bbox, vector)`` patch is a person whose crop embeds to ``vector``."""

    shape = (480, 640)

    def __init__(self, patches, *, detected: bool = True) -> None:
        self.patches = patches
        self.detected = detected


class _PatchDetector:
    def detect(self, image):
        if not image.detected:
            return []
        return [Detection(bbox, class_name="person") for bbox, _ in image.patches]


class _PatchEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, image, bbox):
        self.calls += 1
        return max(image.patches, key=lambda patch: iou(patch[0], bbox))[1]


# Point re-identification by appearance


def test_point_follows_appearance_when_geometry_points_at_a_crossing_lookalike() -> None:
    embedder = _PatchEmbedder()
    system = PerceptionSystem(_PatchDetector(), PersistentTracker(embedder=embedder))
    red_then, red_now, blue_now = (100, 100, 140, 200), (220, 100, 260, 200), (110, 100, 150, 200)
    old = feed_frame(system, _PatchImage([(red_then, (1.0, 0.0))], detected=False), time.monotonic())
    crossed = [(red_now, (1.0, 0.0)), (blue_now, (0.0, 1.0))]
    for _ in range(2):
        feed_frame(system, _PatchImage(crossed), time.monotonic())
    ids = {tuple(row["bbox"]): row["id"] for row in system.list_candidates()}
    calls_before = embedder.calls

    # The pointed box from the earlier frame now overlaps the blue look-alike; the red
    # person's appearance must win.
    result = system.point(old.frame_id, box=list(red_then))

    assert result.candidate.id == ids[tuple(float(v) for v in red_now)]
    assert result.candidate.source != "point"
    # Reference plus at most one current-appearance encode per latest candidate.
    assert embedder.calls - calls_before <= 3
