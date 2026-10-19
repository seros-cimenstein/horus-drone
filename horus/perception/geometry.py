"""Pixel-box geometry shared by detection, tracking, grounding and recognition.

Boxes are ``(x1, y1, x2, y2)`` in source-image pixels.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from .types import BBox


def area(box: BBox) -> float:
    """Area of a box; inverted boxes have zero area."""
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def intersection(left: BBox, right: BBox) -> float:
    """Area shared by two boxes."""
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def iou(left: BBox, right: BBox) -> float:
    """Intersection over union."""
    shared = intersection(left, right)
    union = area(left) + area(right) - shared
    return shared / union if union > 0.0 else 0.0


def coverage(left: BBox, right: BBox) -> float:
    """Intersection over the smaller box's area: 1.0 when one box lies inside the other."""
    smaller = min(area(left), area(right))
    return intersection(left, right) / smaller if smaller > 0.0 else 0.0


def overlap_score(reference: BBox, candidate: BBox) -> float:
    """IoU, or the share of the reference box that lies inside the candidate if larger.

    Asymmetric on purpose: a torso box inside a person track matches that person, but a large
    cabinet box that merely contains a small person box does not claim the person's ID.
    """
    shared = intersection(reference, candidate)
    if shared <= 0:
        return 0.0
    reference_area = area(reference)
    covered = shared / reference_area if reference_area > 0 else 0.0
    return max(iou(reference, candidate), covered)


def in_ignore_region(
    bbox: BBox, width: int, height: int, regions: Sequence[Sequence[float]]
) -> bool:
    """True when at least half of the box lies inside one normalised ``(x1, y1, x2, y2)`` region."""
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return False
    half = area(bbox) * 0.5
    return any(
        intersection(bbox, (x1 * width, y1 * height, x2 * width, y2 * height)) >= half
        for x1, y1, x2, y2 in regions
    )


def image_size(image: Any) -> tuple[int, int] | None:
    """``(width, height)`` of an image array (or a PIL-style ``size``), else ``None``."""
    shape = getattr(image, "shape", None)
    try:
        if shape is not None and len(shape) >= 2:
            width, height = int(shape[1]), int(shape[0])
        else:
            size = getattr(image, "size", None)
            if not isinstance(size, Sequence) or len(size) != 2:
                return None
            width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def clamp_box(bbox: BBox, width: int, height: int) -> tuple[int, int, int, int]:
    """Integer crop window covering the box, clamped to the image; empty when x2 <= x1 or y2 <= y1."""
    return (
        max(0, min(width, math.floor(bbox[0]))),
        max(0, min(height, math.floor(bbox[1]))),
        max(0, min(width, math.ceil(bbox[2]))),
        max(0, min(height, math.ceil(bbox[3]))),
    )


def crop_size(image: Any, bbox: BBox) -> tuple[int, int]:
    """Pixel ``(width, height)`` of the box's crop inside the image; ``(0, 0)`` without an image."""
    size = image_size(image)
    if size is None:
        return 0, 0
    x1, y1, x2, y2 = clamp_box(bbox, *size)
    return max(0, x2 - x1), max(0, y2 - y1)


def validate_box(value: Any, image: Any = None, *, require_image: bool = False) -> BBox:
    """Four finite coordinates with x1 < x2 and y1 < y2, inside ``image`` when it has a size.

    Raises ``ValueError`` with the violated rule. ``require_image`` rejects a missing image.
    """
    try:
        if (
            isinstance(value, (str, bytes))
            or len(value) != 4
            or any(isinstance(item, (str, bytes, bool)) for item in value)
        ):
            raise ValueError
        box = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("box must contain exactly four finite numeric coordinates") from exc
    if not all(math.isfinite(item) for item in box):
        raise ValueError("box coordinates must be finite")
    x1, y1, x2, y2 = box
    if x1 >= x2 or y1 >= y2:
        raise ValueError("box coordinates must satisfy x1 < x2 and y1 < y2")
    size = image_size(image)
    if size is None:
        if require_image:
            raise ValueError("box frame must contain an image array")
        return box  # type: ignore[return-value]
    width, height = size
    if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
        raise ValueError(f"box must lie within frame bounds [0,{width:g}]x[0,{height:g}]")
    return box  # type: ignore[return-value]
