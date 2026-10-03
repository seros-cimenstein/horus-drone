"""Deterministic perception checks that do not load external model weights."""

from __future__ import annotations

import base64
import importlib.util
import json
import math
import time
import unittest
from threading import Event
from unittest.mock import patch

import pytest

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
    def test_person_evidence_survives_frames_that_mislabel_the_track(self) -> None:
        tracker = PersistentTracker(embedder=_VectorEmbedder())
        image = _ImageVector((1.0, 0.0))
        person = tracker.update(
            [Detection((10, 10, 40, 60), class_name="person", class_id=0)],
            image=image,
            frame_id=1,
            timestamp=10.0,
        )[0]
        # An inconclusive "object" frame may take over the label; a later "kite" mislabel must not
        # strip the track's person evidence.
        tracker.update(
            [Detection((11, 10, 41, 60), class_name="object")],
            image=image,
            frame_id=2,
            timestamp=10.1,
        )
        mislabeled = tracker.update(
            [Detection((12, 10, 42, 60), class_name="kite")],
            image=image,
            frame_id=3,
            timestamp=10.2,
        )[0]

        self.assertEqual(mislabeled.id, person.id)
        self.assertEqual(mislabeled.class_name, "kite")
        self.assertTrue(mislabeled.person_evidence)
        self.assertTrue(mislabeled.to_dict()["person_evidence"])

        fresh_kite = tracker.update(
            [Detection((300, 10, 330, 60), class_name="kite")],
            image=image,
            frame_id=4,
            timestamp=10.3,
        )[0]
        self.assertEqual(fresh_kite.class_name, "kite")
        self.assertFalse(fresh_kite.person_evidence)

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
        # The two-frame re-acquisition confirmation has its own test; this one covers the
        # embedding schedule of a single matching call, so the confirmation is disabled here.
        tracker = PersistentTracker(
            TrackerConfig(reacquisition_confirm_s=0.0), embedder=embedder
        )
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
        # The re-acquisition confirmation needs a second distinct frame; the first call is pending.
        self.assertIsNone(tracker.reidentify_candidate(target.id))
        candidates = tracker.update([reappeared], image=image, frame_id=5, timestamp=now + 0.2)

        match = tracker.reidentify_candidate(target.id)

        self.assertIsNotNone(match)
        self.assertEqual(match[0].id, candidates[0].id)
        self.assertNotEqual(match[0].id, target.id)

    def test_reidentification_logs_color_excluded_candidates(self) -> None:
        # One-vote window: this test is about the log records and the colour gate, not the vote.
        tracker = PersistentTracker(
            TrackerConfig(shirt_colour_window=1, shirt_colour_min_votes=1), embedder=_VectorEmbedder()
        )
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

        with self.assertLogs("horus.perception.tracker", level="DEBUG") as captured:
            pending = tracker.reidentify_candidate(reference.id)
            tracker.update([blue, red], image=_ImageVector((1.0, 0.0)), frame_id=4, timestamp=10.3)
            match = tracker.reidentify_candidate(reference.id)

        self.assertIsNone(pending)  # first passing frame confirms nothing on its own

        self.assertIsNotNone(match)
        self.assertEqual(match[0].id, candidates[0].id)
        # The pending-confirmation debug line carries no structured record extras; only the
        # per-comparison records are asserted here.
        records = [
            record for record in captured.records
            if getattr(record, "event", None) == "perception_reid_similarity"
        ]
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


def _cos(left, right) -> float:
    return sum(a * b for a, b in zip(left, right))


def _unit_view(cos_to_primary: float, *, up: bool = True) -> tuple[float, float]:
    """A unit vector whose cosine similarity to the primary view (1, 0) is the given value."""
    cosine = max(-1.0, min(1.0, float(cos_to_primary)))
    sine = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return (cosine, sine if up else -sine)


class TrackerMultiViewReferenceTests(unittest.TestCase):
    """R1 multi-view references: gallery for live tracks, primary-only re-acquisition."""

    @staticmethod
    def _tracker(**overrides) -> PersistentTracker:
        return PersistentTracker(TrackerConfig(**overrides), embedder=_VectorEmbedder())

    @staticmethod
    def _frames_for(tracker: PersistentTracker, vector, *, count: int, label="person", colour=None,
                    box=(10, 10, 40, 60), start_frame: int = 1, start_time: float = 10.0,
                    tracker_id: int | None = None):
        """Feed ``count`` frames of the same detection; return (candidate, last_frame_id)."""
        detection = Detection(box, class_name=label,
                              attributes={} if colour is None else {"shirt_colour": colour})
        candidate = None
        frame_id = start_frame
        timestamp = start_time
        for step in range(count):
            frame_id = start_frame + step
            timestamp = start_time + 0.05 * step
            candidates = tracker.update(
                [detection], image=_ImageVector(vector), frame_id=frame_id, timestamp=timestamp
            )
            candidate = next(item for item in candidates if tracker_id is None or item.id == tracker_id)
        return candidate, frame_id, timestamp

    def test_reference_views_are_admitted_only_at_the_full_threshold_from_the_creation_view(self) -> None:
        tracker = self._tracker()
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
        track = tracker._tracks[target.id]
        self.assertEqual(track.reference_views, [])
        # An accepted validation starts the grace window (the identical view is deduped, not stored).
        self.assertIsNotNone(tracker.validate_candidate(target.id))
        self.assertEqual(track.reference_views, [])

        # A frame inside the grace margin (0.70 vs the 0.72 threshold) still validates, but it is
        # never admitted: admission is anchored on the creation view at the full threshold.
        self._frames_for(tracker, _unit_view(0.70), count=1, colour="blue", start_frame=10)
        self.assertIsNotNone(tracker.validate_candidate(target.id))
        self.assertEqual(track.reference_views, [])

        # A strictly passing frame is admitted.
        self._frames_for(tracker, _unit_view(0.80), count=1, colour="blue", start_frame=20)
        self.assertIsNotNone(tracker.validate_candidate(target.id))
        self.assertEqual(len(track.reference_views), 1)

    def test_reference_view_admission_cannot_chain_away_from_the_creation_view(self) -> None:
        tracker = self._tracker()
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
        track = tracker._tracks[target.id]
        self._frames_for(tracker, _unit_view(0.80), count=1, colour="blue", start_frame=10)
        self.assertIsNotNone(tracker.validate_candidate(target.id))
        self.assertEqual(len(track.reference_views), 1)
        admitted, _ = track.reference_views[0], None

        # 0.95 similar to the admitted view but only 0.60 to the creation view: the gallery keeps
        # the track alive (validation passes), yet the view is NOT stored, so no drift chain forms.
        chained = _unit_view(0.60)
        self.assertGreater(_cos(admitted, chained), 0.90)
        self._frames_for(tracker, chained, count=1, colour="blue", start_frame=20)
        self.assertIsNotNone(tracker.validate_candidate(target.id))
        self.assertEqual(len(track.reference_views), 1)
        self.assertEqual(track.reference_embedding, (1.0, 0.0))

    def test_reference_view_admission_gates(self) -> None:
        # These gates act on a settled colour; the vote has its own tests.
        tracker = self._tracker(shirt_colour_window=1, shirt_colour_min_votes=1)
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="green")
        track = tracker._tracks[target.id]
        good_vector = _unit_view(0.80)

        def candidate(**overrides):
            values = {
                "id": 99,
                "bbox": (10, 10, 40, 60),
                "class_name": "person",
                "confidence": 0.9,
                "frame_id": track.frame_id,
                "timestamp": track.timestamp,
                "attributes": {"shirt_colour": "green"},
            }
            values.update(overrides)
            return Candidate(**values)

        # Class gate: a differing known class is never stored.
        self.assertFalse(tracker._admit_reference_view(track, candidate(class_name="kite"), good_vector))
        # Shirt-colour gate: a differing known colour is never stored.
        self.assertFalse(tracker._admit_reference_view(track, candidate(attributes={"shirt_colour": "red"}), good_vector))
        # Colour gate is defensive: an inconclusive colour cannot vouch for the view either when
        # the track has a known one.
        self.assertFalse(tracker._admit_reference_view(track, candidate(attributes={}), good_vector))
        # Crop gate: a small crop's embedding is noise (10-25 px slivers in the campaign logs).
        self.assertFalse(tracker._admit_reference_view(track, candidate(bbox=(10, 10, 20, 35)), good_vector))
        # The same good view passes every gate.
        self.assertTrue(tracker._admit_reference_view(track, candidate(), good_vector))
        # Dedupe: a near-identical view adds nothing.
        self.assertFalse(tracker._admit_reference_view(track, candidate(id=98), good_vector))

    def test_reference_views_are_capped_and_the_oldest_extra_view_is_evicted(self) -> None:
        tracker = self._tracker(reference_views=2)  # creation view + one extra
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
        track = tracker._tracks[target.id]
        first = _unit_view(0.75, up=True)
        second = _unit_view(0.75, up=False)
        self.assertLess(_cos(first, second), 0.98)
        for index, vector in enumerate((first, second)):
            self._frames_for(tracker, vector, count=1, colour="blue", start_frame=10 + 10 * index)
            self.assertIsNotNone(tracker.validate_candidate(target.id))
        self.assertEqual(len(track.reference_views), 1)
        self.assertEqual(track.reference_views[0], second)  # oldest extra evicted
        self.assertEqual(track.reference_embedding, (1.0, 0.0))  # creation view never evicted

    def test_live_validation_recovers_a_marginal_dip_through_a_stored_view(self) -> None:
        tracker = self._tracker()
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
        track = tracker._tracks[target.id]
        self._frames_for(tracker, _unit_view(0.80), count=1, colour="blue", start_frame=10)
        self.assertIsNotNone(tracker.validate_candidate(target.id))
        self.assertEqual(len(track.reference_views), 1)

        # The smoke6 dip: 0.7196 against the creation view after 0.7917 on the previous frame.
        dip = _unit_view(0.7196)
        self._frames_for(tracker, dip, count=1, colour="blue", start_frame=20)
        with self.assertLogs("horus.perception.tracker", level="DEBUG") as captured:
            validated = tracker.validate_candidate(target.id)
        self.assertIsNotNone(validated)
        self.assertIsNone(tracker.validate_candidate_failure(target.id))
        records = [json.loads(record.getMessage()) for record in captured.records
                   if getattr(record, "event", None) == "perception_reid_similarity"]
        self.assertEqual(records[-1]["mode"], "selected_validation")
        self.assertAlmostEqual(records[-1]["primary_similarity"], 0.7196, places=4)
        self.assertEqual(records[-1]["matched_view"], 1)
        self.assertEqual(records[-1]["reference_view_count"], 2)

    def test_reacquisition_refuses_a_same_colour_lookalike_the_gallery_could_match(self) -> None:
        tracker = self._tracker(reacquisition_confirm_s=0.0)
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="green")
        track = tracker._tracks[target.id]
        stored = _unit_view(0.80)
        self._frames_for(tracker, stored, count=1, colour="green", start_frame=10)
        self.assertIsNotNone(tracker.validate_candidate(target.id))
        self.assertEqual(len(track.reference_views), 1)

        # A look-alike with the same class and colour, a person-sized crop, 0.7004 against the
        # creation view but 0.99 against the stored view - the campaign's 147/261 band.
        lookalike = _unit_view(0.7004)
        self.assertGreater(_cos(lookalike, stored), 0.90)
        self._frames_for(tracker, lookalike, count=2, colour="green", box=(100, 100, 155, 181),
                         start_frame=20)

        self.assertIsNone(tracker.reidentify_candidate(target.id))

    def test_reacquisition_refuses_a_different_colour_high_similarity_candidate(self) -> None:
        # These gates act on a settled colour; the vote has its own tests.
        one_vote = {"shirt_colour_window": 1, "shirt_colour_min_votes": 1}
        tracker = self._tracker(reacquisition_confirm_s=0.0, **one_vote)
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="green")
        # 1592-style: 0.8767 similarity but a red shirt against the green reference, elsewhere in
        # the frame so it stays its own track.
        vector = _unit_view(0.8767)
        self._frames_for(tracker, vector, count=2, colour="red", box=(300, 10, 330, 60),
                         start_frame=20)
        self.assertIsNone(tracker.reidentify_candidate(target.id))

        # Control: the same appearance with the matching colour is re-acquired.
        control = self._tracker(reacquisition_confirm_s=0.0, **one_vote)
        control_target, _, _ = self._frames_for(control, _unit_view(1.0), count=2, colour="green")
        self._frames_for(control, vector, count=2, colour="green", box=(300, 10, 330, 60),
                         start_frame=20)
        match = control.reidentify_candidate(control_target.id)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(match[1], control.config.revalidation_similarity_threshold)

    def test_reacquisition_needs_two_confirmed_frames_of_the_same_candidate(self) -> None:
        clock = [100.0]
        tracker = self._tracker()
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
        returning = _unit_view(0.95)
        with patch("horus.perception.tracker.time.monotonic", lambda: clock[0]):
            self._frames_for(tracker, returning, count=2, colour="blue", box=(300, 10, 330, 60),
                             start_frame=20, start_time=clock[0])

            self.assertIsNone(tracker.reidentify_candidate(target.id))  # first frame: pending
            self.assertIsNone(tracker.reidentify_candidate(target.id))  # same frame: still pending

            clock[0] += 0.2
            self._frames_for(tracker, returning, count=1, colour="blue", box=(300, 10, 330, 60),
                             start_frame=30, start_time=clock[0])
            match = tracker.reidentify_candidate(target.id)
            self.assertIsNotNone(match)
            self.assertEqual(match[1], pytest.approx(0.95, abs=1e-3))

    def test_reacquisition_confirmation_expires_with_the_window(self) -> None:
        clock = [100.0]
        tracker = self._tracker(reacquisition_confirm_s=0.5)
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
        returning = _unit_view(0.95)
        with patch("horus.perception.tracker.time.monotonic", lambda: clock[0]):
            self._frames_for(tracker, returning, count=2, colour="blue", box=(300, 10, 330, 60),
                             start_frame=20, start_time=clock[0])
            self.assertIsNone(tracker.reidentify_candidate(target.id))

            clock[0] += 1.0  # past the confirmation window
            self._frames_for(tracker, returning, count=1, colour="blue", box=(300, 10, 330, 60),
                             start_frame=30, start_time=clock[0])
            self.assertIsNone(tracker.reidentify_candidate(target.id))

            clock[0] += 0.2
            self._frames_for(tracker, returning, count=1, colour="blue", box=(300, 10, 330, 60),
                             start_frame=40, start_time=clock[0])
            self.assertIsNotNone(tracker.reidentify_candidate(target.id))

    def test_pipeline_candidate_records_expose_the_reference_view_count(self) -> None:
        detection = Detection((10, 10, 40, 60), class_name="person", attributes={"shirt_colour": "blue"})
        system = PerceptionSystem(_FixedDetector(detection), PersistentTracker(embedder=_VectorEmbedder()))
        candidate = system.process_frame(_ImageVector((1.0, 0.0)), timestamp=10.0).candidates[0]
        system.process_frame(_ImageVector((1.0, 0.0)), timestamp=10.1)
        self.assertIsNotNone(system.validate_candidate(candidate.id))
        self.assertEqual(system.list_candidates()[0]["reference_views"], 1)  # dedupe: nothing added

        system.process_frame(_ImageVector((0.8, 0.6)), timestamp=10.2)
        self.assertIsNotNone(system.validate_candidate(candidate.id))
        self.assertEqual(system.list_candidates()[0]["reference_views"], 2)

    def test_legacy_single_view_behaviour_when_reference_views_is_one(self) -> None:
        tracker = self._tracker(reference_views=1)
        target, _, _ = self._frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
        track = tracker._tracks[target.id]
        self._frames_for(tracker, _unit_view(0.80), count=1, colour="blue", start_frame=10)
        self.assertIsNotNone(tracker.validate_candidate(target.id))
        self.assertEqual(track.reference_views, [])  # nothing stored
        self.assertEqual(track.reference_embedding, (1.0, 0.0))


class TrackerValidationTests(unittest.TestCase):
    def test_revalidation_grace_keeps_a_track_through_a_marginal_dip(self) -> None:
        clock = [100.0]
        tracker = PersistentTracker(
            TrackerConfig(revalidation_grace_s=1.0), embedder=_VectorEmbedder()
        )
        detection = Detection((10, 10, 40, 60), class_name="person")
        # cos((1, 0), (0.70, 0.714)) = 0.70: inside the default 0.05 grace margin below the 0.72
        # threshold, like the campaign's correct-target dip 0.7917 -> 0.7196.
        marginal = _ImageVector((0.70, 0.714))
        with patch("horus.perception.tracker.time.monotonic", lambda: clock[0]):
            tracker.update(
                [detection], image=_ImageVector((1.0, 0.0)), frame_id=1, timestamp=clock[0]
            )
            clock[0] += 0.05
            current = tracker.update(
                [detection], image=_ImageVector((1.0, 0.0)), frame_id=2, timestamp=clock[0]
            )[0]
            self.assertIsNotNone(tracker.validate_candidate(current.id))

            clock[0] += 0.10
            dip = tracker.update([detection], image=marginal, frame_id=3, timestamp=clock[0])[0]
            self.assertEqual(dip.id, current.id)
            self.assertIsNotNone(tracker.validate_candidate(dip.id))
            self.assertIsNone(tracker.validate_candidate_failure(dip.id))

            # A total appearance change (an identity swap) is never forgiven by the margin.
            clock[0] += 0.10
            swapped = tracker.update(
                [detection], image=_ImageVector((0.0, 1.0)), frame_id=4, timestamp=clock[0]
            )[0]
            self.assertIsNone(tracker.validate_candidate(swapped.id))
            self.assertEqual(
                tracker.validate_candidate_failure(swapped.id), "appearance_mismatch"
            )

            # Even a marginal dip expires once the grace window has passed.
            clock[0] += 2.0
            late = tracker.update([detection], image=marginal, frame_id=5, timestamp=clock[0])[0]
            self.assertIsNone(tracker.validate_candidate(late.id))
            self.assertEqual(tracker.validate_candidate_failure(late.id), "appearance_mismatch")

    def test_reid_similarity_logging_is_one_summary_per_second(self) -> None:
        clock = [100.0]
        tracker = PersistentTracker(embedder=_VectorEmbedder())
        detection = Detection((10, 10, 40, 60), class_name="person")
        with patch("horus.perception.tracker.time.monotonic", lambda: clock[0]):
            tracker.update(
                [detection], image=_ImageVector((1.0, 0.0)), frame_id=1, timestamp=clock[0]
            )
            clock[0] += 0.05
            current = tracker.update(
                [detection], image=_ImageVector((1.0, 0.0)), frame_id=2, timestamp=clock[0]
            )[0]
            with self.assertLogs("horus.perception.tracker", level="DEBUG") as captured:
                for step in range(6):
                    # Two validation ticks per frame, like the executor's control loop.
                    self.assertIsNotNone(tracker.validate_candidate(current.id))
                    self.assertIsNotNone(tracker.validate_candidate(current.id))
                    clock[0] += 0.3
                    tracker.update(
                        [detection],
                        image=_ImageVector((1.0, 0.0)),
                        frame_id=10 + step,
                        timestamp=clock[0],
                    )

        records = [json.loads(record.getMessage()) for record in captured.records]
        summaries = [record for record in records if record["event"] == "perception_reid_summary"]
        per_comparison = [
            record for record in records if record["event"] == "perception_reid_similarity"
        ]
        # 12 comparisons (well over a second of ticks), one INFO window summary; every
        # per-comparison record stays at DEBUG so a hold cannot flood the log.
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["mode"], "selected_validation")
        self.assertEqual(summaries[0]["comparisons"], summaries[0]["total_comparisons"])
        self.assertGreaterEqual(summaries[0]["comparisons"], 6)
        self.assertTrue(summaries[0]["own_track_seen"])
        self.assertEqual(summaries[0]["best_candidate_id"], current.id)
        self.assertAlmostEqual(summaries[0]["best_similarity"], 1.0)
        self.assertEqual(len(per_comparison), 12)

    def test_validation_failure_reason_distinguishes_missing_stale_and_mismatch(self) -> None:
        tracker = PersistentTracker(embedder=_VectorEmbedder())
        detection = Detection((10, 10, 40, 60), class_name="person")
        created = tracker.update(
            [detection], image=_ImageVector((1.0, 0.0)), frame_id=1, timestamp=10.0
        )[0]

        self.assertEqual(tracker.validate_candidate_failure(999), "not_visible")
        # A single-hit track is in the current frame but not fresh enough to validate.
        self.assertEqual(tracker.validate_candidate_failure(created.id), "stale")

        tracker.update([detection], image=_ImageVector((1.0, 0.0)), frame_id=2, timestamp=10.1)
        self.assertIsNotNone(tracker.validate_candidate(created.id))
        self.assertIsNone(tracker.validate_candidate_failure(created.id))

        tracker.update([detection], image=_ImageVector((0.0, 1.0)), frame_id=3, timestamp=10.2)
        self.assertEqual(tracker.validate_candidate_failure(created.id), "appearance_mismatch")
        self.assertIsNone(tracker.validate_candidate(created.id))

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
        with self.assertLogs("horus.perception.tracker", level="DEBUG") as captured:
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



class TrackerSeedEvidenceTests(unittest.TestCase):
    """A grounder seed is not person evidence until the detector says so."""

    def test_grounder_seed_gains_person_evidence_only_from_a_detector_label(self) -> None:
        import numpy as np

        tracker = PersistentTracker()
        image = np.zeros((120, 160, 3), dtype=np.uint8)
        image[40:90, 60:100] = 200
        seeded = tracker.seed(
            (60, 40, 100, 90),
            Frame(1, 10.0, image),
            label="person",
            confidence=0.42,
            source="open_vocab",
            attributes={"grounder_span": "person person person"},
        )

        self.assertFalse(seeded.person_evidence)
        self.assertEqual(seeded.class_name, "person")
        self.assertEqual(seeded.attributes["grounder_span"], "person person person")

        tracker.update(
            [Detection((60, 40, 100, 90), class_name="person", class_id=0, confidence=0.7)],
            image=image,
            frame_id=2,
            timestamp=10.1,
        )

        self.assertTrue(tracker._tracks[seeded.id].person_evidence)

    def test_loosely_overlapping_detection_does_not_colour_a_seeded_track(self) -> None:
        # Koko regression koko-after-forest-15178-ttl3: the grounder seed boxed on a
        # solid:crimson hiker (seed box [277, 422, 322, 478]) reported green because a detector
        # track on a different, green-shirted person ([275, 372, 311, 442], IoU 0.156) was
        # associated with the seed and its colour merged into the seed's vote. A seeded track
        # must only accept a detection whose box agrees strongly.
        import numpy as np

        tracker = PersistentTracker()
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        image[360:478, 260:330] = 200
        # CSRT needs texture to advance the seed; a checkerboard inside the seed box.
        image[422:478:6, 277:322] = 40
        image[422:478, 277:322:6] = 40
        seeded = tracker.seed(
            (277, 422, 322, 478),
            Frame(1, 10.0, image),
            label="person",
            confidence=0.61,
            source="open_vocab",
            attributes={"grounder_span": "person"},
        )
        green_person = Detection(
            (275, 372, 311, 442),
            class_name="person",
            class_id=0,
            confidence=0.7,
            attributes={"shirt_colour": "green", "shirt_colour_confidence": 1.0},
        )

        candidates = ()
        for step in range(4):  # more frames than the vote window, so a merge would have settled
            candidates = tracker.update(
                [green_person],
                image=image,
                frame_id=2 + step,
                timestamp=10.1 + 0.1 * step,
            )

        seed_candidate = next(item for item in candidates if item.id == seeded.id)

        assert "shirt_colour" not in seed_candidate.attributes
        assert seed_candidate.attributes["grounder_span"] == "person"
        # The seed keeps tracking its own box (CSRT drift of a few pixels), not the other
        # person's box 50 px above it.
        assert all(
            abs(seed_candidate.bbox[index] - expected) <= 5.0
            for index, expected in enumerate((277.0, 422.0, 322.0, 478.0))
        )

    def test_strongly_overlapping_detection_still_colours_a_seeded_track(self) -> None:
        # The legitimate path: the detector really tracks the seeded person, so its vote (and the
        # person evidence it carries) still reaches the seed.
        import numpy as np

        tracker = PersistentTracker()
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        image[100:200, 100:200] = 200
        seeded = tracker.seed(
            (100, 100, 160, 200),
            Frame(1, 10.0, image),
            label="person",
            confidence=0.61,
            source="open_vocab",
        )
        same_person = Detection(
            (102, 102, 162, 202),
            class_name="person",
            class_id=0,
            confidence=0.7,
            attributes={"shirt_colour": "green", "shirt_colour_confidence": 1.0},
        )

        candidates = ()
        for step in range(4):
            candidates = tracker.update(
                [same_person],
                image=image,
                frame_id=2 + step,
                timestamp=10.1 + 0.1 * step,
            )

        seed_candidate = next(item for item in candidates if item.id == seeded.id)

        assert seed_candidate.attributes["shirt_colour"] == "green"
        assert tracker._tracks[seeded.id].person_evidence is True

    def test_grounder_seed_does_not_gain_evidence_from_its_own_label(self) -> None:
        import numpy as np

        tracker = PersistentTracker()
        image = np.zeros((120, 160, 3), dtype=np.uint8)
        image[40:90, 60:100] = 200
        seeded = tracker.seed(
            (60, 40, 100, 90),
            Frame(1, 10.0, image),
            label="person",
            confidence=0.42,
            source="open_vocab",
        )

        for frame_id in (2, 3):
            tracker.update([], image=image, frame_id=frame_id, timestamp=10.0 + 0.1 * frame_id)

        self.assertFalse(tracker._tracks[seeded.id].person_evidence)


class TrackShirtColourVoteTests(unittest.TestCase):
    """The published colour is a bounded majority vote, not the latest classification."""

    @staticmethod
    def _tracker(**overrides) -> PersistentTracker:
        return PersistentTracker(TrackerConfig(**overrides), embedder=_VectorEmbedder())

    @staticmethod
    def _feed(tracker: PersistentTracker, colours, *, start_frame: int = 1):
        candidate = None
        for step, colour in enumerate(colours):
            detection = Detection(
                (10, 10, 40, 60),
                class_name="person",
                attributes={} if colour is None else {"shirt_colour": colour},
            )
            candidate = tracker.update(
                [detection],
                image=_ImageVector((1.0, 0.0)),
                frame_id=start_frame + step,
                timestamp=10.0 + 0.05 * step,
            )[0]
        return candidate

    def test_one_misread_frame_does_not_label_a_track(self) -> None:
        tracker = self._tracker()

        first = self._feed(tracker, ["red"])

        self.assertNotIn("shirt_colour", first.attributes)

        settled = self._feed(tracker, ["blue", "blue", "blue"], start_frame=2)

        self.assertEqual(settled.attributes["shirt_colour"], "blue")
        self.assertAlmostEqual(settled.attributes["shirt_colour_confidence"], 3 / 4)

    def test_colour_flip_needs_a_majority_of_the_window(self) -> None:
        tracker = self._tracker()

        self._feed(tracker, ["red", "red", "red"])
        still_red = self._feed(tracker, ["blue", "blue"], start_frame=4)

        # red,red,red,blue,blue: red still holds the majority of the window.
        self.assertEqual(still_red.attributes["shirt_colour"], "red")

        flipped = self._feed(tracker, ["blue"], start_frame=6)

        # The window slid to red,red,blue,blue,blue: the flip now has its majority.
        self.assertEqual(flipped.attributes["shirt_colour"], "blue")

    def test_tied_votes_publish_nothing(self) -> None:
        tracker = self._tracker(shirt_colour_window=4, shirt_colour_min_votes=2)

        tied = self._feed(tracker, ["red", "red", "blue", "blue"])

        self.assertNotIn("shirt_colour", tied.attributes)

    def test_inconclusive_frames_neither_vote_nor_erase_a_settled_colour(self) -> None:
        tracker = self._tracker()

        self._feed(tracker, ["green", "green", "green"])
        after_none = self._feed(tracker, [None, None], start_frame=4)

        self.assertEqual(after_none.attributes["shirt_colour"], "green")

    def test_vote_config_requires_a_positive_window_and_reachable_majority(self) -> None:
        for window, minimum in ((0, 1), (5, 0), (True, 1), (5, 6)):
            with self.assertRaises(ValueError):
                TrackerConfig(shirt_colour_window=window, shirt_colour_min_votes=minimum)


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

        candidate = None
        for _ in range(3):  # the published colour is a majority vote over recent frames
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

    def test_supported_garment_hues_report_their_family(self) -> None:
        import numpy as np

        from horus.perception.attributes import classify_shirt_colour

        # Nominal worldgen fabric colours (sim/worldgen/appearance.py COLOURS).
        for pixel, expected in (
            ((36, 28, 200), "red"),          # red / crimson / scarlet
            ((34, 18, 165), "red"),
            ((40, 52, 226), "red"),
            ((40, 28, 110), "red"),          # maroon
            ((22, 108, 240), "red"),         # orange reads as red
            ((18, 110, 250), "red"),         # hi-vis orange
            ((62, 150, 42), "green"),        # green / kelly / lime
            ((50, 176, 70), "green"),
            ((40, 214, 160), "green"),       # lime
            ((52, 108, 104), "green"),       # olive
            ((50, 88, 74), "green"),         # woodland green
            ((186, 74, 32), "blue"),         # blue / royal blue
            ((210, 96, 44), "blue"),
            ((72, 36, 26), "blue"),          # navy
            ((214, 160, 110), "blue"),       # sky blue
        ):
            image = np.zeros((100, 100, 3), dtype=np.uint8)
            image[20:60, 20:80] = pixel
            self.assertEqual(
                classify_shirt_colour(image, (0, 0, 100, 100))[0], expected, f"{pixel} should read {expected}"
            )

    def test_achromatic_and_off_hue_fabrics_report_no_colour(self) -> None:
        import numpy as np

        from horus.perception.attributes import classify_shirt_colour

        # Fabrics the worldgen registry lists with no possible classifier colour. A label
        # here is a false positive that the tracker then keeps for the track's lifetime.
        for pixel in (
            (232, 236, 236),   # white
            (186, 186, 184),   # light grey
            (132, 130, 128),   # grey
            (68, 64, 62),      # charcoal
            (26, 24, 24),      # black
            (168, 196, 210),   # beige
            (118, 160, 176),   # khaki
            (128, 172, 196),   # sand
            (132, 146, 150),   # stone
            (44, 70, 104),     # brown
            (40, 150, 196),    # mustard
            (44, 204, 232),    # yellow
            (206, 204, 200),   # reflective silver
        ):
            image = np.zeros((100, 100, 3), dtype=np.uint8)
            image[20:60, 20:80] = pixel
            self.assertIsNone(
                classify_shirt_colour(image, (0, 0, 100, 100)), f"{pixel} must stay indeterminate"
            )

    def test_warm_lit_neutral_fabric_does_not_read_as_red(self) -> None:
        import numpy as np

        from horus.perception.attributes import classify_shirt_colour

        # Campaign regression: warm-lit khaki, sand, grey and hi-vis grey torsos were
        # reported "red" with high confidence (e.g. heather:white, solid:khaki,
        # hivis:grey+reflective_silver in runs/campaign/verify-urban-15178).
        for bgr in ((110, 160, 190), (105, 145, 190), (150, 160, 170), (140, 143, 148),
                    (206, 204, 200)):
            image = np.zeros((100, 100, 3), dtype=np.uint8)
            image[20:60, 20:80] = bgr
            self.assertIsNone(
                classify_shirt_colour(image, (0, 0, 100, 100)), f"warm neutral {bgr} must stay indeterminate"
            )
            # A few strongly saturated warm pixels inside an otherwise neutral torso are
            # not enough either: skin, hair and background keep that share below the floor.
            image[25:35, 25:55] = (22, 108, 240)  # orange patch, 300 px of the 1750 px band
            self.assertIsNone(classify_shirt_colour(image, (0, 0, 100, 100)))
        # An orange torso covering the band is still a red family garment.
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        image[20:60, 20:80] = (22, 108, 240)
        self.assertEqual(classify_shirt_colour(image, (0, 0, 100, 100))[0], "red")

    def test_light_grey_garment_gets_no_shirt_colour_attribute(self) -> None:
        import numpy as np

        system = PerceptionSystem(
            _FixedDetector(Detection((0, 0, 100, 100), class_name="person")),
            PersistentTracker(),
        )

        warm_grey = np.zeros((100, 100, 3), dtype=np.uint8)
        warm_grey[20:60, 20:80] = (150, 160, 170)
        # The tracker keeps an accepted attribute for the life of a track, so a neutral
        # garment must stay unlabelled on every frame, not just the first.
        for _ in range(3):
            grey_candidate = system.process_frame(warm_grey).candidates[0]
            self.assertNotIn("shirt_colour", grey_candidate.attributes)

        scarlet = np.zeros((100, 100, 3), dtype=np.uint8)
        scarlet[20:60, 20:80] = (36, 28, 200)
        for _ in range(3):
            red_candidate = system.process_frame(scarlet).candidates[0]

        self.assertEqual(red_candidate.attributes["shirt_colour"], "red")

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
