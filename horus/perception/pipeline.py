"""Integrated perception API joining frames, proposals, tracking, and latency recovery."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
import statistics
import threading
import time
from typing import Any, NamedTuple

from horus.events import EventBus
from .camera import CameraSupervisor, OpenCVCameraReader
from .frame_buffer import FrameBuffer
from .geometry import in_ignore_region
from .grounding import Grounder, GrounderConfig, GroundingPointer
from .open_vocab import OpenVocabSeeder
from .scene import SceneMonitor
from .tracker import PersistentTracker
from .types import Candidate, Frame, FrameObservation, PointResult


class _FrameStats(NamedTuple):
    finished_at: float
    detector_ms: float
    tracker_ms: float
    detections: int
    ignored: int
    candidates: int


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
        self.detector = detector
        self.tracker = tracker
        self.ignore_regions = tuple(tuple(region) for region in ignore_regions)
        self.camera = camera
        self.buffer = buffer if buffer is not None else FrameBuffer()
        self._lock = threading.RLock()
        grounder_config = grounder_config or GrounderConfig()
        self._pointer = GroundingPointer(
            self.buffer,
            tracker,
            grounder,
            grounder_config=grounder_config,
            state_lock=self._lock,
        )
        self.mode = "active"
        self._stop_event = threading.Event()
        # Camera-relative motion history and region watches; region events go to the shared bus.
        self._scene = SceneMonitor(event_bus.publish if event_bus is not None else None)
        # The watchdog keeps running while capture stalls or backs off, so region-watch TTLs
        # expire on time even when no frame arrives and nobody queries the watches.
        self.camera_supervisor = CameraSupervisor(
            camera,
            self.process_frame,
            stop_event=self._stop_event,
            on_watchdog_tick=self._scene.expire_watches,
        )
        self.open_vocab = OpenVocabSeeder(
            grounder,
            grounder_config,
            buffer=self.buffer,
            tracker=tracker,
            state_lock=self._lock,
            stop_event=self._stop_event,
            hz=open_vocab_hz,
            vocabulary=vocabulary,
            event_bus=event_bus,
            ignore_regions=self.ignore_regions,
        )
        self._stats_lock = threading.Lock()
        self._frame_stats: deque[_FrameStats] = deque(maxlen=_STATS_WINDOW)

    def process_frame(self, frame: Frame) -> FrameObservation:
        """Detect/track and buffer a camera ``Frame``."""
        with self._lock:
            image = frame.image
            processed = Frame(
                frame_id=frame.frame_id,
                timestamp=frame.timestamp,
                image=image,
                pose=frame.pose,
            )
            started = time.perf_counter()
            detections = self.detector.detect(image) if self.mode == "active" else ()
            detected_count = len(detections)
            if self.ignore_regions and detections:
                height, width = image.shape[:2]
                detections = tuple(
                    detection
                    for detection in detections
                    if not in_ignore_region(detection.bbox, width, height, self.ignore_regions)
                )
            detected = time.perf_counter()
            processed.candidates = self.tracker.update(detections, processed)
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
            return _observation(processed)

    def latest_observation(self) -> FrameObservation | None:
        """Return the newest buffered observation, including image and candidate metadata."""
        frame = self.buffer.latest()
        return _observation(frame) if frame is not None else None

    def list_candidates(self) -> list[dict[str, Any]]:
        """Return current candidates as dict-friendly records with stable integer IDs."""
        frame = self.buffer.latest()
        return [self._candidate_dict(candidate) for candidate in frame.candidates] if frame else []

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
        return self._pointer.point(frame_id, description, box=box, compensate=compensate)

    def set_vocabulary(self, terms: list[str]) -> dict[str, Any]:
        """Replace up to sixteen open-vocabulary terms and wake the detection loop."""
        return self.open_vocab.set_vocabulary(terms)

    def set_mode(self, name: str) -> str:
        """Run (``active``) or pause (``paused``) detection."""
        selected = name.strip().lower()
        if selected not in {"active", "paused"}:
            raise ValueError("mode must be active or paused")
        self.mode = selected
        return self.mode

    def start(
        self,
        on_observation: Callable[[FrameObservation], Any] | None = None,
    ) -> "PerceptionSystem":
        """Start camera capture and its watchdog in background threads."""
        if self.camera is None:
            raise RuntimeError("start() requires an OpenCVCameraReader")
        with self._lock:
            if self.camera_supervisor.running():
                return self
            self._stop_event.clear()
            self.camera_supervisor.start(on_observation)
            self.open_vocab.start()
        return self

    def close(self) -> None:
        """Stop camera and open-vocabulary processing without waiting indefinitely."""
        self._stop_event.set()
        self.open_vocab.wake()
        self.camera_supervisor.release_async()
        for thread in (*self.camera_supervisor.threads(), self.open_vocab.thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        with self._lock:
            self.camera_supervisor.stop()
            self.open_vocab.forget_finished_thread()

    def camera_status(self) -> dict[str, Any]:
        """Return capture state and monotonic age of the latest camera frame."""
        return {**self.camera_supervisor.status(), "grounder": self.open_vocab.status()}

    def processing_stats(self) -> dict[str, Any]:
        """Median detector/tracker cost and recent grounder availability."""
        with self._stats_lock:
            samples = tuple(self._frame_stats)
        grounder_status = self.open_vocab.status()
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

    def _candidate_dict(self, candidate: Candidate) -> dict[str, Any]:
        result = candidate.to_dict()
        # The candidate object is a per-frame snapshot; report the track's current gallery size so
        # a consumer sees views admitted after the frame was processed.
        result["reference_views"] = max(1, len(self.tracker.reference_embeddings(candidate.id)))
        return result


def _observation(frame: Frame) -> FrameObservation:
    return FrameObservation(
        frame_id=frame.frame_id,
        timestamp=frame.timestamp,
        pose=frame.pose,
        candidates=frame.candidates,
        frame=frame,
    )
