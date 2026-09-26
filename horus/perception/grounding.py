"""Latency frame buffering and lazy text-grounding fallback with revalidation."""

from __future__ import annotations

from dataclasses import dataclass
import math
from threading import RLock
from typing import Any, Protocol, Sequence

from .tracker import PersistentTracker, box_iou, cosine_similarity
from .types import (
    BBox,
    Candidate,
    Frame,
    GroundingBox,
    NoGroundingMatch,
    PointResult,
    StaleFrameError,
)


class Grounder(Protocol):
    """Adapter for a text-conditioned box model."""

    def ground(self, image: Any, description: str) -> Sequence[GroundingBox]: ...


@dataclass(frozen=True, slots=True)
class GrounderConfig:
    """Hugging Face Grounding DINO or OWLv2 model selection and score gates."""

    backend: str = "grounding-dino"
    model_name: str | None = None
    box_threshold: float = 0.25
    text_threshold: float = 0.25
    device: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in {"grounding-dino", "owlv2"}:
            raise ValueError("backend must be 'grounding-dino' or 'owlv2'")
        if not 0.0 <= self.box_threshold <= 1.0 or not 0.0 <= self.text_threshold <= 1.0:
            raise ValueError("grounding thresholds must be in [0, 1]")


class FrameBuffer:
    """Thread-safe time- and count-bounded ring of frames and their tracked candidates."""

    def __init__(self, *, max_seconds: float = 3.0, max_frames: int = 90) -> None:
        if max_seconds <= 0 or max_frames < 1:
            raise ValueError("max_seconds and max_frames must be positive")
        self.max_seconds = float(max_seconds)
        self.max_frames = int(max_frames)
        self._frames: list[Frame] = []
        self._lock = RLock()

    def append(self, frame: Frame) -> None:
        """Retain a processed frame; IDs and timestamps must advance monotonically."""
        with self._lock:
            if self._frames:
                latest = self._frames[-1]
                if frame.frame_id <= latest.frame_id:
                    raise ValueError("frame_id must increase monotonically")
                if frame.timestamp < latest.timestamp:
                    raise ValueError("frame timestamps must not go backwards")
            self._frames.append(frame)
            cutoff = frame.timestamp - self.max_seconds
            while self._frames and (
                len(self._frames) > self.max_frames or self._frames[0].timestamp < cutoff
            ):
                self._frames.pop(0)

    def get(self, frame_id: int) -> Frame:
        """Get one retained frame or raise ``StaleFrameError`` when it has expired."""
        with self._lock:
            for frame in self._frames:
                if frame.frame_id == frame_id:
                    return frame
        raise StaleFrameError(f"frame {frame_id} is no longer in the perception buffer")

    def snapshot(self) -> tuple[Frame, ...]:
        """Return a stable chronological view of retained frame objects."""
        with self._lock:
            return tuple(self._frames)

    def latest(self) -> Frame | None:
        """Return the newest retained frame, if any."""
        with self._lock:
            return self._frames[-1] if self._frames else None

    def frames_after(self, frame_id: int) -> tuple[Frame, ...]:
        """Return the requested frame and all later frames in chronological order."""
        frames = self.snapshot()
        for index, frame in enumerate(frames):
            if frame.frame_id == frame_id:
                return frames[index:]
        raise StaleFrameError(f"frame {frame_id} is no longer in the perception buffer")

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)


class TransformersGrounder:
    """Load a Hugging Face Grounding DINO or OWLv2 model on first grounding request."""

    def __init__(self, config: GrounderConfig | None = None) -> None:
        self.config = config or GrounderConfig()
        self._processor: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device: str | None = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoProcessor
        except ImportError as exc:
            raise RuntimeError("Install horus-drone[grounding] for the optional text-grounding fallback") from exc
        model_name = self.config.model_name
        if self.config.backend == "grounding-dino":
            model_name = model_name or "IDEA-Research/grounding-dino-tiny"
            try:
                from transformers import AutoModelForZeroShotObjectDetection
            except ImportError as exc:
                raise RuntimeError("The installed transformers version lacks Grounding DINO") from exc
            model = AutoModelForZeroShotObjectDetection.from_pretrained(model_name)
        else:
            model_name = model_name or "google/owlv2-base-patch16-ensemble"
            try:
                from transformers import Owlv2ForObjectDetection
            except ImportError as exc:
                raise RuntimeError("The installed transformers version lacks OWLv2") from exc
            model = Owlv2ForObjectDetection.from_pretrained(model_name)
        device = self.config.device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._processor = AutoProcessor.from_pretrained(model_name)
        self._model = model.to(device).eval()
        self._torch = torch
        self._device = device

    def ground(self, image: Any, description: str) -> list[GroundingBox]:
        """Return description-conditioned boxes in image pixel coordinates."""
        if not description.strip():
            raise ValueError("description must not be empty")
        self._load()
        pil_image, height, width = _as_pil_rgb(image)
        if self.config.backend == "grounding-dino":
            inputs = self._processor(images=pil_image, text=description, return_tensors="pt")
        else:
            inputs = self._processor(
                images=pil_image, text=[[description]], return_tensors="pt"
            )
        inputs = {key: value.to(self._device) for key, value in inputs.items()}
        target_sizes = self._torch.tensor([[height, width]], device=self._device)
        with self._torch.inference_mode():
            outputs = self._model(**inputs)
            if self.config.backend == "grounding-dino":
                results = self._processor.post_process_grounded_object_detection(
                    outputs,
                    inputs.get("input_ids"),
                    box_threshold=self.config.box_threshold,
                    text_threshold=self.config.text_threshold,
                    target_sizes=target_sizes,
                )
            else:
                results = self._processor.post_process_object_detection(
                    outputs,
                    threshold=self.config.box_threshold,
                    target_sizes=target_sizes,
                )
        result = results[0]
        boxes = result.get("boxes", []).detach().cpu().tolist()
        scores = result.get("scores", []).detach().cpu().tolist()
        labels = result.get("text", result.get("labels", []))
        if hasattr(labels, "detach"):
            labels = labels.detach().cpu().tolist()
        return [
            GroundingBox(
                bbox=tuple(float(value) for value in box),
                confidence=float(score),
                label=str(labels[index]) if index < len(labels) else description,
            )
            for index, (box, score) in enumerate(zip(boxes, scores))
        ]


class GroundingPointer:
    """Resolve a buffered description or box to a current, revalidated track ID."""

    def __init__(
        self,
        buffer: FrameBuffer,
        tracker: PersistentTracker,
        grounder: Grounder | None = None,
        *,
        grounder_config: GrounderConfig | None = None,
        min_grounding_confidence: float = 0.2,
        reid_threshold: float = 0.72,
        overlap_threshold: float = 0.2,
    ) -> None:
        if not 0.0 <= min_grounding_confidence <= 1.0:
            raise ValueError("min_grounding_confidence must be in [0, 1]")
        if not 0.0 <= reid_threshold <= 1.0 or not 0.0 <= overlap_threshold <= 1.0:
            raise ValueError("matching thresholds must be in [0, 1]")
        self.buffer = buffer
        self.tracker = tracker
        self._grounder = grounder
        self.grounder_config = grounder_config or GrounderConfig()
        self.min_grounding_confidence = min_grounding_confidence
        self.reid_threshold = reid_threshold
        self.overlap_threshold = overlap_threshold

    def point(
        self,
        frame_id: int,
        description: str | Sequence[float] | None = None,
        *,
        box: Sequence[float] | None = None,
        compensate: bool = True,
    ) -> PointResult:
        """Resolve a buffered description or box, optionally replaying identity to now.

        ``compensate=False`` only accepts the candidate overlapping the old-frame
        reference when that same track ID is still visible in the latest frame.
        """
        frames = self.buffer.frames_after(frame_id)
        if not isinstance(compensate, bool):
            raise ValueError("compensate must be a boolean")
        if box is not None and description is not None:
            raise ValueError("provide either description or box, not both")
        reference_box = box
        description_text: str | None = None
        if isinstance(description, str):
            description_text = description
        elif description is not None:
            if box is not None:
                raise ValueError("provide either description or box, not both")
            reference_box = description
        old_frame = frames[0]
        if reference_box is None:
            if description_text is None or not description_text.strip():
                raise ValueError("description must not be empty")
            grounder = self._grounder
            if grounder is None:
                grounder = TransformersGrounder(self.grounder_config)
                self._grounder = grounder
            grounded_boxes = [
                _coerce_grounding_box(item)
                for item in grounder.ground(old_frame.image, description_text)
            ]
            grounded_boxes = [
                item for item in grounded_boxes
                if item.confidence >= self.min_grounding_confidence
            ]
            if not grounded_boxes:
                raise NoGroundingMatch(
                    f"grounder found no confident box for {description_text!r}"
                )
            grounded = max(grounded_boxes, key=lambda item: item.confidence)
        else:
            grounded = GroundingBox(
                bbox=_validate_reference_box(reference_box, old_frame.image),
                confidence=1.0,
                label="box",
            )

        latest_frame = frames[-1]
        latest_candidates = latest_frame.candidates
        if not latest_candidates:
            raise NoGroundingMatch("no tracked candidates are visible on the latest buffered frame")

        old_match = _best_overlapping(grounded.bbox, old_frame.candidates, self.overlap_threshold)
        if old_match is not None:
            old_score = _box_overlap_score(grounded.bbox, old_match.bbox)
            result = self._current_track_result(
                old_match.id,
                latest_candidates,
                grounded,
                match_confidence=old_score,
            )
            if result is not None:
                return result
        if not compensate:
            raise NoGroundingMatch(
                f"old-frame reference {grounded.bbox!r} has no matching current track "
                f"on frame {latest_frame.frame_id} with compensation disabled"
            )

        reference = self.tracker.encode(old_frame.image, grounded.bbox)
        if reference is not None:
            reidentified = self._fast_forward_reid(reference, frames, latest_candidates)
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
            result = self._current_track_result(
                track_id, latest_candidates, grounded, match_confidence=score
            )
            if result is not None:
                return result
        target = repr(description_text) if description_text is not None else repr(grounded.bbox)
        raise NoGroundingMatch(
            f"grounded target {target} could not be revalidated on frame {latest_frame.frame_id}"
        )

    def _fast_forward_reid(
        self,
        reference: Sequence[float],
        frames: Sequence[Frame],
        latest_candidates: Sequence[Candidate],
    ) -> tuple[Candidate, float] | None:
        matching_ids: set[int] = set()
        for frame in frames:
            for candidate in frame.candidates:
                similarity = cosine_similarity(reference, candidate.embedding)
                if similarity is not None and similarity >= self.reid_threshold:
                    matching_ids.add(candidate.id)
        best: tuple[Candidate, float] | None = None
        for candidate in latest_candidates:
            if candidate.id not in matching_ids:
                continue
            similarity = cosine_similarity(reference, candidate.embedding)
            if similarity is not None and similarity >= self.reid_threshold:
                if best is None or similarity > best[1]:
                    best = (candidate, similarity)
        return best

    def _fast_forward_geometry(
        self, grounded_box: BBox, frames: Sequence[Frame]
    ) -> tuple[int, float] | None:
        previous_box = grounded_box
        track_id: int | None = None
        confidence = 0.0
        for frame in frames[1:]:
            candidate = _best_overlapping(previous_box, frame.candidates, self.overlap_threshold)
            if candidate is None:
                continue
            track_id = candidate.id
            confidence = max(confidence, _box_overlap_score(previous_box, candidate.bbox))
            previous_box = candidate.bbox
        if track_id is None:
            candidate = _best_overlapping(grounded_box, frames[0].candidates, self.overlap_threshold)
            if candidate is not None:
                track_id = candidate.id
                confidence = _box_overlap_score(grounded_box, candidate.bbox)
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
        validated = self.tracker.validate_candidate(track_id)
        if candidate is None or validated is None:
            return None
        return PointResult(validated.frame_id, validated, grounded, match_confidence)


def _validate_reference_box(value: Sequence[float], image: Any = None) -> BBox:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 4:
        raise ValueError("box must contain exactly four finite numeric coordinates")
    if any(isinstance(item, (str, bytes, bool)) for item in value):
        raise ValueError("box must contain exactly four finite numeric coordinates")
    try:
        coordinates = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("box must contain exactly four finite numeric coordinates") from exc
    if not all(math.isfinite(item) for item in coordinates):
        raise ValueError("box coordinates must be finite")
    x1, y1, x2, y2 = coordinates
    if x1 >= x2 or y1 >= y2:
        raise ValueError("box coordinates must satisfy x1 < x2 and y1 < y2")

    shape = getattr(image, "shape", None)
    if shape is not None and len(shape) >= 2:
        height, width = float(shape[0]), float(shape[1])
    else:
        size = getattr(image, "size", None)
        if isinstance(size, Sequence) and len(size) == 2:
            width, height = float(size[0]), float(size[1])
        else:
            width = height = None
    if width is not None and height is not None and (
        x1 < 0 or y1 < 0 or x2 > width or y2 > height
    ):
        raise ValueError(
            f"box must lie within referenced frame bounds [0,{width:g}]x[0,{height:g}]"
        )
    return coordinates  # type: ignore[return-value]


def _as_pil_rgb(image: Any) -> tuple[Any, int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to prepare images for Hugging Face grounding") from exc
    if hasattr(image, "convert") and hasattr(image, "size"):
        rgb = image.convert("RGB")
        return rgb, int(rgb.height), int(rgb.width)
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("OpenCV and NumPy are required to prepare camera frames for grounding") from exc
    array = np.asarray(image)
    if array.ndim == 2:
        rgb_array = cv2.cvtColor(array, cv2.COLOR_GRAY2RGB)
    elif array.shape[2] == 4:
        rgb_array = cv2.cvtColor(array, cv2.COLOR_BGRA2RGB)
    else:
        rgb_array = cv2.cvtColor(array, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb_array), int(array.shape[0]), int(array.shape[1])


def _coerce_grounding_box(item: GroundingBox | dict[str, Any]) -> GroundingBox:
    if isinstance(item, GroundingBox):
        return item
    return GroundingBox(
        bbox=tuple(float(value) for value in item["bbox"]),
        confidence=float(item.get("confidence", item.get("score", 1.0))),
        label=str(item.get("label", "")),
    )


def _best_overlapping(
    box: BBox, candidates: Sequence[Candidate], threshold: float
) -> Candidate | None:
    best: tuple[Candidate, float] | None = None
    for candidate in candidates:
        score = _box_overlap_score(box, candidate.bbox)
        if best is None or score > best[1]:
            best = (candidate, score)
    return best[0] if best is not None and best[1] >= threshold else None


def _box_overlap_score(left: BBox, right: BBox) -> float:
    intersection = _intersection_area(left, right)
    if intersection <= 0:
        return 0.0
    union = _area(left) + _area(right) - intersection
    coverage = intersection / min(_area(left), _area(right)) if min(_area(left), _area(right)) > 0 else 0.0
    return max(box_iou(left, right), coverage)


def _intersection_area(left: BBox, right: BBox) -> float:
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _area(box: BBox) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _box_iou(left: BBox, right: BBox) -> float:
    return box_iou(left, right)
