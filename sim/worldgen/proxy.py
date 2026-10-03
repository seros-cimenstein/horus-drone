"""Collision/oracle proxy shapes shared by the asset registry and the scenery tile writer.

A proxy is a bounded primitive (box, cylinder, sphere) that stands in for a visual mesh in both the
physics trimesh written for a scenery tile and the planner's collision oracle.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

Pose = tuple[float, float, float, float, float, float]
Scale = float | tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class ProxyShape:
    """A proxy primitive; ``pose`` is relative to its owner, sizes follow SDF conventions.

    box: (size_x, size_y, size_z); cylinder: (radius, length) along the local z axis;
    sphere: (radius,).
    """

    shape: str
    pose: Pose
    size: tuple[float, ...]

    def __post_init__(self) -> None:
        expected = {"box": 3, "cylinder": 2, "sphere": 1}.get(self.shape)
        if expected is None:
            raise ValueError(f"unsupported proxy shape {self.shape!r}")
        if len(self.size) != expected or any(value <= 0.0 for value in self.size):
            raise ValueError(f"{self.shape} proxy needs {expected} positive sizes, got {self.size!r}")
        if self.shape != "box" and (self.pose[3] or self.pose[4]):
            # Owners only yaw their instances, so round shapes stay upright; keep that invariant explicit.
            raise ValueError(f"{self.shape} proxies must be upright (roll and pitch 0)")

    def transformed(self, x: float, y: float, z: float, yaw: float, scale: Scale = 1.0) -> "ProxyShape":
        """This proxy placed by an owner pose (yaw only) and scale, in the owner's parent frame."""
        sx, sy, sz = (scale, scale, scale) if isinstance(scale, (int, float)) else scale
        px, py, pz = self.pose[0] * sx, self.pose[1] * sy, self.pose[2] * sz
        cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
        pose: Pose = (
            x + px * cos_yaw - py * sin_yaw,
            y + px * sin_yaw + py * cos_yaw,
            z + pz,
            self.pose[3],
            self.pose[4],
            self.pose[5] + yaw,
        )
        if self.shape == "box":
            size: tuple[float, ...] = (self.size[0] * sx, self.size[1] * sy, self.size[2] * sz)
        elif self.shape == "cylinder":
            if sx != sy:
                raise ValueError("cylinder proxies need equal x/y scale")
            size = (self.size[0] * sx, self.size[1] * sz)
        else:
            if not sx == sy == sz:
                raise ValueError("sphere proxies need uniform scale")
            size = (self.size[0] * sx,)
        return ProxyShape(self.shape, pose, size)
