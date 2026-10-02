from __future__ import annotations

import time
import unittest

from horus.perception import (
    Detection,
    PerceptionSystem,
    PersistentTracker,
    TinyEmbeddingClassifier,
    box_iou,
)


class _VectorEmbedder:
    def embed(self, image, bbox):
        return image


class _FixedDetector:
    def detect(self, image):
        return [Detection((10, 10, 40, 60), class_name="object")]


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
}


def _score(row) -> float | None:
    return row["target_match_confidence"]


class TinyHeadTests(unittest.TestCase):
    def test_linear_head_learns_labeled_reference_embedding_classes(self) -> None:
        classifier = TinyEmbeddingClassifier(epochs=250)
        classifier.fit(
            [[1, 0], [0.9, 0.1], [0, 1], [0.1, 0.9]],
            ["red", "red", "blue", "blue"],
        )

        label, confidence = classifier.predict([1, 0])

        self.assertEqual(label, "red")
        self.assertGreater(confidence, 0.9)

    def test_profile_trains_classifier_and_exposes_target_match_confidence(self) -> None:
        system = PerceptionSystem(
            _FixedDetector(),
            PersistentTracker(embedder=_VectorEmbedder()),
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
        self.assertTrue(result["classifier_trained"])
        self.assertEqual(system.set_mode("step_2"), "classifier")
        system.process_frame((1.0, 0.0), timestamp=10.0)

        candidate = system.list_candidates()[0]
        self.assertEqual(candidate["recognized_class"], "red")
        self.assertGreater(candidate["target_match_confidence"], 0.9)


class TargetProfileScoringTests(unittest.TestCase):
    def test_embedding_profile_scores_current_appearance_on_every_frame_of_a_track(self) -> None:
        system, embedder = _person_system()
        system.set_target_profile({"class_name": "person", "reference_embeddings": [[1, 0]]})

        track_id = None
        for frame_index in range(3):
            system.process_frame(_AppearanceImage(0.0, 1.0), timestamp=10.0 + 0.1 * frame_index)
            [row] = system.list_candidates()
            track_id = row["id"] if track_id is None else track_id
            self.assertEqual(row["id"], track_id)
            self.assertAlmostEqual(_score(row), 0.0)
            self.assertAlmostEqual(_score(system.get_candidate(track_id)), 0.0)
            if frame_index:
                validated = system.validate_candidate(track_id, max_age_seconds=60.0)
                self.assertAlmostEqual(_score(validated), 0.0)
        # List, get and executor validation share one lazy embedding per track per frame.
        self.assertEqual(embedder.calls, 3)

        system.process_frame(_AppearanceImage(0.6, 0.8), timestamp=10.3)
        [row] = system.list_candidates()
        self.assertEqual(row["id"], track_id)
        self.assertAlmostEqual(_score(row), 0.6)

        system.set_target_profile({"class_name": "person", "reference_embeddings": [[0, 1]]})
        self.assertAlmostEqual(_score(system.get_candidate(track_id)), 0.8)

    def test_class_only_profile_matches_by_class_without_embedding_tracks(self) -> None:
        system, embedder = _person_system()
        system.process_frame(_AppearanceImage(0.0, 1.0), timestamp=10.0)
        system.process_frame(_AppearanceImage(0.0, 1.0), timestamp=10.1)

        system.set_target_profile({"class_name": "person"})
        [row] = system.list_candidates()
        self.assertEqual(_score(row), 1.0)
        system.set_target_profile({"class_name": "car"})
        self.assertEqual(_score(system.get_candidate(row["id"])), 0.0)
        self.assertEqual(embedder.calls, 1)

    def test_classifier_profile_recognizes_tracks_after_their_first_frame(self) -> None:
        system, _ = _person_system()
        system.set_target_profile(_TRAINED_RED_PROFILE)
        system.process_frame(_AppearanceImage(1.0, 0.0), timestamp=10.0)
        system.process_frame(_AppearanceImage(0.0, 1.0), timestamp=10.1)

        [row] = system.list_candidates()
        self.assertEqual(row["recognized_class"], "blue")
        self.assertEqual(_score(row), 0.0)

    def test_rejected_profile_update_keeps_embedding_profile_scores(self) -> None:
        for reason, rejected in _REJECTED_PROFILES.items():
            with self.subTest(reason):
                system, _ = _person_system()
                system.set_target_profile({"class_name": "person", "reference_embeddings": [[1, 0]]})
                system.process_frame(_AppearanceImage(0.0, 1.0), timestamp=10.0)
                system.process_frame(_AppearanceImage(0.0, 1.0), timestamp=10.1)

                with self.assertRaises(ValueError):
                    system.set_target_profile(rejected)

                [row] = system.list_candidates()
                self.assertAlmostEqual(_score(row), 0.0)
                validated = system.validate_candidate(row["id"], max_age_seconds=60.0)
                self.assertAlmostEqual(_score(validated), 0.0)

    def test_rejected_profile_update_keeps_trained_classifier_and_mode(self) -> None:
        for reason, rejected in _REJECTED_PROFILES.items():
            with self.subTest(reason):
                system, _ = _person_system()
                system.set_target_profile(_TRAINED_RED_PROFILE)
                self.assertEqual(system.set_mode("classifier"), "classifier")
                system.process_frame(_AppearanceImage(1.0, 0.0), timestamp=10.0)
                before = system.list_candidates()

                with self.assertRaises(ValueError):
                    system.set_target_profile(rejected)

                self.assertEqual(system.list_candidates(), before)
                self.assertEqual(before[0]["recognized_class"], "red")
                self.assertGreater(_score(before[0]), 0.9)
                self.assertEqual(system.set_mode("step_2"), "classifier")


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
        return max(image.patches, key=lambda patch: box_iou(patch[0], bbox))[1]


class PointAppearanceReidTests(unittest.TestCase):
    def test_point_follows_appearance_when_geometry_points_at_a_crossing_lookalike(self) -> None:
        embedder = _PatchEmbedder()
        system = PerceptionSystem(_PatchDetector(), PersistentTracker(embedder=embedder))
        red_then, red_now, blue_now = (100, 100, 140, 200), (220, 100, 260, 200), (110, 100, 150, 200)
        old = system.process_frame(_PatchImage([(red_then, (1.0, 0.0))], detected=False), timestamp=time.monotonic())
        crossed = [(red_now, (1.0, 0.0)), (blue_now, (0.0, 1.0))]
        for _ in range(2):
            system.process_frame(_PatchImage(crossed), timestamp=time.monotonic())
        ids = {tuple(row["bbox"]): row["id"] for row in system.list_candidates()}
        calls_before = embedder.calls

        # The old box now overlaps the blue look-alike; the red person's appearance must win.
        result = system.point(old.frame_id, box=list(red_then))

        self.assertEqual(result.candidate.id, ids[tuple(float(v) for v in red_now)])
        self.assertNotEqual(result.candidate.source, "point")
        # Reference plus at most one current-appearance encode per latest candidate.
        self.assertLessEqual(embedder.calls - calls_before, 3)
