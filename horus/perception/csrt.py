"""OpenCV CSRT box tracking: seeded tracks and old-frame box compensation."""

from __future__ import annotations

from typing import Any, Callable, Sequence

from .frame_buffer import FrameBuffer
from .geometry import validate_box
from .types import BBox, Frame


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


def initialize_csrt(image: Any, bbox: BBox) -> Any:
    """A CSRT tracker initialised on ``bbox`` in ``image``; ``RuntimeError`` when OpenCV refuses."""
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


def advance_csrt(tracker: Any, image: Any) -> BBox | None:
    """The tracker's box in ``image`` clipped to the frame, or ``None`` when tracking fails."""
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
        self.box: BBox | None = validate_box(bbox, frame.image, require_image=True)
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
                    self._tracker = initialize_csrt(self._image, self.box)
                except (RuntimeError, ValueError):
                    self.box = None
                    return None
                self._image = None
            self.box = advance_csrt(self._tracker, frame.image)
            self.frame_id = int(frame.frame_id)
        return self.box


def commit_on_latest(
    buffer: FrameBuffer,
    compensator: BoxCompensator,
    lock: Any,
    commit: Callable[[BBox | None, Frame | None], Any],
    *,
    rounds: int = 4,
) -> Any:
    """Carry a compensated box up to the newest frame and call ``commit(box, frame)`` under lock.

    Most catch-up happens outside the perception lock; frames cannot be appended while it is
    held, so the final step commits on the true latest frame instead of racing the camera.
    """
    for _ in range(rounds):
        compensator.advance(buffer.frames_after(compensator.frame_id)[1:])
        with lock:
            latest = buffer.latest()
            if latest is not None and latest.frame_id == compensator.frame_id:
                return commit(compensator.box, latest)
    with lock:
        compensator.advance(buffer.frames_after(compensator.frame_id)[1:])
        return commit(compensator.box, buffer.latest())
