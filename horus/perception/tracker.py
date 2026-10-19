"""Persistent multi-object tracking with spatial association and appearance re-ID."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import replace
import logging
import math
import time
from typing import Any, Mapping, NamedTuple, Sequence

from .attributes import vote_shirt_colour
from .csrt import advance_csrt, initialize_csrt
from .embedders import ColorHistogramEmbedder, Embedder, cosine_similarity, normalize_embedding
from .geometry import coverage, crop_size, image_size, iou, validate_box
from .reid_log import ReacquisitionGate, ReidSimilarityLog
from .tracks import (
    FRESH_CANDIDATE_MAX_AGE_S,
    Track,
    TrackerConfig,
    center,
    classes_conflict,
    is_known_class,
    is_person_detection,
    normalized_center_distance,
)
from .types import BBox, Candidate, Detection, Embedding, Frame

_LOGGER = logging.getLogger(__name__)

# Track attributes set when a track is seeded that per-frame detections never carry.
_PROVENANCE_KEYS = ("grounder_span",)


class _Step(NamedTuple):
    """The processed frame and the clocks and geometry every update stage shares."""

    frame: Frame
    now: float
    arrival: float
    size: tuple[int, int] | None
    camera_yaw: float | None


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
        self._tracks: dict[int, Track] = {}
        # Expired tracks keep their reference appearance so a followed target that stays hidden
        # longer than max_lost_seconds can still be re-identified when it reappears (bounded, FIFO).
        self._retired: OrderedDict[int, Track] = OrderedDict()
        self._next_id = 1
        self._current: tuple[Candidate, ...] = ()
        self._current_image: Any = None
        self._embedding_cache: dict[tuple[int, int], Embedding | None] = {}
        self._cache_frame_id: int | None = None
        self._reid_log = ReidSimilarityLog(_LOGGER)
        self._reacquisition = ReacquisitionGate(self.config.reacquisition_confirm_s)

    def seed(
        self,
        bbox: BBox,
        frame: Frame,
        *,
        label: str,
        confidence: float,
        source: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> Candidate:
        """Create a CSRT-backed free-text track in a processed frame.

        ``attributes`` carries provenance for the caller (for example the grounder span a
        vocabulary seed came from). A seed never carries detector person evidence by itself:
        only a detector person detection or the person-only shirt-colour attribute grants that.
        """
        if source not in {"open_vocab", "point"}:
            raise ValueError("seed source must be 'open_vocab' or 'point'")
        class_name = str(label).strip()
        if not class_name:
            raise ValueError("seed label must not be empty")
        image = frame.image
        box = validate_box(bbox, image, require_image=True)
        tracker = initialize_csrt(image, box)
        track_id = self._next_id
        self._next_id += 1
        embedding = self.encode(image, box)
        track = Track(
            id=track_id,
            bbox=box,
            class_name=class_name,
            class_id=None,
            confidence=float(confidence),
            attributes=dict(attributes or {}),
            reference_embedding=embedding,
            current_embedding=embedding,
            arrival_monotonic=time.monotonic(),
            timestamp=float(frame.timestamp),
            frame_id=int(frame.frame_id),
            pose=frame.pose,
            source=source,
            csrt_tracker=tracker,
            frame_size=image_size(image),
        )
        self._tracks[track_id] = track
        if self._cache_frame_id != frame.frame_id:
            self._embedding_cache.clear()
            self._cache_frame_id = frame.frame_id
        self._current_image = image
        self._embedding_cache[(frame.frame_id, track_id)] = embedding
        candidate = track.candidate()
        self._current = (*self._current, candidate)
        return candidate

    def confirm_seed(
        self, track_id: int, bbox: BBox, frame: Frame, *, confidence: float, label: str | None = None
    ) -> bool:
        """Snap an open-vocabulary seed back onto a fresh grounder box and reset its miss count.

        ``label`` renames the seed (a generic ``object`` seed whose grounder score now passes the
        term-label floor); returns True when the label changed.
        """
        track = self._tracks.get(track_id)
        if track is None or track.source != "open_vocab":
            return False
        box = validate_box(bbox, frame.image, require_image=True)
        track.csrt_tracker = initialize_csrt(frame.image, box)
        track.bbox = box
        track.confidence = float(confidence)
        track.seed_misses = 0
        if label is None or label == track.class_name:
            return False
        track.class_name = label
        self._current = tuple(track.candidate() if item.id == track_id else item for item in self._current)
        return True

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

    def update(self, detections: Sequence[Detection], frame: Frame) -> tuple[Candidate, ...]:
        """Associate detections and advance seeded CSRT tracks in the current frame."""
        if frame.frame_id != self._cache_frame_id:
            self._embedding_cache.clear()
            self._cache_frame_id = frame.frame_id
        self._current_image = frame.image
        step = _Step(
            frame=frame,
            now=float(frame.timestamp),
            arrival=time.monotonic(),
            size=image_size(frame.image),
            camera_yaw=_camera_yaw_deg(frame.pose, self.config.max_heading_age_s),
        )
        self._expire_tracks(step.now)
        advanced_seed_ids = self._advance_seeds(step)
        self._rotate_into_heading(step)
        detection_track_ids: dict[int, int] = {}
        associated: set[int] = set()
        for _, track_id, index in self._score_associations(detections, step.now):
            if track_id in associated or index in detection_track_ids:
                continue
            self._apply_association(self._tracks[track_id], detections[index], step)
            associated.add(track_id)
            detection_track_ids[index] = track_id
        self._spawn_tracks(detections, detection_track_ids, step)
        candidates = [
            self._tracks[detection_track_ids[index]].candidate()
            for index in range(len(detections))
        ]
        candidates.extend(
            self._tracks[track_id].candidate()
            for track_id in sorted(advanced_seed_ids - associated)
        )
        self._current = tuple(candidates)
        return self._current

    def _expire_tracks(self, now: float) -> None:
        """Drop tracks unseen for ``max_lost_seconds``; keep their appearance for re-identification."""
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

    def _advance_seeds(self, step: _Step) -> set[int]:
        """Move seeded tracks with CSRT into this frame; returns the IDs that were advanced."""
        advanced: set[int] = set()
        for track_id, track in self._tracks.items():
            if track.source == "detector" or step.frame.frame_id <= track.frame_id:
                continue
            updated_box = advance_csrt(track.csrt_tracker, step.frame.image)
            if updated_box is None:
                continue
            old_center = center(track.bbox)
            new_center = center(updated_box)
            elapsed = max(1e-3, step.now - track.timestamp)
            track.velocity_x = (new_center[0] - old_center[0]) / elapsed
            track.velocity_y = (new_center[1] - old_center[1]) / elapsed
            track.bbox = updated_box
            _mark_seen(track, step)
            advanced.add(track_id)
        return advanced

    def _rotate_into_heading(self, step: _Step) -> None:
        """Re-express detector boxes in this frame's camera heading before association."""
        focal_px = _focal_px(step.size, self.config.camera_horizontal_fov_deg)
        if step.camera_yaw is None or focal_px is None or step.size is None:
            return
        for track in self._tracks.values():
            if track.source == "detector" and track.camera_yaw_deg is not None:
                track.bbox = _rotate_box(
                    track.bbox, step.camera_yaw - track.camera_yaw_deg, focal_px, step.size[0]
                )
            track.camera_yaw_deg = step.camera_yaw

    def _score_associations(
        self, detections: Sequence[Detection], now: float
    ) -> list[tuple[float, int, int]]:
        """Every admissible (spatial score, track ID, detection index) pair, best first."""
        associations: list[tuple[float, int, int]] = []
        for track_id, track in self._tracks.items():
            age = max(0.0, now - track.timestamp)
            predicted = track.predicted_box(age)
            for index, detection in enumerate(detections):
                overlap = self._association_overlap(track, predicted, detection, age)
                if overlap is None:
                    continue
                distance = normalized_center_distance(predicted, detection.bbox)
                spatial = min(1.0, overlap + (1.0 - min(distance, 1.0)) * 0.15)
                associations.append((spatial, track_id, index))
        associations.sort(reverse=True)
        return associations

    def _association_overlap(
        self, track: Track, predicted: BBox, detection: Detection, age: float
    ) -> float | None:
        """Overlap that may associate the detection with the track, or ``None`` when it may not."""
        if track.source != "detector":
            # A seeded track (open-vocabulary or point) takes its identity from the grounder or
            # the operator, so a detection may refresh it only when the boxes agree strongly; a
            # weak overlap would attach another person's detection and shirt colour to the seed.
            overlap = max(iou(predicted, detection.bbox), coverage(predicted, detection.bbox))
            return overlap if overlap >= self.config.revalidation_iou_threshold else None
        if classes_conflict(track.class_name, detection.class_name) or (
            track.class_id is not None
            and detection.class_id is not None
            and track.class_id != detection.class_id
        ):
            return None
        overlap = iou(predicted, detection.bbox)
        if overlap < self.config.iou_threshold:
            return None
        if age > self.config.revalidate_after_seconds and overlap < self.config.revalidation_iou_threshold:
            return None
        return overlap

    def _apply_association(self, track: Track, detection: Detection, step: _Step) -> None:
        """Refresh a track from its associated detection."""
        elapsed = max(1e-3, step.now - track.timestamp)
        old_center = center(track.bbox)
        new_center = center(detection.bbox)
        velocity_x = (new_center[0] - old_center[0]) / elapsed
        velocity_y = (new_center[1] - old_center[1]) / elapsed
        track.velocity_x = 0.5 * track.velocity_x + 0.5 * velocity_x
        track.velocity_y = 0.5 * track.velocity_y + 0.5 * velocity_y
        track.bbox = detection.bbox
        if track.source == "detector":
            track.class_name = detection.class_name
            track.class_id = detection.class_id
        track.confidence = detection.confidence
        if step.size is not None:
            track.frame_size = step.size
        # Seed provenance (the grounder span) belongs to the track, not to the frame.
        provenance = {
            key: track.attributes[key] for key in _PROVENANCE_KEYS if key in track.attributes
        }
        track.attributes = {
            **provenance,
            **self._vote_shirt_colour(detection.attributes, track.shirt_colour_votes),
        }
        _mark_seen(track, step)
        if is_person_detection(detection):
            track.person_evidence = True
        if track.source != "detector":
            try:
                track.csrt_tracker = initialize_csrt(step.frame.image, detection.bbox)
            except (RuntimeError, ValueError):
                pass

    def _spawn_tracks(
        self, detections: Sequence[Detection], detection_track_ids: dict[int, int], step: _Step
    ) -> None:
        """Start a track for every detection no existing track claimed."""
        for index, detection in enumerate(detections):
            if index in detection_track_ids:
                continue
            track_id = self._next_id
            self._next_id += 1
            embedding = self.encode(step.frame.image, detection.bbox)
            votes: deque[str] = deque()
            self._tracks[track_id] = Track(
                id=track_id,
                bbox=detection.bbox,
                class_name=detection.class_name,
                class_id=detection.class_id,
                confidence=detection.confidence,
                attributes=self._vote_shirt_colour(detection.attributes, votes),
                reference_embedding=embedding,
                current_embedding=embedding,
                arrival_monotonic=step.arrival,
                timestamp=step.now,
                frame_id=step.frame.frame_id,
                pose=step.frame.pose,
                person_evidence=is_person_detection(detection),
                shirt_colour_votes=votes,
                frame_size=step.size,
                camera_yaw_deg=step.camera_yaw,
            )
            self._embedding_cache[(step.frame.frame_id, track_id)] = embedding
            detection_track_ids[index] = track_id

    def _vote_shirt_colour(self, attributes: Any, votes: deque[str]) -> dict[str, Any]:
        return vote_shirt_colour(
            attributes,
            votes,
            window=self.config.shirt_colour_window,
            min_votes=self.config.shirt_colour_min_votes,
        )

    @property
    def current_candidates(self) -> tuple[Candidate, ...]:
        """Candidates visible in the most recently processed frame."""
        return self._current

    def encode(self, image: Any, bbox: BBox) -> Embedding | None:
        """Encode an image region with this tracker's appearance model as a unit vector."""
        vector = self.embedder.embed(image, bbox)
        return normalize_embedding(vector) if vector is not None else None

    def candidate(self, track_id: int) -> Candidate | None:
        """Return the currently visible candidate for an ID, or ``None`` if occluded."""
        return next((item for item in self._current if item.id == track_id), None)

    def _candidate_is_fresh(
        self,
        candidate: Candidate | None,
        track: Track | None,
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

    def _log_similarity(self, candidate: Candidate, **fields: Any) -> None:
        self._reid_log.record(
            candidate=candidate, crop_px=crop_size(self._current_image, candidate.bbox), **fields
        )

    def validate_candidate(
        self,
        track_id: int,
        *,
        max_age_seconds: float = FRESH_CANDIDATE_MAX_AGE_S,
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
        max_age_seconds: float = FRESH_CANDIDATE_MAX_AGE_S,
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

    def reference_embeddings(self, track_id: int) -> tuple[Embedding | None, ...]:
        """A track's reference gallery: the creation view, then admitted extras oldest first.

        Empty for an unknown track; the creation view is ``None`` when no embedder ran.
        """
        track = self._tracks.get(int(track_id))
        if track is None:
            return ()
        return (track.reference_embedding, *track.reference_views)

    def _match_reference_views(
        self, track: Track, embedding: Embedding | None
    ) -> tuple[float | None, int]:
        """Best similarity across the stored views and the index of the view that produced it.

        Index 0 is the creation view (``track.reference_embedding``); 1.. are admitted extra
        views. ``None`` when there is no reference appearance or no live embedding.
        """
        if track.reference_embedding is None or embedding is None:
            return None, 0
        best = cosine_similarity(track.reference_embedding, embedding)
        best_index = 0
        for index, view in enumerate(track.reference_views, start=1):
            similarity = cosine_similarity(view, embedding)
            if similarity is not None and (best is None or similarity > best):
                best, best_index = similarity, index
        return best, best_index

    def _crop_is_usable(self, candidate: Candidate) -> bool:
        """True when the candidate's crop (clamped to the frame) meets the minimum view size."""
        width_px, height_px = crop_size(self._current_image, candidate.bbox)
        return (
            width_px >= self.config.reference_view_min_width_px
            and height_px >= self.config.reference_view_min_height_px
        )

    def _admit_reference_view(
        self, track: Track, candidate: Candidate, live_embedding: Embedding | None
    ) -> bool:
        """Store a confirmed appearance view for a live, continuously associated track.

        Rules (all must hold; the caller guarantees the view passed the FULL threshold against the
        creation view, never on a grace-forgiven frame):
          1. the candidate is the track's own fresh candidate (guaranteed by the caller);
          2. it passed the full threshold against the creation view (caller);
          3. class gate: a differing known class is never stored;
          4. shirt-colour gate: a differing known colour is never stored;
          5. crop gate: the minimum view size (see _crop_is_usable);
          6. diversity: a view within ``reference_view_dedupe_cos`` of a stored one adds nothing;
          7. the gallery is capped at ``reference_views`` views in total; the oldest extra view is
             evicted first and the creation view is never evicted.
        """
        if live_embedding is None:
            return False
        limit = max(0, self.config.reference_views - 1)
        if limit == 0:
            return False
        if classes_conflict(track.class_name, candidate.class_name):
            return False
        reference_colour = track.attributes.get("shirt_colour")
        if reference_colour is not None and candidate.attributes.get("shirt_colour") != reference_colour:
            return False
        if not self._crop_is_usable(candidate):
            return False
        stored = (track.reference_embedding, *track.reference_views)
        for view in stored:
            similarity = cosine_similarity(view, live_embedding)
            if similarity is not None and similarity >= self.config.reference_view_dedupe_cos:
                return False
        track.reference_views.append(live_embedding)
        if len(track.reference_views) > limit:
            del track.reference_views[0]
        return True

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
        threshold = self.config.revalidation_similarity_threshold
        # The creation view decides admission; matching may use any stored view, so a marginal dip
        # against the creation view does not lose a correctly tracked, associated target.
        primary_similarity = cosine_similarity(track.reference_embedding, live_embedding)
        similarity, matched_view = self._match_reference_views(track, live_embedding)
        if log_similarity:
            self._log_similarity(
                candidate,
                mode="selected_validation",
                similarity=similarity,
                reference_id=track.id,
                threshold=threshold,
                primary_similarity=primary_similarity,
                matched_view=matched_view,
                view_count=1 + len(track.reference_views),
            )
        now = time.monotonic()
        if similarity is None or similarity < threshold:
            # A correctly tracked target's score wobbles across the threshold frame to frame, so
            # a dip within the configured margin is forgiven while the track is fresh and inside
            # the grace window after an accepted validation. A total appearance change (an
            # identity swap) fails immediately, a persistent dip expires the window, and
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
        if primary_similarity is not None and primary_similarity >= threshold:
            if self._admit_reference_view(track, candidate, live_embedding):
                _LOGGER.debug(
                    "track %d stored reference view %d (primary %.3f, gallery max %.3f)",
                    track.id, len(track.reference_views), primary_similarity, similarity,
                )
        return replace(candidate, embedding=live_embedding), None

    def reidentify_candidate(self, track_id: int) -> tuple[Candidate, float] | None:
        """Re-match a lost track to a fresh current candidate.

        Re-acquisition is anchored on the creation view alone (``reference_embedding``) at the
        full threshold; the multi-view gallery is deliberately not consulted, because a widened
        acceptance region admits same-colour look-alikes. A candidate needs the track's class
        (a generic label cannot re-acquire a known class), its shirt colour and a usable crop,
        and the same candidate id must pass on two distinct processed frames within
        ``reacquisition_confirm_s`` before the ID is rematched.
        """
        reference_id = int(track_id)
        track = self._tracks.get(reference_id) or self._retired.get(reference_id)
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
                max_age_seconds=FRESH_CANDIDATE_MAX_AGE_S,
                min_confidence=0.0,
            ):
                continue
            live_embedding = self._embed_candidate(candidate)
            similarity = cosine_similarity(track.reference_embedding, live_embedding)
            self._log_similarity(
                candidate,
                mode="reidentification",
                similarity=similarity,
                reference_id=track.id,
                threshold=threshold,
                view_count=1 + len(track.reference_views),
            )
            if (
                is_known_class(track.class_name)
                and candidate.class_name.casefold() != track.class_name.casefold()
            ):
                continue
            if (
                reference_colour is not None
                and candidate.attributes.get("shirt_colour") != reference_colour
            ):
                continue
            if not self._crop_is_usable(candidate):
                continue
            if (
                similarity is not None
                and similarity >= threshold
                and (best is None or similarity > best[1])
            ):
                best = (replace(candidate, embedding=live_embedding), similarity)
        if best is None:
            self._reacquisition.reset(reference_id)
            return None
        candidate, similarity = best
        if not self._reacquisition.confirm(reference_id, candidate.id, candidate.frame_id):
            _LOGGER.debug(
                "re-identification of %d as candidate %d is pending a second confirmed frame",
                reference_id, candidate.id,
            )
            return None
        return candidate, similarity


def _mark_seen(track: Track, step: _Step) -> None:
    """Stamp a track as observed in this frame; its live appearance is re-embedded lazily."""
    track.current_embedding = None
    track.arrival_monotonic = step.arrival
    track.timestamp = step.now
    track.frame_id = step.frame.frame_id
    track.pose = step.frame.pose
    track.hits += 1


def _camera_yaw_deg(pose: Any, max_age_s: float) -> float | None:
    """Fresh vehicle heading at frame capture (the camera's yaw in a level-gimbal airframe)."""
    if not isinstance(pose, Mapping):
        return None
    heading, age = pose.get("heading_deg"), pose.get("heading_age_s")
    if isinstance(heading, bool) or not isinstance(heading, (int, float)) or not math.isfinite(heading):
        return None
    if isinstance(age, (int, float)) and not isinstance(age, bool) and age > max_age_s:
        return None
    return float(heading)


def _focal_px(frame_size: tuple[int, int] | None, hfov_deg: float | None) -> float | None:
    if frame_size is None or hfov_deg is None:
        return None
    return frame_size[0] / (2.0 * math.tan(math.radians(hfov_deg) / 2.0))


def _rotate_box(box: BBox, yaw_change_deg: float, focal_px: float, width: int) -> BBox:
    """Re-express a box after the camera yawed by ``yaw_change_deg`` (clockwise positive).

    Pure rotation maps a column at angle a from the optical axis to a - Δψ, independent of depth;
    the box keeps its size. A box rotated out of the field of view keeps its last position.
    """
    delta = (yaw_change_deg + 180.0) % 360.0 - 180.0
    centre = (box[0] + box[2]) * 0.5
    angle = math.atan((centre - width * 0.5) / focal_px) - math.radians(delta)
    if abs(angle) >= math.radians(89.0):
        return box
    shift = width * 0.5 + focal_px * math.tan(angle) - centre
    return box[0] + shift, box[1], box[2] + shift, box[3]
