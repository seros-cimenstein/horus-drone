"""Open-vocabulary seeding: ground configured free-text terms and keep them as tracker seeds."""

from __future__ import annotations

from collections.abc import Sequence
import threading
import time
from typing import Any

from horus.events import EventBus
from .csrt import BoxCompensator, commit_on_latest
from .frame_buffer import FrameBuffer
from .geometry import in_ignore_region, overlap_score
from .grounding import Grounder, GrounderConfig
from .tracker import PersistentTracker
from .types import Candidate, Frame, GroundingBox, StaleFrameError

MAX_VOCABULARY_TERMS = 16
_SEED_DEDUPE_OVERLAP = 0.5
_SEED_CONFIRM_OVERLAP = 0.3
# Grounder cycles an open-vocabulary seed may go unconfirmed before it is dropped (~6 s at 0.5 Hz).
_SEED_MAX_MISSES = 3
# Each seeded track costs one CSRT update (~18 ms on Koko) per processed frame.
_MAX_SEEDED_TRACKS = 6
# A grounder hit names its vocabulary term only at this score or above; weaker hits are kept as
# generic "object" seeds (grounder_span still shows what was matched). Over urban seed 21 the
# false person boxes on empty grass/road scored 0.30-0.37, just over the 0.30 grounder floor.
# A seed renames to its term (and is announced) once a later grounder cycle scores it this high.
TERM_LABEL_MIN_CONFIDENCE = 0.40
GENERIC_SEED_LABEL = "object"
# Poll interval while there is no vocabulary or no grounder.
_IDLE_POLL_S = 0.25


class OpenVocabSeeder:
    """Background grounder loop that turns vocabulary matches into ``open_vocab`` tracker seeds.

    A matched box is compensated from its source frame to the newest frame, then either snaps
    an existing open-vocabulary seed back onto the grounder box, is dropped as already tracked,
    or seeds a new track. Seeds the grounder stops confirming are dropped after a few cycles.
    """

    def __init__(
        self,
        grounder: Grounder | None,
        config: GrounderConfig,
        *,
        buffer: FrameBuffer,
        tracker: PersistentTracker,
        state_lock: Any,
        stop_event: threading.Event,
        hz: float,
        vocabulary: Sequence[str],
        event_bus: EventBus | None,
        ignore_regions: Sequence[Sequence[float]],
    ) -> None:
        self._grounder = grounder
        self._config = config
        self._buffer = buffer
        self._tracker = tracker
        self._state_lock = state_lock
        self._stop_event = stop_event
        self._interval_s = 1.0 / float(hz)
        self._event_bus = event_bus
        self._ignore_regions = ignore_regions
        self._vocabulary_lock = threading.Lock()
        self._vocabulary = normalize_vocabulary(vocabulary)
        self._wake = threading.Event()
        self.thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "status": "unavailable",
            "reason": "grounder is not configured" if grounder is None else "not queried yet",
            "last_latency_ms": None,
        }

    def status(self) -> dict[str, Any]:
        """Grounder availability and the latency of its last call."""
        with self._status_lock:
            return dict(self._status)

    def set_vocabulary(self, terms: Sequence[str]) -> dict[str, Any]:
        """Replace the terms, publish ``vocabulary_changed`` on a change, and wake the loop."""
        vocabulary = normalize_vocabulary(terms)
        with self._vocabulary_lock:
            changed = vocabulary != self._vocabulary
            self._vocabulary = vocabulary
        if changed and self._event_bus is not None:
            self._event_bus.publish(
                "vocabulary_changed",
                f"Open-vocabulary terms changed ({len(vocabulary)} terms)",
                terms=list(vocabulary),
            )
        self._wake.set()
        if vocabulary:
            self.start()
        return {"terms": list(vocabulary), "count": len(vocabulary)}

    def start(self) -> None:
        """Start the loop when a grounder and terms exist and perception is running."""
        if self._grounder is None or self._stop_event.is_set():
            return
        with self._vocabulary_lock:
            if not self._vocabulary or (self.thread is not None and self.thread.is_alive()):
                return
            self.thread = threading.Thread(target=self._run, name="horus-open-vocabulary", daemon=True)
            self.thread.start()

    def wake(self) -> None:
        self._wake.set()

    def forget_finished_thread(self) -> None:
        if self.thread is not None and not self.thread.is_alive():
            self.thread = None

    def _run(self) -> None:
        next_run = 0.0
        while not self._stop_event.is_set():
            wait_s = max(0.0, next_run - time.monotonic())
            woke = self._wake.wait(wait_s) if wait_s else False
            self._wake.clear()
            if self._stop_event.is_set():
                return
            if woke:
                next_run = 0.0
            with self._vocabulary_lock:
                vocabulary = self._vocabulary
            if not vocabulary or self._grounder is None:
                next_run = time.monotonic() + _IDLE_POLL_S
                continue
            if time.monotonic() < next_run:
                continue
            frame = self._buffer.latest()
            if frame is None:
                next_run = time.monotonic() + min(self._interval_s, 0.1)
                continue
            started = time.perf_counter()
            try:
                boxes = self._grounder.detect(
                    frame.image,
                    list(vocabulary),
                    box_threshold=self._config.box_threshold,
                    text_threshold=self._config.text_threshold,
                )
            except Exception as exc:
                self._set_status("unavailable", f"{type(exc).__name__}: {exc}", started)
            else:
                self._set_status("ok", None, started)
                self._seed_boxes(frame.frame_id, boxes, vocabulary)
            next_run = time.monotonic() + self._interval_s

    def _set_status(self, status: str, reason: str | None, started: float) -> None:
        latency_ms = (time.perf_counter() - started) * 1000.0
        with self._status_lock:
            self._status = {
                "status": status,
                "reason": reason,
                "last_latency_ms": round(latency_ms, 1),
            }

    def _seed_boxes(
        self, frame_id: int, boxes: Sequence[GroundingBox], vocabulary: Sequence[str]
    ) -> None:
        confirmed: set[int] = set()
        try:
            source_frame = self._buffer.frames_after(frame_id)[0]
        except StaleFrameError:
            return
        height, width = source_frame.image.shape[:2]
        for item in boxes:
            if item.confidence < self._config.box_threshold:
                continue
            # Propellers and other configured regions are never targets.
            if self._ignore_regions and in_ignore_region(item.bbox, width, height, self._ignore_regions):
                continue
            span = item.label.strip() if item.label else ""
            # The configured term names the seed for selection and display; the raw span stays on
            # the candidate so the commander can see what the grounder actually matched. A grounder
            # label never grants person evidence by itself.
            term = matched_vocabulary_term(span, vocabulary) or span or vocabulary[0]
            label = term if item.confidence >= TERM_LABEL_MIN_CONFIDENCE else GENERIC_SEED_LABEL
            try:
                compensator = BoxCompensator(item.bbox, self._buffer.frames_after(frame_id)[0])
            except (StaleFrameError, RuntimeError, ValueError):
                continue

            def commit(placed_box: Any, current_frame: Frame | None) -> Candidate | None:
                return self._commit_seed(placed_box, current_frame, item, label, span, confirmed)

            try:
                candidate = commit_on_latest(self._buffer, compensator, self._state_lock, commit)
            except StaleFrameError:
                continue
            if candidate is not None and candidate.class_name != GENERIC_SEED_LABEL and self._event_bus is not None:
                self._event_bus.publish(
                    "object_detected",
                    f"Open-vocabulary object detected: {label}",
                    id=candidate.id,
                    label=label,
                    span=span or label,
                    score=candidate.confidence,
                )
        with self._state_lock:
            if self._tracker.age_unconfirmed_seeds(confirmed, max_misses=_SEED_MAX_MISSES):
                latest = self._buffer.latest()
                if latest is not None:
                    latest.candidates = self._tracker.current_candidates

    def _commit_seed(
        self,
        placed_box: Any,
        current_frame: Frame | None,
        item: GroundingBox,
        label: str,
        span: str,
        confirmed: set[int],
    ) -> Candidate | None:
        """Confirm, skip or seed one compensated grounder box on the newest frame (under lock).

        Returns the candidate to announce: a new seed or a seed renamed to its term.
        """
        if placed_box is None or current_frame is None:
            return None
        candidates = current_frame.candidates
        best = max(
            candidates,
            key=lambda candidate: overlap_score(placed_box, candidate.bbox),
            default=None,
        )
        best_overlap = overlap_score(placed_box, best.bbox) if best is not None else 0.0
        if best is not None and best.source == "open_vocab" and best_overlap >= _SEED_CONFIRM_OVERLAP:
            # Re-detected: snap the drifting CSRT seed back onto the grounder box; a generic seed
            # takes its term once the grounder scores it above the label floor.
            try:
                renamed = self._tracker.confirm_seed(
                    best.id, placed_box, current_frame, confidence=item.confidence,
                    label=label if label != GENERIC_SEED_LABEL and best.class_name == GENERIC_SEED_LABEL else None,
                )
            except (RuntimeError, ValueError):
                return None
            confirmed.add(best.id)
            if not renamed:
                return None
            current_frame.candidates = self._tracker.current_candidates
            return self._tracker.candidate(best.id)
        # Already tracked by the detector or a point seed: keep that ID.
        if best_overlap >= _SEED_DEDUPE_OVERLAP:
            return None
        if sum(candidate.source != "detector" for candidate in candidates) >= _MAX_SEEDED_TRACKS:
            return None
        try:
            seeded = self._tracker.seed(
                placed_box,
                current_frame,
                label=label,
                confidence=item.confidence,
                source="open_vocab",
                attributes={"grounder_span": span} if span else None,
            )
        except (RuntimeError, ValueError):
            return None
        confirmed.add(seeded.id)
        current_frame.candidates = self._tracker.current_candidates
        return seeded


def normalize_vocabulary(terms: Sequence[str]) -> tuple[str, ...]:
    """Stripped, case-insensitively de-duplicated terms; at most ``MAX_VOCABULARY_TERMS``."""
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
    if len(normalized) > MAX_VOCABULARY_TERMS:
        raise ValueError(f"vocabulary may contain at most {MAX_VOCABULARY_TERMS} distinct terms")
    return tuple(normalized)


def matched_vocabulary_term(span: str, vocabulary: Sequence[str]) -> str | None:
    """Map a grounder span onto the configured term it came from, or ``None``.

    The grounder returns the matched text span, which with a multi-phrase vocabulary can repeat
    the term ("person person person") or be a fragment of it ('ground' from "person lying on the
    ground"). Content tokens (longer than three characters) are compared and the term sharing
    the most of them wins; ties go to the earlier vocabulary term.
    """
    span_tokens = {token for token in span.casefold().split() if len(token) > 3}
    if not span_tokens:
        return None
    best: tuple[int, int, str] | None = None
    for index, term in enumerate(vocabulary):
        term_tokens = {token for token in term.casefold().split() if len(token) > 3}
        shared = len(span_tokens & term_tokens)
        if shared and (best is None or shared > best[0]):
            best = (shared, -index, term)
    return None if best is None else best[2]
