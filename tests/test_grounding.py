from __future__ import annotations

import threading
import time

import pytest

from horus.perception import Detection, PerceptionSystem, PersistentTracker
from horus.perception.frame_buffer import FrameBuffer
from horus.perception.geometry import iou
from horus.perception.grounding import GroundingPointer
from horus.perception.types import (
    Candidate,
    Frame,
    GroundingBox,
    NoGroundingMatch,
    StaleFrameError,
)

from tests.fakes import feed_frame


def _candidate(
    candidate_id: int,
    bbox: tuple[float, float, float, float],
    *,
    frame_id: int = 1,
    timestamp: float = 1.0,
) -> Candidate:
    return Candidate(
        id=candidate_id,
        bbox=bbox,
        class_name="person",
        confidence=0.9,
        frame_id=frame_id,
        timestamp=timestamp,
    )


class _GeometryOnlyTracker:
    def __init__(self, current: Candidate):
        self.current = current
        self.encode_calls = 0

    def validate_candidate(self, candidate_id: int):
        return self.current if candidate_id == self.current.id else None

    def encode(self, image, bbox):
        self.encode_calls += 1
        return None


class _DescriptionGrounder:
    def detect(self, image, prompts, *, box_threshold, text_threshold):
        assert prompts == ["blue-shirt person"]
        return [GroundingBox((0.0, 0.0, 10.0, 10.0), 0.95, prompts[0])]


def _buffer_with_reidentified_target() -> tuple[FrameBuffer, _GeometryOnlyTracker]:
    old = _candidate(1, (0.0, 0.0, 10.0, 10.0), frame_id=1, timestamp=1.0)
    middle = _candidate(3, (5.0, 0.0, 15.0, 10.0), frame_id=2, timestamp=2.0)
    current = _candidate(3, (10.0, 0.0, 20.0, 10.0), frame_id=3, timestamp=3.0)
    frames = FrameBuffer(max_seconds=10, max_frames=10)
    for frame_id, timestamp, candidates in (
        (1, 1.0, (old,)),
        (2, 2.0, (middle,)),
        (3, 3.0, (current,)),
    ):
        frames.append(Frame(frame_id, timestamp, image=None, candidates=candidates))
    return frames, _GeometryOnlyTracker(current)


def test_box_compensation_replays_multiple_buffered_frames_only_when_enabled():
    frames, tracker = _buffer_with_reidentified_target()
    pointer = GroundingPointer(frames, tracker, state_lock=threading.RLock())

    with pytest.raises(NoGroundingMatch, match="compensation disabled"):
        pointer.point(1, [0, 0, 10, 10], compensate=False)
    assert tracker.encode_calls == 0

    result = pointer.point(1, box=[0, 0, 10, 10], compensate=True)
    assert result.frame_id == 3
    assert result.candidate.id == 3
    assert tracker.encode_calls == 1


def test_description_grounds_and_revalidates():
    frames, tracker = _buffer_with_reidentified_target()
    pointer = GroundingPointer(frames, tracker, _DescriptionGrounder(), state_lock=threading.RLock())

    result = pointer.point(1, "blue-shirt person")

    assert result.candidate.id == 3
    assert result.grounded_box.label == "blue-shirt person"


def test_box_rejects_malformed_and_expired_references_clearly():
    frames, tracker = _buffer_with_reidentified_target()
    pointer = GroundingPointer(frames, tracker, state_lock=threading.RLock())

    with pytest.raises(ValueError, match="finite"):
        pointer.point(1, box=[0, 0, float("nan"), 10])

    expired = FrameBuffer(max_seconds=10, max_frames=2)
    for frame in frames.snapshot()[1:]:
        expired.append(frame)
    with pytest.raises(StaleFrameError, match="frame 1 is no longer"):
        GroundingPointer(expired, tracker, state_lock=threading.RLock()).point(1, box=[0, 0, 10, 10])


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
