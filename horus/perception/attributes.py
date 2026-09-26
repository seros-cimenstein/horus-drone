"""Lightweight visual attributes derived from detector crops."""

from __future__ import annotations

import math
from typing import Any


def classify_shirt_colour(
    image: Any, bbox: tuple[float, float, float, float]
) -> tuple[str, float] | None:
    """Classify a person's torso as red, green, or blue from its dominant BGR hue."""
    try:
        import numpy as np
    except ImportError:
        return None
    if image is None or len(getattr(image, "shape", ())) != 3 or image.shape[2] < 3:
        return None
    height, width = int(image.shape[0]), int(image.shape[1])
    x1, y1, x2, y2 = map(float, bbox)
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
        return None

    box_width, box_height = x2 - x1, y2 - y1
    left = max(0, min(width, int(x1 + box_width * 0.2)))
    right = max(0, min(width, int(x2 - box_width * 0.2)))
    top = max(0, min(height, int(y1 + box_height * 0.2)))
    bottom = max(0, min(height, int(y1 + box_height * 0.6)))
    if right <= left or bottom <= top:
        return None

    pixels = np.asarray(image[top:bottom, left:right, :3], dtype=np.int16)
    blue, green, red = pixels.transpose(2, 0, 1)
    maximum = np.maximum(np.maximum(red, green), blue)
    minimum = np.minimum(np.minimum(red, green), blue)
    chromatic = (maximum >= 40) & ((maximum - minimum) >= 30)
    chromatic_count = int(chromatic.sum())
    if chromatic_count < max(8, int(chromatic.size * 0.08)):
        return None

    counts = {
        "blue": int((chromatic & (blue >= red * 1.3) & (blue >= green * 1.08)).sum()),
        "green": int((chromatic & (green >= red * 1.18) & (green >= blue * 1.05)).sum()),
        "red": int((chromatic & (red >= blue * 1.3) & (red >= green * 1.18)).sum()),
    }
    colour, count = max(counts.items(), key=lambda item: (item[1], item[0]))
    confidence = count / chromatic_count
    if confidence < 0.55:
        return None
    return colour, confidence
