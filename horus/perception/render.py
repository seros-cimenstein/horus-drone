"""Render bounded-size JPEG frames and candidate tables for set-of-mark grounding."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Sequence

from .types import Frame, SetOfMarkPayload

_PERSON_COLOR = (80, 220, 40)
_OBJECT_COLOR = (0, 165, 255)
_OPEN_VOCAB_COLOR = (255, 0, 255)
_POINT_COLOR = (0, 255, 255)


def render_live_view(
    frame: Frame,
    stats: Mapping[str, Any],
    *,
    ignore_regions: Sequence[Sequence[float]] = (),
    detector_name: str = "YOLO",
) -> Any:
    """Draw every tracked candidate and detector/tracker timings on a BGR copy of the frame.

    Human-facing debug view: boxes carry tracker ID, detector class and confidence, and the
    shirt-colour attribute; shaded ignore regions show where detections are discarded.
    """
    import cv2

    image = frame.image
    if image is None or len(image.shape) != 3:
        raise ValueError("frame.image must be a BGR image array")
    rendered = image.copy()
    height, width = rendered.shape[:2]

    if ignore_regions:
        shaded = rendered.copy()
        for x1, y1, x2, y2 in ignore_regions:
            cv2.rectangle(
                shaded,
                (int(x1 * width), int(y1 * height)),
                (int(x2 * width), int(y2 * height)),
                (70, 70, 70),
                cv2.FILLED,
            )
        cv2.addWeighted(shaded, 0.45, rendered, 0.55, 0.0, dst=rendered)

    for candidate in frame.candidates:
        x1, y1, x2, y2 = (int(round(value)) for value in candidate.bbox)
        is_person = candidate.class_name.casefold() == "person"
        color = {
            "open_vocab": _OPEN_VOCAB_COLOR,
            "point": _POINT_COLOR,
        }.get(candidate.source, _PERSON_COLOR if is_person else _OBJECT_COLOR)
        cv2.rectangle(rendered, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
        lines = [
            f"#{candidate.id} {candidate.class_name} {candidate.confidence:.2f}",
            f"source {candidate.source}",
        ]
        shirt = candidate.attributes.get("shirt_colour")
        if shirt is not None:
            shirt_confidence = candidate.attributes.get("shirt_colour_confidence")
            lines.append(
                f"shirt {shirt} {shirt_confidence:.2f}"
                if isinstance(shirt_confidence, (int, float))
                else f"shirt {shirt}"
            )
        _label_block(cv2, rendered, lines, (x1, y1), color)

    fps = stats.get("fps")
    detector_ms = stats.get("detector_ms")
    tracker_ms = stats.get("tracker_ms")
    hud = [
        f"{detector_name} {_ms(detector_ms)} | track+ReID {_ms(tracker_ms)} | "
        f"{fps:.1f} fps" if isinstance(fps, (int, float)) else f"{detector_name} warming up",
        f"frame {frame.frame_id} | detections {stats.get('detections', 0)} "
        f"(ignored {stats.get('ignored', 0)}) | tracks {len(frame.candidates)}",
    ]
    cv2.rectangle(rendered, (0, height - 42), (width - 1, height - 1), (0, 0, 0), cv2.FILLED)
    for row, text in enumerate(hud):
        cv2.putText(
            rendered,
            text,
            (6, height - 26 + row * 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    return rendered


def _ms(value: Any) -> str:
    return f"{value:.0f} ms" if isinstance(value, (int, float)) else "-"


def _label_block(cv2: Any, image: Any, lines: Sequence[str], origin: tuple[int, int], color: Any) -> None:
    height, width = image.shape[:2]
    font, scale = cv2.FONT_HERSHEY_SIMPLEX, 0.5
    sizes = [cv2.getTextSize(line, font, scale, 1)[0] for line in lines]
    line_height = max(size[1] for size in sizes) + 6
    block_width = max(size[0] for size in sizes) + 6
    x = max(0, min(width - block_width, origin[0]))
    top = origin[1] - line_height * len(lines)
    if top < 0:
        top = min(height - line_height * len(lines), origin[1] + 2)
    cv2.rectangle(image, (x, top), (x + block_width, top + line_height * len(lines)), color, cv2.FILLED)
    for row, line in enumerate(lines):
        cv2.putText(
            image,
            line,
            (x + 3, top + line_height * (row + 1) - 4),
            font,
            scale,
            (0, 0, 0),
            1,
            cv2.LINE_AA,
        )


def render_set_of_marks(
    frame: Frame,
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
    for candidate in frame.candidates:
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
                "source": candidate.source,
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
