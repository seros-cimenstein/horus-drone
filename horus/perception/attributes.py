"""Lightweight visual attributes derived from detector crops.

The shirt-colour attribute exists to give the commander and the re-identification checks
human-usable person evidence, so it must agree with the simulation's garment registry
(``sim/worldgen/appearance.py``: every fabric lists the classifier colours it may
plausibly produce). Achromatic and off-hue fabrics - white, light grey, grey, charcoal,
black, beige, khaki, sand, stone, brown, yellow, mustard - list *no* colour: a saturated
label on them is a false positive, and because the tracker keeps the first accepted
attribute for the life of a track, a single false positive mislabels that person for the
whole run. Missing a colour is the safe direction (the commander reports no match instead
of selecting the wrong actor).
"""

from __future__ import annotations

import math
from typing import Any

# Hue windows in degrees, fitted to the worldgen palette: red/crimson/scarlet/maroon/pink
# render near 345-18, orange and hi-vis orange 18-30, the green family 60-170 (olive and
# woodland green included), and the blue family 175-265 (navy and royal blue included).
_RED_HUE = ((345.0, 360.0), (0.0, 10.0))
_AMBER_HUE = (10.0, 18.0)
_ORANGE_HUE = (18.0, 30.0)
_GREEN_HUE = (60.0, 170.0)
_BLUE_HUE = (175.0, 265.0)

_SATURATION_FLOOR = 0.35
_VALUE_FLOOR = 0.15
# Warm hues are shared with skin, hair, wood and warm-lit neutrals, so they need real
# saturation and brightness. A red/orange garment render sits above 0.7; brown (0.58),
# khaki (0.33), beige (0.20) and warm-lit grey stay below.
_AMBER_SATURATION = 0.65
_ORANGE_SATURATION = 0.70
_WARM_VALUE_FLOOR = 0.40

_MIN_COLOURED_PIXELS = 12
_MIN_SHARE = 0.55
# A colour has to cover a real part of the torso band, not a patch inside a neutral
# garment (a bare arm, a backpack strap, or background showing through a loose box).
_MIN_BAND_SHARE = 0.35


def classify_shirt_colour(
    image: Any, bbox: tuple[float, float, float, float]
) -> tuple[str, float] | None:
    """Classify a person's torso as red, green, or blue, else return ``None``.

    Returns ``(colour, confidence)`` where ``confidence`` is the winning hue band's share
    of the torso's saturated pixels. ``None`` means indeterminate: too little saturated
    evidence, no supported hue, or a hue family that no garment can plausibly produce.
    """
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
    # Central torso band: inset from the box edges so background and limb pixels at the
    # box border (and the arms beside a small box) contribute as little as possible.
    left = max(0, min(width, int(x1 + box_width * 0.25)))
    right = max(0, min(width, int(x2 - box_width * 0.25)))
    top = max(0, min(height, int(y1 + box_height * 0.25)))
    bottom = max(0, min(height, int(y1 + box_height * 0.6)))
    if right <= left or bottom <= top:
        return None

    pixels = np.asarray(image[top:bottom, left:right, :3])
    hue, saturation, value = _bgr_to_hsv(pixels)
    coloured = (saturation >= _SATURATION_FLOOR) & (value >= _VALUE_FLOOR)
    coloured_count = int(coloured.sum())
    if coloured_count < _MIN_COLOURED_PIXELS:
        return None
    warm = coloured & (value >= _WARM_VALUE_FLOOR)
    amber_red = warm & (saturation >= _AMBER_SATURATION) & _in_hue_range(hue, (_AMBER_HUE,))
    # Orange and hi-vis orange tops read as red; the saturation floor keeps brown, khaki
    # and warm-lit neutrals out of the family.
    orange_red = warm & (saturation >= _ORANGE_SATURATION) & _in_hue_range(hue, (_ORANGE_HUE,))
    counts = {
        "red": int(((coloured & _in_hue_range(hue, _RED_HUE)) | amber_red | orange_red).sum()),
        "green": int((coloured & _in_hue_range(hue, (_GREEN_HUE,))).sum()),
        "blue": int((coloured & _in_hue_range(hue, (_BLUE_HUE,))).sum()),
    }
    colour, count = max(counts.items(), key=lambda item: (item[1], item[0]))
    if count < _MIN_COLOURED_PIXELS or count < _MIN_BAND_SHARE * int(coloured.size):
        return None
    confidence = count / coloured_count
    if confidence < _MIN_SHARE:
        return None
    return colour, confidence


def _bgr_to_hsv(pixels: Any) -> tuple[Any, Any, Any]:
    """Vectorised BGR -> (hue degrees, saturation, value) for a small crop, NumPy only."""
    import numpy as np

    values = pixels.astype(np.float64)
    blue, green, red = values[:, :, 0], values[:, :, 1], values[:, :, 2]
    maximum = np.maximum(np.maximum(red, green), blue)
    minimum = np.minimum(np.minimum(red, green), blue)
    delta = maximum - minimum
    safe = np.where(delta == 0.0, 1.0, delta)
    hue = np.zeros_like(maximum)
    red_max = (maximum == red) & (delta > 0.0)
    green_max = (maximum == green) & (delta > 0.0) & ~red_max
    blue_max = (maximum == blue) & (delta > 0.0) & ~red_max & ~green_max
    hue[red_max] = (60.0 * ((green - blue) / safe) % 360.0)[red_max]
    hue[green_max] = (60.0 * ((blue - red) / safe) + 120.0)[green_max]
    hue[blue_max] = (60.0 * ((red - green) / safe) + 240.0)[blue_max]
    saturation = np.where(maximum > 0.0, delta / np.where(maximum == 0.0, 1.0, maximum), 0.0)
    return hue, saturation, maximum / 255.0


def _in_hue_range(hue: Any, bands: tuple[tuple[float, float], ...]) -> Any:
    import numpy as np

    mask = np.zeros(hue.shape, dtype=bool)
    for low, high in bands:
        mask |= (hue >= low) & (hue < high)
    return mask
