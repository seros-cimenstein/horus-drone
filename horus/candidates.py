"""Readers for MCP candidate records (``list_candidates`` rows and set-of-mark tables).

Standard library only: commander-side scripts import this without the vision stack.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any


def candidate_bbox(candidate: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    """The ``bbox`` of a candidate record as finite ``(x1, y1, x2, y2)`` with x1 < x2 and y1 < y2.

    Accepts a four-number sequence or an ``x1/y1/x2/y2`` (or ``left/top/right/bottom``) mapping;
    anything else is ``None``.
    """
    value = candidate.get("bbox")
    if isinstance(value, Mapping):
        value = (
            value.get("x1", value.get("left")),
            value.get("y1", value.get("top")),
            value.get("x2", value.get("right")),
            value.get("y2", value.get("bottom")),
        )
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 4:
        return None
    try:
        box = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(item) for item in box) or box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box  # type: ignore[return-value]
