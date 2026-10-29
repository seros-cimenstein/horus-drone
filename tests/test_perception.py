"""Deterministic perception checks that do not load external model weights."""

from __future__ import annotations

import importlib.util
import math
import sys
import time
from threading import Event

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
from horus.perception.tracks import Track

from tests.fakes import FixedDetector, feed_frame


# Camera backend


def test_udp_source_rejects_opencv_build_without_gstreamer(monkeypatch) -> None:
    from horus.perception import CameraConfig, OpenCVCameraReader

    class OpenCVWithoutGStreamer:
        CAP_GSTREAMER = 1800

        @staticmethod
        def getBuildInformation() -> str:
            return "Video I/O:\n    GStreamer:                   NO\n"

        @staticmethod
        def VideoCapture(*args, **kwargs):
            raise AssertionError("capture must not be opened without GStreamer")

    monkeypatch.setitem(sys.modules, "cv2", OpenCVWithoutGStreamer)
    with pytest.raises(RuntimeError, match="built with GStreamer"):
        OpenCVCameraReader(CameraConfig(source="udp://0.0.0.0:5600")).open()


def test_udp_source_opens_when_gstreamer_rejects_timeout_parameters(monkeypatch) -> None:
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
    monkeypatch.setitem(sys.modules, "cv2", OpenCVWithGStreamer)
    frame = OpenCVCameraReader(config).open().read()

    assert isinstance(frame, Frame)
    assert frame.frame_id == 1


def test_camera_retries_after_open_failure() -> None:
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
            self.released.wait(5.0)  # bounded: a missed release fails instead of hanging
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
        assert camera.read_started.wait(timeout=2.0)
        assert frame_processed.wait(timeout=1.0)
        assert camera.open_calls == 2
        status = system.camera_status()
        assert status["camera"] == "streaming"
        assert status["last_frame_age_s"] is not None
    finally:
        system.close()


# Track persistence


def test_person_evidence_survives_frames_that_mislabel_the_track() -> None:
    tracker = PersistentTracker(embedder=_VectorEmbedder())
    image = _ImageVector((1.0, 0.0))
    person = tracker.update(
        [Detection((10, 10, 40, 60), class_name="person", class_id=0)], Frame(1, 10.0, image)
    )[0]
    # An inconclusive "object" frame may take over the label; a later "kite" mislabel must not
    # strip the track's person evidence.
    tracker.update([Detection((11, 10, 41, 60), class_name="object")], Frame(2, 10.1, image))
    mislabeled = tracker.update(
        [Detection((12, 10, 42, 60), class_name="kite")], Frame(3, 10.2, image)
    )[0]

    assert mislabeled.id == person.id
    assert mislabeled.class_name == "kite"
    assert mislabeled.person_evidence
    assert mislabeled.to_dict()["person_evidence"]

    fresh_kite = tracker.update(
        [Detection((300, 10, 330, 60), class_name="kite")], Frame(4, 10.3, image)
    )[0]
    assert fresh_kite.class_name == "kite"
    assert not fresh_kite.person_evidence


def test_id_survives_short_occlusion_and_expires_after_retention() -> None:
    tracker = PersistentTracker(TrackerConfig(max_lost_seconds=2.5))
    first = tracker.update(
        [Detection((10, 10, 40, 60), class_name="person")], Frame(1, 10.0, None)
    )[0]
    tracker.update([], Frame(2, 11.0, None))
    reacquired = tracker.update(
        [Detection((10, 10, 40, 60), class_name="person")], Frame(3, 12.1, None)
    )[0]
    assert reacquired.id == first.id

    expired = tracker.update(
        [Detection((10, 10, 40, 60), class_name="person")], Frame(4, 15.0, None)
    )[0]
    assert expired.id != first.id


class _VectorEmbedder:
    def __init__(self):
        self.calls = []

    def embed(self, image, bbox):
        self.calls.append((image, bbox))
        return getattr(image, "vector", image)


class _ImageVector:
    # Frame size for crop gating.
    shape = (480, 640)

    def __init__(self, vector):
        self.vector = vector


# Tracker embedding schedule


def test_update_embeds_new_tracks_but_not_each_matched_frame() -> None:
    embedder = _VectorEmbedder()
    tracker = PersistentTracker(embedder=embedder)
    detection = Detection((10, 10, 40, 60), class_name="person")

    first = tracker.update([detection], Frame(1, 10.0, _ImageVector((1.0, 0.0))))[0]
    second = tracker.update([detection], Frame(2, 10.1, _ImageVector((0.0, 1.0))))[0]

    assert second.id == first.id
    assert len(embedder.calls) == 1
    assert tracker.reference_embeddings(first.id)[0] == (1.0, 0.0)
    assert second.embedding is None


# Tracker re-identification


# Ignore regions


@pytest.mark.skipif(
    importlib.util.find_spec("numpy") is None,
    reason="NumPy is required for ignore-region frame processing",
)
def test_drops_boxes_with_at_least_half_their_area_in_an_ignore_region() -> None:
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
    observation = feed_frame(system, np.zeros((100, 200, 3), dtype=np.uint8), 1.0)

    assert sorted(candidate.bbox for candidate in observation.candidates) == sorted(
        [(100, 0, 150, 20), (20, 30, 50, 70)]
    )
    stats = system.processing_stats()
    assert (stats["frames"], stats["detections"], stats["ignored"], stats["candidates"]) == (
        1, 4, 2, 2,
    )
    assert stats["fps"] is None


def _cos(left, right) -> float:
    return sum(a * b for a, b in zip(left, right))


def _unit_view(cos_to_primary: float, *, up: bool = True) -> tuple[float, float]:
    """A unit vector whose cosine similarity to the primary view (1, 0) is the given value."""
    cosine = max(-1.0, min(1.0, float(cos_to_primary)))
    sine = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return (cosine, sine if up else -sine)


# Tracker multi-view references: gallery for live tracks, primary-only re-acquisition.


def _tracker(**overrides) -> PersistentTracker:
    return PersistentTracker(TrackerConfig(**overrides), embedder=_VectorEmbedder())


def _frames_for(tracker: PersistentTracker, vector, *, count: int, label="person",
                box=(10, 10, 40, 60), start_frame: int = 1, start_time: float = 10.0,
                tracker_id: int | None = None):
    """Feed ``count`` frames of the same detection; return (candidate, last_frame_id)."""
    detection = Detection(box, class_name=label)
    candidate = None
    frame_id = start_frame
    timestamp = start_time
    for step in range(count):
        frame_id = start_frame + step
        timestamp = start_time + 0.05 * step
        candidates = tracker.update([detection], Frame(frame_id, timestamp, _ImageVector(vector)))
        candidate = next(item for item in candidates if tracker_id is None or item.id == tracker_id)
    return candidate, frame_id, timestamp


def test_reference_views_are_admitted_only_at_the_full_threshold_from_the_creation_view() -> None:
    tracker = _tracker()
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2)
    assert list(tracker.reference_embeddings(target.id)[1:]) == []
    # The identical view is deduped, not stored.
    assert tracker.validate_candidate(target.id) is not None
    assert list(tracker.reference_embeddings(target.id)[1:]) == []

    # A frame just below the threshold (0.70 vs 0.72) fails validation and is never admitted:
    # admission is anchored on the creation view at the full threshold.
    _frames_for(tracker, _unit_view(0.70), count=1, start_frame=10)
    assert tracker.validate_candidate(target.id) is None
    assert list(tracker.reference_embeddings(target.id)[1:]) == []

    # A strictly passing frame is admitted.
    _frames_for(tracker, _unit_view(0.80), count=1, start_frame=20)
    assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1


def test_reference_view_admission_cannot_chain_away_from_the_creation_view() -> None:
    tracker = _tracker()
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2)
    _frames_for(tracker, _unit_view(0.80), count=1, start_frame=10)
    assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1
    admitted = tracker.reference_embeddings(target.id)[1]

    # 0.95 similar to the admitted view but only 0.60 to the creation view: the gallery keeps
    # the track alive (validation passes), yet the view is NOT stored, so no drift chain forms.
    chained = _unit_view(0.60)
    assert _cos(admitted, chained) > 0.90
    _frames_for(tracker, chained, count=1, start_frame=20)
    assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1
    assert tracker.reference_embeddings(target.id)[0] == (1.0, 0.0)


def test_reference_view_admission_gates() -> None:
    tracker = _tracker()
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2)
    # _admit_reference_view is exercised directly on a track record mirroring the tracker's own
    # (class, creation view), read through the public accessors.
    current = tracker.candidate(target.id)
    track = Track(
        id=target.id,
        bbox=current.bbox,
        class_name=current.class_name,
        class_id=current.class_id,
        confidence=current.confidence,
        attributes=dict(current.attributes),
        reference_embedding=tracker.reference_embeddings(target.id)[0],
        current_embedding=None,
        arrival_monotonic=0.0,
        timestamp=current.timestamp,
        frame_id=current.frame_id,
        pose=current.pose,
    )
    good_vector = _unit_view(0.80)

    def candidate(**overrides):
        values = {
            "id": 99,
            "bbox": (10, 10, 40, 60),
            "class_name": "person",
            "confidence": 0.9,
            "frame_id": current.frame_id,
            "timestamp": current.timestamp,
        }
        values.update(overrides)
        return Candidate(**values)

    # Class gate: a differing known class is never stored.
    assert not tracker._admit_reference_view(track, candidate(class_name="kite"), good_vector)
    # Crop gate: a small crop's embedding is noise.
    assert not tracker._admit_reference_view(track, candidate(bbox=(10, 10, 20, 35)), good_vector)
    # The same good view passes every gate.
    assert tracker._admit_reference_view(track, candidate(), good_vector)
    # Dedupe: a near-identical view adds nothing.
    assert not tracker._admit_reference_view(track, candidate(id=98), good_vector)


def test_reference_views_are_capped_and_the_oldest_extra_view_is_evicted() -> None:
    tracker = _tracker(reference_views=2)  # creation view + one extra
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2)
    first = _unit_view(0.75, up=True)
    second = _unit_view(0.75, up=False)
    assert _cos(first, second) < 0.98
    for index, vector in enumerate((first, second)):
        _frames_for(tracker, vector, count=1, start_frame=10 + 10 * index)
        assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1
    assert tracker.reference_embeddings(target.id)[1] == second  # oldest extra evicted
    assert tracker.reference_embeddings(target.id)[0] == (1.0, 0.0)  # creation view never evicted


def test_live_validation_recovers_a_marginal_dip_through_a_stored_view() -> None:
    tracker = _tracker()
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2)
    _frames_for(tracker, _unit_view(0.80), count=1, start_frame=10)
    assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1

    # A marginal dip: 0.7196 against the creation view (below the 0.72 threshold) after 0.7917
    # on the previous frame; only the stored 0.80 view accepts it.
    dip = _unit_view(0.7196)
    _frames_for(tracker, dip, count=1, start_frame=20)
    assert tracker.validate_candidate(target.id) is not None
    assert tracker.validate_candidate_failure(target.id) is None


def test_pipeline_candidate_records_expose_the_reference_view_count() -> None:
    detection = Detection((10, 10, 40, 60), class_name="person")
    system = PerceptionSystem(FixedDetector(detection), PersistentTracker(embedder=_VectorEmbedder()))
    candidate = feed_frame(system, _ImageVector((1.0, 0.0)), 10.0).candidates[0]
    feed_frame(system, _ImageVector((1.0, 0.0)), 10.1)
    assert system.tracker.validate_candidate(candidate.id) is not None
    assert system.list_candidates()[0]["reference_views"] == 1  # dedupe: nothing added

    feed_frame(system, _ImageVector((0.8, 0.6)), 10.2)
    assert system.tracker.validate_candidate(candidate.id) is not None
    assert system.list_candidates()[0]["reference_views"] == 2


def test_one_reference_view_stores_no_extra_views_and_keeps_the_creation_embedding() -> None:
    tracker = _tracker(reference_views=1)
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2)
    _frames_for(tracker, _unit_view(0.80), count=1, start_frame=10)
    assert tracker.validate_candidate(target.id) is not None
    assert list(tracker.reference_embeddings(target.id)[1:]) == []  # nothing stored
    assert tracker.reference_embeddings(target.id)[0] == (1.0, 0.0)


# Tracker validation


def test_validation_failure_reason_distinguishes_missing_stale_and_mismatch() -> None:
    tracker = PersistentTracker(embedder=_VectorEmbedder())
    detection = Detection((10, 10, 40, 60), class_name="person")
    created = tracker.update([detection], Frame(1, 10.0, _ImageVector((1.0, 0.0))))[0]

    assert tracker.validate_candidate_failure(999) == "not_visible"
    # A single-hit track is in the current frame but not fresh enough to validate.
    assert tracker.validate_candidate_failure(created.id) == "stale"

    tracker.update([detection], Frame(2, 10.1, _ImageVector((1.0, 0.0))))
    assert tracker.validate_candidate(created.id) is not None
    assert tracker.validate_candidate_failure(created.id) is None

    tracker.update([detection], Frame(3, 10.2, _ImageVector((0.0, 1.0))))
    assert tracker.validate_candidate_failure(created.id) == "appearance_mismatch"
    assert tracker.validate_candidate(created.id) is None


def test_validation_rejects_current_embedding_mismatch() -> None:
    detection = Detection((10, 10, 40, 60), class_name="person")
    system = PerceptionSystem(
        FixedDetector(detection),
        PersistentTracker(embedder=_VectorEmbedder()),
    )
    first = feed_frame(system, _ImageVector((1.0, 0.0)), 10.0).candidates[0]
    assert system.tracker.validate_candidate(first.id) is None
    feed_frame(system, _ImageVector((1.0, 0.0)), 10.1)
    assert system.tracker.validate_candidate(first.id) is not None
    feed_frame(system, _ImageVector((0.0, 1.0)), 10.2)
    assert system.tracker.validate_candidate(first.id) is None


def test_validation_encodes_selected_candidate_once_per_frame() -> None:
    embedder = _VectorEmbedder()
    tracker = PersistentTracker(embedder=embedder)
    detection = Detection((10, 10, 40, 60), class_name="person")
    first = tracker.update([detection], Frame(1, 10.0, _ImageVector((1.0, 0.0))))[0]
    current = tracker.update([detection], Frame(2, 10.1, _ImageVector((1.0, 0.0))))[0]

    assert len(embedder.calls) == 1
    validated = tracker.validate_candidate(current.id)
    repeated = tracker.validate_candidate(current.id)

    assert validated is not None
    assert repeated is not None
    assert validated.embedding == (1.0, 0.0)
    assert repeated.embedding == validated.embedding
    assert first.id == current.id
    assert len(embedder.calls) == 2


# Tracker seed evidence. A grounder seed is not person evidence until the detector says so.


def test_grounder_seed_gains_person_evidence_only_from_a_detector_label() -> None:
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

    assert not seeded.person_evidence
    assert seeded.class_name == "person"
    assert seeded.attributes["grounder_span"] == "person person person"

    tracker.update(
        [Detection((60, 40, 100, 90), class_name="person", class_id=0, confidence=0.7)],
        Frame(2, 10.1, image),
    )

    assert tracker.candidate(seeded.id).person_evidence
    assert tracker.candidate(seeded.id).attributes["grounder_span"] == "person person person"


def test_loosely_overlapping_detection_does_not_refresh_a_seeded_track() -> None:
    # A grounder seed boxed on one person (seed box [277, 422, 322, 478]) must not take over
    # the detection of a different person ([275, 372, 311, 442], IoU 0.156): a seeded track
    # only accepts a detection whose box agrees strongly.
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
    other_person = Detection(
        (275, 372, 311, 442),
        class_name="person",
        class_id=0,
        confidence=0.7,
    )

    candidates = ()
    for step in range(4):
        candidates = tracker.update([other_person], Frame(2 + step, 10.1 + 0.1 * step, image))

    seed_candidate = next(item for item in candidates if item.id == seeded.id)

    # The detector person would have granted person evidence had it merged.
    assert tracker.candidate(seeded.id).person_evidence is False
    assert seed_candidate.attributes["grounder_span"] == "person"
    # The seed keeps tracking its own box (CSRT drift of a few pixels), not the other
    # person's box 50 px above it.
    assert all(
        abs(seed_candidate.bbox[index] - expected) <= 5.0
        for index, expected in enumerate((277.0, 422.0, 322.0, 478.0))
    )


def test_strongly_overlapping_detection_still_refreshes_a_seeded_track() -> None:
    # The legitimate path: the detector really tracks the seeded person, so the person
    # evidence it carries still reaches the seed.
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
    )

    candidates = ()
    for step in range(4):
        candidates = tracker.update([same_person], Frame(2 + step, 10.1 + 0.1 * step, image))

    assert any(item.id == seeded.id for item in candidates)
    assert tracker.candidate(seeded.id).person_evidence is True


def test_grounder_seed_does_not_gain_evidence_from_its_own_label() -> None:
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
        tracker.update([], Frame(frame_id, 10.0 + 0.1 * frame_id, image))

    assert not tracker.candidate(seeded.id).person_evidence


# Set-of-marks rendering


@pytest.mark.skipif(
    importlib.util.find_spec("cv2") is None or importlib.util.find_spec("numpy") is None,
    reason="OpenCV and NumPy are required for JPEG rendering",
)
def test_payload_contains_scaled_numbered_candidate_table_and_jpeg() -> None:
    import numpy as np

    candidate = Candidate(
        id=7,
        bbox=(100, 80, 500, 400),
        class_name="person",
        confidence=0.91,
        frame_id=12,
        timestamp=44.5,
        pose={"x": 1.0},
        attributes={"grounder_span": "person"},
    )
    frame = Frame(
        frame_id=12,
        timestamp=44.5,
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        pose={"x": 1.0},
        candidates=(candidate,),
    )
    payload = render_set_of_marks(frame)
    assert max(payload.width, payload.height) <= 640
    assert payload.candidates[0]["id"] == 7
    assert payload.candidates[0]["mark"] == 7
    assert payload.image_jpeg.startswith(b"\xff\xd8")

    assert payload.frame_id == 12
    record = candidate.to_dict()
    assert record["id"] == 7
    assert record["bbox"] == [100, 80, 500, 400]
    assert record["class_name"] == "person"
    assert "box" not in record
    assert "class" not in record


def test_any_80_class_yolo_export_reports_coco_labels_whatever_its_file_name():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from horus.perception.detector import DetectorConfig, YOLODetector

    class _Session:
        def run(self, _names, _feeds):
            rows = np.zeros((1, 84, 2), dtype=np.float32)
            rows[0, :4, 0] = (320, 320, 100, 200)  # one box in the centre of the 640 input
            rows[0, 4 + 0, 0] = 0.9  # class 0 = person
            return [rows]

    detector = YOLODetector(DetectorConfig(model_name="models/yolov8s.onnx"))
    detector._model, detector._input_name = _Session(), "images"

    [detection] = detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))

    assert (detection.class_name, detection.class_id) == ("person", 0)


def test_camera_yaw_between_frames_keeps_a_narrow_box_on_its_track():
    from horus.perception.tracker import PersistentTracker, TrackerConfig

    np = pytest.importorskip("numpy")
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    focal = 320 / math.tan(math.radians(114.59155902616465) / 2)
    person = lambda centre: Detection((centre - 15, 240, centre + 15, 380), class_name="person", class_id=0)

    class NoAppearance:
        # Geometry alone: appearance recovery (a lost box that looks the same) cannot step in.
        def embed(self, image, bbox):
            return None

    def run(hfov):
        tracker = PersistentTracker(TrackerConfig(camera_horizontal_fov_deg=hfov), embedder=NoAppearance())
        ids = []
        heading, centre = 350.0, 320.0
        for frame_id in range(1, 7):
            # The vehicle yaws 8 deg right per frame; a static person drifts left in the image.
            angle = math.atan((centre - 320) / focal) - math.radians(8.0 if frame_id > 1 else 0.0)
            centre = 320 + focal * math.tan(angle)
            heading = (heading + (8.0 if frame_id > 1 else 0.0)) % 360.0
            pose = {"heading_deg": heading, "heading_age_s": 0.05}
            [candidate] = tracker.update([person(centre)], Frame(frame_id, 10.0 + 0.2 * frame_id, image, pose=pose))
            ids.append(candidate.id)
        return ids

    assert len(set(run(None))) > 1  # uncompensated: the 28 px jump per frame breaks IoU association
    assert run(114.59155902616465) == [1] * 6


class _SceneByBox:
    """A 1280x720 frame whose embedder answers per box: one appearance vector per person."""

    shape = (720, 1280)

    def __init__(self, people):
        self.people = people  # [(box, vector)]


class _ByBoxEmbedder:
    def embed(self, image, bbox):
        return next((vector for box, vector in image.people if tuple(box) == tuple(bbox)), None)


def test_an_image_jump_during_an_approach_keeps_the_target_id_without_merging_a_bystander():
    # An approach that begins with a lurch can make a small person's box come back under a new
    # ID every frame. Here the box grows 1.5x/s and slides down at 6 fps, one frame is
    # missed, and an image jump the yaw compensation does not see (vehicle pitch/translation)
    # moves every box by 30 px right and 12 px down, more than IoU association tolerates.
    target_look, bystander_look, stranger_look = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    tracker = PersistentTracker(TrackerConfig(), embedder=_ByBoxEmbedder())
    target_ids, bystander_ids = [], []
    for frame in range(18):
        t = frame / 6.0
        jump_x, jump_y = (30.0, 12.0) if frame >= 7 else (0.0, 0.0)
        height = 90.0 * 1.5**t
        bottom = 429.0 + 40.0 * t + jump_y
        centre = 530.0 + jump_x
        target = (centre - 0.2 * height, bottom - height, centre + 0.2 * height, bottom)
        bystander = (612.0 + jump_x, 345.0 + jump_y, 646.0 + jump_x, 431.0 + jump_y)
        people = [(bystander, bystander_look)]
        detections = [Detection(bystander, class_name="person")]
        if frame != 6:  # the frame before the jump is missed
            people.append((target, target_look))
            detections.append(Detection(target, class_name="person"))
        candidates = tracker.update(detections, Frame(frame + 1, 100.0 + t, _SceneByBox(people)))
        ids = {tuple(candidate.bbox): candidate.id for candidate in candidates}
        bystander_ids.append(ids[tuple(bystander)])
        if frame != 6:
            target_ids.append(ids[tuple(target)])

    assert set(target_ids) == {target_ids[0]}
    assert set(bystander_ids) == {bystander_ids[0]} != {target_ids[0]}

    # Someone else appearing next to where the target's box would be (outside IoU association,
    # inside the recovery gate) is still a new person: appearance decides.
    last_target = next(box for box, look in people if look == target_look)
    stranger = (last_target[0] + 100.0, last_target[1], last_target[2] + 100.0, last_target[3])
    candidates = tracker.update(
        [Detection(stranger, class_name="person")],
        Frame(30, 100.0 + 18 / 6.0 + 0.3, _SceneByBox([(stranger, stranger_look)])),
    )
    assert candidates[0].id not in {target_ids[0], bystander_ids[0]}
