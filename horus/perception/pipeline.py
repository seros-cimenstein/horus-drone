"""Integrated perception API joining frames, proposals, tracking, and latency recovery."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
import base64
import binascii
import math
import statistics
import threading
import time
from typing import Any, NamedTuple

from horus.events import EventBus
from .attributes import classify_shirt_colour
from .camera import OpenCVCameraReader
from .grounding import (
    FrameBuffer,
    Grounder,
    GrounderConfig,
    GroundingPointer,
    box_overlap_score,
    coerce_grounding_box,
    commit_on_latest,
)
from .recognition import TinyEmbeddingClassifier
from .scene import SceneMonitor
from .tracker import BoxCompensator, PersistentTracker, cosine_similarity
from .types import (
    Candidate,
    Detection,
    Frame,
    FrameObservation,
    GroundingBox,
    PointResult,
    StaleFrameError,
)


class _FrameStats(NamedTuple):
    finished_at: float
    detector_ms: float
    tracker_ms: float
    detections: int
    ignored: int
    candidates: int


class _TargetProfile(NamedTuple):
    """Recognition state replaced as one object so readers never see a half-applied profile."""

    value: Any = None
    embeddings: tuple[tuple[float, ...], ...] = ()
    classifier: TinyEmbeddingClassifier | None = None
    recognition_mode: str = "embedding"


_STATS_WINDOW = 30


class PerceptionSystem:
    """Public perception facade for processing frames and serving tracker-ID observations.

    Construct with a detector exposing ``detect(image)``, a ``PersistentTracker``, and
    optionally an OpenCV camera. Camera/model dependencies remain lazy until first use.
    """

    def __init__(
        self,
        detector: Any,
        tracker: PersistentTracker,
        *,
        camera: OpenCVCameraReader | None = None,
        buffer: FrameBuffer | None = None,
        grounder: Grounder | None = None,
        grounder_config: GrounderConfig | None = None,
        open_vocab_hz: float = 0.5,
        vocabulary: Sequence[str] = (),
        event_bus: EventBus | None = None,
        ignore_regions: Sequence[Sequence[float]] = (),
    ) -> None:
        if isinstance(open_vocab_hz, bool) or not math.isfinite(open_vocab_hz) or open_vocab_hz <= 0:
            raise ValueError("open_vocab_hz must be a finite positive number")
        self.detector = detector
        self.tracker = tracker
        self.ignore_regions = tuple(tuple(region) for region in ignore_regions)
        self.camera = camera
        self.buffer = buffer if buffer is not None else FrameBuffer()
        self._lock = threading.RLock()
        self._event_bus = event_bus
        self._grounder = grounder
        self._grounder_config = grounder_config or GrounderConfig()
        self._pointer = GroundingPointer(
            self.buffer,
            tracker,
            grounder,
            grounder_config=self._grounder_config,
            state_lock=self._lock,
        )
        self.mode = "active"
        self._profile = _TargetProfile()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_error: BaseException | None = None
        self._camera_lock = threading.Lock()
        self._camera_state = "connecting" if camera is not None else "disabled"
        self._camera_reason = None if camera is not None else "perception is disabled"
        self._last_frame_at: float | None = None
        self._camera_operation: str | None = None
        self._camera_operation_started: float | None = None
        self._camera_watchdog_fired = False
        self._watchdog_thread: threading.Thread | None = None
        self._release_lock = threading.Lock()
        self._release_pending = False
        self._release_complete = threading.Event()
        self._release_complete.set()
        self._stats_lock = threading.Lock()
        self._frame_stats: deque[_FrameStats] = deque(maxlen=_STATS_WINDOW)
        self._vocabulary_lock = threading.Lock()
        self._vocabulary = _normalize_vocabulary(vocabulary)
        self._open_vocab_hz = float(open_vocab_hz)
        self._open_vocab_wake = threading.Event()
        self._open_vocab_thread: threading.Thread | None = None
        self._grounder_status_lock = threading.Lock()
        self._grounder_status: dict[str, Any] = {
            "status": "unavailable",
            "reason": "grounder is not configured" if grounder is None else "not queried yet",
            "last_latency_ms": None,
        }
        # Camera-relative motion history and region watches; region events go to the shared bus.
        self._scene = SceneMonitor(event_bus.publish if event_bus is not None else None)

    def process_frame(
        self,
        frame: Frame | Any,
        pose: Any = None,
        timestamp: float | None = None,
    ) -> FrameObservation:
        """Detect/track and buffer a ``Frame`` or raw OpenCV image, with optional metadata overrides."""
        with self._lock:
            if isinstance(frame, Frame):
                image = frame.image
                frame_id = frame.frame_id
                pose = frame.pose if pose is None else pose
                timestamp = frame.timestamp if timestamp is None else timestamp
            else:
                image = frame
                latest = self.buffer.latest()
                frame_id = latest.frame_id + 1 if latest is not None else 1
                timestamp = time.monotonic() if timestamp is None else timestamp
            processed = Frame(
                frame_id=int(frame_id),
                timestamp=float(timestamp),
                image=image,
                pose=pose,
            )
            started = time.perf_counter()
            detections = self.detector.detect(image) if self.mode == "active" else ()
            detected_count = len(detections)
            if self.ignore_regions and detections:
                height, width = image.shape[:2]
                detections = tuple(
                    detection
                    for detection in detections
                    if not _bbox_in_ignore_region(
                        detection.bbox,
                        width,
                        height,
                        self.ignore_regions,
                    )
                )
            detected = time.perf_counter()
            processed.candidates = self.tracker.update(
                tuple(_with_shirt_colour(detection, image) for detection in detections),
                processed,
            )
            finished = time.perf_counter()
            self.buffer.append(processed)
            shape = getattr(image, "shape", None)
            if shape is not None and len(shape) >= 2:
                self._scene.observe(
                    processed.frame_id,
                    processed.timestamp,
                    shape[1],
                    shape[0],
                    processed.candidates,
                    processed.pose,
                )
            with self._stats_lock:
                self._frame_stats.append(
                    _FrameStats(
                        finished_at=finished,
                        detector_ms=(detected - started) * 1000.0,
                        tracker_ms=(finished - detected) * 1000.0,
                        detections=detected_count,
                        ignored=detected_count - len(detections),
                        candidates=len(processed.candidates),
                    )
                )
            return FrameObservation(
                frame_id=processed.frame_id,
                timestamp=processed.timestamp,
                pose=processed.pose,
                candidates=processed.candidates,
                frame=processed,
            )

    def latest_observation(self) -> FrameObservation | None:
        """Return the newest buffered observation, including image and candidate metadata."""
        frame = self.buffer.latest()
        return _observation(frame) if frame is not None else None

    def list_candidates(self) -> list[dict[str, Any]]:
        """Return current candidates as dict-friendly records with stable integer IDs."""
        frame = self.buffer.latest()
        return [self._candidate_dict(candidate, frame) for candidate in frame.candidates] if frame else []

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        """Return a visible current candidate by integer tracker ID, or ``None``."""
        frame = self.buffer.latest()
        if frame is None:
            return None
        candidate = next((item for item in frame.candidates if item.id == int(candidate_id)), None)
        return self._candidate_dict(candidate, frame) if candidate is not None else None

    def candidate_motion(self, candidate_id: int) -> dict[str, Any]:
        """Recent normalized image motion of a tracker ID (camera-relative, never metric speed)."""
        return self._scene.candidate_motion(candidate_id)

    def watch_region(
        self,
        name: str,
        box: Sequence[float],
        *,
        class_name: str | None = None,
        dwell_s: float | None = None,
        absence_s: float = 1.0,
        ttl_s: float | None = None,
    ) -> dict[str, Any]:
        """Create/replace a fixed-camera image-region watch emitting region events on the bus."""
        return self._scene.watch_region(
            name, box, class_name=class_name, dwell_s=dwell_s, absence_s=absence_s, ttl_s=ttl_s
        )

    def list_region_watches(self) -> dict[str, Any]:
        """Active region watches with status, camera reference, occupancy and timers."""
        return self._scene.list_region_watches()

    def remove_region_watch(self, watch_id: str) -> dict[str, Any]:
        """Remove a region watch; unknown IDs raise ``LookupError``."""
        return self._scene.remove_region_watch(watch_id)

    def validate_candidate(
        self,
        candidate_id: int,
        *,
        max_age_seconds: float = 0.75,
        min_confidence: float = 0.0,
    ) -> dict[str, Any] | None:
        """Return a visible candidate only after fresh reference-embedding validation."""
        with self._lock:
            candidate = self.tracker.validate_candidate(
                int(candidate_id),
                max_age_seconds=max_age_seconds,
                min_confidence=min_confidence,
            )
            return self._candidate_dict(candidate) if candidate is not None else None

    def validate_candidate_failure(
        self,
        candidate_id: int,
        *,
        max_age_seconds: float = 0.75,
        min_confidence: float = 0.0,
    ) -> str | None:
        """Reason ``validate_candidate`` rejects this ID, or ``None`` when it validates.

        ``not_visible``, ``stale``, ``no_reference_appearance`` or ``appearance_mismatch``; the
        executor reports the rejected candidate's actual cause from this.
        """
        with self._lock:
            return self.tracker.validate_candidate_failure(
                int(candidate_id),
                max_age_seconds=max_age_seconds,
                min_confidence=min_confidence,
            )

    def validate_id(
        self,
        candidate_id: int,
        *,
        max_age_seconds: float = 0.75,
        min_confidence: float = 0.0,
    ) -> bool:
        """Check current visibility, freshness, and reference-appearance match before motion."""
        return (
            self.validate_candidate(
                candidate_id,
                max_age_seconds=max_age_seconds,
                min_confidence=min_confidence,
            )
            is not None
        )

    def reidentify_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        """Match the stored reference embedding to a fresh, visible candidate."""
        with self._lock:
            match = self.tracker.reidentify_candidate(int(candidate_id))
            if match is None:
                return None
            candidate, similarity = match
            return {
                "reference_id": int(candidate_id),
                "candidate": self._candidate_dict(candidate),
                "similarity": similarity,
                "similarity_threshold": self.tracker.config.revalidation_similarity_threshold,
            }

    def get_frame(self, frame_id: int | None = None) -> Frame | None:
        """Return the newest frame or a retained frame by ID; expired IDs raise ``StaleFrameError``."""
        if frame_id is None:
            return self.buffer.latest()
        return self.buffer.get(frame_id)

    def point(
        self,
        frame_id: int,
        description: str | Sequence[float] | None = None,
        *,
        box: Sequence[float] | None = None,
        compensate: bool = True,
    ) -> PointResult:
        """Resolve an old-frame description or box to a current revalidated candidate."""
        return self._pointer.point(
            frame_id,
            description,
            box=box,
            compensate=compensate,
        )

    def set_vocabulary(self, terms: list[str]) -> dict[str, Any]:
        """Replace up to sixteen open-vocabulary terms and wake the detection loop."""
        vocabulary = _normalize_vocabulary(terms)
        if len(vocabulary) > 16:
            raise ValueError("vocabulary may contain at most 16 distinct terms")
        with self._vocabulary_lock:
            changed = vocabulary != self._vocabulary
            self._vocabulary = vocabulary
        if changed and self._event_bus is not None:
            self._event_bus.publish(
                "vocabulary_changed",
                f"Open-vocabulary terms changed ({len(vocabulary)} terms)",
                terms=list(vocabulary),
            )
        self._open_vocab_wake.set()
        if vocabulary and self._grounder is not None and not self._stop_event.is_set():
            self._start_open_vocab_worker()
        return {"terms": list(vocabulary), "count": len(vocabulary)}

    def set_target_profile(self, profile: Any) -> dict[str, Any]:
        """Set embedding references and optionally train a tiny head on labeled examples.

        The new profile replaces the previous one only after every reference is validated and
        the head is trained; a rejected profile leaves the prior recognition state untouched.
        """
        reference_vectors: list[tuple[float, ...]] = []
        training_vectors: list[tuple[float, ...]] = []
        training_labels: list[str] = []

        if isinstance(profile, Mapping):
            raw_references = profile.get("reference_embeddings", profile.get("embedding"))
            if raw_references is not None:
                if _is_numeric_vector(raw_references):
                    raw_references = (raw_references,)
                for vector in raw_references:
                    normalized = _normalize_vector(vector)
                    if normalized is not None:
                        reference_vectors.append(normalized)

            examples = profile.get(
                "training_examples",
                profile.get(
                    "training_embeddings",
                    profile.get(
                        "labeled_reference_images",
                        profile.get("reference_images", profile.get("reference_crops", ())),
                    ),
                ),
            )
            if isinstance(examples, Mapping):
                examples = (examples,)
            elif isinstance(examples, (str, bytes)):
                raise ValueError("reference examples must be a sequence of image/vector samples")
            profile_labels = profile.get("labels")
            if profile_labels is not None:
                if not isinstance(profile_labels, Sequence) or isinstance(profile_labels, (str, bytes)):
                    raise ValueError("profile labels must be a sequence matching the reference examples")
                if len(profile_labels) != len(examples):
                    raise ValueError("profile labels must match the number of reference examples")

            for index, example in enumerate(examples or ()):
                label = None
                image_value = example
                bbox_value = None
                vector_value = None
                if isinstance(example, Mapping):
                    label = example.get("label")
                    image_value = example.get("image", example.get("image_base64", example.get("data")))
                    bbox_value = example.get("bbox")
                    vector_value = example.get("embedding")
                elif profile_labels is not None:
                    label = profile_labels[index]
                vector = _normalize_vector(vector_value) if vector_value is not None else None
                if vector is None and image_value is not None:
                    image = _decode_reference_image(image_value)
                    bbox = _reference_box(bbox_value, image)
                    if bbox is not None:
                        vector = self.tracker.encode(image, bbox)
                if vector is None:
                    continue
                reference_vectors.append(vector)
                if label is not None:
                    training_vectors.append(vector)
                    training_labels.append(str(label))
        elif _is_numeric_vector(profile):
            vector = _normalize_vector(profile)
            if vector is not None:
                reference_vectors.append(vector)
        else:
            raw_references = getattr(profile, "reference_embeddings", getattr(profile, "embedding", None))
            if raw_references is not None:
                if _is_numeric_vector(raw_references):
                    raw_references = (raw_references,)
                for vector in raw_references:
                    normalized = _normalize_vector(vector)
                    if normalized is not None:
                        reference_vectors.append(normalized)

        classifier: TinyEmbeddingClassifier | None = None
        if training_vectors:
            classifier = TinyEmbeddingClassifier()
            classifier.fit(training_vectors, training_labels)
        with self._lock:
            self._profile = _TargetProfile(profile, tuple(reference_vectors), classifier)
        return {
            "reference_count": len(reference_vectors),
            "training_example_count": len(training_vectors),
            "classifier_trained": classifier is not None,
            "target_class": _profile_value(profile, "target_class"),
        }

    def set_mode(self, name: str) -> str:
        """Select active/paused inference or embedding/tiny-head recognition."""
        selected = name.strip().lower()
        if selected in {"active", "normal"}:
            self.mode = "active"
            return self.mode
        if selected == "paused":
            self.mode = "paused"
            return self.mode
        if selected in {"embedding", "step_1"}:
            recognition_mode = "embedding"
        elif selected in {"classifier", "tiny_head", "step_2"}:
            recognition_mode = "classifier"
        else:
            raise ValueError("mode must be active, paused, embedding/step_1, or classifier/step_2")
        with self._lock:
            profile = self._profile
            if recognition_mode == "classifier" and (
                profile.classifier is None or not profile.classifier.fitted
            ):
                raise RuntimeError(
                    "train a labeled tiny embedding head with set_target_profile before selecting classifier mode"
                )
            self.mode = "active"
            self._profile = profile._replace(recognition_mode=recognition_mode)
        return recognition_mode

    def start(
        self,
        on_observation: Callable[[FrameObservation], Any] | None = None,
    ) -> "PerceptionSystem":
        """Start camera capture and its watchdog in background threads."""
        if self.camera is None:
            raise RuntimeError("start() requires an OpenCVCameraReader")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return self
            self._stop_event.clear()
            self._worker_error = None
            with self._camera_lock:
                self._camera_state = "connecting"
                self._camera_reason = None
                self._camera_operation = None
                self._camera_operation_started = None
                self._camera_watchdog_fired = False
            self._watchdog_thread = threading.Thread(
                target=self._watch_camera,
                name="horus-camera-watchdog",
                daemon=True,
            )
            self._worker = threading.Thread(
                target=self._run_camera,
                args=(on_observation,),
                name="horus-perception",
                daemon=True,
            )
            self._watchdog_thread.start()
            self._start_open_vocab_worker()
            self._worker.start()
        return self

    def close(self) -> None:
        """Stop camera and open-vocabulary processing without waiting indefinitely."""
        self._stop_event.set()
        self._open_vocab_wake.set()
        self._release_camera_async()
        threads = (self._worker, self._watchdog_thread, self._open_vocab_thread)
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        with self._camera_lock:
            self._camera_state = "disabled"
            self._camera_reason = "perception is stopped"
            self._camera_operation = None
            self._camera_operation_started = None
        with self._lock:
            if self._worker is not None and not self._worker.is_alive():
                self._worker = None
            if self._watchdog_thread is not None and not self._watchdog_thread.is_alive():
                self._watchdog_thread = None
            if self._open_vocab_thread is not None and not self._open_vocab_thread.is_alive():
                self._open_vocab_thread = None
    @property
    def worker_error(self) -> BaseException | None:
        """Background-loop failure, if any, for the owner to report or handle."""
        return self._worker_error

    def camera_status(self) -> dict[str, Any]:
        """Return capture state and monotonic age of the latest camera frame."""
        now = time.monotonic()
        with self._camera_lock:
            age = (
                None
                if self._last_frame_at is None
                else max(0.0, now - self._last_frame_at)
            )
            return {
                "camera": self._camera_state,
                "last_frame_age_s": age,
                "reason": self._camera_reason,
                "grounder": self._grounder_status_snapshot(),
            }

    def _grounder_status_snapshot(self) -> dict[str, Any]:
        with self._grounder_status_lock:
            return dict(self._grounder_status)

    def processing_stats(self) -> dict[str, Any]:
        """Median detector/tracker cost and recent grounder availability."""
        with self._stats_lock:
            samples = tuple(self._frame_stats)
        grounder_status = self._grounder_status_snapshot()
        if not samples:
            return {"frames": 0, "grounder": grounder_status}
        span = samples[-1].finished_at - samples[0].finished_at
        latest = samples[-1]
        return {
            "frames": len(samples),
            "fps": round((len(samples) - 1) / span, 2) if span > 0 else None,
            "detector_ms": round(statistics.median(s.detector_ms for s in samples), 1),
            "tracker_ms": round(statistics.median(s.tracker_ms for s in samples), 1),
            "detections": latest.detections,
            "ignored": latest.ignored,
            "candidates": latest.candidates,
            "grounder": grounder_status,
        }

    def _start_open_vocab_worker(self) -> None:
        if self._grounder is None or self._stop_event.is_set():
            return
        with self._vocabulary_lock:
            if not self._vocabulary:
                return
            if self._open_vocab_thread is not None and self._open_vocab_thread.is_alive():
                return
            self._open_vocab_thread = threading.Thread(
                target=self._run_open_vocab,
                name="horus-open-vocabulary",
                daemon=True,
            )
            self._open_vocab_thread.start()

    def _run_open_vocab(self) -> None:
        interval = 1.0 / self._open_vocab_hz
        next_run = 0.0
        while not self._stop_event.is_set():
            wait_s = max(0.0, next_run - time.monotonic())
            woke = self._open_vocab_wake.wait(wait_s) if wait_s else False
            self._open_vocab_wake.clear()
            if self._stop_event.is_set():
                return
            if woke:
                next_run = 0.0
            with self._vocabulary_lock:
                vocabulary = self._vocabulary
            if not vocabulary or self._grounder is None:
                next_run = time.monotonic() + 0.25
                continue
            if time.monotonic() < next_run:
                continue
            frame = self.buffer.latest()
            if frame is None:
                next_run = time.monotonic() + min(interval, 0.1)
                continue
            started = time.perf_counter()
            try:
                boxes = self._grounder.detect(
                    frame.image,
                    list(vocabulary),
                    box_threshold=self._grounder_config.box_threshold,
                    text_threshold=self._grounder_config.text_threshold,
                )
            except Exception as exc:
                latency_ms = (time.perf_counter() - started) * 1000.0
                self._set_grounder_status(
                    "unavailable",
                    f"{type(exc).__name__}: {exc}",
                    latency_ms,
                )
            else:
                latency_ms = (time.perf_counter() - started) * 1000.0
                self._set_grounder_status("ok", None, latency_ms)
                self._seed_open_vocab_boxes(frame.frame_id, boxes, vocabulary)
            next_run = time.monotonic() + interval

    def _set_grounder_status(
        self,
        status: str,
        reason: str | None,
        latency_ms: float,
    ) -> None:
        with self._grounder_status_lock:
            self._grounder_status = {
                "status": status,
                "reason": reason,
                "last_latency_ms": round(latency_ms, 1),
            }

    def _seed_open_vocab_boxes(
        self,
        frame_id: int,
        boxes: Sequence[GroundingBox | Mapping[str, Any]],
        vocabulary: Sequence[str],
    ) -> None:
        confirmed: set[int] = set()
        try:
            source_image = self.buffer.frames_after(frame_id)[0].image
        except StaleFrameError:
            return
        height, width = source_image.shape[:2]
        for raw_box in boxes:
            try:
                item = coerce_grounding_box(raw_box)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if item.confidence < self._grounder_config.box_threshold:
                continue
            # Propellers and other configured regions are never targets.
            if self.ignore_regions and _bbox_in_ignore_region(item.bbox, width, height, self.ignore_regions):
                continue
            label = item.label.strip() if item.label else ""
            if not label:
                label = vocabulary[0]
            try:
                compensator = BoxCompensator(item.bbox, self.buffer.frames_after(frame_id)[0])
            except (StaleFrameError, RuntimeError, ValueError):
                continue

            def commit(placed_box: Any, current_frame: Frame | None) -> Candidate | None:
                if placed_box is None or current_frame is None:
                    return None
                candidates = current_frame.candidates
                best = max(
                    candidates,
                    key=lambda candidate: box_overlap_score(placed_box, candidate.bbox),
                    default=None,
                )
                best_overlap = box_overlap_score(placed_box, best.bbox) if best is not None else 0.0
                if best is not None and best.source == "open_vocab" and best_overlap >= _SEED_CONFIRM_OVERLAP:
                    # Re-detected: snap the drifting CSRT seed back onto the grounder box.
                    try:
                        self.tracker.confirm_seed(best.id, placed_box, current_frame, confidence=item.confidence)
                    except (RuntimeError, ValueError):
                        return None
                    confirmed.add(best.id)
                    return None
                # Already tracked by the detector or a point seed: keep that ID.
                if best_overlap >= _SEED_DEDUPE_OVERLAP:
                    return None
                if sum(candidate.source != "detector" for candidate in candidates) >= _MAX_SEEDED_TRACKS:
                    return None
                try:
                    seeded = self.tracker.seed(
                        placed_box,
                        current_frame,
                        label=label,
                        confidence=item.confidence,
                        source="open_vocab",
                    )
                except (RuntimeError, ValueError):
                    return None
                confirmed.add(seeded.id)
                current_frame.candidates = self.tracker.current_candidates
                return seeded

            try:
                candidate = commit_on_latest(self.buffer, compensator, self._lock, commit)
            except StaleFrameError:
                continue
            if candidate is not None and self._event_bus is not None:
                self._event_bus.publish(
                    "object_detected",
                    f"Open-vocabulary object detected: {label}",
                    id=candidate.id,
                    label=label,
                    score=candidate.confidence,
                )
        with self._lock:
            if self.tracker.age_unconfirmed_seeds(confirmed, max_misses=_SEED_MAX_MISSES):
                latest = self.buffer.latest()
                if latest is not None:
                    latest.candidates = self.tracker.current_candidates

    def _set_camera_status(
        self,
        state: str,
        reason: str | None,
        frame_time: float | None = None,
    ) -> None:
        with self._camera_lock:
            self._camera_state = state
            self._camera_reason = reason
            if frame_time is not None:
                self._last_frame_at = frame_time

    def _begin_camera_operation(self, operation: str) -> None:
        with self._camera_lock:
            self._camera_operation = operation
            self._camera_operation_started = time.monotonic()
            self._camera_watchdog_fired = False

    def _end_camera_operation(self) -> bool:
        with self._camera_lock:
            timed_out = self._camera_watchdog_fired
            self._camera_operation = None
            self._camera_operation_started = None
            self._camera_watchdog_fired = False
        return timed_out

    def _camera_operation_timeout(self, operation: str) -> float:
        config = getattr(self.camera, "config", None)
        name = "open_timeout_s" if operation == "open" else "read_timeout_s"
        try:
            timeout = float(getattr(config, name, 5.0))
        except (TypeError, ValueError, OverflowError):
            return 5.0
        return timeout if math.isfinite(timeout) and timeout > 0 else 5.0

    def _release_camera_async(self) -> None:
        if self.camera is None:
            return
        with self._release_lock:
            if self._release_pending:
                return
            self._release_pending = True
            self._release_complete.clear()

        def release() -> None:
            try:
                self.camera.release()
            finally:
                with self._release_lock:
                    self._release_pending = False
                    self._release_complete.set()

        threading.Thread(
            target=release,
            name="horus-camera-release",
            daemon=True,
        ).start()

    def _wait_for_camera_release(self) -> bool:
        while not self._stop_event.is_set():
            with self._release_lock:
                if not self._release_pending:
                    return True
            self._release_complete.wait(0.1)
        return False

    def _watch_camera(self) -> None:
        while not self._stop_event.is_set():
            # The watchdog keeps running while capture stalls or backs off, so region-watch TTLs
            # expire on time even when no frame arrives and nobody queries the watches.
            self._scene.expire_watches()
            with self._camera_lock:
                operation = self._camera_operation
                started = self._camera_operation_started
            if operation is None or started is None:
                self._stop_event.wait(0.1)
                continue
            timeout = self._camera_operation_timeout(operation)
            interval = max(0.01, min(0.25, timeout / 4.0))
            if self._stop_event.wait(interval):
                return
            now = time.monotonic()
            should_release = False
            with self._camera_lock:
                if (
                    self._camera_operation == operation
                    and self._camera_operation_started == started
                    and not self._camera_watchdog_fired
                    and now - started >= timeout
                ):
                    self._camera_watchdog_fired = True
                    self._camera_state = "stalled"
                    self._camera_reason = (
                        f"camera {operation} timed out after {timeout:g} seconds"
                    )
                    should_release = True
            if should_release:
                self._release_camera_async()

    def _run_camera(
        self, callback: Callable[[FrameObservation], Any] | None
    ) -> None:
        backoff_s = 0.5
        while not self._stop_event.is_set():
            if not self._wait_for_camera_release():
                return
            self._set_camera_status("connecting", None)
            try:
                self._begin_camera_operation("open")
                timed_out = False
                try:
                    self.camera.open()
                finally:
                    timed_out = self._end_camera_operation()
                if timed_out:
                    timeout = self._camera_operation_timeout("open")
                    raise RuntimeError(f"camera open timed out after {timeout:g} seconds")
                while not self._stop_event.is_set():
                    self._begin_camera_operation("read")
                    timed_out = False
                    try:
                        frame = self.camera.read()
                    finally:
                        timed_out = self._end_camera_operation()
                    if self._stop_event.is_set():
                        return
                    if timed_out:
                        timeout = self._camera_operation_timeout("read")
                        raise RuntimeError(f"camera read timed out after {timeout:g} seconds")
                    if frame is None:
                        raise RuntimeError("camera source returned no frame")
                    timestamp = getattr(frame, "timestamp", time.monotonic())
                    self._set_camera_status("streaming", None, float(timestamp))
                    backoff_s = 0.5
                    observation = self.process_frame(frame)
                    if callback is not None:
                        callback(observation)
                return
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                self._worker_error = exc
                self._set_camera_status("stalled", str(exc))
                self._release_camera_async()
                if self._stop_event.wait(backoff_s):
                    return
                backoff_s = min(backoff_s * 2.0, 8.0)

    def _candidate_dict(self, candidate: Candidate, frame: Frame | None = None) -> dict[str, Any]:
        profile = self._profile
        result = candidate.to_dict()
        target = profile.value
        classifier = profile.classifier
        class_name = _profile_value(target, "class_name") if target is not None else None
        class_id = _profile_value(target, "class_id") if target is not None else None
        class_mismatch = (class_name is not None and candidate.class_name != class_name) or (
            class_id is not None and candidate.class_id != class_id
        )
        embedding = candidate.embedding
        needs_embedding = classifier is not None or (bool(profile.embeddings) and not class_mismatch)
        if embedding is None and needs_embedding and frame is not None:
            # Tracks are embedded only when created; score later frames from the shared per-frame cache.
            with self._lock:
                embedding = self.tracker.frame_embedding(candidate, frame.image)
        predicted_label: str | None = None
        recognition_confidence: float | None = None
        if classifier is not None and embedding is not None:
            predicted_label, recognition_confidence = classifier.predict(embedding)
            result["recognized_class"] = predicted_label
            result["recognition_confidence"] = recognition_confidence
        if target is not None:
            target_class = _profile_value(target, "target_class")
            if target_class is None and classifier is not None and class_name in classifier.labels:
                target_class = class_name
            if target_class is not None and classifier is not None:
                result["target_match_confidence"] = (
                    recognition_confidence if predicted_label == str(target_class) else 0.0
                )
            elif class_mismatch:
                result["target_match_confidence"] = 0.0
            elif classifier is None and profile.embeddings:
                # Missing appearance evidence scores 0.0, never the class-only 1.0.
                scores = [
                    score
                    for vector in profile.embeddings
                    if (score := cosine_similarity(vector, embedding)) is not None
                ]
                result["target_match_confidence"] = max(scores, default=0.0)
            elif class_name is not None or class_id is not None:
                result["target_match_confidence"] = 1.0
            else:
                result["target_match_confidence"] = None
        return result


class PerceptionEngine(PerceptionSystem):
    """Convenience facade with camera-first constructor and one-frame ``step()``."""

    def __init__(
        self,
        camera: OpenCVCameraReader,
        detector: Any,
        tracker: PersistentTracker,
        buffer: FrameBuffer | None = None,
    ) -> None:
        super().__init__(detector, tracker, camera=camera, buffer=buffer)

    def step(self) -> FrameObservation | None:
        """Read and process one camera frame; returns ``None`` at end-of-stream."""
        if self.camera is None:
            return None
        frame = self.camera.read()
        return self.process_frame(frame) if frame is not None else None


def _bbox_in_ignore_region(
    bbox: tuple[float, float, float, float],
    image_width: int,
    image_height: int,
    regions: Sequence[Sequence[float]],
) -> bool:
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        return False
    box_area = (x2 - x1) * (y2 - y1)
    for region_x1, region_y1, region_x2, region_y2 in regions:
        overlap_width = max(
            0.0,
            min(x2, region_x2 * image_width) - max(x1, region_x1 * image_width),
        )
        overlap_height = max(
            0.0,
            min(y2, region_y2 * image_height) - max(y1, region_y1 * image_height),
        )
        if overlap_width * overlap_height >= box_area * 0.5:
            return True
    return False

def _with_shirt_colour(detection: Detection, image: Any) -> Detection:
    if detection.class_name.casefold() != "person" or "shirt_colour" in detection.attributes:
        return detection
    classification = classify_shirt_colour(image, detection.bbox)
    if classification is None:
        return detection
    colour, confidence = classification
    attributes = dict(detection.attributes)
    attributes["shirt_colour"] = colour
    attributes["shirt_colour_confidence"] = confidence
    return Detection(
        bbox=detection.bbox,
        confidence=detection.confidence,
        class_name=detection.class_name,
        class_id=detection.class_id,
        attributes=attributes,
    )


def _observation(frame: Frame) -> FrameObservation:
    return FrameObservation(
        frame_id=frame.frame_id,
        timestamp=frame.timestamp,
        pose=frame.pose,
        candidates=frame.candidates,
        frame=frame,
    )


def _full_image_box(image: Any) -> tuple[float, float, float, float] | None:
    shape = getattr(image, "shape", None)
    if shape is None or len(shape) < 2:
        return None
    return 0.0, 0.0, float(shape[1]), float(shape[0])

def _decode_reference_image(value: Any) -> Any:
    if isinstance(value, Mapping):
        value = value.get("image", value.get("image_base64", value.get("data")))
    if isinstance(value, str):
        if value.startswith("data:"):
            _, separator, value = value.partition(",")
            if not separator:
                raise ValueError("reference image data URI has no base64 payload")
        try:
            value = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("reference image must be a base64-encoded image") from exc
    if isinstance(value, (bytes, bytearray, memoryview)):
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(value, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("reference image bytes are not a decodable image")
        return image
    if getattr(value, "shape", None) is None:
        raise ValueError("reference image must be an image array or base64-encoded image")
    return value


def _reference_box(value: Any, image: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return _full_image_box(image)
    if isinstance(value, Mapping):
        coords = (value.get("x1"), value.get("y1"), value.get("x2"), value.get("y2"))
    else:
        coords = value
    if len(coords) != 4:
        raise ValueError("reference bbox must contain x1, y1, x2, y2")
    box = tuple(float(coordinate) for coordinate in coords)
    if not all(math.isfinite(coordinate) for coordinate in box):
        raise ValueError("reference bbox coordinates must be finite")
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("reference bbox must have positive width and height")
    return box


def _profile_value(profile: Any, name: str) -> Any:
    return profile.get(name) if isinstance(profile, Mapping) else getattr(profile, name, None)


def _is_numeric_vector(values: Any) -> bool:
    if isinstance(values, (str, bytes)) or values is None:
        return False
    try:
        if len(values) == 0:
            return False
        float(values[0])
        return True
    except (IndexError, KeyError, TypeError, ValueError):
        return False

def _normalize_vector(values: Sequence[float]) -> tuple[float, ...] | None:
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError):
        return None
    if not vector or not all(math.isfinite(value) for value in vector):
        return None
    scale = max(abs(value) for value in vector)
    if scale == 0:
        return None
    scaled = tuple(value / scale for value in vector)
    norm = math.sqrt(sum(value * value for value in scaled))
    if scale <= 1e-12 / norm:
        return None
    return tuple(value / norm for value in scaled)


def _normalize_vocabulary(terms: Sequence[str]) -> tuple[str, ...]:
    if isinstance(terms, (str, bytes)) or not isinstance(terms, Sequence):
        raise ValueError("vocabulary must be a sequence of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if not isinstance(term, str):
            raise ValueError("vocabulary terms must be strings")
        value = term.strip()
        key = value.casefold()
        if value and key not in seen:
            normalized.append(value)
            seen.add(key)
    if len(normalized) > 16:
        raise ValueError("vocabulary may contain at most 16 distinct terms")
    return tuple(normalized)


_SEED_DEDUPE_OVERLAP = 0.5
_SEED_CONFIRM_OVERLAP = 0.3
# Grounder cycles an open-vocabulary seed may go unconfirmed before it is dropped (~6 s at 0.5 Hz).
_SEED_MAX_MISSES = 3
# Each seeded track costs one CSRT update (~18 ms on Koko) per processed frame.
_MAX_SEEDED_TRACKS = 6
