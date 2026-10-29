"""Flight-safety helpers over generated worlds: the transit altitude and terrain heights.

Frames: Gazebo x east, y north, z up; HORUS NED north = y, east = x, up = z (home pad at the
origin).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

_Point = tuple[float, float, float]


def checked_transit_altitude(required: float, limit: float, *, procedural: bool) -> float:
    """The manifest transit altitude for a ``required`` minimum, refused above ``limit``.

    Generated worlds round up so the invariant transit >= tallest top + 5.9 m stays exact
    (``round`` could fall up to 5 mm short of it); canonical worlds keep ``round``.
    """
    if required > limit:
        raise ValueError(f"layout needs {required:.2f} m transit altitude, above {limit:.0f} m limit")
    if procedural:
        return math.ceil(required * 100.0) / 100.0
    return round(required, 2)


class Ground:
    """Terrain heights from a manifest's ground grid (flat when absent), on the same
    triangulated surface that Gazebo renders and collides with (``sim.worldgen.surface``)."""

    def __init__(self, manifest: dict[str, Any], manifest_path: Path | None = None) -> None:
        from sim.worldgen.surface import TriangulatedGrid

        ground = manifest.get("ground")
        self.surface = TriangulatedGrid(None)
        if ground:
            import numpy as np

            base = manifest_path.parent if manifest_path is not None else Path(".")
            rows = np.load(base / ground["file"])
            self.surface = TriangulatedGrid(rows, ground["origin_xy"][0], ground["origin_xy"][1],
                                            float(ground["cell_m"]))

    @classmethod
    def from_surface(cls, surface) -> "Ground":
        ground = cls({})
        ground.surface = surface
        return ground

    def height(self, x: float, y: float) -> float:
        return self.surface.height(x, y)

    def visible(self, a: _Point, b: _Point) -> bool:
        """True when the terrain does not cut the segment a→b (ignoring the last 0.3 m at b)."""
        length = math.dist(a, b)
        if length <= 0.3:
            return True
        t = 1.0 - 0.3 / length
        end = tuple(a[i] + (b[i] - a[i]) * t for i in range(3))
        return self.surface.segment_clearance(a, end) > 0.0
