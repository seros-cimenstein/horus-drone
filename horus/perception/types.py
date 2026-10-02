"""Dependency-light data types shared by the perception components."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Mapping

BBox = tuple[float, float, float, float]
Embedding = tuple[float, ...]


@dataclass(slots=True)
class Frame:
    """Decoded camera frame; timestamps are monotonic seconds and pose is caller-defined."""

    frame_id: int
    timestamp: float
    image: Any
    pose: Any = None
    candidates: tuple["Candidate", ...] = ()

    def to_dict(self, *, max_dimension: int = 640, jpeg_quality: int = 82) -> dict[str, Any]:
        """Serialize metadata/candidates with a base64 set-of-mark JPEG for API transport."""
        from .render import render_set_of_marks

        payload = render_set_of_marks(
            self, max_dimension=max_dimension, jpeg_quality=jpeg_quality
        )
        return {
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "pose": self.pose,
            "image_base64": base64.b64encode(payload.image_jpeg).decode("ascii"),
            "image_mime_type": "image/jpeg",
            "width": payload.width,
            "height": payload.height,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "candidate_table": [dict(row) for row in payload.candidates],
        }


@dataclass(frozen=True, slots=True)
class Detection:
    """Detector proposal in source-image pixel coordinates (x1, y1, x2, y2)."""

    bbox: BBox
    confidence: float = 1.0
    class_name: str = "object"
    class_id: int | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Persistent int ID and source-pixel box; optional ``distance_m`` is meters and ``bearing`` radians.

    Geometry attributes are passed through from the detector/calibration layer. Bearing is
    camera-forward-relative and positive to image-right; this package does not estimate it.
    """

    id: int
    bbox: BBox
    class_name: str
    confidence: float
    frame_id: int
    timestamp: float
    pose: Any = None
    class_id: int | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    source: str = "detector"
    embedding: Embedding | None = field(default=None, repr=False, compare=False)
    # Track-level person evidence: a person label or person-only attribute was observed at some
    # point in this track's history, so a frame that mislabels it (e.g. "kite") cannot retract it.
    person_evidence: bool = False

    def to_dict(self, *, include_embedding: bool = False) -> dict[str, Any]:
        """Return a JSON-friendly candidate record, omitting private ReID data by default."""
        result: dict[str, Any] = {
            "source": self.source,
            "id": self.id,
            "bbox": list(self.bbox),
            "box": list(self.bbox),
            "class_name": self.class_name,
            "class": self.class_name,
            "confidence": self.confidence,
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "pose": self.pose,
            "class_id": self.class_id,
            "attributes": dict(self.attributes),
            "person_evidence": self.person_evidence,
        }
        for optional in ("distance_m", "bearing"):
            if optional in self.attributes:
                result[optional] = self.attributes[optional]
        if include_embedding:
            result["embedding"] = list(self.embedding) if self.embedding is not None else None
        return result


@dataclass(frozen=True, slots=True)
class FrameObservation:
    """Public immutable view of processed-frame metadata and tracked candidates."""

    frame_id: int
    timestamp: float
    pose: Any
    candidates: tuple[Candidate, ...]
    frame: Frame | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the observation, including a set-of-mark JPEG when its frame is attached."""
        if self.frame is not None:
            return self.frame.to_dict()
        return {
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "pose": self.pose,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class GroundingBox:
    """Grounding model result in source-image pixel coordinates."""

    bbox: BBox
    confidence: float
    label: str = ""


@dataclass(frozen=True, slots=True)
class SetOfMarkPayload:
    """JPEG frame and matching candidate table for set-of-mark prompting."""

    frame_id: int
    timestamp: float
    pose: Any
    image_jpeg: bytes
    candidates: tuple[Mapping[str, Any], ...]
    width: int
    height: int

    def to_dict(self) -> dict[str, Any]:
        """Return the wire-friendly payload; JPEG bytes remain binary for transport."""
        return {
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "pose": self.pose,
            "image_jpeg": self.image_jpeg,
            "width": self.width,
            "height": self.height,
            "candidates": [dict(row) for row in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class PointResult:
    """Up-to-date tracker candidate recovered from a grounding-model box."""

    frame_id: int
    candidate: Candidate
    grounded_box: GroundingBox
    match_confidence: float

    def to_dict(self) -> dict[str, Any]:
        """Return the current ID, its latest box, and the grounding re-match score."""
        return {
            "frame_id": self.frame_id,
            "candidate": self.candidate.to_dict(),
            "grounded_box": {
                "bbox": list(self.grounded_box.bbox),
                "confidence": self.grounded_box.confidence,
                "label": self.grounded_box.label,
            },
            "match_confidence": self.match_confidence,
        }


class StaleFrameError(LookupError):
    """Raised when a requested frame has already left the latency ring buffer."""


class NoGroundingMatch(LookupError):
    """Raised when a grounded box cannot be safely revalidated on the current frame."""
