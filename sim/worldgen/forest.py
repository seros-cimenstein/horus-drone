from __future__ import annotations

import math
import random

from sim.worldgen.dressing import (
    Dressing, add_clutter, add_fauna, animal_models, dress_person, fauna_areas, person_files, sample_events,
)
from sim.worldgen.environment import Environment
from sim.worldgen.layout import (
    CLEARING_SCAN_MARGIN_M, FOREST_VANTAGE_ROLES, ForestLayout, clears_actor_columns,
    clears_actor_routes, clears_clearing_scan, distance_to_path, forest_ground_rows, forest_surface,
)
from sim.worldgen.scenario import person_record
from sim.worldgen.sdf import (
    TREE_BARK, TREE_CONIFER, TREE_DECIDUOUS, box_geometry, collision,
    collision_at, cone_geometry, cylinder_geometry, f, home_pad, include,
    material, pedestrian_include, pose, sphere_geometry, static_model,
    visual, world_header,
)
from sim.worldgen.textures import terrain_model_files, write_npy


def forest_height(x: float, y: float, layout: ForestLayout) -> float:
    """Ground height on the exported terrain mesh that Gazebo renders and collides with."""
    return forest_surface(layout).height(x, y)


BODY_CLEARANCE_M = 0.6  # walker body radius plus the route-clearance margin
# Canonical follow occluder: (height above the bush base, sphere radius) tiers of a dense
# bush / tree crown. The lower tiers keep walkers clear; the upper tiers span the camera ray.
CANONICAL_OCCLUDER_TIERS = ((0.65, 0.65), (1.55, 0.85), (3.10, 1.25))


def tree_visuals(layout: ForestLayout, rng: random.Random, env: Environment) -> str:
    parts: list[str] = []
    occupied: list[tuple[float, float]] = []
    target = layout.tree_count
    missing_tree_x, missing_tree_y = layout.missing_canopy_tree
    low, high = layout.tree_height_range
    trail_clearance = (5.8 if layout.canonical else high * 0.4 + 2.6)
    # The tallest canopy (0.40 * high) must keep the 2.6 m home-column clearance; the extra
    # 0.6 m covers terrain differences between the tree base and the column.
    home_clearance = 9.0 if layout.canonical else high * 0.4 + 3.2
    surface = forest_surface(layout)
    vantage_columns = [(person.position[0], person.position[1],
                        surface.height(*person.position) + 0.25)
                       for person in layout.people if person.role in FOREST_VANTAGE_ROLES]
    for _ in range(24000):
        if len(occupied) >= target:
            break
        x, y = rng.uniform(-71, 71), rng.uniform(-71, 71)
        if (math.hypot(x, y) < home_clearance
                or math.hypot(x - layout.clearing[0], y - layout.clearing[1]) < layout.clearing_radius + 2):
            continue
        if math.hypot(x - layout.camp[0], y - layout.camp[1]) < 14:
            continue
        if (distance_to_path((x, y), layout.trail) < trail_clearance
                or distance_to_path((x, y), layout.creek) < 6.5):
            continue
        if math.hypot(x - layout.missing_hiker.position[0], y - layout.missing_hiker.position[1]) < 2.7:
            continue
        if math.hypot(x - missing_tree_x, y - missing_tree_y) < 5.2:
            continue
        base = forest_height(x, y, layout)
        height = rng.uniform(low, high)
        # The trunk-distance clearing check above is not enough: conifer canopies reach
        # 0.40 * height horizontally, so a tree just outside the clearing can still hang
        # over the scan raster flown at 10 m. Reject its whole conservative canopy
        # envelope when it would come within the scan margin of that corridor.
        if not clears_clearing_scan(x, y, base + 0.26 * height, base + 1.18 * height,
                                    0.40 * height, layout.clearing, layout.clearing_radius):
            continue
        canopy_low, canopy_high = base + 0.26 * height, base + 1.18 * height
        canopy_radius = 0.40 * height
        # No canopy may cover a person-of-interest's descent column (it would leave the
        # planner no vantage), and every route/body point keeps physical clearance.
        if not clears_actor_columns(x, y, canopy_low, canopy_high, canopy_radius,
                                    vantage_columns, CLEARING_SCAN_MARGIN_M):
            continue
        if not clears_actor_routes(x, y, canopy_radius, layout.people, BODY_CLEARANCE_M):
            continue
        occupied.append((x, y))
        tree_number = len(occupied)
        trunk_height = height * rng.uniform(0.29, 0.41)
        trunk_radius = rng.uniform(0.18, 0.34)
        variant = rng.randrange(4)
        trunk_geometry = cylinder_geometry(trunk_radius, trunk_height)
        parts.append(visual(
            f"tree_trunk_{tree_number:03d}", x, y, base + trunk_height / 2, 0,
            trunk_geometry, TREE_BARK[variant],
        ))
        parts.append(collision(
            f"tree_trunk_collision_{tree_number:03d}", x, y, base + trunk_height / 2,
            0, trunk_geometry,
        ))
        if rng.random() < 0.68:
            canopy_colour = env.tint(TREE_CONIFER[variant], "foliage")
            for tier in range(3):
                radius = height * (0.32 - 0.085 * tier) * rng.uniform(0.86, 1.04)
                cone_height = height * 0.47
                z = base + trunk_height + height * (0.18 + 0.16 * tier)
                parts.append(visual(
                    f"tree_conifer_{tree_number:03d}_{tier}", x, y, z, 0,
                    cone_geometry(radius, cone_height), canopy_colour,
                ))
            parts.append(collision(
                f"tree_conifer_canopy_collision_{tree_number:03d}", x, y,
                base + trunk_height + height * 0.37, 0,
                sphere_geometry(height * 0.40),
            ))
        else:
            canopy_colour = env.tint(TREE_DECIDUOUS[variant], "foliage")
            crown_radius = height * rng.uniform(0.19, 0.25)
            clumps = ((0.0, 0.0, 0.70, 1.0), (-0.62, 0.05, 0.68, 0.75),
                      (0.56, -0.10, 0.72, 0.78), (0.04, 0.55, 0.73, 0.66))
            for leaf_index, (dx, dy, dz, scale) in enumerate(clumps):
                parts.append(visual(
                    f"tree_canopy_{tree_number:03d}_{leaf_index}",
                    x + dx * crown_radius, y + dy * crown_radius, base + height * dz,
                    0, sphere_geometry(crown_radius * scale), canopy_colour,
                ))
            parts.append(collision(
                f"tree_canopy_collision_{tree_number:03d}", x, y,
                base + height * 0.71, 0, sphere_geometry(crown_radius * 1.18),
            ))

    missing_base = forest_height(missing_tree_x, missing_tree_y, layout)
    if layout.canonical:
        parts.append(visual(
            "tree_missing_hiker_trunk", missing_tree_x, missing_tree_y, missing_base + 3.0,
            0, cylinder_geometry(0.30, 6.0), TREE_BARK[1],
        ))
        parts.append(collision(
            "tree_missing_hiker_trunk_collision", missing_tree_x, missing_tree_y,
            missing_base + 3.0, 0, cylinder_geometry(0.30, 6.0),
        ))
        for leaf_index, (dx, dy, radius) in enumerate(((0.0, 0.0, 4.6), (2.3, -0.4, 3.5), (-2.0, 0.8, 3.2))):
            parts.append(visual(
                f"tree_missing_hiker_canopy_{leaf_index:02d}",
                missing_tree_x + dx, missing_tree_y + dy, missing_base + 9.0, 0,
                sphere_geometry(radius), env.tint(TREE_DECIDUOUS[1], "foliage"),
            ))
        parts.append(collision(
            "tree_missing_hiker_canopy_collision", missing_tree_x, missing_tree_y,
            missing_base + 9.0, 0, sphere_geometry(4.8),
        ))
    else:
        canopy_h = max(8.0, min(22.0, (missing_tree_x + missing_tree_y) % 14 + 8.0))
        trunk_h = canopy_h * 0.42
        parts.append(visual("tree_missing_hiker_trunk", missing_tree_x, missing_tree_y,
                            missing_base + trunk_h / 2, 0,
                            cylinder_geometry(0.28, trunk_h), TREE_BARK[1]))
        parts.append(collision("tree_missing_hiker_trunk_collision", missing_tree_x, missing_tree_y,
                               missing_base + trunk_h / 2, 0,
                               cylinder_geometry(0.28, trunk_h)))
        crown_radius = 4.8
        parts.append(visual("tree_missing_hiker_canopy_00", missing_tree_x, missing_tree_y,
                            missing_base + canopy_h * 0.72, 0,
                            sphere_geometry(crown_radius), env.tint(TREE_DECIDUOUS[1], "foliage")))
        parts.append(collision("tree_missing_hiker_canopy_collision", missing_tree_x, missing_tree_y,
                               missing_base + canopy_h * 0.72, 0,
                               sphere_geometry(crown_radius)))

    bush_x, bush_y = layout.follow_occluder_xy
    bush_base = forest_height(bush_x, bush_y, layout)
    if layout.canonical:
        for level, (z_absolute, radius), colour in zip(
            ("lower", "middle", "upper", "canopy"),
            CANONICAL_OCCLUDER_TIERS,
            (env.tint(TREE_DECIDUOUS[1], "foliage"), env.tint(TREE_DECIDUOUS[0], "foliage"),
             env.tint(TREE_CONIFER[0], "foliage"), env.tint(TREE_DECIDUOUS[1], "foliage")),
        ):
            z_offset = z_absolute
            foliage = sphere_geometry(radius)
            parts.append(visual(
                f"follow_occlusion_bush_{level}", bush_x, bush_y,
                bush_base + z_offset, 0, foliage, colour,
            ))
            parts.append(collision(
                f"follow_occlusion_bush_{level}_collision", bush_x, bush_y,
                bush_base + z_offset, 0, foliage,
            ))
    else:
        foliage = sphere_geometry(layout.follow_occluder_radius_m)
        bush_z = bush_base + layout.follow_occluder_height_m
        parts.append(visual("follow_occlusion_bush_primary", bush_x, bush_y,
                            bush_z, 0, foliage, env.tint(TREE_DECIDUOUS[1], "foliage")))
        parts.append(collision("follow_occlusion_bush_primary_collision", bush_x, bush_y,
                               bush_z, 0, foliage))
    return "".join(parts)


def forest_static_models(layout: ForestLayout, rng: random.Random,
                         terrain_model_name: str, env: Environment) -> list[str]:
    models: list[str] = []
    models.append(include(terrain_model_name, "forest_terrain", 0, 0, 0))
    models.append(static_model("forest_trees", tree_visuals(layout, rng, env)))

    undergrowth: list[str] = []
    count = 94 if layout.canonical else max(70, int(layout.tree_count * 0.34))
    for i in range(count):
        x, y = rng.uniform(-70, 70), rng.uniform(-70, 70)
        if (math.hypot(x, y) < 9
                or math.hypot(x - layout.clearing[0], y - layout.clearing[1]) < layout.clearing_radius - 1
                or math.hypot(x - layout.camp[0], y - layout.camp[1]) < 13
                or distance_to_path((x, y), layout.trail) < 3.1
                or distance_to_path((x, y), layout.creek) < 4.5):
            continue
        z = forest_height(x, y, layout)
        r = rng.uniform(0.45, 1.25)
        undergrowth.append(visual(
            f"bush_{i:03d}", x, y, z + r * 0.64, 0, sphere_geometry(r),
            env.tint(TREE_DECIDUOUS[rng.randrange(4)], "foliage"),
        ))
    models.append(static_model("forest_undergrowth", "".join(undergrowth)))

    creek: list[str] = []
    for i, (a, b) in enumerate(zip(layout.creek, layout.creek[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        x, y = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        z = forest_height(x, y, layout) - 0.18
        creek.append(visual(f"creek_water_{i:02d}", x, y, z, math.atan2(dy, dx),
                            box_geometry(length + 1.0, 3.2, 0.08), (0.12, 0.31, 0.40), 0.92))
    models.append(static_model("forest_creek", "".join(creek)))

    clutter: list[str] = []
    for i in range(34 if layout.canonical else max(24, layout.tree_count // 8)):
        x, y = rng.uniform(-68, 68), rng.uniform(-68, 68)
        if (math.hypot(x, y) < 9 or math.hypot(x - layout.camp[0], y - layout.camp[1]) < 12
                or distance_to_path((x, y), layout.trail) < 3.0
                or distance_to_path((x, y), layout.creek) < 4.0
                # Procedural clearings are open meadow (animal wander areas are validated there).
                or (not layout.canonical and math.hypot(x - layout.clearing[0], y - layout.clearing[1])
                    < layout.clearing_radius)):
            continue
        r, z, yaw = rng.uniform(0.35, 1.2), forest_height(x, y, layout), rng.random() * 6.28
        rock_z = z + r * 0.45
        clutter.append(visual(f"rock_{i:02d}", x, y, rock_z, yaw,
                              sphere_geometry(r), rng.choice(((0.34, 0.35, 0.32), (0.39, 0.37, 0.32), (0.30, 0.32, 0.31)))))
        clutter.append(collision(f"rock_collision_{i:02d}", x, y, rock_z, yaw, sphere_geometry(r)))
    for i, (x, y, length, yaw) in enumerate(layout.fallen_logs):
        z = forest_height(x, y, layout) + 0.32
        log_pose = f"{f(x)} {f(y)} {f(z)} {f(math.pi / 2)} 0 {f(yaw)}"
        log_geometry = cylinder_geometry(0.28, length)
        clutter.append(f'<visual name="fallen_log_{i:02d}"><pose>{log_pose}</pose><geometry>{log_geometry}</geometry>{material(TREE_BARK[i % 4])}</visual>')
        clutter.append(collision_at(f"fallen_log_collision_{i:02d}", log_pose, log_geometry))
    models.append(static_model("forest_rocks_and_logs", "".join(clutter)))

    cx, cy = layout.camp
    cz = forest_height(cx, cy, layout)
    camp: list[str] = []
    tent_geometry = box_geometry(2.8, 1.5, 0.07)
    for side, y_offset, roll, colour in (
        ("left", -0.52, 0.70, (0.60, 0.34, 0.23)),
        ("right", 0.52, -0.70, (0.68, 0.41, 0.27)),
    ):
        roof_pose = f"{f(cx)} {f(cy + y_offset)} {f(cz + 1.10)} {f(roll)} 0 0"
        camp.append(
            f'<visual name="tent_roof_{side}"><pose>{roof_pose}</pose>'
            f'<geometry>{tent_geometry}</geometry>{material(colour)}</visual>'
        )
        camp.append(collision_at(f"tent_roof_{side}_collision", roof_pose, tent_geometry))
    for i in range(10):
        angle = 2 * math.pi * i / 10
        rx, ry = cx - 3.7 + math.cos(angle) * 0.75, cy + 0.1 + math.sin(angle) * 0.75
        r = 0.22
        camp.append(visual(f"fire_ring_stone_{i:02d}", rx, ry, cz + r * 0.45, 0,
                           sphere_geometry(r), (0.36, 0.34, 0.29)))
        camp.append(collision(f"fire_ring_stone_collision_{i:02d}", rx, ry, cz + r * 0.45, 0,
                              sphere_geometry(r)))
    camp.append(visual("fire_pit", cx - 3.7, cy + 0.1, cz + 0.12, 0,
                       cylinder_geometry(0.45, 0.24), (0.08, 0.07, 0.06)))
    camp.append(collision("fire_pit_collision", cx - 3.7, cy + 0.1, cz + 0.12, 0,
                          cylinder_geometry(0.45, 0.24)))
    camp.append(visual("camp_table", cx + 4.1, cy + 1.0, cz + 0.65, 0,
                       box_geometry(1.8, 0.85, 0.12), (0.32, 0.20, 0.11)))
    camp.append(collision("camp_table_collision", cx + 4.1, cy + 1.0, cz + 0.65, 0,
                          box_geometry(1.8, 0.85, 0.12)))
    for i, (x, y) in enumerate(((cx + 3.5, cy + 1), (cx + 4.7, cy + 1))):
        camp.append(visual(f"camp_table_leg_{i}", x, y, cz + 0.32, 0,
                           box_geometry(0.10, 0.10, 0.6), (0.24, 0.16, 0.09)))
        camp.append(collision(f"camp_table_leg_collision_{i}", x, y, cz + 0.32, 0,
                              box_geometry(0.10, 0.10, 0.6)))
    models.append(static_model("forest_campsite", "".join(camp)))
    return models


def build_forest(layout: ForestLayout, world_dir,
                 terrain_model_name: str, *, textures: bool = True, dressing: Dressing
                 ) -> tuple[str, list[dict], list[list[float]] | None, dict[str, bytes | str]]:
    env = dressing.environment
    rows = [list(row) for row in forest_ground_rows(layout)]
    files = terrain_model_files(rows, layout.seed, terrain_model_name,
                                layout.trail, layout.clearing, textures=textures,
                                clearing_radius=layout.clearing_radius, tint=env.ground_tint)
    ground_name = ("horus_forest_ground.npy" if layout.canonical else
                   f"horus_forest_p{layout.seed}_ground.npy")
    files[ground_name] = write_npy(rows)
    models = forest_static_models(layout, random.Random(layout.seed), terrain_model_name, env)
    height = lambda x, y: forest_height(x, y, layout)  # noqa: E731
    agents = []
    for person in layout.people:
        record = dress_person(dressing, person_record(person))
        x, y = person.position
        record["z"] = height(x, y)
        models.append(pedestrian_include(record))
        agents.append(record)
    files.update(person_files(dressing, agents, textures=textures))
    animals = add_fauna(dressing, "forest", layout, agents, fauna_areas("forest", layout), height)
    models.extend(animal_models(animals, height))
    agents.extend(animals)
    models.extend(add_clutter(dressing, "forest", layout, agents, height))
    sample_events(dressing, agents, layout.seed)
    pad = home_pad(world_dir)
    drone = include("horus_iris_outdoor", "horus_iris", 0, 0, 0.195, math.pi / 2)
    world_name = "horus_forest" if layout.canonical else f"horus_forest_p{layout.seed}"
    world = world_header(world_name, env) + "\n".join(models) + "\n" + pad + "\n" + drone + "\n  </world></sdf>\n"
    return world, agents, rows, files


