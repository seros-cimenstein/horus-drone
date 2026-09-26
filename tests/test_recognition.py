from __future__ import annotations

import unittest

from horus.perception import Detection, PerceptionSystem, PersistentTracker, TinyEmbeddingClassifier


class _VectorEmbedder:
    def embed(self, image, bbox):
        return image


class _FixedDetector:
    def detect(self, image):
        return [Detection((10, 10, 40, 60), class_name="object")]


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
