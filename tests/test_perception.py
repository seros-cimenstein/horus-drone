"""Deterministic perception checks that do not load external model weights."""

from __future__ import annotations

import base64
import importlib.util
import json
import time
import unittest
from threading import Event
from unittest.mock import patch

from horus.perception import (
    Candidate,
    Detection,
    Frame,
    PerceptionSystem,
    PersistentTracker,
    TrackerConfig,
    render_set_of_marks,
)


class CameraBackendTests(unittest.TestCase):
    def test_udp_source_rejects_opencv_build_without_gstreamer(self) -> None:
        from horus.perception import CameraConfig, OpenCVCameraReader

        class OpenCVWithoutGStreamer:
            CAP_GSTREAMER = 1800

            @staticmethod
            def getBuildInformation() -> str:
                return "Video I/O:\n    GStreamer:                   NO\n"

            @staticmethod
            def VideoCapture(*args, **kwargs):
                raise AssertionError("capture must not be opened without GStreamer")

        with patch.dict("sys.modules", {"cv2": OpenCVWithoutGStreamer}):
            with self.assertRaisesRegex(RuntimeError, "built with GStreamer"):
                OpenCVCameraReader(CameraConfig(source="udp://0.0.0.0:5600")).open()

    def test_udp_source_opens_when_gstreamer_rejects_timeout_parameters(self) -> None:
        from horus.perception import CameraConfig, Frame, OpenCVCameraReader

        class Capture:
            def __init__(self, opened: bool) -> None:
                self.opened = opened

            def isOpened(self) -> bool:
                return self.opened

            def read(self):
                return self.opened, object() if self.opened else None

            def release(self) -> None:
                pass

        class OpenCVWithGStreamer:
            CAP_GSTREAMER = 1800
            CAP_PROP_OPEN_TIMEOUT_MSEC = 53
            CAP_PROP_READ_TIMEOUT_MSEC = 54

            @staticmethod
            def getBuildInformation() -> str:
                return "Video I/O:\n    GStreamer:                   YES\n"

            @staticmethod
            def VideoCapture(*args):
                return Capture(opened=len(args) == 2)

        config = CameraConfig(
            source="udp",
            open_timeout_s=1.5,
            read_timeout_s=2.5,
            udp_timeout_s=3.0,
        )
        with patch.dict("sys.modules", {"cv2": OpenCVWithGStreamer}):
            frame = OpenCVCameraReader(config).open().read()

        self.assertIsInstance(frame, Frame)
        self.assertEqual(frame.frame_id, 1)

    def test_camera_retries_after_open_failure(self) -> None:
        from horus.perception import CameraConfig, Frame, PerceptionSystem

        class RetryCamera:
            def __init__(self) -> None:
                self.config = CameraConfig(open_timeout_s=2.0, read_timeout_s=2.0)
                self.open_calls = 0
                self.read_calls = 0
                self.read_started = Event()
                self.released = Event()

            def open(self):
                self.open_calls += 1
                if self.open_calls == 1:
                    raise RuntimeError("temporary source failure")
                return self

            def read(self):
                self.read_calls += 1
                if self.read_calls == 1:
                    self.read_started.set()
                    return Frame(frame_id=1, timestamp=time.monotonic(), image=None)
                self.released.wait()
                return None

            def release(self) -> None:
                if self.read_calls:
                    self.released.set()

        class EmptyDetector:
            @staticmethod
            def detect(image):
                return ()

        camera = RetryCamera()
        system = PerceptionSystem(
            EmptyDetector(),
            PersistentTracker(TrackerConfig()),
            camera=camera,
        )
        frame_processed = Event()
        system.start(on_observation=lambda _: frame_processed.set())
        try:
            self.assertTrue(camera.read_started.wait(timeout=2.0))
            self.assertTrue(frame_processed.wait(timeout=1.0))
            self.assertEqual(camera.open_calls, 2)
            status = system.camera_status()
            self.assertEqual(status["camera"], "streaming")
            self.assertIsNotNone(status["last_frame_age_s"])
        finally:
            system.close()


class TrackerPersistenceTests(unittest.TestCase):
    def test_id_survives_short_occlusion_and_expires_after_retention(self) -> None:
        tracker = PersistentTracker(TrackerConfig(max_lost_seconds=2.5))
        first = tracker.update(
            [Detection((10, 10, 40, 60), class_name="person")],
            frame_id=1,
            timestamp=10.0,
        )[0]
        tracker.update([], frame_id=2, timestamp=11.0)
        reacquired = tracker.update(
            [Detection((10, 10, 40, 60), class_name="person")],
            frame_id=3,
            timestamp=12.1,
        )[0]
        self.assertEqual(reacquired.id, first.id)

        expired = tracker.update(
            [Detection((10, 10, 40, 60), class_name="person")],
            frame_id=4,
            timestamp=15.0,
        )[0]
        self.assertNotEqual(expired.id, first.id)


class _VectorEmbedder:
    def __init__(self):
        self.calls = []

    def embed(self, image, bbox):
        self.calls.append((image, bbox))
        return getattr(image, "vector", image)


class _ImageVector:
    shape = (480, 640, 3)

    def __init__(self, vector):
        self.vector = vector


class TrackerEmbeddingScheduleTests(unittest.TestCase):
    def test_update_embeds_new_tracks_but_not_each_matched_frame(self) -> None:
        embedder = _VectorEmbedder()
        tracker = PersistentTracker(embedder=embedder)
        detection = Detection((10, 10, 40, 60), class_name="person")

        first = tracker.update(
            [detection], image=(1.0, 0.0), frame_id=1, timestamp=10.0
        )[0]
        second = tracker.update(
            [detection], image=(0.0, 1.0), frame_id=2, timestamp=10.1
        )[0]

        self.assertEqual(second.id, first.id)
        self.assertEqual(len(embedder.calls), 1)
        self.assertEqual(tracker._tracks[first.id].reference_embedding, (1.0, 0.0))
        self.assertIsNone(second.embedding)


class TrackerReidentificationTests(unittest.TestCase):
    def test_lost_reference_embedding_rematches_fresh_candidate_under_new_id(self) -> None:
        embedder = _VectorEmbedder()
        tracker = PersistentTracker(embedder=embedder)
        reference = tracker.update(
            [
                Detection(
                    (10, 10, 40, 60),
                    class_name="person",
                    attributes={"shirt_colour": "blue"},
                )
            ],
            image=(1.0, 0.0),
            frame_id=1,
            timestamp=10.0,
        )[0]
        tracker.update([], image=(1.0, 0.0), frame_id=2, timestamp=11.0)
        replacement_detection = Detection(
            (300, 10, 330, 60),
            class_name="person",
            attributes={"shirt_colour": "blue"},
        )
        tracker.update(
            [replacement_detection],
            image=(0.0, 1.0),
            frame_id=3,
            timestamp=11.1,
        )
        replacement = tracker.update(
            [replacement_detection],
            image=(1.0, 0.0),
            frame_id=4,
            timestamp=11.2,
        )[0]

        match = tracker.reidentify_candidate(reference.id)
        repeated_match = tracker.reidentify_candidate(reference.id)

        self.assertNotEqual(replacement.id, reference.id)
        self.assertIsNotNone(match)
        self.assertEqual(match[0].id, replacement.id)
        self.assertGreaterEqual(match[1], tracker.config.revalidation_similarity_threshold)
        self.assertEqual(tracker._tracks[replacement.id].reference_embedding, (0.0, 1.0))
        self.assertEqual(match[0].embedding, (1.0, 0.0))
        self.assertIsNotNone(repeated_match)
        self.assertEqual(repeated_match[0].id, replacement.id)
        self.assertEqual(len(embedder.calls), 3)

    def test_inconclusive_frame_does_not_let_a_lookalike_steal_a_lost_track(self) -> None:
        # Identical embeddings (same mesh): only the remembered shirt colour separates the people.
        tracker = PersistentTracker(embedder=_VectorEmbedder())
        image = _ImageVector((1.0, 0.0))
        target = tracker.update(
            [Detection((10, 10, 40, 60), class_name="person", attributes={"shirt_colour": "blue"})],
            image=image,
            frame_id=1,
            timestamp=10.0,
        )[0]
        tracker.update(
            [Detection((12, 10, 42, 60), class_name="person", attributes={})],  # side view: no colour
            image=image,
            frame_id=2,
            timestamp=10.1,
        )
        red = Detection((400, 10, 430, 60), class_name="person", attributes={"shirt_colour": "red"})
        tracker.update([red], image=image, frame_id=3, timestamp=10.2)
        tracker.update([red], image=image, frame_id=4, timestamp=10.3)

        self.assertIsNone(tracker.reidentify_candidate(target.id))

    def test_target_hidden_longer_than_track_retention_is_still_reidentified(self) -> None:
        tracker = PersistentTracker(embedder=_VectorEmbedder())
        image = _ImageVector((1.0, 0.0))
        blue = {"shirt_colour": "blue"}
        target = tracker.update(
            [Detection((10, 10, 40, 60), class_name="person", attributes=blue)],
            image=image, frame_id=1, timestamp=10.0,
        )[0]
        # Hidden for longer than max_lost_seconds: the track expires while nothing is detected.
        tracker.update([], image=image, frame_id=2, timestamp=10.0 + tracker.config.max_lost_seconds + 1.0)
        reappeared = Detection((300, 10, 330, 60), class_name="person", attributes=blue)
        now = 10.0 + tracker.config.max_lost_seconds + 1.5
        tracker.update([reappeared], image=image, frame_id=3, timestamp=now)
        candidates = tracker.update([reappeared], image=image, frame_id=4, timestamp=now + 0.1)

        match = tracker.reidentify_candidate(target.id)

        self.assertIsNotNone(match)
        self.assertEqual(match[0].id, candidates[0].id)
        self.assertNotEqual(match[0].id, target.id)

    def test_reidentification_logs_color_excluded_candidates(self) -> None:
        tracker = PersistentTracker(embedder=_VectorEmbedder())
        reference = tracker.update(
            [
                Detection(
                    (10, 10, 40, 60),
                    class_name="person",
                    attributes={"shirt_colour": "blue"},
                )
            ],
            image=_ImageVector((1.0, 0.0)),
            frame_id=1,
            timestamp=10.0,
        )[0]
        blue = Detection(
            (300, 10, 330, 60),
            class_name="person",
            attributes={"shirt_colour": "blue"},
        )
        red = Detection(
            (400, 10, 430, 60),
            class_name="person",
            attributes={"shirt_colour": "red"},
        )
        tracker.update(
            [blue, red],
            image=_ImageVector((0.0, 1.0)),
            frame_id=2,
            timestamp=10.1,
        )
        candidates = tracker.update(
            [blue, red],
            image=_ImageVector((1.0, 0.0)),
            frame_id=3,
            timestamp=10.2,
        )

        with self.assertLogs("horus.perception.tracker", level="INFO") as captured:
            match = tracker.reidentify_candidate(reference.id)

        self.assertIsNotNone(match)
        self.assertEqual(match[0].id, candidates[0].id)
        records = captured.records
        self.assertEqual({record.mode for record in records}, {"reidentification"})
        self.assertEqual({record.shirt_colour for record in records}, {"blue", "red"})
        self.assertEqual(
            {record.candidate_id for record in records}, {item.id for item in candidates}
        )
        self.assertTrue(
            all(
                record.event == "perception_reid_similarity"
                and record.reference_id == reference.id
                and record.threshold == tracker.config.revalidation_similarity_threshold
                and record.configured_threshold == record.threshold
                and record.crop_width_px == 30
                and record.crop_height_px == 50
                for record in records
            )
        )
        payloads = [json.loads(record.getMessage()) for record in records]
        self.assertTrue(
            all(
                payload["event"] == "perception_reid_similarity"
                and payload["mode"] == "reidentification"
                and payload["reference_id"] == reference.id
                and payload["threshold"] == tracker.config.revalidation_similarity_threshold
                for payload in payloads
            )
        )


class _FixedDetector:
    def __init__(self, detection):
        self.detection = detection

    def detect(self, image):
        return [self.detection]


@unittest.skipUnless(
    importlib.util.find_spec("numpy") is not None,
    "NumPy is required for ignore-region frame processing",
)
class IgnoreRegionTests(unittest.TestCase):
    def test_drops_boxes_with_at_least_half_their_area_in_an_ignore_region(self) -> None:
        import numpy as np

        detections = (
            Detection((130, 0, 200, 20), class_name="airplane"),
            Detection((100, 0, 160, 20), class_name="airplane"),
            Detection((100, 0, 150, 20), class_name="airplane"),
            Detection((20, 30, 50, 70), class_name="person"),
        )

        class ProposalDetector:
            def detect(self, image):
                return detections

        system = PerceptionSystem(
            ProposalDetector(),
            PersistentTracker(),
            ignore_regions=((0.65, 0.0, 1.0, 0.2),),
        )
        observation = system.process_frame(
            np.zeros((100, 200, 3), dtype=np.uint8),
            timestamp=1.0,
        )

        self.assertCountEqual(
            [candidate.bbox for candidate in observation.candidates],
            [(100, 0, 150, 20), (20, 30, 50, 70)],
        )
        stats = system.processing_stats()
        self.assertEqual(
            (stats["frames"], stats["detections"], stats["ignored"], stats["candidates"]),
            (1, 4, 2, 2),
        )
        self.assertIsNone(stats["fps"])


class TrackerValidationTests(unittest.TestCase):
    def test_validate_id_rejects_current_embedding_mismatch(self) -> None:
        detection = Detection((10, 10, 40, 60), class_name="person")
        system = PerceptionSystem(
            _FixedDetector(detection),
            PersistentTracker(embedder=_VectorEmbedder()),
        )
        first = system.process_frame((1.0, 0.0), timestamp=10.0).candidates[0]
        self.assertFalse(system.validate_id(first.id))
        system.process_frame((1.0, 0.0), timestamp=10.1)
        self.assertTrue(system.validate_id(first.id))
        system.process_frame((0.0, 1.0), timestamp=10.2)
        self.assertFalse(system.validate_id(first.id))

    def test_validation_encodes_selected_candidate_once_per_frame(self) -> None:
        embedder = _VectorEmbedder()
        tracker = PersistentTracker(embedder=embedder)
        detection = Detection((10, 10, 40, 60), class_name="person")
        first = tracker.update(
            [detection],
            image=_ImageVector((1.0, 0.0)),
            frame_id=1,
            timestamp=10.0,
        )[0]
        current = tracker.update(
            [detection],
            image=_ImageVector((1.0, 0.0)),
            frame_id=2,
            timestamp=10.1,
        )[0]

        self.assertEqual(len(embedder.calls), 1)
        with self.assertLogs("horus.perception.tracker", level="INFO") as captured:
            validated = tracker.validate_candidate(current.id)
            repeated = tracker.validate_candidate(current.id)

        self.assertIsNotNone(validated)
        self.assertIsNotNone(repeated)
        self.assertEqual(validated.embedding, (1.0, 0.0))
        self.assertEqual(repeated.embedding, validated.embedding)
        self.assertEqual(first.id, current.id)
        self.assertEqual(len(embedder.calls), 2)
        self.assertEqual(
            [record.mode for record in captured.records],
            ["selected_validation", "selected_validation"],
        )
        self.assertTrue(
            all(
                record.reference_id == first.id
                and record.candidate_id == current.id
                and record.configured_threshold
                == tracker.config.revalidation_similarity_threshold
                and record.bbox == [10, 10, 40, 60]
                and record.crop_width_px == 30
                and record.crop_height_px == 50
                for record in captured.records
            )
        )



@unittest.skipUnless(
    importlib.util.find_spec("numpy") is not None,
    "NumPy is required for shirt-color attribute extraction",
)
class ShirtColourAttributeTests(unittest.TestCase):
    def test_person_candidates_receive_local_torso_colour(self) -> None:
        import numpy as np

        image = np.zeros((100, 100, 3), dtype=np.uint8)
        image[20:60, 20:80] = (220, 45, 10)
        system = PerceptionSystem(
            _FixedDetector(Detection((0, 0, 100, 100), class_name="person")),
            PersistentTracker(),
        )

        candidate = system.process_frame(image).candidates[0]

        self.assertEqual(candidate.attributes["shirt_colour"], "blue")
        self.assertGreaterEqual(candidate.attributes["shirt_colour_confidence"], 0.55)


    def test_distinguishes_static_red_and_green_shirts(self) -> None:
        import numpy as np

        from horus.perception.attributes import classify_shirt_colour

        for pixel, expected in (
            ((220, 45, 10), "blue"),
            ((20, 40, 220), "red"),
            ((30, 180, 30), "green"),
        ):
            image = np.zeros((100, 100, 3), dtype=np.uint8)
            image[20:60, 20:80] = pixel
            self.assertEqual(classify_shirt_colour(image, (0, 0, 100, 100))[0], expected)

@unittest.skipUnless(
    importlib.util.find_spec("cv2") is not None and importlib.util.find_spec("numpy") is not None,
    "OpenCV and NumPy are required for JPEG rendering",
)
class SetOfMarkRenderingTests(unittest.TestCase):
    def test_payload_contains_scaled_numbered_candidate_table_and_jpeg(self) -> None:
        import numpy as np

        candidate = Candidate(
            id=7,
            bbox=(100, 80, 500, 400),
            class_name="person",
            confidence=0.91,
            frame_id=12,
            timestamp=44.5,
            pose={"x": 1.0},
            attributes={"shirt": "red"},
        )
        frame = Frame(
            frame_id=12,
            timestamp=44.5,
            image=np.zeros((720, 1280, 3), dtype=np.uint8),
            pose={"x": 1.0},
            candidates=(candidate,),
        )
        payload = render_set_of_marks(frame)
        self.assertLessEqual(max(payload.width, payload.height), 640)
        self.assertEqual(payload.candidates[0]["id"], 7)
        self.assertEqual(payload.candidates[0]["mark"], 7)
        self.assertTrue(payload.image_jpeg.startswith(b"\xff\xd8"))

        serialized = frame.to_dict()
        self.assertEqual(serialized["frame_id"], 12)
        self.assertEqual(serialized["candidates"][0]["id"], 7)
        self.assertTrue(base64.b64decode(serialized["image_base64"]).startswith(b"\xff\xd8"))
