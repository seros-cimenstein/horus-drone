"""Deterministic perception checks that do not load external model weights."""

from __future__ import annotations

import importlib.util
import json
import logging
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
    # Frame size for crop gating; two-dimensional so no shirt colour is classified from it.
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


def test_lost_reference_embedding_rematches_fresh_candidate_under_new_id() -> None:
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
        Frame(1, 10.0, _ImageVector((1.0, 0.0))),
    )[0]
    tracker.update([], Frame(2, 11.0, _ImageVector((1.0, 0.0))))
    replacement_detection = Detection(
        (300, 10, 330, 60),
        class_name="person",
        attributes={"shirt_colour": "blue"},
    )
    tracker.update([replacement_detection], Frame(3, 11.1, _ImageVector((0.0, 1.0))))
    replacement = tracker.update(
        [replacement_detection], Frame(4, 11.2, _ImageVector((1.0, 0.0)))
    )[0]

    match = tracker.reidentify_candidate(reference.id)
    repeated_match = tracker.reidentify_candidate(reference.id)

    assert replacement.id != reference.id
    assert match is not None
    assert match[0].id == replacement.id
    assert match[1] >= tracker.config.revalidation_similarity_threshold
    assert tracker.reference_embeddings(replacement.id)[0] == (0.0, 1.0)
    assert match[0].embedding == (1.0, 0.0)
    assert repeated_match is not None
    assert repeated_match[0].id == replacement.id
    assert len(embedder.calls) == 3


def test_inconclusive_frame_does_not_let_a_lookalike_steal_a_lost_track() -> None:
    # Identical embeddings (same mesh): only the remembered shirt colour separates the people.
    tracker = PersistentTracker(embedder=_VectorEmbedder())
    image = _ImageVector((1.0, 0.0))
    target = tracker.update(
        [Detection((10, 10, 40, 60), class_name="person", attributes={"shirt_colour": "blue"})],
        Frame(1, 10.0, image),
    )[0]
    tracker.update(
        [Detection((12, 10, 42, 60), class_name="person", attributes={})],  # side view: no colour
        Frame(2, 10.1, image),
    )
    red = Detection((400, 10, 430, 60), class_name="person", attributes={"shirt_colour": "red"})
    tracker.update([red], Frame(3, 10.2, image))
    tracker.update([red], Frame(4, 10.3, image))

    assert tracker.reidentify_candidate(target.id) is None


def test_target_hidden_longer_than_track_retention_is_still_reidentified() -> None:
    tracker = PersistentTracker(embedder=_VectorEmbedder())
    image = _ImageVector((1.0, 0.0))
    blue = {"shirt_colour": "blue"}
    target = tracker.update(
        [Detection((10, 10, 40, 60), class_name="person", attributes=blue)], Frame(1, 10.0, image)
    )[0]
    # Hidden for longer than max_lost_seconds: the track expires while nothing is detected.
    tracker.update([], Frame(2, 10.0 + tracker.config.max_lost_seconds + 1.0, image))
    reappeared = Detection((300, 10, 330, 60), class_name="person", attributes=blue)
    now = 10.0 + tracker.config.max_lost_seconds + 1.5
    tracker.update([reappeared], Frame(3, now, image))
    candidates = tracker.update([reappeared], Frame(4, now + 0.1, image))
    # The re-acquisition confirmation needs a second distinct frame; the first call is pending.
    assert tracker.reidentify_candidate(target.id) is None
    candidates = tracker.update([reappeared], Frame(5, now + 0.2, image))

    match = tracker.reidentify_candidate(target.id)

    assert match is not None
    assert match[0].id == candidates[0].id
    assert match[0].id != target.id


def test_reidentification_logs_color_excluded_candidates(caplog) -> None:
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
        Frame(1, 10.0, _ImageVector((1.0, 0.0))),
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
    tracker.update([blue, red], Frame(2, 10.1, _ImageVector((0.0, 1.0))))
    candidates = tracker.update([blue, red], Frame(3, 10.2, _ImageVector((1.0, 0.0))))

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="horus.perception.tracker"):
        pending = tracker.reidentify_candidate(reference.id)
        tracker.update([blue, red], Frame(4, 10.3, _ImageVector((1.0, 0.0))))
        match = tracker.reidentify_candidate(reference.id)

    assert pending is None  # first passing frame confirms nothing on its own

    assert match is not None
    assert match[0].id == candidates[0].id
    # Every compared candidate is logged, the colour-excluded red one included.
    records = [
        record for record in caplog.records
        if record.name == "horus.perception.tracker"
        and getattr(record, "event", None) == "perception_reid_similarity"
    ]
    assert {record.shirt_colour for record in records} == {"blue", "red"}
    assert {record.candidate_id for record in records} == {item.id for item in candidates}


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


# Tracker multi-view references. R1 multi-view references: gallery for live tracks, primary-only
# re-acquisition.


def _tracker(**overrides) -> PersistentTracker:
    return PersistentTracker(TrackerConfig(**overrides), embedder=_VectorEmbedder())


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
        candidates = tracker.update([detection], Frame(frame_id, timestamp, _ImageVector(vector)))
        candidate = next(item for item in candidates if tracker_id is None or item.id == tracker_id)
    return candidate, frame_id, timestamp


def test_reference_views_are_admitted_only_at_the_full_threshold_from_the_creation_view() -> None:
    tracker = _tracker()
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
    assert list(tracker.reference_embeddings(target.id)[1:]) == []
    # An accepted validation starts the grace window (the identical view is deduped, not stored).
    assert tracker.validate_candidate(target.id) is not None
    assert list(tracker.reference_embeddings(target.id)[1:]) == []

    # A frame inside the grace margin (0.70 vs the 0.72 threshold) still validates, but it is
    # never admitted: admission is anchored on the creation view at the full threshold.
    _frames_for(tracker, _unit_view(0.70), count=1, colour="blue", start_frame=10)
    assert tracker.validate_candidate(target.id) is not None
    assert list(tracker.reference_embeddings(target.id)[1:]) == []

    # A strictly passing frame is admitted.
    _frames_for(tracker, _unit_view(0.80), count=1, colour="blue", start_frame=20)
    assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1


def test_reference_view_admission_cannot_chain_away_from_the_creation_view() -> None:
    tracker = _tracker()
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
    _frames_for(tracker, _unit_view(0.80), count=1, colour="blue", start_frame=10)
    assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1
    admitted = tracker.reference_embeddings(target.id)[1]

    # 0.95 similar to the admitted view but only 0.60 to the creation view: the gallery keeps
    # the track alive (validation passes), yet the view is NOT stored, so no drift chain forms.
    chained = _unit_view(0.60)
    assert _cos(admitted, chained) > 0.90
    _frames_for(tracker, chained, count=1, colour="blue", start_frame=20)
    assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1
    assert tracker.reference_embeddings(target.id)[0] == (1.0, 0.0)


def test_reference_view_admission_gates() -> None:
    # These gates act on a settled colour; the vote has its own tests.
    tracker = _tracker(shirt_colour_window=1, shirt_colour_min_votes=1)
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="green")
    # _admit_reference_view is exercised directly on a track record mirroring the tracker's own
    # (class, settled colour, creation view), read through the public accessors.
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
            "attributes": {"shirt_colour": "green"},
        }
        values.update(overrides)
        return Candidate(**values)

    # Class gate: a differing known class is never stored.
    assert not tracker._admit_reference_view(track, candidate(class_name="kite"), good_vector)
    # Shirt-colour gate: a differing known colour is never stored.
    assert not tracker._admit_reference_view(
        track, candidate(attributes={"shirt_colour": "red"}), good_vector
    )
    # Colour gate is defensive: an inconclusive colour cannot vouch for the view either when
    # the track has a known one.
    assert not tracker._admit_reference_view(track, candidate(attributes={}), good_vector)
    # Crop gate: a small crop's embedding is noise (10-25 px slivers in the campaign logs).
    assert not tracker._admit_reference_view(track, candidate(bbox=(10, 10, 20, 35)), good_vector)
    # The same good view passes every gate.
    assert tracker._admit_reference_view(track, candidate(), good_vector)
    # Dedupe: a near-identical view adds nothing.
    assert not tracker._admit_reference_view(track, candidate(id=98), good_vector)


def test_reference_views_are_capped_and_the_oldest_extra_view_is_evicted() -> None:
    tracker = _tracker(reference_views=2)  # creation view + one extra
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
    first = _unit_view(0.75, up=True)
    second = _unit_view(0.75, up=False)
    assert _cos(first, second) < 0.98
    for index, vector in enumerate((first, second)):
        _frames_for(tracker, vector, count=1, colour="blue", start_frame=10 + 10 * index)
        assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1
    assert tracker.reference_embeddings(target.id)[1] == second  # oldest extra evicted
    assert tracker.reference_embeddings(target.id)[0] == (1.0, 0.0)  # creation view never evicted


def test_live_validation_recovers_a_marginal_dip_through_a_stored_view(caplog) -> None:
    tracker = _tracker()
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
    _frames_for(tracker, _unit_view(0.80), count=1, colour="blue", start_frame=10)
    assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1

    # A marginal dip: 0.7196 against the creation view after 0.7917 on the previous frame.
    dip = _unit_view(0.7196)
    _frames_for(tracker, dip, count=1, colour="blue", start_frame=20)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="horus.perception.tracker"):
        validated = tracker.validate_candidate(target.id)
    assert validated is not None
    assert tracker.validate_candidate_failure(target.id) is None
    records = [json.loads(record.getMessage()) for record in caplog.records
               if record.name == "horus.perception.tracker"
               and getattr(record, "event", None) == "perception_reid_similarity"]
    assert records[-1]["mode"] == "selected_validation"
    assert records[-1]["primary_similarity"] == pytest.approx(0.7196, abs=5e-5)
    assert records[-1]["matched_view"] == 1
    assert records[-1]["reference_view_count"] == 2


def test_reacquisition_refuses_a_same_colour_lookalike_the_gallery_could_match() -> None:
    tracker = _tracker(reacquisition_confirm_s=0.0)
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="green")
    stored = _unit_view(0.80)
    _frames_for(tracker, stored, count=1, colour="green", start_frame=10)
    assert tracker.validate_candidate(target.id) is not None
    assert len(tracker.reference_embeddings(target.id)[1:]) == 1

    # A look-alike with the same class and colour, a person-sized crop, 0.7004 against the
    # creation view but 0.99 against the stored view - the campaign's 147/261 band.
    lookalike = _unit_view(0.7004)
    assert _cos(lookalike, stored) > 0.90
    _frames_for(tracker, lookalike, count=2, colour="green", box=(100, 100, 155, 181),
                start_frame=20)

    assert tracker.reidentify_candidate(target.id) is None


def test_reacquisition_refuses_a_different_colour_high_similarity_candidate() -> None:
    # These gates act on a settled colour; the vote has its own tests.
    one_vote = {"shirt_colour_window": 1, "shirt_colour_min_votes": 1}
    tracker = _tracker(reacquisition_confirm_s=0.0, **one_vote)
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="green")
    # 1592-style: 0.8767 similarity but a red shirt against the green reference, elsewhere in
    # the frame so it stays its own track.
    vector = _unit_view(0.8767)
    _frames_for(tracker, vector, count=2, colour="red", box=(300, 10, 330, 60),
                start_frame=20)
    assert tracker.reidentify_candidate(target.id) is None

    # Control: the same appearance with the matching colour is re-acquired.
    control = _tracker(reacquisition_confirm_s=0.0, **one_vote)
    control_target, _, _ = _frames_for(control, _unit_view(1.0), count=2, colour="green")
    _frames_for(control, vector, count=2, colour="green", box=(300, 10, 330, 60),
                start_frame=20)
    match = control.reidentify_candidate(control_target.id)
    assert match is not None
    assert match[1] >= control.config.revalidation_similarity_threshold


def test_reacquisition_needs_two_confirmed_frames_of_the_same_candidate(monkeypatch) -> None:
    clock = [100.0]
    tracker = _tracker()
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
    returning = _unit_view(0.95)
    with monkeypatch.context() as patched:
        patched.setattr("horus.perception.tracker.time.monotonic", lambda: clock[0])
        _frames_for(tracker, returning, count=2, colour="blue", box=(300, 10, 330, 60),
                    start_frame=20, start_time=clock[0])

        assert tracker.reidentify_candidate(target.id) is None  # first frame: pending
        assert tracker.reidentify_candidate(target.id) is None  # same frame: still pending

        clock[0] += 0.2
        _frames_for(tracker, returning, count=1, colour="blue", box=(300, 10, 330, 60),
                    start_frame=30, start_time=clock[0])
        match = tracker.reidentify_candidate(target.id)
        assert match is not None
        assert match[1] == pytest.approx(0.95, abs=1e-3)


def test_reacquisition_confirmation_expires_with_the_window(monkeypatch) -> None:
    clock = [100.0]
    tracker = _tracker(reacquisition_confirm_s=0.5)
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
    returning = _unit_view(0.95)
    with monkeypatch.context() as patched:
        patched.setattr("horus.perception.tracker.time.monotonic", lambda: clock[0])
        _frames_for(tracker, returning, count=2, colour="blue", box=(300, 10, 330, 60),
                    start_frame=20, start_time=clock[0])
        assert tracker.reidentify_candidate(target.id) is None

        clock[0] += 1.0  # past the confirmation window
        _frames_for(tracker, returning, count=1, colour="blue", box=(300, 10, 330, 60),
                    start_frame=30, start_time=clock[0])
        assert tracker.reidentify_candidate(target.id) is None

        clock[0] += 0.2
        _frames_for(tracker, returning, count=1, colour="blue", box=(300, 10, 330, 60),
                    start_frame=40, start_time=clock[0])
        assert tracker.reidentify_candidate(target.id) is not None


def test_pipeline_candidate_records_expose_the_reference_view_count() -> None:
    detection = Detection((10, 10, 40, 60), class_name="person", attributes={"shirt_colour": "blue"})
    system = PerceptionSystem(FixedDetector(detection), PersistentTracker(embedder=_VectorEmbedder()))
    candidate = feed_frame(system, _ImageVector((1.0, 0.0)), 10.0).candidates[0]
    feed_frame(system, _ImageVector((1.0, 0.0)), 10.1)
    assert system.validate_candidate(candidate.id) is not None
    assert system.list_candidates()[0]["reference_views"] == 1  # dedupe: nothing added

    feed_frame(system, _ImageVector((0.8, 0.6)), 10.2)
    assert system.validate_candidate(candidate.id) is not None
    assert system.list_candidates()[0]["reference_views"] == 2


def test_one_reference_view_stores_no_extra_views_and_keeps_the_creation_embedding() -> None:
    tracker = _tracker(reference_views=1)
    target, _, _ = _frames_for(tracker, _unit_view(1.0), count=2, colour="blue")
    _frames_for(tracker, _unit_view(0.80), count=1, colour="blue", start_frame=10)
    assert tracker.validate_candidate(target.id) is not None
    assert list(tracker.reference_embeddings(target.id)[1:]) == []  # nothing stored
    assert tracker.reference_embeddings(target.id)[0] == (1.0, 0.0)


# Tracker validation


def test_revalidation_grace_keeps_a_track_through_a_marginal_dip(monkeypatch) -> None:
    clock = [100.0]
    tracker = PersistentTracker(
        TrackerConfig(revalidation_grace_s=1.0), embedder=_VectorEmbedder()
    )
    detection = Detection((10, 10, 40, 60), class_name="person")
    # cos((1, 0), (0.70, 0.714)) = 0.70: inside the default 0.05 grace margin below the 0.72
    # threshold, like the campaign's correct-target dip 0.7917 -> 0.7196.
    marginal = _ImageVector((0.70, 0.714))
    with monkeypatch.context() as patched:
        patched.setattr("horus.perception.tracker.time.monotonic", lambda: clock[0])
        tracker.update([detection], Frame(1, clock[0], _ImageVector((1.0, 0.0))))
        clock[0] += 0.05
        current = tracker.update([detection], Frame(2, clock[0], _ImageVector((1.0, 0.0))))[0]
        assert tracker.validate_candidate(current.id) is not None

        clock[0] += 0.10
        dip = tracker.update([detection], Frame(3, clock[0], marginal))[0]
        assert dip.id == current.id
        assert tracker.validate_candidate(dip.id) is not None
        assert tracker.validate_candidate_failure(dip.id) is None

        # A total appearance change (an identity swap) is never forgiven by the margin.
        clock[0] += 0.10
        swapped = tracker.update([detection], Frame(4, clock[0], _ImageVector((0.0, 1.0))))[0]
        assert tracker.validate_candidate(swapped.id) is None
        assert tracker.validate_candidate_failure(swapped.id) == "appearance_mismatch"

        # Even a marginal dip expires once the grace window has passed.
        clock[0] += 2.0
        late = tracker.update([detection], Frame(5, clock[0], marginal))[0]
        assert tracker.validate_candidate(late.id) is None
        assert tracker.validate_candidate_failure(late.id) == "appearance_mismatch"


def test_reid_similarity_logging_is_one_summary_per_second(monkeypatch, caplog) -> None:
    clock = [100.0]
    tracker = PersistentTracker(embedder=_VectorEmbedder())
    detection = Detection((10, 10, 40, 60), class_name="person")
    with monkeypatch.context() as patched:
        patched.setattr("horus.perception.tracker.time.monotonic", lambda: clock[0])
        tracker.update([detection], Frame(1, clock[0], _ImageVector((1.0, 0.0))))
        clock[0] += 0.05
        current = tracker.update([detection], Frame(2, clock[0], _ImageVector((1.0, 0.0))))[0]
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="horus.perception.tracker"):
            for step in range(6):
                # Two validation ticks per frame, like the executor's control loop.
                assert tracker.validate_candidate(current.id) is not None
                assert tracker.validate_candidate(current.id) is not None
                clock[0] += 0.3
                tracker.update([detection], Frame(10 + step, clock[0], _ImageVector((1.0, 0.0))))

    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "horus.perception.tracker"
    ]
    summaries = [record for record in records if record["event"] == "perception_reid_summary"]
    per_comparison = [
        record for record in records if record["event"] == "perception_reid_similarity"
    ]
    # 12 comparisons (well over a second of ticks), one INFO window summary; every
    # per-comparison record stays at DEBUG so a hold cannot flood the log.
    assert len(summaries) == 1
    assert summaries[0]["mode"] == "selected_validation"
    assert summaries[0]["comparisons"] == summaries[0]["total_comparisons"]
    assert summaries[0]["comparisons"] >= 6
    assert summaries[0]["own_track_seen"]
    assert summaries[0]["best_candidate_id"] == current.id
    assert summaries[0]["best_similarity"] == pytest.approx(1.0)
    assert len(per_comparison) == 12


def test_reid_summary_windows_are_capped_per_reference() -> None:
    from horus.perception.reid_log import ReidSimilarityLog

    log = ReidSimilarityLog(logging.getLogger("test.reid_cap"), max_references=3)
    for reference_id in range(1, 7):
        for mode in ("selected_validation", "reidentification"):
            log.record(
                mode=mode,
                similarity=0.5,
                reference_id=reference_id,
                candidate=Candidate(reference_id, (0, 0, 10, 10), "person", 0.9, 1, 1.0),
                threshold=0.72,
                crop_px=(10, 10),
            )
        # Touching an older reference keeps it among the most recently compared ones.
        log.record(
            mode="selected_validation",
            similarity=0.5,
            reference_id=1,
            candidate=Candidate(1, (0, 0, 10, 10), "person", 0.9, 1, 1.0),
            threshold=0.72,
            crop_px=(10, 10),
        )
    assert sorted(log._windows) == [1, 5, 6]
    assert set(log._windows[6]) == {"selected_validation", "reidentification"}


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
    assert system.validate_candidate(first.id) is None
    feed_frame(system, _ImageVector((1.0, 0.0)), 10.1)
    assert system.validate_candidate(first.id) is not None
    feed_frame(system, _ImageVector((0.0, 1.0)), 10.2)
    assert system.validate_candidate(first.id) is None


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


def test_loosely_overlapping_detection_does_not_colour_a_seeded_track() -> None:
    # A grounder seed boxed on a crimson hiker (seed box [277, 422, 322, 478]) must not take
    # the colour of a detector track on a different, green-shirted person
    # ([275, 372, 311, 442], IoU 0.156): a seeded track only accepts a detection whose box
    # agrees strongly.
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
        candidates = tracker.update([green_person], Frame(2 + step, 10.1 + 0.1 * step, image))

    seed_candidate = next(item for item in candidates if item.id == seeded.id)

    assert "shirt_colour" not in seed_candidate.attributes
    assert seed_candidate.attributes["grounder_span"] == "person"
    # The seed keeps tracking its own box (CSRT drift of a few pixels), not the other
    # person's box 50 px above it.
    assert all(
        abs(seed_candidate.bbox[index] - expected) <= 5.0
        for index, expected in enumerate((277.0, 422.0, 322.0, 478.0))
    )


def test_strongly_overlapping_detection_still_colours_a_seeded_track() -> None:
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
        candidates = tracker.update([same_person], Frame(2 + step, 10.1 + 0.1 * step, image))

    seed_candidate = next(item for item in candidates if item.id == seeded.id)

    assert seed_candidate.attributes["shirt_colour"] == "green"
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


# Track shirt-colour vote. The published colour is a bounded majority vote, not the latest
# classification.


def _feed(tracker: PersistentTracker, colours, *, start_frame: int = 1):
    candidate = None
    for step, colour in enumerate(colours):
        detection = Detection(
            (10, 10, 40, 60),
            class_name="person",
            attributes={} if colour is None else {"shirt_colour": colour},
        )
        candidate = tracker.update(
            [detection], Frame(start_frame + step, 10.0 + 0.05 * step, _ImageVector((1.0, 0.0)))
        )[0]
    return candidate


def test_one_misread_frame_does_not_label_a_track() -> None:
    tracker = _tracker()

    first = _feed(tracker, ["red"])

    assert "shirt_colour" not in first.attributes

    settled = _feed(tracker, ["blue", "blue", "blue"], start_frame=2)

    assert settled.attributes["shirt_colour"] == "blue"
    assert settled.attributes["shirt_colour_confidence"] == pytest.approx(3 / 4)


def test_colour_flip_needs_a_majority_of_the_window() -> None:
    tracker = _tracker()

    _feed(tracker, ["red", "red", "red"])
    still_red = _feed(tracker, ["blue", "blue"], start_frame=4)

    # red,red,red,blue,blue: red still holds the majority of the window.
    assert still_red.attributes["shirt_colour"] == "red"

    flipped = _feed(tracker, ["blue"], start_frame=6)

    # The window slid to red,red,blue,blue,blue: the flip now has its majority.
    assert flipped.attributes["shirt_colour"] == "blue"


def test_tied_votes_publish_nothing() -> None:
    tracker = _tracker(shirt_colour_window=4, shirt_colour_min_votes=2)

    tied = _feed(tracker, ["red", "red", "blue", "blue"])

    assert "shirt_colour" not in tied.attributes


def test_inconclusive_frames_neither_vote_nor_erase_a_settled_colour() -> None:
    tracker = _tracker()

    _feed(tracker, ["green", "green", "green"])
    after_none = _feed(tracker, [None, None], start_frame=4)

    assert after_none.attributes["shirt_colour"] == "green"


def test_vote_config_requires_a_positive_window_and_reachable_majority() -> None:
    from horus.config import PerceptionSettings

    for window, minimum in ((0, 1), (5, 0), (True, 1), (5, 6)):
        with pytest.raises(ValueError):
            PerceptionSettings(shirt_colour_window=window, shirt_colour_min_votes=minimum)


# Shirt-colour attributes

_needs_numpy_for_shirt_colour = pytest.mark.skipif(
    importlib.util.find_spec("numpy") is None,
    reason="NumPy is required for shirt-color attribute extraction",
)


@_needs_numpy_for_shirt_colour
def test_person_candidates_receive_local_torso_colour() -> None:
    import numpy as np

    image = np.zeros((100, 100, 3), dtype=np.uint8)
    image[20:60, 20:80] = (220, 45, 10)
    system = PerceptionSystem(
        FixedDetector(Detection((0, 0, 100, 100), class_name="person")),
        PersistentTracker(),
    )

    candidate = None
    for step in range(3):  # the published colour is a majority vote over recent frames
        candidate = feed_frame(system, image, 10.0 + 0.1 * step).candidates[0]

    assert candidate.attributes["shirt_colour"] == "blue"
    assert candidate.attributes["shirt_colour_confidence"] >= 0.55


@_needs_numpy_for_shirt_colour
def test_distinguishes_static_red_and_green_shirts() -> None:
    import numpy as np

    from horus.perception.attributes import classify_shirt_colour

    for pixel, expected in (
        ((220, 45, 10), "blue"),
        ((20, 40, 220), "red"),
        ((30, 180, 30), "green"),
    ):
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        image[20:60, 20:80] = pixel
        assert classify_shirt_colour(image, (0, 0, 100, 100))[0] == expected


@_needs_numpy_for_shirt_colour
def test_supported_garment_hues_report_their_family() -> None:
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
        assert classify_shirt_colour(image, (0, 0, 100, 100))[0] == expected, (
            f"{pixel} should read {expected}"
        )


@_needs_numpy_for_shirt_colour
def test_achromatic_and_off_hue_fabrics_report_no_colour() -> None:
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
        assert classify_shirt_colour(image, (0, 0, 100, 100)) is None, (
            f"{pixel} must stay indeterminate"
        )


@_needs_numpy_for_shirt_colour
def test_warm_lit_neutral_fabric_does_not_read_as_red() -> None:
    import numpy as np

    from horus.perception.attributes import classify_shirt_colour

    # Warm-lit khaki, sand, grey and hi-vis grey torsos (e.g. heather:white, solid:khaki,
    # hivis:grey+reflective_silver) are achromatic garments: no colour, never "red".
    for bgr in ((110, 160, 190), (105, 145, 190), (150, 160, 170), (140, 143, 148),
                (206, 204, 200)):
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        image[20:60, 20:80] = bgr
        assert classify_shirt_colour(image, (0, 0, 100, 100)) is None, (
            f"warm neutral {bgr} must stay indeterminate"
        )
        # A few strongly saturated warm pixels inside an otherwise neutral torso are
        # not enough either: skin, hair and background keep that share below the floor.
        image[25:35, 25:55] = (22, 108, 240)  # orange patch, 300 px of the 1750 px band
        assert classify_shirt_colour(image, (0, 0, 100, 100)) is None
    # An orange torso covering the band is still a red family garment.
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    image[20:60, 20:80] = (22, 108, 240)
    assert classify_shirt_colour(image, (0, 0, 100, 100))[0] == "red"


@_needs_numpy_for_shirt_colour
def test_light_grey_garment_gets_no_shirt_colour_attribute() -> None:
    import numpy as np

    system = PerceptionSystem(
        FixedDetector(Detection((0, 0, 100, 100), class_name="person")),
        PersistentTracker(),
    )

    warm_grey = np.zeros((100, 100, 3), dtype=np.uint8)
    warm_grey[20:60, 20:80] = (150, 160, 170)
    # The tracker keeps an accepted attribute for the life of a track, so a neutral
    # garment must stay unlabelled on every frame, not just the first.
    for step in range(3):
        grey_candidate = feed_frame(system, warm_grey, 10.0 + 0.1 * step).candidates[0]
        assert "shirt_colour" not in grey_candidate.attributes

    scarlet = np.zeros((100, 100, 3), dtype=np.uint8)
    scarlet[20:60, 20:80] = (36, 28, 200)
    for step in range(3):
        red_candidate = feed_frame(system, scarlet, 10.3 + 0.1 * step).candidates[0]

    assert red_candidate.attributes["shirt_colour"] == "red"


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

    def run(hfov):
        tracker = PersistentTracker(TrackerConfig(camera_horizontal_fov_deg=hfov))
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
