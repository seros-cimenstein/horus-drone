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
    # A live, continuously associated track whose appearance score dips just below the threshold
    # (within revalidation_grace_margin) for an unlucky frame keeps its identity for
    # revalidation_grace_s after the last accepted validation. The margin keeps a total
    # appearance change (an identity swap) failing immediately, and a persistent dip still
    # loses the target once the window expires; re-acquisition never grants this grace.
    revalidation_grace_s: float = 1.0
    revalidation_grace_margin: float = 0.05
    max_reid_center_distance: float = 3.0

    def __post_init__(self) -> None:
        if self.max_lost_seconds <= 0 or self.revalidate_after_seconds < 0:
            raise ValueError("track retention must be positive and revalidation delay non-negative")
        if not math.isfinite(self.revalidation_grace_s) or self.revalidation_grace_s < 0:
            raise ValueError("revalidation_grace_s must be a finite non-negative number")
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
    # One INFO summary per (mode, reference) per second; per-comparison records stay at DEBUG.
    REID_SUMMARY_INTERVAL_S = 1.0

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
        self._similarity_windows: dict[tuple[str, int], dict[str, Any]] = {}
        self._similarity_totals: dict[tuple[str, int], int] = {}

    def seed(
        self,
        bbox: BBox,
        frame: Frame,
        *,
        label: str,
        confidence: float,
        source: str,
    ) -> Candidate:
        """Create a CSRT-backed free-text track in a processed frame."""
        if source not in {"open_vocab", "point"}:
            raise ValueError("seed source must be 'open_vocab' or 'point'")
        class_name = str(label).strip()
        if not class_name:
            raise ValueError("seed label must not be empty")
        image = frame.image
        box = _validate_bbox(bbox, image)
        tracker = _initialize_csrt(image, box)
        self._updates += 1
        track_id = self._next_id
        self._next_id += 1
        embedding = self.encode(image, box)
        track = _Track(
            id=track_id,
            bbox=box,
            class_name=class_name,
            class_id=None,
            confidence=float(confidence),
            attributes={},
            reference_embedding=embedding,
            current_embedding=embedding,
            arrival_monotonic=time.monotonic(),
            timestamp=float(frame.timestamp),
            frame_id=int(frame.frame_id),
            pose=frame.pose,
            source=source,
            csrt_tracker=tracker,
        )
        self._tracks[track_id] = track
        if self._cache_frame_id != frame.frame_id:
            self._embedding_cache.clear()
            self._cache_frame_id = frame.frame_id
        self._current_image = image
        self._embedding_cache[(frame.frame_id, track_id)] = embedding
        candidate = _candidate_from_track(track)
        self._current = (*self._current, candidate)
        return candidate

    def confirm_seed(self, track_id: int, bbox: BBox, frame: Frame, *, confidence: float) -> None:
        """Snap an open-vocabulary seed back onto a fresh grounder box and reset its miss count."""
        track = self._tracks.get(track_id)
        if track is None or track.source != "open_vocab":
            return
        box = _validate_bbox(bbox, frame.image)
        track.csrt_tracker = _initialize_csrt(frame.image, box)
        track.bbox = box
        track.confidence = float(confidence)
        track.seed_misses = 0

    def age_unconfirmed_seeds(self, confirmed: set[int], *, max_misses: int = 3) -> list[int]:
        """Count a grounder cycle against open-vocabulary seeds it did not re-detect; drop stale ones."""
        dropped = []
        for track_id, track in list(self._tracks.items()):
            if track.source != "open_vocab" or track_id in confirmed:
                continue
            track.seed_misses += 1
            if track.seed_misses >= max_misses:
                del self._tracks[track_id]
                dropped.append(track_id)
        if dropped:
            self._current = tuple(item for item in self._current if item.id not in dropped)
        return dropped

    @property
    def track_count(self) -> int:
        """Return the number of live detector and seeded tracks."""
        return len(self._tracks)

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
        """Associate detections and advance seeded CSRT tracks in the current frame."""
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

        advanced_seed_ids: set[int] = set()
        for track_id, track in self._tracks.items():
            if track.source == "detector" or frame_id <= track.frame_id:
                continue
            updated_box = _advance_csrt(track.csrt_tracker, image)
            if updated_box is None:
                continue
            old_center = _center(track.bbox)
            new_center = _center(updated_box)
            elapsed = max(1e-3, now - track.timestamp)
            track.velocity_x = (new_center[0] - old_center[0]) / elapsed
            track.velocity_y = (new_center[1] - old_center[1]) / elapsed
            track.bbox = updated_box
            track.current_embedding = None
            track.arrival_monotonic = arrival_monotonic
            track.timestamp = now
            track.frame_id = frame_id
            track.pose = pose
            track.hits += 1
            advanced_seed_ids.add(track_id)

        associations: list[tuple[float, int, int]] = []
        for track_id, track in self._tracks.items():
            age = max(0.0, now - track.timestamp)
            predicted = _predict(track, age)
            for detection_index, detection in enumerate(normalized):
                overlap = box_iou(predicted, detection.bbox)
                if track.source == "detector":
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
                    association_overlap = overlap
                else:
                    association_overlap = max(overlap, _box_coverage(predicted, detection.bbox))
                if track.source == "detector":
                    if association_overlap < self.config.iou_threshold:
                        continue
                elif association_overlap <= 0.0:
                    continue
                if (
                    track.source == "detector"
                    and age > self.config.revalidate_after_seconds
                    and association_overlap < self.config.revalidation_iou_threshold
                ):
                    continue
                distance = _normalized_center_distance(predicted, detection.bbox)
                spatial = min(1.0, association_overlap + (1.0 - min(distance, 1.0)) * 0.15)
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
            if track.source == "detector":
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
            if _is_person_detection(detection):
                track.person_evidence = True
            if track.source != "detector":
                try:
                    track.csrt_tracker = _initialize_csrt(image, detection.bbox)
                except (RuntimeError, ValueError):
                    pass
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
                person_evidence=_is_person_detection(detection),
            )
            self._embedding_cache[(frame_id, track_id)] = embedding
            detection_track_ids[detection_index] = track_id

        candidates = [
            _candidate_from_track(self._tracks[detection_track_ids[index]])
            for index in range(len(normalized))
        ]
        visible_ids = set(detection_track_ids.values())
        candidates.extend(
            _candidate_from_track(self._tracks[track_id])
            for track_id in sorted(advanced_seed_ids - visible_ids)
        )
        self._current = tuple(candidates)
        return self._current

    @property
    def current_candidates(self) -> tuple[Candidate, ...]:
        """Candidates visible in the most recently processed frame."""
        return self._current

    def encode(self, image: Any, bbox: BBox) -> Embedding | None:
        """Encode an image region with this tracker's configured appearance model."""
        vector = self.embedder.embed(image, bbox)
        return _normalize_embedding(vector) if vector is not None else None

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
        return self.frame_embedding(candidate, self._current_image)

    def frame_embedding(self, candidate: Candidate, image: Any) -> Embedding | None:
        """Embed a candidate's box in its frame image, cached while that frame is current.

        Associated tracks are not re-embedded every frame; callers that need current appearance
        share this lazy cache. A candidate from an older frame is encoded without caching.
        """
        if candidate.frame_id != self._cache_frame_id:
            return self.encode(image, candidate.bbox)
        cache_key = (candidate.frame_id, candidate.id)
        if cache_key not in self._embedding_cache:
            self._embedding_cache[cache_key] = self.encode(image, candidate.bbox)
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
        """Record one appearance comparison at DEBUG and aggregate it into a per-second summary.

        A hold re-identifies the same target against every visible candidate on every control
        tick, so per-comparison INFO lines reached tens of thousands per hold. The per-second
        INFO summary keeps every field the diagnosis needs (how many comparisons, the best score
        and candidate, whether the reference's own track was among them) at a bounded volume.
        """
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
        _LOGGER.debug(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            extra=record,
        )
        self._summarize_similarity(record)

    def _summarize_similarity(self, record: dict[str, Any]) -> None:
        now = time.monotonic()
        key = (str(record["mode"]), int(record["reference_id"]))
        window = self._similarity_windows.get(key)
        if window is None:
            window = {
                "started": now,
                "comparisons": 0,
                "total": self._similarity_totals.get(key, 0),
                "best_similarity": None,
                "best_candidate_id": None,
                "best_crop_px": None,
                "last_similarity": None,
                "own_track_seen": False,
            }
            self._similarity_windows[key] = window
        window["comparisons"] += 1
        window["total"] += 1
        self._similarity_totals[key] = window["total"]
        similarity = record.get("similarity")
        if isinstance(similarity, float):
            window["last_similarity"] = similarity
            if window["best_similarity"] is None or similarity > window["best_similarity"]:
                window["best_similarity"] = similarity
                window["best_candidate_id"] = record.get("candidate_id")
                window["best_crop_px"] = (record.get("crop_width_px"), record.get("crop_height_px"))
        if record.get("candidate_id") == record.get("reference_id"):
            window["own_track_seen"] = True
        elapsed = now - window["started"]
        if elapsed < self.REID_SUMMARY_INTERVAL_S:
            return
        summary = {
            "event": "perception_reid_summary",
            "mode": record["mode"],
            "reference_id": record["reference_id"],
            "threshold": record["threshold"],
            "window_s": round(elapsed, 3),
            "comparisons": window["comparisons"],
            "total_comparisons": window["total"],
            "best_similarity": window["best_similarity"],
            "best_candidate_id": window["best_candidate_id"],
            "best_crop_px": list(window["best_crop_px"]) if window["best_crop_px"] else None,
            "last_similarity": window["last_similarity"],
            "own_track_seen": window["own_track_seen"],
        }
        _LOGGER.info(
            json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            extra=summary,
        )
        window.update(
            {
                "started": now,
                "comparisons": 0,
                "best_similarity": None,
                "best_candidate_id": None,
                "best_crop_px": None,
                "own_track_seen": False,
            }
        )

    def validate_candidate(
        self,
        track_id: int,
        *,
        max_age_seconds: float = 0.75,
        min_confidence: float = 0.0,
    ) -> Candidate | None:
        """Return a fresh candidate only when its current appearance matches its track reference."""
        candidate, _ = self._validate_candidate(
            track_id, max_age_seconds=max_age_seconds, min_confidence=min_confidence
        )
        return candidate

    def validate_candidate_failure(
        self,
        track_id: int,
        *,
        max_age_seconds: float = 0.75,
        min_confidence: float = 0.0,
    ) -> str | None:
        """Why ``validate_candidate`` rejects this ID, or ``None`` when it validates.

        Reasons: ``not_visible`` (no candidate in the current frame), ``stale`` (unresolved or
        not-fresh track), ``no_reference_appearance`` (no stored embedding to compare), and
        ``appearance_mismatch`` (live embedding below the revalidation threshold). Callers use
        this to report the actual cause after a rejected validation.
        """
        _, reason = self._validate_candidate(
            track_id,
            max_age_seconds=max_age_seconds,
            min_confidence=min_confidence,
            log_similarity=False,
        )
        return reason

    def _validate_candidate(
        self,
        track_id: int,
        *,
        max_age_seconds: float,
        min_confidence: float,
        log_similarity: bool = True,
    ) -> tuple[Candidate | None, str | None]:
        track_id = int(track_id)
        candidate = self.candidate(track_id)
        if candidate is None:
            return None, "not_visible"
        track = self._tracks.get(track_id)
        if track is None or not self._candidate_is_fresh(
            candidate,
            track,
            max_age_seconds=max_age_seconds,
            min_confidence=min_confidence,
        ):
            return None, "stale"
        if track.reference_embedding is None:
            return None, "no_reference_appearance"
        live_embedding = self._embed_candidate(candidate)
        similarity = cosine_similarity(track.reference_embedding, live_embedding)
        threshold = self.config.revalidation_similarity_threshold
        if log_similarity:
            self._log_similarity(
                mode="selected_validation",
                similarity=similarity,
                reference_id=track.id,
                candidate=candidate,
                threshold=threshold,
            )
        now = time.monotonic()
        if similarity is None or similarity < threshold:
            # A correctly tracked target's score wobbles across the threshold (the campaign saw
            # 0.7917 -> 0.7196 frame to frame), so a marginal dip on one frame must not declare
            # the target lost while the same track is still fresh and spatially associated.
            # Only a score within the configured margin of the threshold is forgiven, and only
            # within the grace window after an accepted validation: a total appearance change
            # (an identity swap) fails immediately, a persistent dip expires the window, and
            # re-acquisition (reidentify_candidate) never grants this grace.
            if (
                similarity is not None
                and similarity >= threshold - self.config.revalidation_grace_margin
                and track.last_validated_monotonic > 0.0
                and self.config.revalidation_grace_s > 0.0
                and now - track.last_validated_monotonic <= self.config.revalidation_grace_s
            ):
                _LOGGER.debug(
                    "appearance score %.3f within %.2f of the %.2f threshold for track %d and "
                    "%.2fs after the last accepted validation; keeping the identity",
                    similarity, self.config.revalidation_grace_margin, threshold, track.id,
                    now - track.last_validated_monotonic,
                )
                return replace(candidate, embedding=live_embedding), None
            return None, "appearance_mismatch"
        track.last_validated_monotonic = now
        return replace(candidate, embedding=live_embedding), None

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

def _box_coverage(left: BBox, right: BBox) -> float:
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    smaller_area = min(
        max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1]),
        max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1]),
    )
    return intersection / smaller_area if smaller_area > 0.0 else 0.0


def _validate_bbox(bbox: BBox, image: Any) -> BBox:
    try:
        box = tuple(float(value) for value in bbox)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("seed bbox must contain four finite coordinates") from exc
    if len(box) != 4 or not all(math.isfinite(value) for value in box):
        raise ValueError("seed bbox must contain four finite coordinates")
    x1, y1, x2, y2 = box
    shape = getattr(image, "shape", None)
    if shape is None or len(shape) < 2:
        raise ValueError("seed frame must contain an image array")
    height, width = int(shape[0]), int(shape[1])
    if x1 < 0 or y1 < 0 or x1 >= x2 or y1 >= y2 or x2 > width or y2 > height:
        raise ValueError("seed bbox must lie within frame bounds")
    return box  # type: ignore[return-value]


def _create_csrt_tracker() -> Any:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required for seeded CSRT tracking") from exc
    factory = getattr(cv2, "TrackerCSRT_create", None)
    if factory is None:
        factory = getattr(getattr(cv2, "legacy", None), "TrackerCSRT_create", None)
    if factory is None:
        raise RuntimeError("this OpenCV build does not provide TrackerCSRT_create")
    return factory()


def _initialize_csrt(image: Any, bbox: BBox) -> Any:
    tracker = _create_csrt_tracker()
    x1, y1, x2, y2 = bbox
    result = tracker.init(
        image,
        (
            int(round(x1)),
            int(round(y1)),
            max(1, int(round(x2 - x1))),
            max(1, int(round(y2 - y1))),
        ),
    )
    if result is False:
        raise RuntimeError("OpenCV CSRT could not initialize the seeded box")
    return tracker


def _advance_csrt(tracker: Any, image: Any) -> BBox | None:
    if tracker is None or image is None:
        return None
    try:
        ok, rectangle = tracker.update(image)
    except Exception:
        return None
    if not ok:
        return None
    x, y, width, height = (float(value) for value in rectangle)
    image_height, image_width = int(image.shape[0]), int(image.shape[1])
    x1, y1 = max(0.0, x), max(0.0, y)
    x2, y2 = min(float(image_width), x + width), min(float(image_height), y + height)
    return (x1, y1, x2, y2) if x1 < x2 and y1 < y2 else None


class BoxCompensator:
    """Carry a box from one buffered frame to newer frames with a single CSRT tracker.

    Advancing is incremental, so a caller can catch up with frames that arrived while it was
    compensating and finish under the perception lock with only a frame or two left.
    """

    def __init__(self, bbox: BBox, frame: Frame) -> None:
        self.box: BBox | None = _validate_bbox(bbox, frame.image)
        self.frame_id = int(frame.frame_id)
        self._image: Any = frame.image
        self._tracker: Any = None

    def advance(self, frames: Sequence[Frame]) -> BBox | None:
        """Track through ``frames`` newer than the current one; ``None`` once tracking fails."""
        for frame in frames:
            if self.box is None:
                return None
            if frame.frame_id <= self.frame_id:
                continue
            if self._tracker is None:
                try:
                    self._tracker = _initialize_csrt(self._image, self.box)
                except (RuntimeError, ValueError):
                    self.box = None
                    return None
                self._image = None
            self.box = _advance_csrt(self._tracker, frame.image)
            self.frame_id = int(frame.frame_id)
        return self.box


def compensate_bbox(bbox: BBox, frames: Sequence[Frame]) -> BBox | None:
    """Track a free-text/point box through retained images with one CSRT tracker."""
    if not frames:
        return None
    try:
        compensator = BoxCompensator(bbox, frames[0])
    except (RuntimeError, ValueError):
        return None
    return compensator.advance(frames[1:])






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


def _is_person_detection(detection: Detection) -> bool:
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
        source=track.source,
        embedding=track.current_embedding if embedding is None else embedding,
        person_evidence=track.person_evidence,
    )
