"""Small pure-standard-library loft mesh builder shared by procedural actors and dressing.

Meshes are built in metres with per-vertex normals and UVs (u/v in texture repeats, one
``tile_m`` of surface per repeat). ``triangles()`` returns zero-based triangles ready for
``scenery.SceneryMesh`` (world coordinates) or an OBJ writer (committed asset meshes).
"""

from __future__ import annotations

import math

DEFAULT_TILE_M = 0.35


class LoftMesh:
    """Collects tubes, ellipsoids and boxes as quads/triangles with smooth vertex normals."""

    def __init__(self) -> None:
        self.positions: list[tuple[float, float, float]] = []
        self.uvs: list[tuple[float, float]] = []
        self.faces: list[tuple[int, ...]] = []

    # -- primitives ---------------------------------------------------------------
    def add_tube(self, points, radii, *, segments: int = 12, cap_start: bool = True,
                 cap_end: bool = True, tile_m: float = DEFAULT_TILE_M) -> None:
        """A lofted tube: circular/elliptical rings along a polyline, capped on request.

        ``radii[i]`` is the ring's half extent along the frame's side and up axes; frames are
        the tangent with a stable up reference so tubes can run in any direction.
        """
        count = len(points)
        tangents = []
        for index in range(count):
            before = points[max(0, index - 1)]
            after = points[min(count - 1, index + 1)]
            dx, dy, dz = after[0] - before[0], after[1] - before[1], after[2] - before[2]
            length = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
            tangents.append((dx / length, dy / length, dz / length))
        arc = [0.0]
        for index in range(1, count):
            arc.append(arc[-1] + math.dist(points[index - 1], points[index]))
        rings: list[list[int]] = []
        for index in range(count):
            tx, ty, tz = tangents[index]
            up = (0.0, 0.0, 1.0) if abs(tz) < 0.9 else (0.0, 1.0, 0.0)
            sx, sy, sz = up[1] * tz - up[2] * ty, up[2] * tx - up[0] * tz, up[0] * ty - up[1] * tx
            side_len = math.sqrt(sx * sx + sy * sy + sz * sz) or 1.0
            sx, sy, sz = sx / side_len, sy / side_len, sz / side_len
            ux, uy, uz = ty * sz - tz * sy, tz * sx - tx * sz, tx * sy - ty * sx
            rx, rz = radii[index]
            u = max(1.0, 2.0 * math.pi * max(rx, rz) / tile_m)
            v = arc[index] / tile_m
            ring = []
            for j in range(segments + 1):
                angle = 2.0 * math.pi * j / segments
                cos, sin = math.cos(angle), math.sin(angle)
                point = (points[index][0] + sx * rx * cos + ux * rz * sin,
                         points[index][1] + sy * rx * cos + uy * rz * sin,
                         points[index][2] + sz * rx * cos + uz * rz * sin)
                ring.append(self._vertex(point, (j / segments * u, v)))
            rings.append(ring)
        for index in range(count - 1):
            lower, upper = rings[index], rings[index + 1]
            for j in range(segments):
                self.faces.append((lower[j], lower[j + 1], upper[j + 1], upper[j]))
        if cap_start:
            self._cap(points[0], rings[0], (-tangents[0][0], -tangents[0][1], -tangents[0][2]))
        if cap_end:
            self._cap(points[-1], rings[-1], tangents[-1])

    def add_ellipsoid(self, center, radii, *, segments: int = 12, rings: int = 6,
                      tile_m: float = DEFAULT_TILE_M) -> None:
        """A smooth ellipsoid lofted as a tube of elliptical rings along local x."""
        points, section_radii = [], []
        for index in range(rings + 1):
            phi = -math.pi / 2 + math.pi * (index / rings)
            points.append((center[0] + radii[0] * math.sin(phi), center[1], center[2]))
            shrink = max(0.06, math.cos(phi))
            section_radii.append((radii[1] * shrink, radii[2] * shrink))
        self.add_tube(points, section_radii, segments=segments, tile_m=tile_m)

    def add_box(self, center, size, *, yaw: float = 0.0, tile_m: float = DEFAULT_TILE_M,
                texture_per_face: bool = True) -> None:
        """An axis/yaw-aligned box with outward per-face UVs."""
        hx, hy, hz = size[0] / 2, size[1] / 2, size[2] / 2
        cos, sin = math.cos(yaw), math.sin(yaw)

        def place(local):
            x, y, z = local
            return (center[0] + x * cos - y * sin, center[1] + x * sin + y * cos, center[2] + z)

        corners = {}
        for sx in (0, 1):
            for sy in (0, 1):
                for sz in (0, 1):
                    corners[(sx, sy, sz)] = place((-hx + size[0] * sx, -hy + size[1] * sy,
                                                   -hz + size[2] * sz))
        # (face normal axis, four corners in CCW order seen from outside, u/v extents)
        faces = (
            ((0, 0, -1), ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)), (size[0], size[1])),
            ((0, 0, 1), ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)), (size[0], size[1])),
            ((0, -1, 0), ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)), (size[0], size[2])),
            ((0, 1, 0), ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)), (size[0], size[2])),
            ((-1, 0, 0), ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)), (size[1], size[2])),
            ((1, 0, 0), ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)), (size[1], size[2])),
        )
        for outward, keys, (extent_u, extent_v) in faces:
            u_repeat = max(1.0, extent_u / tile_m) if texture_per_face else 1.0
            v_repeat = max(1.0, extent_v / tile_m) if texture_per_face else 1.0
            quad = ((0.0, 0.0), (u_repeat, 0.0), (u_repeat, v_repeat), (0.0, v_repeat))
            indices = [self._vertex(corners[key], uv) for key, uv in zip(keys, quad)]
            self._face(indices, outward)

    def add_quad(self, corners, *, outward, u_repeat: float = 1.0, v_repeat: float = 1.0) -> None:
        """One textured quad (four world-space corners, CCW seen from outside)."""
        uvs = ((0.0, 0.0), (u_repeat, 0.0), (u_repeat, v_repeat), (0.0, v_repeat))
        indices = [self._vertex(corner, uv) for corner, uv in zip(corners, uvs)]
        self._face(indices, outward)

    def add_triangle(self, corners, *, outward, u_repeat: float = 1.0, v_repeat: float = 1.0) -> None:
        """One textured triangle (three world-space corners, CCW seen from outside)."""
        uvs = ((0.0, 0.0), (u_repeat, 0.0), (u_repeat * 0.5, v_repeat))
        indices = [self._vertex(corner, uv) for corner, uv in zip(corners, uvs)]
        self._triangle(indices, outward)

    # -- internals ----------------------------------------------------------------
    def _face(self, indices, outward) -> None:
        ax, ay, az = self.positions[indices[0]]
        bx, by, bz = self.positions[indices[1]]
        cx, cy, cz = self.positions[indices[2]]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        if nx * outward[0] + ny * outward[1] + nz * outward[2] < 0.0:
            self.faces.append((indices[0], indices[3], indices[2], indices[1]))
        else:
            self.faces.append(tuple(indices))

    def _cap(self, center, ring, outward) -> None:
        v = self.uvs[ring[0]][1]
        center_index = self._vertex(center, (0.5, v))
        for j in range(len(ring) - 1):
            self._triangle((center_index, ring[j], ring[j + 1]), outward)

    def _triangle(self, indices, outward) -> None:
        a, b, c = indices
        ax, ay, az = self.positions[a]
        bx, by, bz = self.positions[b]
        cx, cy, cz = self.positions[c]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        if nx * outward[0] + ny * outward[1] + nz * outward[2] < 0.0:
            b, c = c, b
        self.faces.append((a, b, c))

    def _vertex(self, point, uv) -> int:
        self.positions.append(tuple(round(value, 6) for value in point))
        self.uvs.append((round(uv[0], 6), round(uv[1], 6)))
        return len(self.positions) - 1

    # -- output -------------------------------------------------------------------
    def bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        low = tuple(min(point[axis] for point in self.positions) for axis in range(3))
        high = tuple(max(point[axis] for point in self.positions) for axis in range(3))
        return low, high

    def triangles(self):
        """(positions, normals, uvs, triangles) with smooth accumulated vertex normals."""
        normals = [[0.0, 0.0, 0.0] for _ in self.positions]
        triangles: list[tuple[int, int, int]] = []
        for face in self.faces:
            indices = ((face,) if len(face) == 3
                       else ((face[0], face[1], face[2]), (face[0], face[2], face[3])))
            for a, b, c in indices:
                ax, ay, az = self.positions[a]
                bx, by, bz = self.positions[b]
                cx, cy, cz = self.positions[c]
                ux, uy, uz = bx - ax, by - ay, bz - az
                vx, vy, vz = cx - ax, cy - ay, cz - az
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                for index in (a, b, c):
                    normals[index][0] += nx
                    normals[index][1] += ny
                    normals[index][2] += nz
                triangles.append((a, b, c))
        out_normals = []
        for normal in normals:
            length = math.sqrt(sum(value * value for value in normal)) or 1.0
            out_normals.append(tuple(round(value / length, 6) for value in normal))
        return self.positions, out_normals, self.uvs, triangles

    def obj_text(self, name: str, mtl_name: str, material: str, texture: str | None,
                 header: str = "") -> tuple[str, str]:
        """(OBJ text, MTL text) for a committed asset mesh; texture is a file name."""
        positions, normals, uvs, triangles = self.triangles()
        lines = [f"# {header or name}", f"o {name}", f"mtllib {mtl_name}"]
        lines.extend(f"v {value:.6f} {value2:.6f} {value3:.6f}"
                     for value, value2, value3 in positions)
        lines.extend(f"vt {u:.6f} {v:.6f}" for u, v in uvs)
        lines.extend(f"vn {x:.6f} {y:.6f} {z:.6f}" for x, y, z in normals)
        lines.append(f"usemtl {material}")
        lines.extend(f"f {a + 1}/{a + 1}/{a + 1} {b + 1}/{b + 1}/{b + 1} {c + 1}/{c + 1}/{c + 1}"
                     for a, b, c in triangles)
        mtl = (f"# {header or name} material\nnewmtl {material}\nKa 0.2 0.2 0.2\nKd 0.72 0.72 0.72\n"
               f"Ks 0.02 0.02 0.02\nNs 8\n")
        if texture:
            mtl += f"map_Kd ../materials/textures/{texture}\n"
        return "\n".join(lines) + "\n", mtl


def rotate_xy(x: float, y: float, yaw: float) -> tuple[float, float]:
    """Rotate a 2D offset by ``yaw`` (SDF z rotation): the copy-paste-free local->world helper."""
    cos, sin = math.cos(yaw), math.sin(yaw)
    return x * cos - y * sin, x * sin + y * cos


__all__ = ["DEFAULT_TILE_M", "LoftMesh", "rotate_xy"]
