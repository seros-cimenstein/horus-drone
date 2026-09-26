"""Render bounded-size JPEG frames and candidate tables for set-of-mark grounding."""

from __future__ import annotations

from typing import Sequence

from .types import Candidate, Frame, SetOfMarkPayload


def render_set_of_marks(
    frame: Frame,
    candidates: Sequence[Candidate] | None = None,
    *,
    max_dimension: int = 640,
    jpeg_quality: int = 82,
) -> SetOfMarkPayload:
    """Draw tracker IDs and return a ≤640px JPEG plus scaled candidate-box table."""
    if max_dimension < 1:
        raise ValueError("max_dimension must be positive")
    if not 0 <= jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be in [0, 100]")
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required to render set-of-mark JPEG frames") from exc

    candidates = frame.candidates if candidates is None else tuple(candidates)
    image = frame.image
    if image is None or len(image.shape) < 2:
        raise ValueError("frame.image must be a non-empty image array")
    source_height, source_width = int(image.shape[0]), int(image.shape[1])
    if source_width < 1 or source_height < 1:
        raise ValueError("frame.image must have positive dimensions")
    scale = min(1.0, max_dimension / max(source_width, source_height))
    width = max(1, int(round(source_width * scale)))
    height = max(1, int(round(source_height * scale)))
    if (width, height) == (source_width, source_height):
        rendered = image.copy()
    else:
        rendered = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)

    table: list[dict[str, object]] = []
    for candidate in candidates:
        x1, y1, x2, y2 = (
            int(round(coordinate * scale)) for coordinate in candidate.bbox
        )
        x1, x2 = max(0, min(width - 1, x1)), max(0, min(width - 1, x2))
        y1, y2 = max(0, min(height - 1, y1)), max(0, min(height - 1, y2))
        mark = str(candidate.id)
        color = (40, 220, 80)
        cv2.rectangle(rendered, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
        label = f"{mark} {candidate.confidence:.2f}"
        (text_width, text_height), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1
        )
        label_top = max(0, y1 - text_height - baseline - 5)
        cv2.rectangle(
            rendered,
            (x1, label_top),
            (min(width - 1, x1 + text_width + 6), min(height - 1, label_top + text_height + baseline + 5)),
            color,
            cv2.FILLED,
        )
        cv2.putText(
            rendered,
            label,
            (x1 + 3, min(height - 1, label_top + text_height + 1)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 0, 0),
            1,
            cv2.LINE_AA,
        )
        table.append(
            {
                "mark": candidate.id,
                "id": candidate.id,
                "bbox": [x1, y1, x2, y2],
                "class_name": candidate.class_name,
                "class_id": candidate.class_id,
                "attributes": dict(candidate.attributes),
                "confidence": candidate.confidence,
            }
        )

    ok, encoded = cv2.imencode(
        ".jpg", rendered, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
    )
    if not ok:
        raise RuntimeError("OpenCV failed to encode the set-of-mark JPEG")
    return SetOfMarkPayload(
        frame_id=frame.frame_id,
        timestamp=frame.timestamp,
        pose=frame.pose,
        image_jpeg=encoded.tobytes(),
        candidates=tuple(table),
        width=width,
        height=height,
    )
