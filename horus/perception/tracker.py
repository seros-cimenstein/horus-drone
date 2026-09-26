"""Persistent multi-object tracking with spatial association and appearance re-ID."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
import math
import time
from dataclasses import dataclass, replace
from typing import Any, Protocol, Sequence

from .types import BBox, Candidate, Detection, Embedding, Frame

_LOGGER = logging.getLogger(__name__)


class Embedder(Protocol):
    """Small adapter contract for any local or model-backed appearance encoder."""

    def embed(self, image: Any, bbox: BBox) -> Sequence[float] | None: ...


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Association and track-retention thresholds, expressed in seconds where noted."""

    max_lost_seconds: float = 2.5
    revalidate_after_seconds: float = 0.5
    iou_threshold: float = 0.15
    revalidation_iou_threshold: float = 0.35
    reid_similarity_threshold: float = 0.62
    revalidation_similarity_threshold: float = 0.72
    max_reid_center_distance: float = 3.0

    def __post_init__(self) -> None:
        if self.max_lost_seconds <= 0 or self.revalidate_after_seconds < 0:
            raise ValueError("track retention must be positive and revalidation delay non-negative")
        for name in (
            "iou_threshold",
            "revalidation_iou_threshold",
            "reid_similarity_threshold",
            "revalidation_similarity_threshold",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.max_reid_center_distance <= 0:
            raise ValueError("max_reid_center_distance must be positive")


@dataclass(slots=True)
class _Track:
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
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    hits: int = 1


class ColorHistogramEmbedder:
    """Dependency-lazy HSV histogram embedding useful as a no-training ReID baseline."""

    def embed(self, image: Any, bbox: BBox) -> Embedding | None:
        """Return a normalized HSV histogram; missing OpenCV/NumPy degrades to no embedding."""
        if image is None:
            return None
        try:
            import cv2
            import numpy as np
        except ImportError:
            return None
        height, width = int(image.shape[0]), int(image.shape[1])
        x1 = max(0, min(width, int(math.floor(bbox[0]))))
        y1 = max(0, min(height, int(math.floor(bbox[1]))))
        x2 = max(0, min(width, int(math.ceil(bbox[2]))))
        y2 = max(0, min(height, int(math.ceil(bbox[3]))))
        if x2 <= x1 or y2 <= y1:
            return None
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
        cv2.normalize(hist, hist, alpha=1.0, norm_type=cv2.NORM_L2)
        vector = np.asarray(hist, dtype=np.float32).reshape(-1)
        return tuple(float(value) for value in vector)


class ONNXReIDEmbedder:
    """Run a person-ReID ONNX model with ONNX Runtime's CPU provider."""

    def __init__(
        self,
        model_path: str,
        *,
        input_size: tuple[int, int] = (128, 256),
        onnx_intra_op_num_threads: int = 2,
        onnx_inter_op_num_threads: int = 1,
    ) -> None:
        for name, value in (
            ("onnx_intra_op_num_threads", onnx_intra_op_num_threads),
            ("onnx_inter_op_num_threads", onnx_inter_op_num_threads),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.model_path = model_path
        self.input_size = input_size
        self.onnx_intra_op_num_threads = onnx_intra_op_num_threads
        self.onnx_inter_op_num_threads = onnx_inter_op_num_threads
        self._session: Any = None
        self._input_name: str | None = None

    def _load(self) -> None:
        if self._session is not None:
            return
        try:
            import onnxruntime
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime is required for ReID inference; install horus-drone[perception]"
            ) from exc
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = self.onnx_intra_op_num_threads
        options.inter_op_num_threads = self.onnx_inter_op_num_threads
        self._session = onnxruntime.InferenceSession(
            self.model_path,
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        inputs = self._session.get_inputs()
        if not inputs:
            self._session = None
            raise RuntimeError(f"ReID ONNX model has no inputs: {self.model_path}")
        self._input_name = inputs[0].name

    def embed(self, image: Any, bbox: BBox) -> Embedding | None:
        """Crop a person box, apply Torchreid ImageNet preprocessing, and normalize the feature."""
        if image is None:
            return None
        self._load()
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("OpenCV and NumPy are required to preprocess ReID crops") from exc

        height, width = int(image.shape[0]), int(image.shape[1])
        x1, y1 = max(0, int(bbox[0])), max(0, int(bbox[1]))
        x2, y2 = min(width, int(math.ceil(bbox[2]))), min(height, int(math.ceil(bbox[3])))
        if x2 <= x1 or y2 <= y1:
            return None
        crop = image[y1:y2, x1:x2]
        resized = cv2.resize(crop, self.input_size, interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        values = np.asarray(rgb, dtype=np.float32) / 255.0
        values -= np.asarray((0.485, 0.456, 0.406), dtype=np.float32)
        values /= np.asarray((0.229, 0.224, 0.225), dtype=np.float32)
        tensor = np.transpose(values, (2, 0, 1))[None, ...]
        outputs = self._session.run(None, {self._input_name: tensor})
        if not outputs:
            raise RuntimeError("ReID ONNX model returned no feature output")
        vector = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        if vector.size == 0 or not np.isfinite(vector).all():
            return None
        scale = float(np.max(np.abs(vector)))
        if scale <= 1e-12:
            return None
        vector /= scale
        norm = float(np.linalg.norm(vector))
        vector /= norm
        return tuple(float(value) for value in vector)


class PersistentTracker:
    """Track detector proposals, retaining identities through brief occlusions."""

    MAX_RETIRED_TRACKS = 64

    def __init__(
        self,
        config: TrackerConfig | None = None,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self.config = config or TrackerConfig()
        self.embedder: Embedder = embedder or ColorHistogramEmbedder()
        self._tracks: dict[int, _Track] = {}
        # Expired tracks keep their reference appearance so a followed target that stays hidden
        # longer than max_lost_seconds can still be re-identified when it reappears (bounded, FIFO).
        self._retired: OrderedDict[int, _Track] = OrderedDict()
        self._next_id = 1
        self._updates = 0
        self._current: tuple[Candidate, ...] = ()
        self._current_image: Any = None
        self._embedding_cache: dict[tuple[int, int], Embedding | None] = {}
        self._cache_frame_id: int | None = None

    @property
    def current_candidates(self) -> tuple[Candidate, ...]:
        """Candidates visible in the most recently processed frame."""
        return self._current

    def encode(self, image: Any, bbox: BBox) -> Embedding | None:
        """Encode an image region with this tracker's configured appearance model."""
        vector = self.embedder.embed(image, bbox)
        return _normalize_embedding(vector) if vector is not None else None

    def update(
        self,
        detections: Sequence[Detection],
        frame: Frame | None = None,
        *,
        image: Any = None,
        frame_id: int | None = None,
        timestamp: float | None = None,
        pose: Any = None,
    ) -> tuple[Candidate, ...]:
        """Associate detections with persistent IDs and return visible candidates."""
        self._updates += 1
        arrival_monotonic = time.monotonic()
        if frame is not None:
            image = frame.image
            frame_id = frame.frame_id
            timestamp = frame.timestamp
            pose = frame.pose
        if frame_id is None:
            frame_id = self._updates
        if timestamp is None:
            timestamp = time.monotonic()
        now = float(timestamp)
        if frame_id != self._cache_frame_id:
            self._embedding_cache.clear()
            self._cache_frame_id = frame_id
        self._current_image = image

        normalized = [_coerce_detection(item) for item in detections]
        expired = [
            track_id
            for track_id, track in self._tracks.items()
            if now - track.timestamp > self.config.max_lost_seconds
        ]
        for track_id in expired:
            track = self._tracks.pop(track_id)
            if track.reference_embedding is not None:
                self._retired[track_id] = track
                while len(self._retired) > self.MAX_RETIRED_TRACKS:
                    self._retired.popitem(last=False)

        associations: list[tuple[float, int, int]] = []
        for track_id, track in self._tracks.items():
            age = max(0.0, now - track.timestamp)
            predicted = _predict(track, age)
            for detection_index, detection in enumerate(normalized):
                known_track_class = track.class_name not in ("", "object", "unknown")
                known_detection_class = detection.class_name not in ("", "object", "unknown")
                if known_track_class and known_detection_class and track.class_name != detection.class_name:
                    continue
                if (
                    track.class_id is not None
                    and detection.class_id is not None
                    and track.class_id != detection.class_id
                ):
                    continue
                overlap = box_iou(predicted, detection.bbox)
                distance = _normalized_center_distance(predicted, detection.bbox)
                if overlap < self.config.iou_threshold:
                    continue
                if (
                    age > self.config.revalidate_after_seconds
                    and overlap < self.config.revalidation_iou_threshold
                ):
                    continue
                spatial = min(1.0, overlap + (1.0 - min(distance, 1.0)) * 0.15)
                associations.append((spatial, track_id, detection_index))

        associations.sort(reverse=True)
        assigned_tracks: set[int] = set()
        assigned_detections: set[int] = set()
        detection_track_ids: dict[int, int] = {}
        for _, track_id, detection_index in associations:
            if track_id in assigned_tracks or detection_index in assigned_detections:
                continue
            track = self._tracks[track_id]
            detection = normalized[detection_index]
            elapsed = max(1e-3, now - track.timestamp)
            old_center = _center(track.bbox)
            new_center = _center(detection.bbox)
            velocity_x = (new_center[0] - old_center[0]) / elapsed
            velocity_y = (new_center[1] - old_center[1]) / elapsed
            track.velocity_x = 0.5 * track.velocity_x + 0.5 * velocity_x
            track.velocity_y = 0.5 * track.velocity_y + 0.5 * velocity_y
            track.bbox = detection.bbox
            track.class_name = detection.class_name
            track.class_id = detection.class_id
            track.confidence = detection.confidence
            attributes = dict(detection.attributes)
            if attributes.get("shirt_colour") is None and track.attributes.get("shirt_colour") is not None:
                # An inconclusive frame (side view, partial occlusion) must not erase the track's known
                # shirt colour: re-identification uses it to reject look-alike people.
                attributes["shirt_colour"] = track.attributes["shirt_colour"]
                if "shirt_colour_confidence" in track.attributes:
                    attributes["shirt_colour_confidence"] = track.attributes["shirt_colour_confidence"]
            track.attributes = attributes
            track.current_embedding = None
            track.arrival_monotonic = arrival_monotonic
            track.timestamp = now
            track.frame_id = frame_id
            track.pose = pose
            track.hits += 1
            assigned_tracks.add(track_id)
            assigned_detections.add(detection_index)
            detection_track_ids[detection_index] = track_id

        for detection_index, detection in enumerate(normalized):
            if detection_index in assigned_detections:
                continue
            track_id = self._next_id
            self._next_id += 1
            embedding = self.encode(image, detection.bbox)
            self._tracks[track_id] = _Track(
                id=track_id,
                bbox=detection.bbox,
                class_name=detection.class_name,
                class_id=detection.class_id,
                confidence=detection.confidence,
                attributes=dict(detection.attributes),
                reference_embedding=embedding,
                current_embedding=embedding,
                arrival_monotonic=arrival_monotonic,
                timestamp=now,
                frame_id=frame_id,
                pose=pose,
            )
            self._embedding_cache[(frame_id, track_id)] = embedding
            detection_track_ids[detection_index] = track_id

        self._current = tuple(
            _candidate_from_track(self._tracks[detection_track_ids[index]])
            for index in range(len(normalized))
        )
        return self._current

    def candidate(self, track_id: int) -> Candidate | None:
        """Return the currently visible candidate for an ID, or ``None`` if occluded."""
        return next((item for item in self._current if item.id == track_id), None)

    def _candidate_is_fresh(
        self,
        candidate: Candidate | None,
        track: _Track | None,
        *,
        max_age_seconds: float,
        min_confidence: float,
    ) -> bool:
        return (
            candidate is not None
            and track is not None
            and track.hits >= 2
            and candidate.confidence >= min_confidence
            and time.monotonic() - track.arrival_monotonic <= max_age_seconds
        )

    def _embed_candidate(self, candidate: Candidate) -> Embedding | None:
        cache_key = (candidate.frame_id, candidate.id)
        if cache_key not in self._embedding_cache:
            self._embedding_cache[cache_key] = self.encode(
                self._current_image, candidate.bbox
            )
        return self._embedding_cache[cache_key]

    def _log_similarity(
        self,
        *,
        mode: str,
        similarity: float | None,
        reference_id: int,
        candidate: Candidate,
        threshold: float,
    ) -> None:
        crop_width_px, crop_height_px = _crop_pixel_dimensions(
            self._current_image, candidate.bbox
        )
        record = {
            "event": "perception_reid_similarity",
            "mode": mode,
            "similarity": similarity,
            "threshold": threshold,
            "configured_threshold": threshold,
            "reference_id": reference_id,
            "candidate_id": candidate.id,
            "shirt_colour": candidate.attributes.get("shirt_colour"),
            "bbox": list(candidate.bbox),
            "crop_width_px": crop_width_px,
            "crop_height_px": crop_height_px,
            "frame_id": candidate.frame_id,
        }
        _LOGGER.info(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            extra=record,
        )

    def validate_candidate(
        self,
        track_id: int,
        *,
        max_age_seconds: float = 0.75,
        min_confidence: float = 0.0,
    ) -> Candidate | None:
        """Return a fresh candidate only when its current appearance matches its track reference."""
        track_id = int(track_id)
        candidate = self.candidate(track_id)
        track = self._tracks.get(track_id)
        if (
            candidate is None
            or not self._candidate_is_fresh(
                candidate,
                track,
                max_age_seconds=max_age_seconds,
                min_confidence=min_confidence,
            )
            or track is None
            or track.reference_embedding is None
        ):
            return None
        live_embedding = self._embed_candidate(candidate)
        similarity = cosine_similarity(track.reference_embedding, live_embedding)
        threshold = self.config.revalidation_similarity_threshold
        self._log_similarity(
            mode="selected_validation",
            similarity=similarity,
            reference_id=track.id,
            candidate=candidate,
            threshold=threshold,
        )
        if similarity is None or similarity < threshold:
            return None
        return replace(candidate, embedding=live_embedding)

    def validate_id(self, track_id: int, **kwargs: Any) -> bool:
        """Require a recent detection and matching appearance before acting on an ID."""
        return self.validate_candidate(track_id, **kwargs) is not None

    def reidentify_candidate(self, track_id: int) -> tuple[Candidate, float] | None:
        """Re-match a lost track's reference embedding to a fresh current candidate."""
        track = self._tracks.get(int(track_id)) or self._retired.get(int(track_id))
        if track is None or track.reference_embedding is None:
            return None
        reference_colour = track.attributes.get("shirt_colour")
        threshold = self.config.revalidation_similarity_threshold
        best: tuple[Candidate, float] | None = None
        for candidate in self._current:
            candidate_track = self._tracks.get(candidate.id)
            if not self._candidate_is_fresh(
                candidate,
                candidate_track,
                max_age_seconds=0.75,
                min_confidence=0.0,
            ):
                continue
            live_embedding = self._embed_candidate(candidate)
            similarity = cosine_similarity(track.reference_embedding, live_embedding)
            self._log_similarity(
                mode="reidentification",
                similarity=similarity,
                reference_id=track.id,
                candidate=candidate,
                threshold=threshold,
            )
            if (
                track.class_name not in ("", "object", "unknown")
                and candidate.class_name != track.class_name
            ):
                continue
            if (
                reference_colour is not None
                and candidate.attributes.get("shirt_colour") != reference_colour
            ):
                continue
            if (
                similarity is not None
                and similarity >= threshold
                and (best is None or similarity > best[1])
            ):
                best = (replace(candidate, embedding=live_embedding), similarity)
        return best

    def rematch(
        self,
        embedding: Sequence[float],
        candidates: Sequence[Candidate] | None = None,
        *,
        threshold: float | None = None,
    ) -> tuple[Candidate, float] | None:
        """Re-identify an appearance vector against candidates and apply a confidence gate."""
        if candidates is None:
            candidates = tuple(
                replace(candidate, embedding=self._embed_candidate(candidate))
                for candidate in self._current
            )
        required = (
            self.config.revalidation_similarity_threshold if threshold is None else threshold
        )
        best: tuple[Candidate, float] | None = None
        for candidate in candidates:
            similarity = cosine_similarity(embedding, candidate.embedding)
            if similarity is not None and (best is None or similarity > best[1]):
                best = (candidate, similarity)
        return best if best is not None and best[1] >= required else None


def box_iou(left: BBox, right: BBox) -> float:
    """Intersection-over-union for pixel-coordinate boxes."""
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0


def cosine_similarity(left: Sequence[float] | None, right: Sequence[float] | None) -> float | None:
    """Cosine similarity for equal-length appearance vectors."""
    if left is None or right is None or len(left) != len(right) or not left:
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 1e-12 or right_norm <= 1e-12:
        return None
    return dot / (left_norm * right_norm)


def _normalize_embedding(values: Sequence[float]) -> Embedding | None:
    vector = tuple(float(value) for value in values)
    norm = math.sqrt(sum(value * value for value in vector))
    if not vector or norm <= 1e-12:
        return None
    return tuple(value / norm for value in vector)


def _crop_pixel_dimensions(image: Any, bbox: BBox) -> tuple[int, int]:
    shape = getattr(image, "shape", None)
    if shape is None or len(shape) < 2:
        return 0, 0
    try:
        height, width = int(shape[0]), int(shape[1])
    except (TypeError, ValueError):
        return 0, 0
    x1, y1 = max(0, int(bbox[0])), max(0, int(bbox[1]))
    x2, y2 = min(width, int(math.ceil(bbox[2]))), min(
        height, int(math.ceil(bbox[3]))
    )
    return max(0, x2 - x1), max(0, y2 - y1)


def _center(box: BBox) -> tuple[float, float]:
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def _predict(track: _Track, age: float) -> BBox:
    lead = min(age, 0.75)
    dx, dy = track.velocity_x * lead, track.velocity_y * lead
    return track.bbox[0] + dx, track.bbox[1] + dy, track.bbox[2] + dx, track.bbox[3] + dy


def _normalized_center_distance(left: BBox, right: BBox) -> float:
    left_center, right_center = _center(left), _center(right)
    distance = math.hypot(left_center[0] - right_center[0], left_center[1] - right_center[1])
    scale = max(
        math.hypot(left[2] - left[0], left[3] - left[1]),
        math.hypot(right[2] - right[0], right[3] - right[1]),
        1.0,
    )
    return distance / scale


def _coerce_detection(item: Detection) -> Detection:
    if isinstance(item, Detection):
        return item
    if isinstance(item, dict):
        return Detection(
            bbox=tuple(float(value) for value in item["bbox"]),
            confidence=float(item.get("confidence", 1.0)),
            class_name=str(item.get("class_name", "object")),
            class_id=item.get("class_id"),
            attributes=item.get("attributes", {}),
        )
    raise TypeError(f"expected Detection, got {type(item).__name__}")


def _candidate_from_track(track: _Track, *, embedding: Embedding | None = None) -> Candidate:
    return Candidate(
        id=track.id,
        bbox=track.bbox,
        class_name=track.class_name,
        confidence=track.confidence,
        frame_id=track.frame_id,
        timestamp=track.timestamp,
        pose=track.pose,
        class_id=track.class_id,
        attributes=dict(track.attributes),
        embedding=track.current_embedding if embedding is None else embedding,
    )
