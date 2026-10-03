"""The one terrain surface shared by the rendered mesh, the physics collider and the oracles.

Forest ground is a regular height grid exported as a COLLADA triangle mesh (``terrain_dae``)
that Gazebo uses for both the visual and the collision. Each grid cell is split along its
lower-left to upper-right diagonal into triangles (a, b, c) and (a, c, d) where, for column
``i`` and row ``j``, a = (i, j), b = (i + 1, j), c = (i + 1, j + 1) and d = (i, j + 1).
Everything that places, moves or scores actors on the ground samples this piecewise-planar
surface, so the simulated feet, the physical contact surface and the rendered ground agree.

This module is simulation-only and standard-library only; heights may be nested sequences
or a 2D NumPy array (``heights[row][col]``, rows along +y).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

HEIGHT_DECIMALS = 4  # the COLLADA writer's precision; grids are rounded to it before export


@dataclass(frozen=True, slots=True)
class MapSpec:
    """The square map extent shared by the terrain grid, textures and the planners.

    ``half_m`` is the half extent (the map spans ``-half_m .. +half_m`` on both axes) and
    ``cell_m`` the terrain grid spacing in metres. ``grid_side`` grid nodes span one axis
    end to end, so the exported height grid is ``grid_side x grid_side``.
    """

    half_m: float
    cell_m: float

    def __post_init__(self) -> None:
        if self.half_m <= 0.0:
            raise ValueError(f"half_m must be positive, got {self.half_m!r}")
        if self.cell_m <= 0.0:
            raise ValueError(f"cell_m must be positive, got {self.cell_m!r}")

    @property
    def min(self) -> float:
        """World coordinate of the lower map edge (both axes)."""
        return -self.half_m

    @property
    def size(self) -> float:
        """Full side length of the map in metres."""
        return 2.0 * self.half_m

    @property
    def grid_side(self) -> int:
        """Number of terrain grid nodes along one axis."""
        return int(self.size / self.cell_m) + 1


CANONICAL_MAP = MapSpec(75.0, 2.0)      # the committed 150 m worlds
PROCEDURAL_MAP = MapSpec(600.0, 4.0)    # generated worlds: 1.2 km, 4 m terrain cells (301 x 301)


def quantize_rows(rows: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[round(float(value), HEIGHT_DECIMALS) for value in row] for row in rows]


class TriangulatedGrid:
    """Height of the triangulated terrain; flat ``z = 0`` when ``heights`` is None."""

    def __init__(self, heights: Any | None, origin_x: float = 0.0, origin_y: float = 0.0,
                 cell_m: float = 1.0) -> None:
        self.heights = heights
        self.origin_x = float(origin_x)
        self.origin_y = float(origin_y)
        self.cell_m = float(cell_m)
        if heights is not None:
            self.rows = len(heights)
            self.columns = len(heights[0])
            if self.rows < 1 or self.columns < 1:
                raise ValueError("terrain height grid must be non-empty")

    def _cell(self, x: float, y: float) -> tuple[float, float, float, float, float, float]:
        """(fx, fy, za, zb, zc, zd) of the cell under (x, y), clamped to the grid."""
        gx = min(max((x - self.origin_x) / self.cell_m, 0.0), self.columns - 1.0)
        gy = min(max((y - self.origin_y) / self.cell_m, 0.0), self.rows - 1.0)
        col = min(int(math.floor(gx)), max(0, self.columns - 2))
        row = min(int(math.floor(gy)), max(0, self.rows - 2))
        heights = self.heights
        col1 = min(col + 1, self.columns - 1)
        row1 = min(row + 1, self.rows - 1)
        return (gx - col, gy - row, float(heights[row][col]), float(heights[row][col1]),
                float(heights[row1][col1]), float(heights[row1][col]))

    def height(self, x: float, y: float) -> float:
        """Height of the triangle under (x, y); points beyond the grid clamp to its edge."""
        if self.heights is None:
            return 0.0
        fx, fy, za, zb, zc, zd = self._cell(x, y)
        if fx >= fy:  # triangle (a, b, c)
            return za + (zb - za) * fx + (zc - zb) * fy
        return za + (zd - za) * fy + (zc - zd) * fx  # triangle (a, c, d)

    def normal(self, x: float, y: float) -> tuple[float, float, float]:
        """Unit upward normal of the supporting triangle (on the diagonal, triangle a-b-c)."""
        if self.heights is None:
            return (0.0, 0.0, 1.0)
        fx, fy, za, zb, zc, zd = self._cell(x, y)
        if fx >= fy:
            dzdx, dzdy = (zb - za) / self.cell_m, (zc - zb) / self.cell_m
        else:
            dzdx, dzdy = (zc - zd) / self.cell_m, (zd - za) / self.cell_m
        norm = math.sqrt(dzdx * dzdx + dzdy * dzdy + 1.0)
        return (-dzdx / norm, -dzdy / norm, 1.0 / norm)

    def segment_clearance(self, a: Sequence[float], b: Sequence[float]) -> float:
        """Exact minimum height of the segment a→b above the surface (negative: it dips below).

        Along the segment both the surface and the segment are linear between the parameters
        where it crosses a grid line or a cell diagonal (and the clamped grid border), so the
        minimum is attained at one of those breakpoints or an endpoint.
        """
        if self.heights is None:
            return min(float(a[2]), float(b[2]))
        ax, ay = (a[0] - self.origin_x) / self.cell_m, (a[1] - self.origin_y) / self.cell_m
        bx, by = (b[0] - self.origin_x) / self.cell_m, (b[1] - self.origin_y) / self.cell_m
        breaks = {0.0, 1.0}
        for start, end in ((ax, bx), (ay, by), (ax - ay, bx - by)):
            if abs(end - start) < 1e-12:
                continue
            low, high = sorted((start, end))
            for k in range(math.ceil(low), math.floor(high) + 1):
                t = (k - start) / (end - start)
                if 0.0 < t < 1.0:
                    breaks.add(t)
        lowest = math.inf
        for t in breaks:
            x = a[0] + (b[0] - a[0]) * t
            y = a[1] + (b[1] - a[1]) * t
            z = a[2] + (b[2] - a[2]) * t
            lowest = min(lowest, z - self.height(x, y))
        return lowest
