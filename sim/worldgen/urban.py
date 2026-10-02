from __future__ import annotations

import math
import random
from pathlib import Path

from sim.worldgen.dressing import (
    Dressing, add_clutter, add_fauna, animal_models, dress_person, fauna_areas, person_files, sample_events,
)
from sim.worldgen.environment import Environment
from sim.worldgen.layout import CROSSWALK_OFFSET_M, UrbanLayout, VehiclePlacement, crosswalk_side
from sim.worldgen.scenario import person_record
from sim.worldgen.sdf import (
    SURFACE_COLOURS, TREE_BARK, TREE_DECIDUOUS, box_geometry, collision, collision_at,
    cylinder_geometry, f, home_pad, include, material, pedestrian_include, pose, sphere_geometry,
    static_model, visual, world_header,
)
from sim.worldgen.vehicles import vehicle_model, vehicle_type


def _vehicle_record(vehicle: VehiclePlacement) -> dict:
    kind = vehicle_type(vehicle.type)
    return {"model": vehicle.model, "kind": "vehicle", "asset": vehicle.type,
            "position": vehicle.position, "yaw": vehicle.yaw,
            "behavior": "route", "group": "ambient",
            "waypoints": vehicle.waypoints, "speed": vehicle.speed,
            "accel": vehicle.accel, "mode": vehicle.mode,
            "start_offset_s": vehicle.start_offset_s or None,
            "footprint": kind.footprint(), "decel": kind.decel_m_s2,
            "turn_radius": kind.turn_radius_m, "variant": vehicle.variant}


def _legacy_paint(vehicle: VehiclePlacement) -> tuple[float, float, float] | None:
    """The canonical world's primitive body colours."""
    if vehicle.type == "bus":
        return (0.71, 0.69, 0.64)
    if vehicle.type == "van":
        return (0.72, 0.75, 0.77)
    if vehicle.type == "car":
        return (0.18, 0.31, 0.57) if int(vehicle.model[-2:]) % 2 else (0.56, 0.20, 0.13)
    return None


def urban_static_models(layout: UrbanLayout, rng: random.Random, env: Environment) -> list[str]:
    def surface(name: str, x: float, y: float, z: float, yaw: float, geometry: str, kind: str) -> str:
        tint = "ground" if kind == "urban_ground" else "surface"
        return visual(name, x, y, z, yaw, geometry, env.tint(SURFACE_COLOURS[kind], tint), cast_shadows=False)

    ground = (
        '<collision name="terrain_collision"><pose>0 0 -0.5 0 0 0</pose><geometry><box><size>150 150 1</size></box></geometry></collision>'
        + surface("ground", 0, 0, 0, 0, '<plane><normal>0 0 1</normal><size>150 150</size></plane>', "urban_ground")
    )
    models = [static_model("urban_ground", ground)]

    roads: list[str] = []
    curb = (0.68, 0.66, 0.60)
    for index in range(max(len(layout.roads_x), len(layout.roads_y))):
        vertical = layout.roads_x[index] if index < len(layout.roads_x) else None
        horizontal = layout.roads_y[index] if index < len(layout.roads_y) else None
        if vertical is not None:
            roads.append(surface(f"road_vertical_{index}", vertical, 0, 0.035, 0, box_geometry(9.5, 150, 0.07), "asphalt"))
        if horizontal is not None:
            roads.append(surface(f"road_horizontal_{index}", 0, horizontal, 0.038, 0, box_geometry(150, 9.5, 0.074), "asphalt"))
        for side in (-1, 1):
            if vertical is not None:
                roads.append(surface(f"vertical_sidewalk_{index}_{side}", vertical + side * 5.65, 0, 0.01, 0, box_geometry(1.8, 150, 0.02), "sidewalk"))
            if horizontal is not None:
                roads.append(surface(f"horizontal_sidewalk_{index}_{side}", 0, horizontal + side * 5.65, 0.01, 0, box_geometry(150, 1.8, 0.02), "sidewalk"))
            if vertical is not None:
                roads.append(visual(f"vertical_curb_{index}_{side}", vertical + side * 4.68, 0, 0.08, 0, box_geometry(0.18, 150, 0.16), curb))
            if horizontal is not None:
                roads.append(visual(f"horizontal_curb_{index}_{side}", 0, horizontal + side * 4.68, 0.08, 0, box_geometry(150, 0.18, 0.16), curb))
        for dash in range(-7, 8):
            if dash % 2 == 0:
                y = dash * 9.2
                x = dash * 9.2
                if vertical is not None:
                    roads.append(visual(f"lane_v_{index}_{dash}", vertical, y, 0.078, 0, box_geometry(0.12, 4.2, 0.012), (0.86, 0.81, 0.62)))
                if horizontal is not None:
                    roads.append(visual(f"lane_h_{index}_{dash}", x, horizontal, 0.08, 0, box_geometry(4.2, 0.12, 0.012), (0.86, 0.81, 0.62)))
        for dash in range(-7, 8, 2):
            y = dash * 9.2
            x = dash * 9.2
            if vertical is not None:
                roads.append(visual(f"edge_v_{index}_{dash}", vertical - 3.9, y, 0.078, 0, box_geometry(0.10, 3.8, 0.012), (0.86, 0.86, 0.81)))
            if horizontal is not None:
                roads.append(visual(f"edge_h_{index}_{dash}", x, horizontal - 3.9, 0.078, 0, box_geometry(3.8, 0.10, 0.012), (0.86, 0.86, 0.81)))
    for idx, (cx, cy) in enumerate(layout.crosswalk_centres):
        for stripe in range(7):
            offset = -3.0 + stripe
            roads.append(visual(f"crosswalk_{idx}_{stripe}", cx + offset, cy, 0.081, 0,
                                box_geometry(0.52, 7.4, 0.016), (0.92, 0.92, 0.88)))
    roads.append(surface("plaza", 0, 0, 0.001, 0,
                                f'<plane><normal>0 0 1</normal><size>{f(layout.plaza_half_size * 2)} {f(layout.plaza_half_size * 2)}</size></plane>',
                                "plaza_stone"))
    for axis in (-1, 1):
        for step in range(-3, 4):
            roads.append(visual(f"plaza_joint_{axis}_{step}", step * 2.5 if axis == -1 else 0,
                                0 if axis == -1 else step * 2.5, 0.096, 0,
                                box_geometry(0.035, layout.plaza_half_size * 2, 0.01) if axis == -1 else box_geometry(layout.plaza_half_size * 2, 0.035, 0.01), (0.39, 0.4, 0.38)))
    models.append(static_model("urban_streets", "".join(roads)))

    buildings = []
    for index, building in enumerate(layout.buildings):
        x, y, sx, sy, h, colour = (building.x, building.y, building.sx,
                                    building.sy, building.height, building.colour)
        facade_colour = tuple(
            min(0.95, max(0.2, component + rng.uniform(-0.025, 0.025)))
            for component in colour
        )
        base = 0.08
        details = [
            f'<collision name="building_collision"><pose>0 0 {f(base + h / 2)} 0 0 0</pose><geometry>{box_geometry(sx, sy, h)}</geometry></collision>',
            visual("building_body", 0, 0, base + h / 2, 0, box_geometry(sx, sy, h), facade_colour),
            visual("roof_parapet", 0, 0, base + h + 0.25, 0, box_geometry(sx + 0.35, sy + 0.35, 0.5), tuple(max(0.0, c - 0.07) for c in facade_colour)),
            collision("roof_parapet_collision", 0, 0, base + h + 0.25, 0,
                      box_geometry(sx + 0.35, sy + 0.35, 0.5)),
        ]
        floors = max(2, int(h // 3.2))
        cols_x = max(2, int(sx / 4.5))
        cols_y = max(2, int(sy / 4.5))
        for floor in range(floors):
            z = base + 1.9 + floor * 3.1
            if z > base + h - 0.8:
                continue
            for col in range(cols_x):
                px = -sx / 2 + (col + 0.5) * sx / cols_x
                details.append(visual(f"window_n_{floor}_{col}", px, sy / 2 + 0.026, z, 0,
                                      box_geometry(sx / cols_x * 0.53, 0.055, 1.28),
                                      (0.105, 0.21, 0.29), cast_shadows=False))
                details.append(visual(f"window_s_{floor}_{col}", px, -sy / 2 - 0.026, z, 0,
                                      box_geometry(sx / cols_x * 0.53, 0.055, 1.28),
                                      (0.11, 0.22, 0.30), cast_shadows=False))
            for col in range(cols_y):
                py = -sy / 2 + (col + 0.5) * sy / cols_y
                details.append(visual(f"window_e_{floor}_{col}", sx / 2 + 0.026, py, z, 0,
                                      box_geometry(0.055, sy / cols_y * 0.52, 1.28),
                                      (0.10, 0.20, 0.28), cast_shadows=False))
                details.append(visual(f"window_w_{floor}_{col}", -sx / 2 - 0.026, py, z, 0,
                                      box_geometry(0.055, sy / cols_y * 0.52, 1.28),
                                      (0.11, 0.22, 0.30), cast_shadows=False))
        for hvac in range(2 if h > 18 else 1):
            details.append(visual(f"roof_hvac_{hvac}", -sx * 0.22 + hvac * 2.0, 0, base + h + 0.7, 0,
                                  box_geometry(1.3, 1.1, 0.9), (0.43, 0.45, 0.44)))
            details.append(collision(f"roof_hvac_collision_{hvac}", -sx * 0.22 + hvac * 2.0, 0,
                                     base + h + 0.7, 0, box_geometry(1.3, 1.1, 0.9)))
        buildings.append(f'<model name="urban_building_{index + 1:02d}"><static>true</static><pose>{pose(x, y, 0)}</pose><link name="building">{"".join(details)}</link></model>')
    models.extend(buildings)

    park: list[str] = []
    pcx, pcy = layout.park_center
    park_size = layout.park_half_size * 2
    park.append(surface("park_lawn", pcx, pcy, 0.04, 0,
                               box_geometry(park_size, park_size, 0.08), "urban_ground"))
    for index, tree in enumerate(layout.park_trees):
        h, x, y = tree.height, tree.x, tree.y
        park.append(visual(f"park_tree_trunk_{index}", x, y, h * 0.30, 0,
                           cylinder_geometry(0.22, h * 0.6), TREE_BARK[index % 4]))
        park.append(collision(f"park_tree_trunk_collision_{index}", x, y, h * 0.30, 0,
                              cylinder_geometry(0.22, h * 0.6)))
        park.append(visual(f"park_tree_crown_{index}", x, y, h * 0.77, 0,
                           sphere_geometry(h * 0.31), env.tint(TREE_DECIDUOUS[index % 4], "foliage")))
        park.append(collision(f"park_tree_crown_collision_{index}", x, y, h * 0.77, 0,
                              sphere_geometry(h * 0.31)))
    for index, bench in enumerate(layout.benches):
        x, y, yaw = bench.x, bench.y, bench.yaw
        park.append(visual(f"park_bench_seat_{index}", x, y, 0.55, yaw,
                           box_geometry(1.8, 0.42, 0.12), (0.35, 0.22, 0.13)))
        park.append(collision(f"park_bench_seat_collision_{index}", x, y, 0.55, yaw,
                              box_geometry(1.8, 0.42, 0.12)))
        park.append(visual(f"park_bench_back_{index}", x, y - 0.19, 0.92, yaw,
                           box_geometry(1.8, 0.10, 0.65), (0.35, 0.22, 0.13)))
        park.append(collision(f"park_bench_back_collision_{index}", x, y - 0.19, 0.92, yaw,
                              box_geometry(1.8, 0.10, 0.65)))
    models.append(static_model("urban_park", "".join(park)))

    street_furniture: list[str] = []
    lamp_height = layout.lamp_height_m
    lamp_base_z = lamp_height / 2
    lamp_arm_z = lamp_height - 0.25
    lamp_head_z = lamp_height - 0.3
    for index, (x, y) in enumerate(layout.lamp_positions):
        street_furniture.append(f'<collision name="lamp_collision_{index}"><pose>{pose(x, y, lamp_base_z)}</pose><geometry>{cylinder_geometry(0.11, lamp_height)}</geometry></collision>')
        street_furniture.append(f'<visual name="lamp_pole_{index}"><pose>{pose(x, y, lamp_base_z)}</pose><geometry>{cylinder_geometry(0.09, lamp_height)}</geometry>{material((0.18, 0.20, 0.21))}</visual>')
        street_furniture.append(visual(f"lamp_arm_{index}", x + 0.5, y, lamp_arm_z, 0,
                                       box_geometry(1.0, 0.10, 0.11), (0.20, 0.22, 0.23)))
        street_furniture.append(collision(f"lamp_arm_collision_{index}", x + 0.5, y, lamp_arm_z, 0,
                                          box_geometry(1.0, 0.10, 0.11)))
        street_furniture.append(visual(f"lamp_head_{index}", x + 0.9, y, lamp_head_z, 0,
                                       box_geometry(0.42, 0.32, 0.17), (0.84, 0.78, 0.52)))
        street_furniture.append(collision(f"lamp_head_collision_{index}", x + 0.9, y, lamp_head_z, 0,
                                          box_geometry(0.42, 0.32, 0.17)))
    utility = layout.utility_poles
    for index, (x, y) in enumerate(utility):
        street_furniture.append(f'<collision name="utility_pole_collision_{index}"><pose>{pose(x, y, 4.8)}</pose><geometry>{cylinder_geometry(0.19, 9.6)}</geometry></collision>')
        street_furniture.append(f'<visual name="utility_pole_{index}"><pose>{pose(x, y, 4.8)}</pose><geometry>{cylinder_geometry(0.16, 9.6)}</geometry>{material((0.28, 0.21, 0.14))}</visual>')
        street_furniture.append(visual(f"utility_crossarm_{index}", x, y, 8.35, 0,
                                       box_geometry(2.2, 0.14, 0.14), (0.34, 0.28, 0.20)))
        street_furniture.append(collision(f"utility_crossarm_collision_{index}", x, y, 8.35, 0,
                                          box_geometry(2.2, 0.14, 0.14)))
    for index, (a, b) in enumerate(zip(utility[:4], utility[1:4])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        wire_pose = f"{f((a[0] + b[0]) / 2)} {f((a[1] + b[1]) / 2)} 8.8 {f(math.pi / 2)} 0 {f(math.atan2(dy, dx))}"
        wire_geometry = cylinder_geometry(0.035, length)
        street_furniture.append(f'<visual name="utility_wire_{index}"><pose>{wire_pose}</pose><geometry>{wire_geometry}</geometry>{material((0.08, 0.08, 0.09))}</visual>')
        street_furniture.append(collision_at(f"utility_wire_collision_{index}", wire_pose, wire_geometry))
    for index, (x, y) in enumerate(layout.trash_bins):
        street_furniture.append(visual(f"trash_bin_{index}", x, y, 0.55, 0,
                                       box_geometry(0.62, 0.58, 1.05), (0.18, 0.29, 0.24)))
        street_furniture.append(collision(f"trash_bin_collision_{index}", x, y, 0.55, 0,
                                          box_geometry(0.62, 0.58, 1.05)))

    bus_x, bus_y = layout.bus_shelter_center
    street_furniture.extend((
        collision("bus_shelter_roof_collision", bus_x, bus_y, 2.25, 0,
                  box_geometry(4.4, 1.7, 0.18)),
        collision("bus_shelter_back_collision", bus_x, bus_y - 0.7, 1.25, 0,
                  box_geometry(4.2, 0.07, 1.8)),
        collision("bus_shelter_side_collision", bus_x - 2.1, bus_y, 1.25, 0,
                  box_geometry(0.07, 1.4, 1.8)),
        collision("bus_shelter_bench_collision", bus_x, bus_y - 0.3, 0.56, 0,
                  box_geometry(3.2, 0.45, 0.13)),
        collision("bus_stop_sign_collision", bus_x + 2.3, bus_y + 0.1, 2.3, 0,
                  box_geometry(0.36, 0.12, 0.7)),
        visual("bus_shelter_roof", bus_x, bus_y, 2.25, 0, box_geometry(4.4, 1.7, 0.18), (0.23, 0.28, 0.30)),
        visual("bus_shelter_back", bus_x, bus_y - 0.7, 1.25, 0, box_geometry(4.2, 0.07, 1.8), (0.35, 0.52, 0.57), 0.45),
        visual("bus_shelter_side", bus_x - 2.1, bus_y, 1.25, 0, box_geometry(0.07, 1.4, 1.8), (0.35, 0.52, 0.57), 0.45),
        visual("bus_shelter_bench", bus_x, bus_y - 0.3, 0.56, 0, box_geometry(3.2, 0.45, 0.13), (0.24, 0.20, 0.16)),
        visual("bus_stop_sign", bus_x + 2.3, bus_y + 0.1, 2.3, 0, box_geometry(0.36, 0.12, 0.7), (0.05, 0.28, 0.62)),
    ))
    shelter_x, shelter_y = layout.jogger_shelter_center
    shelter_w, shelter_l = layout.jogger_shelter_size
    shelter_yaw = layout.follow_occluder_yaw
    cos_yaw, sin_yaw = math.cos(shelter_yaw), math.sin(shelter_yaw)

    def shelter_point(dx: float, dy: float) -> tuple[float, float]:
        return (shelter_x + dx * cos_yaw - dy * sin_yaw,
                shelter_y + dx * sin_yaw + dy * cos_yaw)

    canopy = box_geometry(shelter_w, shelter_l, 0.18)
    street_furniture.append(collision("jogger_shelter_roof_collision",
                                      shelter_x, shelter_y, 3.65, shelter_yaw, canopy))
    street_furniture.append(visual("jogger_shelter_roof",
                                   shelter_x, shelter_y, 3.65, shelter_yaw, canopy, (0.23, 0.28, 0.30)))
    back = box_geometry(0.08, max(1.0, shelter_l - 0.4), 2.3)
    back_x, back_y = shelter_point(1.65, 0.0)
    street_furniture.append(collision("jogger_shelter_back_collision",
                                      back_x, back_y, 1.65, shelter_yaw, back))
    street_furniture.append(visual("jogger_shelter_back",
                                   back_x, back_y, 1.65, shelter_yaw, back, (0.35, 0.52, 0.57), 0.45))
    bench = box_geometry(0.42, min(4.8, shelter_l * 0.75), 0.13)
    # Procedural benches sit against the back wall, clear of the sidewalk walking lanes.
    bench_x, bench_y = shelter_point(1.0 if layout.canonical else 1.3, 0.0)
    street_furniture.append(collision("jogger_shelter_bench_collision",
                                      bench_x, bench_y, 0.56, shelter_yaw, bench))
    street_furniture.append(visual("jogger_shelter_bench",
                                   bench_x, bench_y, 0.56, shelter_yaw, bench, (0.24, 0.20, 0.16)))
    post_y = shelter_l / 2 - 0.5
    for index, (dx, dy) in enumerate(((-1.65, -post_y), (1.65, -post_y),
                                      (-1.65, 0.0), (1.65, 0.0),
                                      (-1.65, post_y), (1.65, post_y))):
        post_x, post_y = shelter_point(dx, dy)
        post = cylinder_geometry(0.07, 3.6)
        street_furniture.append(collision(
            f"jogger_shelter_post_collision_{index}", post_x, post_y, 1.8, shelter_yaw, post))
        street_furniture.append(visual(
            f"jogger_shelter_post_{index}", post_x, post_y, 1.8, shelter_yaw, post, (0.18, 0.20, 0.21)))
    models.append(static_model("urban_street_furniture", "".join(street_furniture)))

    for index, car in enumerate(layout.parked_cars):
        name = f"veh_car_{index + 10:02d}" if layout.canonical else f"veh_parked_{index + 1:02d}"
        models.append(vehicle_model(name, car.type, car.x, car.y, 0.0, car.yaw, static=True,
                                    variant=car.variant, paint=car.colour if car.type == "car" else None))
    return models


def build_urban(layout: UrbanLayout, world_dir: Path, *, textures: bool = True, dressing: Dressing
                ) -> tuple[str, list[dict], dict[str, bytes | str]]:
    env = dressing.environment
    models = urban_static_models(layout, random.Random(layout.seed), env)
    height = lambda x, y: 0.0  # noqa: E731
    agents = []
    for person in layout.people:
        record = dress_person(dressing, person_record(person))
        models.append(pedestrian_include(record))
        agents.append(record)
    files = person_files(dressing, agents, textures=textures)
    for vehicle in layout.vehicles:
        record = _vehicle_record(vehicle)
        models.append(vehicle_model(vehicle.model, vehicle.type, vehicle.position[0], vehicle.position[1], 0.0,
                                    vehicle.yaw, static=False, variant=vehicle.variant,
                                    paint=_legacy_paint(vehicle)))
        agents.append(record)
    animals = add_fauna(dressing, "urban", layout, agents, fauna_areas("urban", layout), height)
    models.extend(animal_models(animals, height))
    agents.extend(animals)
    models.extend(add_clutter(dressing, "urban", layout, agents, height))
    sample_events(dressing, agents, layout.seed)
    pad = home_pad(world_dir)
    drone = include("horus_iris_outdoor", "horus_iris", 0, 0, 0.195, math.pi / 2)
    world_name = "horus_urban" if layout.canonical else f"horus_urban_p{layout.seed}"
    world = (world_header(world_name, env) + "\n".join(models) + "\n" + pad + "\n" + drone + "\n  </world></sdf>\n")
    return world, agents, files


