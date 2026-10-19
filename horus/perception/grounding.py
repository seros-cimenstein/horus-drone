"""External text grounding and old-frame box resolution to current, revalidated track IDs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from .csrt import BoxCompensator, commit_on_latest
from .embedders import cosine_similarity
from .frame_buffer import FrameBuffer
from .geometry import overlap_score, validate_box
from .tracker import PersistentTracker
from .types import (
    BBox,
    Candidate,
    Frame,
    GroundingBox,
    NoGroundingMatch,
    PointResult,
)

# Grounder score gates; the grounder service and client default to these.
DEFAULT_BOX_THRESHOLD = 0.30
DEFAULT_TEXT_THRESHOLD = 0.25


class Grounder(Protocol):
    """Client contract for the external open-vocabulary detection service."""

    def detect(
        self,
        image: Any,
        prompts: Sequence[str],
        *,
        box_threshold: float = DEFAULT_BOX_THRESHOLD,
        text_threshold: float = DEFAULT_TEXT_THRESHOLD,
    ) -> Sequence[GroundingBox]: ...


@dataclass(frozen=True, slots=True)
class GrounderConfig:
    """Score gates shared with the external grounder service."""

    box_threshold: float = DEFAULT_BOX_THRESHOLD
    text_threshold: float = DEFAULT_TEXT_THRESHOLD

    def __post_init__(self) -> None:
        if not 0.0 <= self.box_threshold <= 1.0 or not 0.0 <= self.text_threshold <= 1.0:
            raise ValueError("grounding thresholds must be in [0, 1]")


class GroundingPointer:
    """Resolve a buffered description or box to a current, revalidated track ID.

    Appearance re-identification of the old-frame reference uses the tracker's
    ``revalidation_similarity_threshold``.
    """

    def __init__(
        self,
        buffer: FrameBuffer,
        tracker: PersistentTracker,
        grounder: Grounder | None = None,
        *,
        grounder_config: GrounderConfig | None = None,
        min_grounding_confidence: float = 0.2,
        overlap_threshold: float = 0.5,
        state_lock: Any,
    ) -> None:
        if not 0.0 <= min_grounding_confidence <= 1.0:
            raise ValueError("min_grounding_confidence must be in [0, 1]")
        if not 0.0 <= overlap_threshold <= 1.0:
            raise ValueError("overlap_threshold must be in [0, 1]")
        self.buffer = buffer
        self.tracker = tracker
        self._grounder = grounder
        self.grounder_config = grounder_config or GrounderConfig()
        self.min_grounding_confidence = min_grounding_confidence
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
        description_text = description if isinstance(description, str) else None
        if box is None and description is not None and description_text is None:
            box = description
        old_frame = frames[0]
        if box is None:
            grounded = self._ground_description(description_text, old_frame)
        else:
            grounded = GroundingBox(bbox=validate_box(box, old_frame.image), confidence=1.0, label="box")
        result = self._resolve_existing_track(grounded, frames, compensate)
        if result is not None:
            return result

        def commit(placed_box: BBox | None, current_frame: Frame | None) -> PointResult:
            return self._seed_point(
                placed_box, current_frame, grounded, description_text, old_frame, compensate
            )

        if not compensate:
            with self._state_lock:
                return commit(grounded.bbox, self.buffer.latest())
        try:
            compensator = BoxCompensator(grounded.bbox, old_frame)
        except (RuntimeError, ValueError):
            with self._state_lock:
                return commit(None, self.buffer.latest())
        return commit_on_latest(self.buffer, compensator, self._state_lock, commit)

    def _ground_description(self, description: str | None, frame: Frame) -> GroundingBox:
        """The grounder's most confident box for ``description`` in ``frame``."""
        if description is None or not description.strip():
            raise ValueError("description must not be empty")
        if self._grounder is None:
            raise RuntimeError("grounder service is unavailable")
        grounded_boxes = [
            item
            for item in self._grounder.detect(
                frame.image,
                [description],
                box_threshold=self.grounder_config.box_threshold,
                text_threshold=self.grounder_config.text_threshold,
            )
            if item.confidence >= self.min_grounding_confidence
        ]
        if not grounded_boxes:
            raise NoGroundingMatch(f"grounder found no confident box for {description!r}")
        grounded = max(grounded_boxes, key=lambda item: item.confidence)
        return GroundingBox(
            bbox=validate_box(grounded.bbox, frame.image),
            confidence=grounded.confidence,
            label=grounded.label or description,
        )

    def _resolve_existing_track(
        self, grounded: GroundingBox, frames: Sequence[Frame], compensate: bool
    ) -> PointResult | None:
        """A revalidated current track for the reference, tried by old-frame overlap, then
        appearance (when compensating), then the overlap chain through buffered frames."""
        old_frame, latest_frame = frames[0], frames[-1]
        latest_candidates = latest_frame.candidates
        old_match = _best_overlapping(grounded.bbox, old_frame.candidates, self.overlap_threshold)
        if old_match is not None:
            result = self._current_track_result(
                old_match.id,
                latest_candidates,
                grounded,
                match_confidence=overlap_score(grounded.bbox, old_match.bbox),
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
            return self._current_track_result(
                track_id, latest_candidates, grounded, match_confidence=score
            )
        return None

    def _seed_point(
        self,
        placed_box: BBox | None,
        current_frame: Frame | None,
        grounded: GroundingBox,
        description: str | None,
        old_frame: Frame,
        compensate: bool,
    ) -> PointResult:
        """Bind the compensated box on the newest frame (under lock): reuse an overlapping,
        revalidated track or seed a ``point`` track."""
        if current_frame is None:
            raise NoGroundingMatch("no current frame is available")
        if not compensate and current_frame.frame_id != old_frame.frame_id:
            raise NoGroundingMatch(
                f"old-frame reference {grounded.bbox!r} has no matching current track "
                f"on frame {current_frame.frame_id} with compensation disabled"
            )
        if placed_box is None:
            target = repr(description) if description is not None else repr(grounded.bbox)
            raise NoGroundingMatch(
                f"grounded target {target} could not be compensated to frame {current_frame.frame_id}"
            )
        current_match = _best_overlapping(placed_box, current_frame.candidates, self.overlap_threshold)
        if current_match is not None:
            result = self._current_track_result(
                current_match.id,
                current_frame.candidates,
                grounded,
                match_confidence=overlap_score(placed_box, current_match.bbox),
            )
            if result is not None:
                return result
            raise NoGroundingMatch(
                f"grounded target overlaps track {current_match.id}, but it is not revalidated"
            )
        label = (grounded.label if grounded.label != "box" else "") or description or "point target"
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
            overlap_score(placed_box, grounded.bbox),
        )

    def _fast_forward_reid(
        self,
        reference: Sequence[float],
        latest_frame: Frame,
    ) -> tuple[Candidate, float] | None:
        """Pick the latest-frame candidate whose current appearance best matches the reference.

        Tracks are embedded only when created, so current appearance comes from the tracker's
        shared lazy per-frame cache: at most one encode per latest candidate, none for history.
        """
        threshold = self.tracker.config.revalidation_similarity_threshold
        best: tuple[Candidate, float] | None = None
        for candidate in latest_frame.candidates:
            with self._state_lock:
                embedding = self.tracker.frame_embedding(candidate, latest_frame.image)
            similarity = cosine_similarity(reference, embedding)
            if similarity is not None and similarity >= threshold:
                if best is None or similarity > best[1]:
                    best = (candidate, similarity)
        return best

    def _fast_forward_geometry(
        self, grounded_box: BBox, frames: Sequence[Frame]
    ) -> tuple[int, float] | None:
        """Follow the best-overlapping track through the newer buffered frames."""
        previous_box = grounded_box
        track_id: int | None = None
        confidence = 0.0
        for frame in frames[1:]:
            candidate = _best_overlapping(previous_box, frame.candidates, self.overlap_threshold)
            if candidate is None:
                continue
            track_id = candidate.id
            confidence = max(confidence, overlap_score(previous_box, candidate.bbox))
            previous_box = candidate.bbox
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
        with self._state_lock:
            validated = self.tracker.validate_candidate(track_id)
        if validated is None:
            return None
        return PointResult(validated.frame_id, validated, grounded, match_confidence)


def _best_overlapping(
    box: BBox, candidates: Sequence[Candidate], threshold: float
) -> Candidate | None:
    best: tuple[Candidate, float] | None = None
    for candidate in candidates:
        score = overlap_score(box, candidate.bbox)
        if best is None or score > best[1]:
            best = (candidate, score)
    return best[0] if best is not None and best[1] >= threshold else None
