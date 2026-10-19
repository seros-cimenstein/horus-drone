"""Tracker configuration, the per-track record and the pure rules applied to one track."""

from __future__ import annotations

from collections import deque
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
    # A live, continuously associated track whose appearance score dips just below the threshold
    # (within revalidation_grace_margin) for an unlucky frame keeps its identity for
    # revalidation_grace_s after the last accepted validation. The margin keeps a total
    # appearance change (an identity swap) failing immediately, and a persistent dip still
    # loses the target once the window expires; re-acquisition never grants this grace.
    revalidation_grace_s: float = 1.0
    revalidation_grace_margin: float = 0.05
    # Multi-view references: a live, continuously associated track stores up to
    # ``reference_views`` views in total (the creation view plus admitted extras) and validates
    # against the best of them, so a marginal appearance dip does not lose a correctly tracked
    # target. Admission is anchored on the creation view (full threshold) and gated by class,
    # shirt colour, crop size and near-duplicate distance (rules in _admit_reference_view).
    # Re-acquisition after a loss compares against the creation view only, with the full
    # threshold and all gates: a gallery-widened acceptance region admits same-colour look-alikes.
    reference_views: int = 4
    # Crops smaller than this carry no usable appearance: they are never stored as reference
    # views and never re-acquire a lost identity.
    reference_view_min_width_px: int = 12
    reference_view_min_height_px: int = 30
    reference_view_dedupe_cos: float = 0.98
    # Re-acquisition must pass on this many distinct processed frames within this window before
    # a lost ID is rematched (filters one-frame artifacts); 0 disables the confirmation.
    reacquisition_confirm_s: float = 1.0
    # The published shirt colour is a bounded vote over the track's recent accepted
    # classifications (attributes.vote_shirt_colour): one misread frame must not stick to the
    # track's identity gates. A colour needs ``shirt_colour_min_votes`` votes within the last
    # ``shirt_colour_window`` and strictly more than any other colour.
    shirt_colour_window: int = 5
    shirt_colour_min_votes: int = 3
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
        # Fields fed from PerceptionSettings / ExecutorSettings (reference views, shirt-colour
        # vote, grace, confirmation, camera FOV) are validated there.
        if not 0.0 <= self.revalidation_grace_margin <= 1.0:
            raise ValueError("revalidation_grace_margin must be in [0, 1]")
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
    # Any person label or person-only attribute observed in this track's history, never cleared:
    # a later frame that mislabels the person must not strip the track's person evidence.
    person_evidence: bool = False
    # Last time validate_candidate accepted this track at or above the similarity threshold.
    last_validated_monotonic: float = 0.0
    # Additional confirmed appearance views (the creation view is ``reference_embedding``), capped
    # at TrackerConfig.reference_views - 1; the oldest extra view is evicted first.
    reference_views: list[Embedding] = field(default_factory=list)
    # Accepted shirt-colour classifications of this track, oldest first, bounded by
    # TrackerConfig.shirt_colour_window; only the vote result is published in ``attributes``.
    shirt_colour_votes: deque[str] = field(default_factory=deque)
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
    """True when a detection carries person evidence.

    COCO person is ``class_id`` 0; ``shirt_colour`` is classified only for person detections
    (``horus.perception.pipeline._with_shirt_colour``), so it is person evidence even when this
    frame's class label disagrees (a person mislabeled as a kite).
    """
    return (
        detection.class_name.casefold() == "person"
        or detection.class_id == 0
        or detection.attributes.get("shirt_colour") is not None
    )
