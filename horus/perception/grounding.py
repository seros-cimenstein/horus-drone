"""Latency frame buffering, external text grounding, and box revalidation."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import math
from threading import RLock
from typing import Any, Protocol, Sequence

from .tracker import BoxCompensator, PersistentTracker, box_iou, cosine_similarity
from .types import (
    BBox,
    Candidate,
    Frame,
    GroundingBox,
    NoGroundingMatch,
    PointResult,
    StaleFrameError,
)


class Grounder(Protocol):
    """Client contract for the external open-vocabulary detection service."""

    def detect(
        self,
        image: Any,
        prompts: Sequence[str],
        *,
        box_threshold: float = 0.30,
        text_threshold: float = 0.25,
    ) -> Sequence[GroundingBox]: ...


@dataclass(frozen=True, slots=True)
class GrounderConfig:
    """Score gates shared with the external grounder service."""

    box_threshold: float = 0.30
    text_threshold: float = 0.25

    def __post_init__(self) -> None:
        if not 0.0 <= self.box_threshold <= 1.0 or not 0.0 <= self.text_threshold <= 1.0:
            raise ValueError("grounding thresholds must be in [0, 1]")


class FrameBuffer:
    """Thread-safe time- and count-bounded ring of frames and their tracked candidates."""

    def __init__(self, *, max_seconds: float = 3.0, max_frames: int = 90) -> None:
        if max_seconds <= 0 or max_frames < 1:
            raise ValueError("max_seconds and max_frames must be positive")
        self.max_seconds = float(max_seconds)
        self.max_frames = int(max_frames)
        self._frames: list[Frame] = []
        self._lock = RLock()

    def append(self, frame: Frame) -> None:
        """Retain a processed frame; IDs and timestamps must advance monotonically."""
        with self._lock:
            if self._frames:
                latest = self._frames[-1]
                if frame.frame_id <= latest.frame_id:
                    raise ValueError("frame_id must increase monotonically")
                if frame.timestamp < latest.timestamp:
                    raise ValueError("frame timestamps must not go backwards")
            self._frames.append(frame)
            cutoff = frame.timestamp - self.max_seconds
            while self._frames and (
                len(self._frames) > self.max_frames or self._frames[0].timestamp < cutoff
            ):
                self._frames.pop(0)

    def get(self, frame_id: int) -> Frame:
        """Get one retained frame or raise ``StaleFrameError`` when it has expired."""
        with self._lock:
            for frame in self._frames:
                if frame.frame_id == frame_id:
                    return frame
        raise StaleFrameError(f"frame {frame_id} is no longer in the perception buffer")

    def snapshot(self) -> tuple[Frame, ...]:
        """Return a stable chronological view of retained frame objects."""
        with self._lock:
            return tuple(self._frames)

    def latest(self) -> Frame | None:
        """Return the newest retained frame, if any."""
        with self._lock:
            return self._frames[-1] if self._frames else None

    def frames_after(self, frame_id: int) -> tuple[Frame, ...]:
        """Return the requested frame and all later frames in chronological order."""
        frames = self.snapshot()
        for index, frame in enumerate(frames):
            if frame.frame_id == frame_id:
                return frames[index:]
        raise StaleFrameError(f"frame {frame_id} is no longer in the perception buffer")

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)




class GroundingPointer:
    """Resolve a buffered description or box to a current, revalidated track ID."""

    def __init__(
        self,
        buffer: FrameBuffer,
        tracker: PersistentTracker,
        grounder: Grounder | None = None,
        *,
        grounder_config: GrounderConfig | None = None,
        min_grounding_confidence: float = 0.2,
        reid_threshold: float = 0.72,
        overlap_threshold: float = 0.5,
        state_lock: Any = None,
    ) -> None:
        if not 0.0 <= min_grounding_confidence <= 1.0:
            raise ValueError("min_grounding_confidence must be in [0, 1]")
        if not 0.0 <= reid_threshold <= 1.0 or not 0.0 <= overlap_threshold <= 1.0:
            raise ValueError("matching thresholds must be in [0, 1]")
        self.buffer = buffer
        self.tracker = tracker
        self._grounder = grounder
        self.grounder_config = grounder_config or GrounderConfig()
        self.min_grounding_confidence = min_grounding_confidence
        self.reid_threshold = reid_threshold
        self.overlap_threshold = overlap_threshold
        self._state_lock = state_lock

    def point(
        self,
        frame_id: int,
        description: str | Sequence[float] | None = None,
        *,
        box: Sequence[float] | None = None,
        compensate: bool = True,
    ) -> PointResult:
        """Resolve a description or box, revalidate an existing ID, or seed a new one."""
        frames = self.buffer.frames_after(frame_id)
        if not isinstance(compensate, bool):
            raise ValueError("compensate must be a boolean")
        if box is not None and description is not None:
            raise ValueError("provide either description or box, not both")
        reference_box = box
        description_text: str | None = None
        if isinstance(description, str):
            description_text = description
        elif description is not None:
            reference_box = description
        old_frame = frames[0]
        if reference_box is None:
            if description_text is None or not description_text.strip():
                raise ValueError("description must not be empty")
            grounder = self._grounder
            if grounder is None:
                raise RuntimeError("grounder service is unavailable")
            detect = getattr(grounder, "detect", None)
            if callable(detect):
                raw_boxes = detect(
                    old_frame.image,
                    [description_text],
                    box_threshold=self.grounder_config.box_threshold,
                    text_threshold=self.grounder_config.text_threshold,
                )
            else:
                # Simple injected adapters used by callers/tests may expose a single-prompt API.
                ground = getattr(grounder, "ground", None)
                if not callable(ground):
                    raise RuntimeError("grounder client does not provide detect()")
                raw_boxes = ground(old_frame.image, description_text)
            grounded_boxes = [
                coerce_grounding_box(item)
                for item in raw_boxes
            ]
            grounded_boxes = [
                item for item in grounded_boxes
                if item.confidence >= self.min_grounding_confidence
            ]
            if not grounded_boxes:
                raise NoGroundingMatch(
                    f"grounder found no confident box for {description_text!r}"
                )
            grounded = max(grounded_boxes, key=lambda item: item.confidence)
            grounded = GroundingBox(
                bbox=_validate_reference_box(grounded.bbox, old_frame.image),
                confidence=grounded.confidence,
                label=grounded.label or description_text,
            )
        else:
            grounded = GroundingBox(
                bbox=_validate_reference_box(reference_box, old_frame.image),
                confidence=1.0,
                label="box",
            )

        latest_frame = frames[-1]
        latest_candidates = latest_frame.candidates
        old_match = _best_overlapping(grounded.bbox, old_frame.candidates, self.overlap_threshold)
        if old_match is not None:
            old_score = box_overlap_score(grounded.bbox, old_match.bbox)
            result = self._current_track_result(
                old_match.id,
                latest_candidates,
                grounded,
                match_confidence=old_score,
            )
            if result is not None:
                return result
        if not compensate and len(frames) > 1:
            raise NoGroundingMatch(
                f"old-frame reference {grounded.bbox!r} has no matching current track "
                f"on frame {latest_frame.frame_id} with compensation disabled"
            )

        reference = self.tracker.encode(old_frame.image, grounded.bbox)
        if compensate and reference is not None:
            reidentified = self._fast_forward_reid(reference, latest_frame)
            if reidentified is not None:
                candidate, score = reidentified
                result = self._current_track_result(
                    candidate.id, latest_candidates, grounded, match_confidence=score
                )
                if result is not None:
                    return result

        geometric = self._fast_forward_geometry(grounded.bbox, frames)
        if geometric is not None:
            track_id, score = geometric
            result = self._current_track_result(
                track_id, latest_candidates, grounded, match_confidence=score
            )
            if result is not None:
                return result

        lock = self._state_lock if self._state_lock is not None else nullcontext()

        def commit(placed_box: BBox | None, current_frame: Frame | None) -> PointResult:
            if current_frame is None:
                raise NoGroundingMatch("no current frame is available")
            if not compensate and current_frame.frame_id != old_frame.frame_id:
                raise NoGroundingMatch(
                    f"old-frame reference {grounded.bbox!r} has no matching current track "
                    f"on frame {current_frame.frame_id} with compensation disabled"
                )
            if placed_box is None:
                target = repr(description_text) if description_text is not None else repr(grounded.bbox)
                raise NoGroundingMatch(
                    f"grounded target {target} could not be compensated to frame {current_frame.frame_id}"
                )
            current_match = _best_overlapping(placed_box, current_frame.candidates, self.overlap_threshold)
            if current_match is not None:
                result = self._current_track_result(
                    current_match.id,
                    current_frame.candidates,
                    grounded,
                    match_confidence=box_overlap_score(placed_box, current_match.bbox),
                )
                if result is not None:
                    return result
                raise NoGroundingMatch(
                    f"grounded target overlaps track {current_match.id}, but it is not revalidated"
                )
            label = grounded.label or description_text or "point target"
            if grounded.label == "box":
                label = description_text or "point target"
            candidate = self.tracker.seed(
                placed_box,
                current_frame,
                label=label,
                confidence=grounded.confidence,
                source="point",
            )
            current_frame.candidates = self.tracker.current_candidates
            return PointResult(
                current_frame.frame_id,
                candidate,
                grounded,
                box_overlap_score(placed_box, grounded.bbox),
            )

        if not compensate:
            with lock:
                return commit(grounded.bbox, self.buffer.latest())
        try:
            compensator = BoxCompensator(grounded.bbox, old_frame)
        except (RuntimeError, ValueError):
            with lock:
                return commit(None, self.buffer.latest())
        return commit_on_latest(self.buffer, compensator, lock, commit)

    def _fast_forward_reid(
        self,
        reference: Sequence[float],
        latest_frame: Frame,
    ) -> tuple[Candidate, float] | None:
        """Pick the latest-frame candidate whose current appearance best matches the reference.

        Tracks are embedded only when created, so current appearance comes from the tracker's
        shared lazy per-frame cache: at most one encode per latest candidate, none for history.
        """
        lock = self._state_lock if self._state_lock is not None else nullcontext()
        best: tuple[Candidate, float] | None = None
        for candidate in latest_frame.candidates:
            with lock:
                embedding = self.tracker.frame_embedding(candidate, latest_frame.image)
            similarity = cosine_similarity(reference, embedding)
            if similarity is not None and similarity >= self.reid_threshold:
                if best is None or similarity > best[1]:
                    best = (candidate, similarity)
        return best

    def _fast_forward_geometry(
        self, grounded_box: BBox, frames: Sequence[Frame]
    ) -> tuple[int, float] | None:
        previous_box = grounded_box
        track_id: int | None = None
        confidence = 0.0
        for frame in frames[1:]:
            candidate = _best_overlapping(previous_box, frame.candidates, self.overlap_threshold)
            if candidate is None:
                continue
            track_id = candidate.id
            confidence = max(confidence, box_overlap_score(previous_box, candidate.bbox))
            previous_box = candidate.bbox
        if track_id is None:
            candidate = _best_overlapping(grounded_box, frames[0].candidates, self.overlap_threshold)
            if candidate is not None:
                track_id = candidate.id
                confidence = box_overlap_score(grounded_box, candidate.bbox)
        return (track_id, confidence) if track_id is not None else None

    def _current_track_result(
        self,
        track_id: int,
        latest_candidates: Sequence[Candidate],
        grounded: GroundingBox,
        *,
        match_confidence: float,
    ) -> PointResult | None:
        candidate = next((item for item in latest_candidates if item.id == track_id), None)
        if candidate is None:
            return None
        lock = self._state_lock if self._state_lock is not None else nullcontext()
        with lock:
            validated = self.tracker.validate_candidate(track_id)
        if validated is None:
            return None
        return PointResult(validated.frame_id, validated, grounded, match_confidence)


def _validate_reference_box(value: Sequence[float], image: Any = None) -> BBox:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 4:
        raise ValueError("box must contain exactly four finite numeric coordinates")
    if any(isinstance(item, (str, bytes, bool)) for item in value):
        raise ValueError("box must contain exactly four finite numeric coordinates")
    try:
        coordinates = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("box must contain exactly four finite numeric coordinates") from exc
    if not all(math.isfinite(item) for item in coordinates):
        raise ValueError("box coordinates must be finite")
    x1, y1, x2, y2 = coordinates
    if x1 >= x2 or y1 >= y2:
        raise ValueError("box coordinates must satisfy x1 < x2 and y1 < y2")

    shape = getattr(image, "shape", None)
    if shape is not None and len(shape) >= 2:
        height, width = float(shape[0]), float(shape[1])
    else:
        size = getattr(image, "size", None)
        if isinstance(size, Sequence) and len(size) == 2:
            width, height = float(size[0]), float(size[1])
        else:
            width = height = None
    if width is not None and height is not None and (
        x1 < 0 or y1 < 0 or x2 > width or y2 > height
    ):
        raise ValueError(
            f"box must lie within referenced frame bounds [0,{width:g}]x[0,{height:g}]"
        )
    return coordinates  # type: ignore[return-value]


def commit_on_latest(
    buffer: FrameBuffer,
    compensator: BoxCompensator,
    lock: Any,
    commit: Any,
    *,
    rounds: int = 4,
) -> Any:
    """Carry a compensated box up to the newest frame and call ``commit(box, frame)`` under lock.

    Most catch-up happens outside the perception lock; frames cannot be appended while it is
    held, so the final step commits on the true latest frame instead of racing the camera.
    """
    for _ in range(rounds):
        compensator.advance(buffer.frames_after(compensator.frame_id)[1:])
        with lock:
            latest = buffer.latest()
            if latest is not None and latest.frame_id == compensator.frame_id:
                return commit(compensator.box, latest)
    with lock:
        compensator.advance(buffer.frames_after(compensator.frame_id)[1:])
        return commit(compensator.box, buffer.latest())


def coerce_grounding_box(item: GroundingBox | dict[str, Any]) -> GroundingBox:
    if isinstance(item, GroundingBox):
        return item
    return GroundingBox(
        bbox=tuple(float(value) for value in item["bbox"]),
        confidence=float(item.get("confidence", item.get("score", 1.0))),
        label=str(item.get("label", "")),
    )


def _best_overlapping(
    box: BBox, candidates: Sequence[Candidate], threshold: float
) -> Candidate | None:
    best: tuple[Candidate, float] | None = None
    for candidate in candidates:
        score = box_overlap_score(box, candidate.bbox)
        if best is None or score > best[1]:
            best = (candidate, score)
    return best[0] if best is not None and best[1] >= threshold else None


def box_overlap_score(reference: BBox, candidate: BBox) -> float:
    """IoU, or the share of the reference box that lies inside the candidate if larger.

    Asymmetric on purpose: a torso box inside a person track matches that person, but a large
    cabinet box that merely contains a small person box does not claim the person's ID.
    """
    intersection = _intersection_area(reference, candidate)
    if intersection <= 0:
        return 0.0
    reference_area = _area(reference)
    covered = intersection / reference_area if reference_area > 0 else 0.0
    return max(box_iou(reference, candidate), covered)


def _intersection_area(left: BBox, right: BBox) -> float:
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _area(box: BBox) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


