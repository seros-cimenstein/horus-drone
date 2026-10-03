"""Vehicle types with physical dimensions, dynamics limits and rendered geometry.

Mesh vehicles reuse pinned OpenRobotics Fuel meshes imported by
``sim/scripts/import_licensed_assets.py`` (see each model's PROVENANCE.txt). Dimensions below
are the measured mesh bounds after the listed scale and yaw (front along +x), and the
collision/oracle footprint is the same box. ``car``, ``van`` and ``bus`` are HORUS primitive
bodies kept for the byte-frozen canonical worlds; procedural worlds sample and park only mesh
types (``SAMPLED_TYPES`` / ``PARKED_TYPES``), and ``vehicle_asset`` maps a mesh type's variant
to its ``assets.ASSETS`` key for scenery placement. Unknown types are errors; nothing falls
back to a car.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from sim.worldgen.sdf import ACTOR_COLLIDE_BITMASK, box_geometry, cylinder_geometry, f, material, pose, visual


@dataclass(frozen=True)
class VehicleType:
    name: str
    length_m: float
    width_m: float
    height_m: float
    max_speed_m_s: tuple[float, float]
    accel_m_s2: tuple[float, float]
    decel_m_s2: float  # comfortable service braking
    turn_radius_m: float  # path radius of the vehicle centre
    mesh: str | None = None  # model:// URI
    mesh_scale: float = 1.0
    mesh_yaw: float = 0.0
    mesh_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)  # visual origin so the box centres
    variants: tuple[str, ...] = ()  # alternative mesh URIs (paint textures)
    heavy: bool = False
    assets: tuple[str, ...] = ()  # ASSETS keys per paint variant (mesh types only)

    def footprint(self) -> dict:
        return {"length_m": self.length_m, "width_m": self.width_m, "height_m": self.height_m}


EMERGENCY_DECEL_M_S2 = 6.5
LATERAL_ACCEL_M_S2 = 2.0

VEHICLE_TYPES: dict[str, VehicleType] = {vehicle.name: vehicle for vehicle in (
    # Primitive bodies: canonical worlds only (their bytes are frozen). Procedural worlds
    # sample / park mesh types exclusively.
    VehicleType("car", 4.4, 1.9, 2.05, (5.0, 8.0), (1.6, 2.2), 2.5, 5.0),
    VehicleType("van", 5.4, 2.15, 2.65, (4.5, 7.0), (1.3, 1.8), 2.2, 5.8),
    VehicleType("bus", 9.2, 2.55, 3.6, (4.0, 6.0), (0.9, 1.4), 1.8, 7.0, heavy=True),
    VehicleType("hatchback", 4.0, 2.14, 1.57, (5.0, 9.0), (1.6, 2.4), 2.6, 5.0,
                mesh="model://horus_vehicle_hatchback/meshes/hatchback.obj", mesh_scale=0.0254,
                mesh_yaw=math.pi / 2, mesh_offset=(-0.344, 0.0, 0.011),
                variants=("model://horus_vehicle_hatchback_red/meshes/hatchback.obj",
                          "model://horus_vehicle_hatchback_blue/meshes/hatchback.obj"),
                assets=("vehicle_hatchback", "vehicle_hatchback_red", "vehicle_hatchback_blue")),
    VehicleType("sedan", 4.62, 2.01, 1.54, (5.0, 9.0), (1.5, 2.2), 2.6, 5.4,
                mesh="model://horus_vehicle_sedan/meshes/Hybrid.obj", mesh_scale=0.01,
                mesh_yaw=math.pi / 2, mesh_offset=(0.04, 0.0, 0.007),
                assets=("vehicle_sedan",)),
    # The Fuel SDF scales this mesh by 0.06 (5.02 x 2.60 x 2.16 m, oversized for an SUV); 0.05
    # gives 4.18 x 2.17 x 1.80 m.
    VehicleType("suv", 4.18, 2.17, 1.8, (5.0, 8.5), (1.4, 2.0), 2.4, 5.8,
                mesh="model://horus_vehicle_suv/meshes/suv.obj", mesh_scale=0.05,
                mesh_yaw=-math.pi / 2, mesh_offset=(-0.056, -0.028, 0.007),
                assets=("vehicle_suv",)),
    VehicleType("pickup", 5.67, 2.47, 1.87, (5.0, 8.0), (1.3, 1.9), 2.3, 6.3,
                mesh="model://horus_vehicle_pickup/meshes/pickup.dae", mesh_yaw=-math.pi / 2,
                mesh_offset=(0.12, 0.0, 0.007), assets=("vehicle_pickup",)),
    VehicleType("box_truck", 4.0, 1.63, 2.0, (4.0, 6.5), (1.0, 1.5), 2.0, 6.5,
                mesh="model://horus_vehicle_box_truck/meshes/truck.obj", heavy=True,
                assets=("vehicle_box_truck",)),
    VehicleType("delivery_truck", 5.12, 1.81, 1.79, (4.0, 7.0), (1.1, 1.6), 2.1, 6.5,
                mesh="model://horus_vehicle_delivery_truck/meshes/truck.obj", heavy=True,
                assets=("vehicle_delivery_truck",)),
    # The Fuel coach bus mesh (12.49 m); the "bus" primitive above stays for canonical worlds.
    VehicleType("coach", 12.49, 2.71, 3.7, (4.0, 6.5), (0.8, 1.2), 1.8, 10.0,
                mesh="model://horus_vehicle_bus/meshes/bus.obj", mesh_scale=0.01,
                mesh_yaw=math.pi / 2, mesh_offset=(-0.052, 0.0, 0.001), heavy=True,
                assets=("bus",)),
)}
# Procedural worlds use mesh vehicles only: no primitive car/van/bus body is ever emitted.
SAMPLED_TYPES = ("hatchback", "sedan", "suv", "pickup", "box_truck", "delivery_truck", "coach")
SAMPLED_WEIGHTS = (5, 5, 3, 2, 1, 2, 1)
PARKED_TYPES = ("hatchback", "sedan", "suv", "pickup", "box_truck", "delivery_truck")
PAINTS = ((0.18, 0.31, 0.57), (0.56, 0.20, 0.13), (0.72, 0.72, 0.70), (0.12, 0.12, 0.13),
          (0.42, 0.44, 0.46), (0.24, 0.35, 0.24), (0.66, 0.60, 0.46), (0.80, 0.78, 0.74))


def vehicle_type(name: str) -> VehicleType:
    try:
        return VEHICLE_TYPES[name]
    except KeyError:
        raise ValueError(f"unknown vehicle type {name!r}; known: {sorted(VEHICLE_TYPES)}") from None


def vehicle_asset(name: str, variant: int = 0) -> str:
    """scenery ``ASSETS`` key for a mesh vehicle type's paint variant.

    Parked cars and other static placements use the registry asset (measured proxies) instead
    of an inline model; primitive-only types (canonical ``car``/``van``/``bus``) raise.
    """
    kind = vehicle_type(name)
    if not kind.assets:
        raise ValueError(f"vehicle type {name!r} has no scenery asset")
    return kind.assets[variant % len(kind.assets)]


def _surface() -> str:
    return f"<surface><contact><collide_bitmask>{ACTOR_COLLIDE_BITMASK:#06x}</collide_bitmask></contact></surface>"


def _primitive_parts(kind: VehicleType, paint: tuple[float, float, float]) -> list[str]:
    length, width = kind.length_m, kind.width_m
    body_h = {"bus": 2.55, "van": 1.8}.get(kind.name, 1.4)
    z_body = body_h * 0.58
    parts = [visual("body", 0, 0, z_body, 0, box_geometry(length, width, body_h), paint)]
    if kind.name != "bus":
        cabin_h = 0.75 if kind.name == "car" else 0.95
        parts.append(visual("cabin", -0.15, 0, body_h + cabin_h / 2 - 0.1, 0,
                            box_geometry(length * 0.48, width * 0.82, cabin_h), (0.20, 0.28, 0.33)))
    else:
        parts.append(visual("windows", 0, 0, body_h + 0.45, 0,
                            box_geometry(length * 0.88, width, 1.05), (0.20, 0.35, 0.41)))
    radius = 0.36 if kind.name == "bus" else 0.29
    tread = 0.15
    for axle_x in (-length * 0.31, length * 0.31):
        # Rolled about x so the cylinder axis is the axle; the outer face is flush with the
        # footprint side and the tyre touches the ground.
        for side_y in (-(width / 2 - tread / 2), width / 2 - tread / 2):
            parts.append(f'<visual name="wheel_{len(parts)}"><pose>{f(axle_x)} {f(side_y)} {f(radius)} '
                         f'{f(math.pi / 2)} 0 0</pose><geometry>{cylinder_geometry(radius, tread)}</geometry>'
                         f'{material((0.035, 0.04, 0.045))}</visual>')
    return parts


def vehicle_model(model_name: str, type_name: str, x: float, y: float, z: float, yaw: float, *,
                  static: bool, variant: int = 0, paint: tuple[float, float, float] | None = None) -> str:
    """A vehicle model whose body box (the footprint) is centred on the model origin."""
    kind = vehicle_type(type_name)
    size = box_geometry(kind.length_m, kind.width_m, kind.height_m)
    parts = [f'<collision name="body_collision"><pose>0 0 {f(kind.height_m / 2 + 0.02)} 0 0 0</pose>'
             f"<geometry>{size}</geometry>{_surface()}</collision>"]
    if kind.mesh is None:
        parts.extend(_primitive_parts(kind, paint or PAINTS[variant % len(PAINTS)]))
    else:
        uris = (kind.mesh, *kind.variants)
        uri = uris[variant % len(uris)]
        dx, dy, dz = kind.mesh_offset
        scale = "" if kind.mesh_scale == 1.0 else f"<scale>{f(kind.mesh_scale)} {f(kind.mesh_scale)} {f(kind.mesh_scale)}</scale>"
        parts.append(f'<visual name="body"><pose>{f(dx)} {f(dy)} {f(dz)} 0 0 {f(kind.mesh_yaw)}</pose>'
                     f"<geometry><mesh><uri>{uri}</uri>{scale}</mesh></geometry></visual>")
    return (f'<model name="{model_name}"><static>{"true" if static else "false"}</static>'
            f"<pose>{pose(x, y, z, yaw)}</pose><link name=\"chassis\">"
            + ("" if static else '<inertial><mass>1000</mass><inertia><ixx>2000</ixx><ixy>0</ixy><ixz>0</ixz>'
               '<iyy>2000</iyy><iyz>0</iyz><izz>2000</izz></inertia></inertial><gravity>false</gravity>'
               "<kinematic>true</kinematic>")
            + "".join(parts) + "</link></model>")
