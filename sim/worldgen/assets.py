"""Registry of the imported third-party assets used by procedural scenery.

Every asset is a Fuel model imported by ``sim/scripts/import_licensed_assets.py`` (pinned
owner/name/version + SHA-256, PROVENANCE.txt beside the files). The registry is the single
source of truth for a scenery writer: mesh URIs and their local placement, real-world size,
collision/oracle proxies and licensing.

Frames and units
----------------
* ``AssetVisual.pose`` maps the mesh into the *instance* frame: z=0 is the ground the instance
  stands on and the mesh base is moved onto it; rotations are radians (roll, pitch, yaw) about
  the instance origin. ``AssetVisual.scale`` is the canonical real-world scale of the mesh
  (mesh units -> metres); a ``SceneryInstance`` scale multiplies it.
* ``Asset.proxies`` are ``ProxyShape``s in the same instance frame at instance scale 1, i.e.
  they already include the visual poses and canonical scales and are aligned with the posed
  mesh. Every proxy lies inside the measured mesh bounds; ``ProxyShape.transformed`` then
  places them for an instance.
* ``native_height_m`` and ``footprint_radius_m`` are measured from the mesh vertex bounds (see
  ``mesh_bounds``) after the visual poses and scales: the z extent of the posed mesh and the
  largest horizontal distance from the instance origin (the base-offset translation is ignored
  for the radius; it is vertical for every asset here).

Measured meshes (up axis, orientation, base offset)
---------------------------------------------------
All Fuel meshes are COLLADA ``Z_UP`` (the OBJ files are Z-up too) and are loaded by gz-common
with their ``<unit>`` scale applied; measurements below are therefore in metres with no
rotation fix needed. Base offsets (folded into ``AssetVisual.pose``): the boulder's mesh origin
sits at its centre (+0.6961 m), the seated dummy's origin is 0.6177 m below the mesh at scale 1
(it is modelled as if sitting on a 0.62 m seat, pose -1.0153 m), the bus origin is 0.0007 m
below its wheels and the oak origin 0.1065 m below its root flare. Orientation: the lamp arm
points -y, the bus and bench long axes run along y, and the seated person faces -y. Canonical
scales follow the upstream Fuel model where it declares one (lamp 3, bus 0.01) and otherwise
size the asset to a plausible real-world instance (pine 12 m, oak 10 m, seated person 1.2 m).

Regenerate the committed standalone ``model.sdf`` files with::

    python3 -c "from sim.worldgen.assets import write_model_sdfs; write_model_sdfs()"
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from sim.worldgen.proxy import ProxyShape, Pose
from sim.worldgen.sdf import box_geometry, cylinder_geometry, f

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "sim" / "models"

_MODEL_PREFIX = "model://"
_COLLADA_NS = "{http://www.collada.org/2005/11/COLLADASchema}"


@dataclass(frozen=True, slots=True)
class MeshBounds:
    """Axis-aligned mesh bounds plus the largest horizontal radius from the mesh z axis."""

    min: tuple[float, float, float]
    max: tuple[float, float, float]
    radius_m: float

    @property
    def size(self) -> tuple[float, float, float]:
        return tuple(self.max[axis] - self.min[axis] for axis in range(3))

    def merged(self, other: "MeshBounds") -> "MeshBounds":
        return MeshBounds(
            tuple(min(self.min[axis], other.min[axis]) for axis in range(3)),
            tuple(max(self.max[axis], other.max[axis]) for axis in range(3)),
            max(self.radius_m, other.radius_m),
        )


def _mat_translate(vector: tuple[float, float, float]) -> list[float]:
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, vector[0], vector[1], vector[2], 1]


def _mat_scale(vector: tuple[float, float, float]) -> list[float]:
    return [vector[0], 0, 0, 0, 0, vector[1], 0, 0, 0, 0, vector[2], 0, 0, 0, 0, 1]


def _mat_rotate(axis: tuple[float, float, float], degrees: float) -> list[float]:
    x, y, z = axis
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    rest = 1 - cos
    return [
        rest * x * x + cos, rest * x * y + sin * z, rest * x * z - sin * y, 0,
        rest * x * y - sin * z, rest * y * y + cos, rest * y * z + sin * x, 0,
        rest * x * z + sin * y, rest * y * z - sin * x, rest * z * z + cos, 0,
        0, 0, 0, 1,
    ]


def _mat_identity() -> list[float]:
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def _compose(left: list[float], right: list[float]) -> list[float]:
    """Column-major 4x4 product ``left * right`` (points are column vectors)."""
    return [sum(left[k * 4 + row] * right[column * 4 + k] for k in range(4))
            for column in range(4) for row in range(4)]


def _apply(matrix: list[float], point: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = point
    return (matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
            matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
            matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14])


def _node_matrix(node: ET.Element) -> list[float]:
    """Node transform the way gz-common's ColladaLoader reads it.

    A COLLADA ``<matrix>`` is stored row-major on disk and gz-common feeds the sixteen values
    straight into ``gz::math::Matrix4d::Set`` (row-major), so the on-disk rows become the
    matrix rows; the text is transposed into this module's column-major layout.
    """
    for child in node:
        if child.tag == f"{_COLLADA_NS}matrix":
            values = [float(value) for value in (child.text or "").split()]
            return [values[row * 4 + column] for column in range(4) for row in range(4)]
    matrix = _mat_identity()
    translate = node.find(f"{_COLLADA_NS}translate")
    if translate is not None:
        matrix = _compose(matrix, _mat_translate(tuple(float(v) for v in (translate.text or "").split())))
    for rotate in node.findall(f"{_COLLADA_NS}rotate"):
        values = [float(value) for value in (rotate.text or "").split()]
        matrix = _compose(matrix, _mat_rotate(tuple(values[:3]), values[3]))
    scale = node.find(f"{_COLLADA_NS}scale")
    if scale is not None:
        matrix = _compose(matrix, _mat_scale(tuple(float(v) for v in (scale.text or "").split())))
    return matrix


class _BoundsAccumulator:
    def __init__(self) -> None:
        self.min = [math.inf] * 3
        self.max = [-math.inf] * 3
        self.radius_m = 0.0

    def add(self, point: tuple[float, float, float]) -> None:
        for axis in range(3):
            self.min[axis] = min(self.min[axis], point[axis])
            self.max[axis] = max(self.max[axis], point[axis])
        self.radius_m = max(self.radius_m, math.hypot(point[0], point[1]))

    def bounds(self) -> MeshBounds:
        if self.radius_m == 0 and self.min[0] == math.inf:
            raise ValueError("mesh has no vertices")
        return MeshBounds(tuple(self.min), tuple(self.max), self.radius_m)


def _dae_submesh_bounds(path: Path) -> dict[str, MeshBounds]:
    """Per-submesh bounds of a COLLADA file, submesh names from the visual-scene node names."""
    root = ET.parse(path).getroot()
    unit = 1.0
    asset = root.find(f"{_COLLADA_NS}asset")
    if asset is not None:
        unit_element = asset.find(f"{_COLLADA_NS}unit")
        if unit_element is not None and "meter" in unit_element.attrib:
            unit = float(unit_element.attrib["meter"])
    arrays: dict[str, tuple[list[float], int | None]] = {}
    for source in root.iter(f"{_COLLADA_NS}source"):
        array = source.find(f"{_COLLADA_NS}float_array")
        if array is None or not array.text:
            continue
        count = None
        technique = source.find(f"{_COLLADA_NS}technique_common")
        if technique is not None:
            accessor = technique.find(f"{_COLLADA_NS}accessor")
            if accessor is not None:
                count = int(accessor.get("count"))
        arrays[source.get("id", "")] = ([float(value) for value in array.text.split()], count)
    vertices: dict[str, str] = {}
    for element in root.iter(f"{_COLLADA_NS}vertices"):
        for source_input in element.findall(f"{_COLLADA_NS}input"):
            if source_input.get("semantic") == "POSITION":
                vertices[element.get("id", "")] = source_input.get("source", "").lstrip("#")
    geometries = {geometry.get("id"): geometry for geometry in root.iter(f"{_COLLADA_NS}geometry")}
    accumulators: dict[str, _BoundsAccumulator] = {}

    def walk(node: ET.Element, parent: list[float]) -> None:
        matrix = _compose(parent, _node_matrix(node))
        name = node.get("name", "")
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "node":
                walk(child, matrix)
            elif tag == "instance_geometry":
                geometry = geometries.get(child.get("url", "").lstrip("#"))
                mesh = geometry.find(f"{_COLLADA_NS}mesh") if geometry is not None else None
                if mesh is None:
                    continue
                for primitive in mesh:
                    if primitive.tag.split("}")[-1] not in ("triangles", "polylist", "polygons",
                                                            "tristrips", "trifans"):
                        continue
                    for source_input in primitive.findall(f"{_COLLADA_NS}input"):
                        if source_input.get("semantic") != "VERTEX":
                            continue
                        source_id = source_input.get("source", "").lstrip("#")
                        source_id = vertices.get(source_id, source_id)
                        if source_id not in arrays:
                            continue
                        values, count = arrays[source_id]
                        accumulator = accumulators.setdefault(name, _BoundsAccumulator())
                        for index in range(count if count is not None else len(values) // 3):
                            point = _apply(matrix, tuple(values[index * 3:index * 3 + 3]))
                            accumulator.add((point[0] * unit, point[1] * unit, point[2] * unit))

    for scene in root.iter(f"{_COLLADA_NS}visual_scene"):
        for node in scene.findall(f"{_COLLADA_NS}node"):
            walk(node, _mat_identity())
    return {name: accumulator.bounds() for name, accumulator in accumulators.items()}


def _obj_bounds(path: Path) -> MeshBounds:
    accumulator = _BoundsAccumulator()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("v "):
            parts = line.split()
            accumulator.add((float(parts[1]), float(parts[2]), float(parts[3])))
    return accumulator.bounds()


def mesh_bounds(path: str | Path, submesh: str | None = None) -> MeshBounds:
    """Measured vertex bounds of a mesh file (optionally a single COLLADA submesh)."""
    path = Path(path)
    if path.suffix.lower() == ".obj":
        return _obj_bounds(path)
    submeshes = _dae_submesh_bounds(path)
    if submesh is None:
        merged = next(iter(submeshes.values()))
        for bounds in list(submeshes.values())[1:]:
            merged = merged.merged(bounds)
        return merged
    if submesh not in submeshes:
        raise KeyError(f"{path.name}: no submesh named {submesh!r}; have {sorted(submeshes)}")
    return submeshes[submesh]


def mesh_file(mesh_uri: str) -> Path:
    return MODELS / mesh_uri.removeprefix(_MODEL_PREFIX)


@dataclass(frozen=True, slots=True)
class AssetVisual:
    """One ``<visual>`` of an asset: mesh (or submesh), local pose, scale and material."""

    name: str
    mesh_uri: str
    pose: Pose = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    submesh: str | None = None
    scale: float = 1.0
    albedo_map: str | None = None
    normal_map: str | None = None
    double_sided: bool = False
    # sdformat defaults metalness to 1.0 (a raw metal), which renders black under a scene
    # without an environment map, so every albedo-mapped material states both values.
    metalness: float = 0.0
    roughness: float = 0.8


@dataclass(frozen=True, slots=True)
class Asset:
    key: str
    kind: str
    model_dir: str
    visuals: tuple[AssetVisual, ...]
    native_height_m: float
    footprint_radius_m: float
    proxies: tuple[ProxyShape, ...]
    license: str
    source: str


def _visual_bounds(visual: AssetVisual) -> MeshBounds:
    bounds = mesh_bounds(mesh_file(visual.mesh_uri), visual.submesh)
    roll, pitch, yaw = visual.pose[3:]
    if roll or pitch:
        raise ValueError(f"{visual.name}: only yaw-only visual poses are supported")
    cos, sin = math.cos(yaw), math.sin(yaw)

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point[0] * visual.scale, point[1] * visual.scale
        return (x * cos - y * sin + visual.pose[0], x * sin + y * cos + visual.pose[1])

    corners = [transform((x, y)) for x in (bounds.min[0], bounds.max[0])
               for y in (bounds.min[1], bounds.max[1])]
    z_min = bounds.min[2] * visual.scale + visual.pose[2]
    z_max = bounds.max[2] * visual.scale + visual.pose[2]
    # A yaw rotation about the instance origin and the (vertical) base offset leave the largest
    # horizontal vertex distance unchanged, so the measured radius only needs the scale.
    radius = bounds.radius_m * visual.scale
    return MeshBounds((min(p[0] for p in corners), min(p[1] for p in corners), z_min),
                      (max(p[0] for p in corners), max(p[1] for p in corners), z_max),
                      radius)


def asset_bounds(asset: Asset) -> MeshBounds:
    """Union of the asset's visual meshes as posed and scaled in the instance frame."""
    bounds = _visual_bounds(asset.visuals[0])
    for visual in asset.visuals[1:]:
        bounds = bounds.merged(_visual_bounds(visual))
    return bounds


def proxy_bounds(proxy: ProxyShape) -> MeshBounds:
    """Axis-aligned bounds of a proxy in the instance frame (yaw-only, as ProxyShape allows)."""
    x, y, z = proxy.pose[:3]
    if proxy.shape == "box":
        half = [size / 2 for size in proxy.size]
    elif proxy.shape == "cylinder":
        half = [proxy.size[0], proxy.size[0], proxy.size[1] / 2]
    else:
        half = [proxy.size[0], proxy.size[0], proxy.size[0]]
    cos, sin = math.cos(proxy.pose[5]), math.sin(proxy.pose[5])
    extent_x = abs(half[0] * cos) + abs(half[1] * sin)
    extent_y = abs(half[0] * sin) + abs(half[1] * cos)
    return MeshBounds((x - extent_x, y - extent_y, z - half[2]),
                      (x + extent_x, y + extent_y, z + half[2]),
                      max(math.hypot(x + dx, y + dy)
                          for dx in (-extent_x, extent_x) for dy in (-extent_y, extent_y)))


def _material_sdf(visual: AssetVisual, indent: str) -> str:
    if visual.albedo_map is None:
        return ""
    normal = f"<normal_map>{visual.normal_map}</normal_map>" if visual.normal_map else ""
    double_sided = "<double_sided>true</double_sided>" if visual.double_sided else ""
    # sdformat defaults the material diffuse colour to black and metalness to 1.0, so an
    # albedo-mapped material that omits them renders black; state both explicitly.
    return (f"{indent}<material><diffuse>1 1 1 1</diffuse>"
            f"<pbr><metal><albedo_map>{visual.albedo_map}</albedo_map>{normal}"
            f"<metalness>{f(visual.metalness)}</metalness><roughness>{f(visual.roughness)}</roughness>"
            f"</metal></pbr>{double_sided}</material>\n")


def model_sdf(asset: Asset) -> str:
    """Standalone ``model.sdf`` for one asset: static link, mesh visuals, primitive collisions."""
    body = []
    for visual in asset.visuals:
        scale = f"<scale>{f(visual.scale)} {f(visual.scale)} {f(visual.scale)}</scale>" if visual.scale != 1.0 else ""
        submesh = f"<submesh><name>{visual.submesh}</name></submesh>" if visual.submesh else ""
        body.append(
            f'      <visual name="{visual.name}">\n'
            f'        <pose>{f(visual.pose[0])} {f(visual.pose[1])} {f(visual.pose[2])} 0 0 0</pose>\n'
            f'        <geometry><mesh><uri>{visual.mesh_uri}</uri>{scale}{submesh}</mesh></geometry>\n'
            f'{_material_sdf(visual, "        ")}'
            f'      </visual>\n'
        )
    for index, proxy in enumerate(asset.proxies):
        geometry = {"box": lambda: box_geometry(*proxy.size),
                    "cylinder": lambda: cylinder_geometry(*proxy.size),
                    "sphere": lambda: f"<sphere><radius>{f(proxy.size[0])}</radius></sphere>"}[proxy.shape]()
        body.append(
            f'      <collision name="proxy_{index}_{proxy.shape}">\n'
            f'        <pose>{f(proxy.pose[0])} {f(proxy.pose[1])} {f(proxy.pose[2])} '
            f'{f(proxy.pose[3])} {f(proxy.pose[4])} {f(proxy.pose[5])}</pose>\n'
            f'        <geometry>{geometry}</geometry>\n'
            f'      </collision>\n'
        )
    return (f'<?xml version="1.0"?>\n<sdf version="1.9">\n  <model name="{asset.model_dir}">\n'
            f'    <static>true</static>\n    <link name="link">\n'
            + "".join(body)
            + '    </link>\n  </model>\n</sdf>\n')


def write_model_sdfs() -> list[str]:
    """Write the committed standalone ``model.sdf`` of every registered asset."""
    written = []
    for asset in ASSETS.values():
        path = MODELS / asset.model_dir / "model.sdf"
        if not path.parent.is_dir():
            raise FileNotFoundError(f"{asset.key}: {path.parent} is not imported")
        path.write_text(model_sdf(asset), encoding="utf-8")
        written.append(str(path.relative_to(ROOT)))
    return written


def _texture(model_dir: str, name: str) -> str:
    return f"{_MODEL_PREFIX}{model_dir}/materials/textures/{name}"


def _mesh(model_dir: str, name: str) -> str:
    return f"{_MODEL_PREFIX}{model_dir}/meshes/{name}"


def _fuel_source(owner_name: str, version: int) -> str:
    owner, name = owner_name.split("/", 1)
    url = f"https://fuel.gazebosim.org/1.0/{owner}/models/{name.replace(' ', '%20')}/{version}"
    return f"Gazebo Fuel {owner_name} v{version} ({url})"


CC0 = "CC0 1.0"
CC_BY = "CC BY 4.0"

ASSETS: dict[str, Asset] = {
    "tree_pine": Asset(
        key="tree_pine", kind="tree", model_dir="horus_tree_pine",
        # One material-free visual: the importer embeds the branch (alpha) and bark textures in
        # the COLLADA effects, so every instance shares the mesh materials. Per-visual SDF
        # materials on ~15k branch/bark visuals exhausted Koko's iGPU (RTF 0.008 -> 0.44).
        visuals=(AssetVisual("tree", _mesh("horus_tree_pine", "pine_tree.dae"), scale=2.3632),),
        native_height_m=11.9999, footprint_radius_m=3.7499,
        # Bark cylinder over the full trunk + a canopy cylinder covering the branch sub-mesh
        # bounds (radius = the x/y half extent so the cylinder stays inside them).
        proxies=(
            ProxyShape("cylinder", (0.0, 0.0, 5.94353, 0.0, 0.0, 0.0), (0.35504, 11.88706)),
            ProxyShape("cylinder", (0.0, 0.0, 6.71082, 0.0, 0.0, 0.0), (3.74841, 10.57811)),
        ),
        license=CC0, source=_fuel_source("OpenRobotics/Pine Tree", 6),
    ),
    "tree_oak": Asset(
        key="tree_oak", kind="tree", model_dir="horus_tree_oak",
        visuals=(AssetVisual("tree", _mesh("horus_tree_oak", "oak_tree.dae"),
                             pose=(0.0, 0.0, 0.10655, 0.0, 0.0, 0.0), scale=1.5156),),
        native_height_m=10.0003, footprint_radius_m=9.0199,
        # Trunk cylinder up to the crown + a crown cylinder inscribed in the foliage bounds (the
        # mesh's widest leaves are outside it by <= 1.5 m). The basal root flare (r <= 2.0 m,
        # below 0.6 m) is deliberately not modelled: it is ground-level decoration covered by the
        # merged scenery collision.
        proxies=(
            ProxyShape("cylinder", (0.0, 0.0, 0.98911, 0.0, 0.0, 0.0), (1.24016, 1.97822)),
            ProxyShape("cylinder", (-0.93317, 0.08945, 5.98926, 0.0, 0.0, 0.0), (6.43360, 8.02209)),
        ),
        license=CC0, source=_fuel_source("OpenRobotics/Oak tree", 7),
    ),
    "rock_a": Asset(
        key="rock_a", kind="rock", model_dir="horus_rock_a",
        visuals=(
            AssetVisual("rock", _mesh("horus_rock_a", "FallingRock01.dae"),
                        pose=(0.0, 0.0, 0.6961, 0.0, 0.0, 0.0),
                        albedo_map=_texture("horus_rock_a", "FallingRocks_Albedo.jpg"),
                        normal_map=_texture("horus_rock_a", "FallingRocks_Normal.jpg")),
        ),
        native_height_m=1.3923, footprint_radius_m=0.7889,
        proxies=(ProxyShape("box", (0.0, 0.0, 0.69610, 0.0, 0.0, 0.0), (1.48999, 1.48159, 1.39229)),),
        license=CC_BY, source=_fuel_source("OpenRobotics/Falling Rock 1", 6),
    ),
    "lamp_post": Asset(
        key="lamp_post", kind="lamp", model_dir="horus_lamp_post",
        visuals=(
            AssetVisual("lamp", _mesh("horus_lamp_post", "lamp_post.dae"), scale=3.0),
        ),
        native_height_m=9.2718, footprint_radius_m=3.0764,
        proxies=(
            ProxyShape("cylinder", (0.0, 0.01781, 4.18915, 0.0, 0.0, 0.0), (0.08794, 8.37829)),
            ProxyShape("box", (0.0, -1.48530, 8.82506, 0.0, 0.0, 0.0), (0.48807, 3.18209, 0.89354)),
        ),
        license=CC0, source=_fuel_source("OpenRobotics/Lamp Post", 3),
    ),
    "bus": Asset(
        key="bus", kind="vehicle", model_dir="horus_vehicle_bus",
        visuals=(
            AssetVisual("bus", _mesh("horus_vehicle_bus", "bus.obj"),
                        pose=(0.0, 0.0, 0.000725, 0.0, 0.0, 0.0), scale=0.01),
        ),
        native_height_m=3.6983, footprint_radius_m=6.4124,
        proxies=(ProxyShape("box", (0.0, -0.05164, 1.84915, 0.0, 0.0, 0.0), (2.70766, 12.48613, 3.69830)),),
        license=CC0, source=_fuel_source("OpenRobotics/Bus", 3),
    ),
    # Committed mesh vehicles for procedural traffic and parked cars. Poses put the mesh base
    # on z=0 and centre its measured bounds on the instance origin, front along +x; the box
    # proxies are the measured mesh bounds (see vehicles.py for the physical footprints).
    "vehicle_hatchback": Asset(
        key="vehicle_hatchback", kind="vehicle", model_dir="horus_vehicle_hatchback",
        visuals=(AssetVisual("hatchback", _mesh("horus_vehicle_hatchback", "hatchback.obj"),
                             pose=(-0.34355, 0.0, 0.01134, 0.0, 0.0, 1.570796), scale=0.0254),),
        native_height_m=1.5679, footprint_radius_m=2.3583,
        proxies=(ProxyShape("box", (0.0, 0.0, 0.78395, 0.0, 0.0, 0.0), (4.00109, 2.14051, 1.56790)),),
        license=CC0, source=_fuel_source("OpenRobotics/Hatchback", 3),
    ),
    "vehicle_hatchback_red": Asset(
        key="vehicle_hatchback_red", kind="vehicle", model_dir="horus_vehicle_hatchback_red",
        visuals=(AssetVisual("hatchback", _mesh("horus_vehicle_hatchback_red", "hatchback.obj"),
                             pose=(-0.34355, 0.0, 0.01134, 0.0, 0.0, 1.570796), scale=0.0254),),
        native_height_m=1.5679, footprint_radius_m=2.3583,
        proxies=(ProxyShape("box", (0.0, 0.0, 0.78395, 0.0, 0.0, 0.0), (4.00109, 2.14051, 1.56790)),),
        license=CC0, source=_fuel_source("OpenRobotics/Hatchback red", 4),
    ),
    "vehicle_hatchback_blue": Asset(
        key="vehicle_hatchback_blue", kind="vehicle", model_dir="horus_vehicle_hatchback_blue",
        visuals=(AssetVisual("hatchback", _mesh("horus_vehicle_hatchback_blue", "hatchback.obj"),
                             pose=(-0.34355, 0.0, 0.01134, 0.0, 0.0, 1.570796), scale=0.0254),),
        native_height_m=1.5679, footprint_radius_m=2.3583,
        proxies=(ProxyShape("box", (0.0, 0.0, 0.78395, 0.0, 0.0, 0.0), (4.00109, 2.14051, 1.56790)),),
        license=CC0, source=_fuel_source("OpenRobotics/Hatchback blue", 5),
    ),
    "vehicle_sedan": Asset(
        key="vehicle_sedan", kind="vehicle", model_dir="horus_vehicle_sedan",
        visuals=(AssetVisual("sedan", _mesh("horus_vehicle_sedan", "Hybrid.obj"),
                             pose=(0.04002, 0.0, 0.00652, 0.0, 0.0, 1.570796), scale=0.01),),
        native_height_m=1.5370, footprint_radius_m=2.4514,
        proxies=(ProxyShape("box", (0.0, 0.0, 0.76850, 0.0, 0.0, 0.0), (4.61826, 2.01235, 1.53701)),),
        license=CC0, source=_fuel_source("OpenRobotics/Prius Hybrid", 3),
    ),
    "vehicle_suv": Asset(
        key="vehicle_suv", kind="vehicle", model_dir="horus_vehicle_suv",
        visuals=(AssetVisual("suv", _mesh("horus_vehicle_suv", "suv.obj"),
                             pose=(-0.05560, -0.02824, 0.00698, 0.0, 0.0, -1.570796), scale=0.05),),
        native_height_m=1.7971, footprint_radius_m=2.2410,
        proxies=(ProxyShape("box", (0.0, 0.0, 0.89855, 0.0, 0.0, 0.0), (4.18348, 2.16978, 1.79711)),),
        license=CC0, source=_fuel_source("OpenRobotics/SUV", 4),
    ),
    "vehicle_pickup": Asset(
        key="vehicle_pickup", kind="vehicle", model_dir="horus_vehicle_pickup",
        visuals=(AssetVisual("pickup", _mesh("horus_vehicle_pickup", "pickup.dae"),
                             pose=(0.12012, 0.0, 0.00735, 0.0, 0.0, -1.570796)),),
        native_height_m=1.8655, footprint_radius_m=3.1064,
        proxies=(ProxyShape("box", (0.0, 0.0, 0.93275, 0.0, 0.0, 0.0), (5.66470, 2.47291, 1.86554)),),
        license=CC0, source=_fuel_source("OpenRobotics/Pickup", 4),
    ),
    "vehicle_box_truck": Asset(
        key="vehicle_box_truck", kind="vehicle", model_dir="horus_vehicle_box_truck",
        # The imported MTL names the texture twice on one map_Kd line, so the registry applies
        # the copied texture as an explicit PBR albedo map instead of relying on the MTL.
        visuals=(AssetVisual("box_truck", _mesh("horus_vehicle_box_truck", "truck.obj"),
                             pose=(-0.00244, 0.00128, 0.0, 0.0, 0.0, 0.0),
                             albedo_map=_texture("horus_vehicle_box_truck", "TruckBox_Diffuse.png"),
                             metalness=0.0, roughness=0.6),),
        native_height_m=2.0, footprint_radius_m=2.1212,
        proxies=(ProxyShape("box", (0.0, 0.0, 1.0, 0.0, 0.0, 0.0), (4.0, 1.633, 2.0)),),
        license=CC_BY, source=_fuel_source("OpenRobotics/TruckBox", 2),
    ),
    "vehicle_delivery_truck": Asset(
        key="vehicle_delivery_truck", kind="vehicle", model_dir="horus_vehicle_delivery_truck",
        visuals=(AssetVisual("delivery_truck", _mesh("horus_vehicle_delivery_truck", "truck.obj"),
                             pose=(0.00120, 0.0, 0.0, 0.0, 0.0, 0.0),
                             albedo_map=_texture("horus_vehicle_delivery_truck", "TruckDelivery_Diffuse.png"),
                             metalness=0.0, roughness=0.6),),
        native_height_m=1.7851, footprint_radius_m=2.7030,
        proxies=(ProxyShape("box", (0.0, 0.0, 0.89253, 0.0, 0.0, 0.0), (5.12259, 1.80454, 1.78507)),),
        license=CC_BY, source=_fuel_source("OpenRobotics/TruckDelivery", 2),
    ),
    "person_sitting_a": Asset(
        key="person_sitting_a", kind="person_sitting", model_dir="horus_person_sitting_a",
        visuals=(
            AssetVisual("person", _mesh("horus_person_sitting_a", "rescue_randy.dae"),
                        pose=(0.0, 0.0, -1.01533, 0.0, 0.0, 0.0), scale=1.6438),
        ),
        native_height_m=1.1997, footprint_radius_m=1.1928,
        proxies=(ProxyShape("box", (-0.16322, -0.33734, 0.59985, 0.0, 0.0, 0.0), (1.22825, 1.67406, 1.19973)),),
        license=CC0, source=_fuel_source("OpenRobotics/Rescue Randy Sitting", 8),
    ),
    "bench_a": Asset(
        key="bench_a", kind="bench", model_dir="horus_bench_a",
        visuals=(
            AssetVisual("bench", _mesh("horus_bench_a", "FoodCourtBenchShort.obj")),
        ),
        native_height_m=0.4302, footprint_radius_m=0.4952,
        proxies=(ProxyShape("box", (0.0, 0.0, 0.21510, 0.0, 0.0, 0.0), (0.34933, 0.92676, 0.43020)),),
        license=CC0, source=_fuel_source("OpenRobotics/FoodCourtBenchShort", 2),
    ),
}
