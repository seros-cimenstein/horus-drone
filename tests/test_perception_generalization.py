from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from horus.perception import (
    Detection,
    Frame,
    GroundingBox,
    PerceptionSystem,
    PersistentTracker,
)

from tests.fakes import FixedDetector, feed_frame

cv2 = pytest.importorskip("cv2")


class _EmptyDetector:
    def detect(self, image):
        return []


class _RecordingEventBus:
    def __init__(self):
        self.events = []

    def publish(self, kind, summary, **data):
        self.events.append({"kind": kind, "summary": summary, "data": data})
        return len(self.events)


class _OpenGrounder:
    def __init__(self, boxes):
        self.boxes = boxes
        self.called = threading.Event()

    def detect(self, image, prompts, *, box_threshold=0.30, text_threshold=0.25):
        assert prompts == ["red mug"]
        self.called.set()
        return self.boxes


class _FailingGrounder:
    def __init__(self):
        self.called = threading.Event()

    def detect(self, image, prompts, *, box_threshold=0.30, text_threshold=0.25):
        self.called.set()
        raise RuntimeError("service offline")


class _PromptRecordingGrounder:
    """Returns fixed boxes for any vocabulary (used by the person-pass label tests)."""

    def __init__(self, boxes):
        self.boxes = boxes
        self.called = threading.Event()

    def detect(self, image, prompts, *, box_threshold=0.30, text_threshold=0.25):
        self.called.set()
        return self.boxes


def _moving_square(x: int) -> np.ndarray:
    image = np.full((480, 640, 3), 25, dtype=np.uint8)
    cv2.rectangle(image, (x, 170), (x + 59, 229), (235, 235, 235), -1)
    cv2.rectangle(image, (x + 14, 184), (x + 44, 214), (10, 10, 10), -1)
    cv2.line(image, (x + 2, 198), (x + 56, 198), (110, 110, 110), 3)
    return image


def _wait_for(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_seeded_csrt_track_persists_without_detector_detections():
    tracker = PersistentTracker()
    first = Frame(1, 10.0, _moving_square(100))
    seeded = tracker.seed(
        (100, 170, 160, 230),
        first,
        label="utility cart",
        confidence=0.91,
        source="open_vocab",
    )

    latest = (seeded,)
    for frame_id, x in enumerate(range(106, 137, 6), start=2):
        latest = tracker.update([], Frame(frame_id, 10.0 + frame_id / 10, _moving_square(x)))

    assert len(latest) == 1
    assert latest[0].id == seeded.id
    assert latest[0].source == "open_vocab"
    assert latest[0].class_name == "utility cart"
    assert latest[0].bbox[0] > seeded.bbox[0] + 8
    assert seeded.embedding is not None
    assert tracker.validate_candidate(seeded.id, max_age_seconds=5.0) is not None


def test_point_seeds_new_tracker_id_for_untracked_current_box():
    system = PerceptionSystem(_EmptyDetector(), PersistentTracker())
    observation = feed_frame(system, _moving_square(100), 20.0)

    result = system.point(observation.frame_id, box=[100, 170, 160, 230])

    assert result.candidate.id == 1
    assert result.candidate.source == "point"
    assert result.candidate.class_name == "point target"
    assert system.list_candidates()[0]["source"] == "point"


def test_open_vocabulary_seeds_new_boxes_and_deduplicates_detector_tracks():
    detector_box = (300, 170, 360, 230)
    grounder = _OpenGrounder(
        [
            GroundingBox((100, 170, 160, 230), 0.88, "red mug"),
            GroundingBox(detector_box, 0.95, "red mug"),
        ]
    )
    event_bus = _RecordingEventBus()
    system = PerceptionSystem(
        FixedDetector(Detection(detector_box, class_name="cup", class_id=41)),
        PersistentTracker(),
        grounder=grounder,
        open_vocab_hz=20.0,
        event_bus=event_bus,
    )
    image = _moving_square(100)
    cv2.rectangle(image, (300, 170), (359, 229), (235, 235, 235), -1)
    cv2.rectangle(image, (314, 184), (344, 214), (10, 10, 10), -1)
    try:
        feed_frame(system, image, 30.0)
        system.set_vocabulary([" red mug ", "red mug"])
        assert grounder.called.wait(2.0)
        assert _wait_for(
            lambda: (
                any(row["source"] == "open_vocab" for row in system.list_candidates())
                and any(event["kind"] == "object_detected" for event in event_bus.events)
            )
        )

        candidates = system.list_candidates()
        assert len(candidates) == 2
        assert {candidate["source"] for candidate in candidates} == {"detector", "open_vocab"}
        assert [event["kind"] for event in event_bus.events].count("vocabulary_changed") == 1
        detected = [event for event in event_bus.events if event["kind"] == "object_detected"]
        assert len(detected) == 1
        assert detected[0]["data"]["label"] == "red mug"
        assert detected[0]["data"]["score"] == 0.88
    finally:
        system.close()


def test_person_prompt_maps_a_repeated_span_onto_the_vocabulary_term():
    # The grounder returns the matched text span; a repeat ("person person person") is normalised
    # to the configured term, and the raw span stays visible on the candidate row.
    grounder = _PromptRecordingGrounder([GroundingBox((100, 170, 160, 230), 0.44, "person person person")])
    system = PerceptionSystem(
        _EmptyDetector(),
        PersistentTracker(),
        grounder=grounder,
        open_vocab_hz=20.0,
    )
    try:
        feed_frame(system, _moving_square(100), 50.0)
        system.set_vocabulary(["person", "person lying on the ground"])
        assert grounder.called.wait(2.0)
        assert _wait_for(lambda: any(row["source"] == "open_vocab" for row in system.list_candidates()))

        seeded = [row for row in system.list_candidates() if row["source"] == "open_vocab"]

        assert [row["class_name"] for row in seeded] == ["person"]
        assert seeded[0]["attributes"]["grounder_span"] == "person person person"
        # The row carries the provenance and no person evidence: the executor must not range it
        # from the person height without target_size_m.
        assert seeded[0]["source"] == "open_vocab"
        assert seeded[0]["person_evidence"] is False
    finally:
        system.close()


def test_person_prompt_maps_a_fragment_span_onto_its_vocabulary_term():
    # "ground" is a fragment of the lying phrase: it names that term, never a bare person label.
    grounder = _PromptRecordingGrounder([GroundingBox((100, 170, 160, 230), 0.44, "ground ground")])
    system = PerceptionSystem(
        _EmptyDetector(),
        PersistentTracker(),
        grounder=grounder,
        open_vocab_hz=20.0,
    )
    try:
        feed_frame(system, _moving_square(100), 55.0)
        system.set_vocabulary(["person", "person lying on the ground"])
        assert grounder.called.wait(2.0)
        assert _wait_for(lambda: any(row["source"] == "open_vocab" for row in system.list_candidates()))

        seeded = [row for row in system.list_candidates() if row["source"] == "open_vocab"]

        assert [row["class_name"] for row in seeded] == ["person lying on the ground"]
    finally:
        system.close()


class _SwitchableScoreGrounder:
    """Returns one person box per call: weak until ``strong`` is set, then above the label floor."""

    def __init__(self):
        self.calls = 0
        self.strong = threading.Event()

    def detect(self, image, prompts, *, box_threshold=0.30, text_threshold=0.25):
        self.calls += 1
        return [GroundingBox((100, 170, 160, 230), 0.52 if self.strong.is_set() else 0.32, "person person")]


def test_weak_grounder_hit_is_a_generic_object_until_a_later_cycle_scores_it_as_its_term():
    grounder = _SwitchableScoreGrounder()
    event_bus = _RecordingEventBus()
    system = PerceptionSystem(
        _EmptyDetector(), PersistentTracker(), grounder=grounder, open_vocab_hz=20.0, event_bus=event_bus,
    )
    try:
        feed_frame(system, _moving_square(100), 60.0)
        system.set_vocabulary(["person", "person lying on the ground"])
        assert _wait_for(lambda: grounder.calls >= 1 and any(
            row["source"] == "open_vocab" for row in system.list_candidates()))
        weak = [row for row in system.list_candidates() if row["source"] == "open_vocab"]
        assert [row["class_name"] for row in weak] == ["object"]
        assert weak[0]["attributes"]["grounder_span"] == "person person"
        assert not [event for event in event_bus.events if event["kind"] == "object_detected"]

        # Keep frames arriving so the seed survives until a strong cycle confirms it.
        grounder.strong.set()
        for step in range(1, 60):
            feed_frame(system, _moving_square(100), 60.0 + step * 0.05)
            if any(event["kind"] == "object_detected" for event in event_bus.events):
                break
            time.sleep(0.05)
        detected = [event for event in event_bus.events if event["kind"] == "object_detected"]
        assert len(detected) == 1
        assert detected[0]["data"]["label"] == "person"
        assert detected[0]["data"]["id"] == weak[0]["id"]
        assert [row["class_name"] for row in system.list_candidates() if row["source"] == "open_vocab"] == ["person"]
    finally:
        system.close()


def test_object_prompt_keeps_its_own_label_next_to_a_person_prompt():
    grounder = _PromptRecordingGrounder([GroundingBox((100, 170, 160, 230), 0.51, "red mug")])
    system = PerceptionSystem(
        _EmptyDetector(),
        PersistentTracker(),
        grounder=grounder,
        open_vocab_hz=20.0,
    )
    try:
        feed_frame(system, _moving_square(100), 60.0)
        system.set_vocabulary(["person", "red mug"])
        assert grounder.called.wait(2.0)
        assert _wait_for(lambda: any(row["source"] == "open_vocab" for row in system.list_candidates()))

        seeded = [row for row in system.list_candidates() if row["source"] == "open_vocab"]

        assert [row["class_name"] for row in seeded] == ["red mug"]
    finally:
        system.close()


def test_open_vocabulary_seed_never_gains_a_shirt_colour_without_a_detector_person():
    # _with_shirt_colour classifies only detector person detections. A grounder seed must not
    # acquire a colour attribute: the executor reads shirt_colour as person evidence, so a colour
    # on an open-vocabulary-only track would bypass the person-evidence gate.
    grounder = _PromptRecordingGrounder([GroundingBox((100, 170, 160, 230), 0.44, "person")])
    system = PerceptionSystem(
        _EmptyDetector(),
        PersistentTracker(),
        grounder=grounder,
        open_vocab_hz=20.0,
    )
    try:
        for step in range(4):
            feed_frame(system, _moving_square(100), 80.0 + 0.2 * step)
        system.set_vocabulary(["person"])
        assert grounder.called.wait(2.0)
        assert _wait_for(lambda: any(row["source"] == "open_vocab" for row in system.list_candidates()))

        seeded = [row for row in system.list_candidates() if row["source"] == "open_vocab"]

        assert seeded
        assert all("shirt_colour" not in row["attributes"] for row in seeded)
        assert all(row["person_evidence"] is False for row in seeded)
    finally:
        system.close()


def test_person_vocabulary_without_a_grounder_stays_detector_only():
    # Outdoor profiles carry a prone-pass vocabulary; with grounder_url unset the pass must be
    # inert (no worker, no error) rather than a startup failure.
    system = PerceptionSystem(
        FixedDetector(Detection((100, 170, 160, 230), class_name="person")),
        PersistentTracker(),
        open_vocab_hz=20.0,
        vocabulary=["person", "person lying on the ground"],
    )
    try:
        observations = [feed_frame(system, _moving_square(100 + index), 70.0 + index) for index in range(2)]
        assert [item.frame_id for item in observations] == [1, 2]
        assert system.open_vocab.thread is None
        assert {row["source"] for row in system.list_candidates()} == {"detector"}
    finally:
        system.close()


def test_grounder_failure_does_not_stop_frame_processing():
    grounder = _FailingGrounder()
    system = PerceptionSystem(
        _EmptyDetector(),
        PersistentTracker(),
        grounder=grounder,
        open_vocab_hz=20.0,
    )
    try:
        system.set_vocabulary(["red mug"])
        observations = [
            feed_frame(system, _moving_square(100 + index), 40.0 + index / 10)
            for index in range(3)
        ]
        assert grounder.called.wait(2.0)
        assert [item.frame_id for item in observations] == [1, 2, 3]
        status = system.processing_stats()["grounder"]
        assert status["status"] == "unavailable"
        assert "service offline" in status["reason"]
        assert status["last_latency_ms"] is not None
    finally:
        system.close()


def test_point_on_an_old_frame_catches_up_while_the_camera_keeps_delivering(monkeypatch):
    import horus.perception.grounding as grounding

    system = PerceptionSystem(_EmptyDetector(), PersistentTracker())
    old = feed_frame(system, _moving_square(100), 20.0)
    for step in range(1, 4):
        feed_frame(system, _moving_square(100 + 4 * step), 20.0 + step / 5)
    arrivals = iter([112, 116, 120])

    class CameraTicksDuringCatchUp(grounding.BoxCompensator):
        def advance(self, frames):
            box = super().advance(frames)
            x = next(arrivals, None)  # a new frame lands after every catch-up round
            if x is not None:
                feed_frame(system, _moving_square(x), 21.0 + x / 100)
            return box

    monkeypatch.setattr(grounding, "BoxCompensator", CameraTicksDuringCatchUp)

    result = system.point(old.frame_id, box=[100, 170, 160, 230])

    newest = system.latest_observation()
    assert result.frame_id == newest.frame_id
    assert result.candidate.source == "point"
    assert result.candidate.bbox[0] > 110  # carried along with the moving square


def test_large_box_around_a_small_person_seeds_instead_of_claiming_that_person():
    person = Detection((300, 300, 330, 380), confidence=0.9, class_name="person", class_id=0)
    system = PerceptionSystem(FixedDetector(person), PersistentTracker())
    image = _moving_square(100)
    observation = feed_frame(system, image, 30.0)
    person_id = observation.candidates[0].id

    result = system.point(observation.frame_id, box=[250, 150, 450, 400], compensate=False)

    assert result.candidate.id != person_id
    assert result.candidate.source == "point"


def test_open_vocabulary_seed_survives_while_redetected_and_drops_when_the_grounder_stops_seeing_it():
    class SwitchableGrounder:
        def __init__(self):
            self.boxes = [GroundingBox((100, 170, 160, 230), 0.8, "red mug")]
            self.calls = 0

        def detect(self, image, prompts, *, box_threshold=0.30, text_threshold=0.25):
            self.calls += 1
            return list(self.boxes)

    grounder = SwitchableGrounder()
    system = PerceptionSystem(_EmptyDetector(), PersistentTracker(), grounder=grounder, open_vocab_hz=50.0)
    try:
        feed_frame(system, _moving_square(100), 40.0)
        system.set_vocabulary(["red mug"])
        assert _wait_for(lambda: grounder.calls >= 6)
        seeded = [row for row in system.list_candidates() if row["source"] == "open_vocab"]
        assert len(seeded) == 1  # re-detections confirm the same ID instead of stacking duplicates

        grounder.boxes = []
        calls = grounder.calls
        assert _wait_for(lambda: grounder.calls >= calls + 4)
        assert not any(row["source"] == "open_vocab" for row in system.list_candidates())
    finally:
        system.close()
