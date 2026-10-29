"""Tracker configuration, the per-track record and the pure rules applied to one track."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from .types import BBox, Candidate, Detection, Embedding

# A candidate validates or re-acquires only while its track was updated this recently
# (monotonic seconds since the frame arrived).
FRESH_CANDIDATE_MAX_AGE_S = 0.75
# Constant-velocity box prediction extrapolates at most this far.
_MAX_PREDICTION_LEAD_S = 0.75
# Labels that name no class, compared casefolded: they never conflict with a known class.
_GENERIC_CLASS_NAMES = frozenset({"", "object", "unknown"})


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Association and track-retention thresholds, expressed in seconds where noted."""

    max_lost_seconds: float = 2.5
    revalidate_after_seconds: float = 0.5
    iou_threshold: float = 0.15
    revalidation_iou_threshold: float = 0.35
    reid_similarity_threshold: float = 0.62
    revalidation_similarity_threshold: float = 0.72
    # Multi-view references: a live, continuously associated track stores up to
    # ``reference_views`` views in total (the creation view plus admitted extras) and validates
    # against the best of them, so a marginal appearance dip does not lose a correctly tracked
    # target. Admission is anchored on the creation view (full threshold) and gated by class,
    # crop size and near-duplicate distance (rules in _admit_reference_view).
    reference_views: int = 4
    # Crops smaller than this carry no usable appearance and are never stored as reference views.
    reference_view_min_width_px: int = 12
    reference_view_min_height_px: int = 30
    reference_view_dedupe_cos: float = 0.98
    # Camera horizontal field of view. With it, detector tracks are rotated into the current
    # frame's heading before association: a yawing camera shifts every box sideways by
    # f·tan(Δψ) regardless of depth, which otherwise breaks IoU association of narrow boxes.
    # None disables the compensation.
    camera_horizontal_fov_deg: float | None = None
    # Heading samples older than this (wall seconds at capture) are not used for compensation.
    max_heading_age_s: float = 1.0

    def __post_init__(self) -> None:
        if self.max_lost_seconds <= 0 or self.revalidate_after_seconds < 0:
            raise ValueError("track retention must be positive and revalidation delay non-negative")
        # Fields fed from PerceptionSettings (reference views, camera FOV) are validated there.
        for name in (
            "iou_threshold",
            "revalidation_iou_threshold",
            "reid_similarity_threshold",
            "revalidation_similarity_threshold",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")


@dataclass(slots=True)
class Track:
    """Mutable state of one tracker identity."""

    id: int
    bbox: BBox
    class_name: str
    class_id: int | None
    confidence: float
    attributes: dict[str, Any]
    reference_embedding: Embedding | None
    current_embedding: Embedding | None
    arrival_monotonic: float
    timestamp: float
    frame_id: int
    pose: Any
    source: str = "detector"
    csrt_tracker: Any = None
    # Open-vocabulary seeds must be re-confirmed by the grounder; CSRT alone drifts.
    seed_misses: int = 0
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    hits: int = 1
    # Any person label observed in this track's history, never cleared:
    # a later frame that mislabels the person must not strip the track's person evidence.
    person_evidence: bool = False
    # Additional confirmed appearance views (the creation view is ``reference_embedding``), capped
    # at TrackerConfig.reference_views - 1; the oldest extra view is evicted first.
    reference_views: list[Embedding] = field(default_factory=list)
    # (width, height) of the frame this track was last seen in, for pixel-to-angle geometry.
    frame_size: tuple[int, int] | None = None
    # Camera heading (degrees) the box was last expressed in, for ego-rotation compensation.
    camera_yaw_deg: float | None = None

    def candidate(self) -> Candidate:
        """Immutable snapshot of this track as a public candidate."""
        return Candidate(
            id=self.id,
            bbox=self.bbox,
            class_name=self.class_name,
            confidence=self.confidence,
            frame_id=self.frame_id,
            timestamp=self.timestamp,
            pose=self.pose,
            class_id=self.class_id,
            attributes=dict(self.attributes),
            source=self.source,
            embedding=self.current_embedding,
            person_evidence=self.person_evidence,
            reference_views=1 + len(self.reference_views),
            frame_width=None if self.frame_size is None else self.frame_size[0],
            frame_height=None if self.frame_size is None else self.frame_size[1],
        )

    def predicted_box(self, age: float) -> BBox:
        """The box extrapolated ``age`` seconds at the track's velocity (bounded lead)."""
        lead = min(age, _MAX_PREDICTION_LEAD_S)
        dx, dy = self.velocity_x * lead, self.velocity_y * lead
        return self.bbox[0] + dx, self.bbox[1] + dy, self.bbox[2] + dx, self.bbox[3] + dy


def is_known_class(name: str) -> bool:
    return name.casefold() not in _GENERIC_CLASS_NAMES


def classes_conflict(left: str, right: str) -> bool:
    """True when both labels name a class and the classes differ (casefolded)."""
    return is_known_class(left) and is_known_class(right) and left.casefold() != right.casefold()


def center(box: BBox) -> tuple[float, float]:
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def normalized_center_distance(left: BBox, right: BBox) -> float:
    """Centre distance relative to the larger box diagonal (at least one pixel)."""
    left_center, right_center = center(left), center(right)
    distance = math.hypot(left_center[0] - right_center[0], left_center[1] - right_center[1])
    scale = max(
        math.hypot(left[2] - left[0], left[3] - left[1]),
        math.hypot(right[2] - right[0], right[3] - right[1]),
        1.0,
    )
    return distance / scale


def is_person_detection(detection: Detection) -> bool:
    """True when a detection carries person evidence. COCO person is ``class_id`` 0."""
    return detection.class_name.casefold() == "person" or detection.class_id == 0
