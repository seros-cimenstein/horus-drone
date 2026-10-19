from __future__ import annotations

import threading

import pytest

from horus.perception.frame_buffer import FrameBuffer
from horus.perception.grounding import GroundingPointer
from horus.perception.types import (
    Candidate,
    Frame,
    GroundingBox,
    NoGroundingMatch,
    StaleFrameError,
)


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
        attributes={"shirt_colour": "blue"},
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
