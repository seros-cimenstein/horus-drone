"""Procedural 1.2 km cities: the world builder, transit altitude and manifest fields.

``build_urban_procedural(layout, world_dir, ...)`` emits the world for a layout from
:func:`sim.worldgen.urban_sampler.sample_urban_layout`: the ground model, per-tile scenery from
:mod:`sim.worldgen.urban_scenery` (instanced asset visuals, merged textured meshes, one trimesh
collision per tile and the sidecar proxies) and the kinematic agents. Everything here is
standard-library only and deterministic in the seed.
"""

from __future__ import annotations

from pathlib import Path

from sim.worldgen.assembly import assemble_world
from sim.worldgen.dressing import Dressing
from sim.worldgen.layout import URBAN_SIDEWALK_WIDTH_M, UrbanLayout, road_half_width
from sim.worldgen.plan import checked_transit_altitude
from sim.worldgen.scenario import follow_manifest, procedural_manifest_fields
from sim.worldgen.sdf import (
    SURFACE_COLOURS, TERRAIN_COLLIDE_BITMASK, collide_surface, static_model, visual,
)
from sim.worldgen.urban import urban_areas
from sim.worldgen.urban_grid import MAP_HALF_M
from sim.worldgen.urban_scenery import city_scenery


def build_urban_procedural(layout: UrbanLayout, world_dir: Path, *, textures: bool = True,
                           dressing: Dressing) -> tuple[str, list[dict], dict[str, bytes | str]]:
    """The generated city world: flat ground, per-tile scenery, then the kinematic actors.

    Agents are dressed first: clutter placement avoids every actor route, and its scenery
    pieces join the same per-tile emission as roads, buildings and street furniture.
    """
    world_name = f"horus_urban_p{layout.seed}"
    ground = (
        f'<collision name="terrain_collision"><pose>0 0 -0.5 0 0 0</pose><geometry>'
        f"<box><size>{2 * MAP_HALF_M} {2 * MAP_HALF_M} 1</size></box></geometry>"
        f"{collide_surface(TERRAIN_COLLIDE_BITMASK)}</collision>"
        + visual("ground", 0, 0, -0.02, 0,
                 f"<plane><normal>0 0 1</normal><size>{2 * MAP_HALF_M} {2 * MAP_HALF_M}</size></plane>",
                 dressing.environment.tint(SURFACE_COLOURS["urban_ground"], "ground"),
                 cast_shadows=False)
    )
    return assemble_world(
        "urban", layout, dressing, world_dir, world_name=world_name,
        static_models=[static_model("urban_ground", ground)], textures=textures,
        scenery=lambda clutter: city_scenery(layout, world_name, textures=textures,
                                             clutter=clutter),
        scenery_first=True)


def transit_altitude(layout: UrbanLayout) -> float:
    """Procedural rule (config/horus_procedural_urban.yaml): >= 38 m, >= tallest scenery
    top + 5.9 m, <= 48 m."""
    return checked_transit_altitude(max(38.0, layout.scenery_max_top_m + 5.9), 48.0,
                                    procedural=True)


def manifest_extras(layout: UrbanLayout, people: dict[str, dict]) -> tuple[dict, dict, dict]:
    """The generated city's ``(follow, areas, extra manifest fields)``."""
    city = layout.city
    follow = follow_manifest(layout, layout.follow_jogger, people, {
        "occluder_models_or_collisions": ["follow_shelter_glass", "follow_shelter_roof"],
        "occluder_type": layout.follow_occluder_kind,
        "occluder_xy": [layout.jogger_shelter_center[0], layout.jogger_shelter_center[1]],
    })
    areas = urban_areas(layout)
    mission = next(plaza for plaza in city.plazas if plaza.mission)
    areas["plaza"] = {"center": [mission.x, mission.y], "half_size": mission.half_size}
    areas["plazas"] = [{"center": [p.x, p.y], "half_size": p.half_size, "mission": p.mission}
                       for p in city.plazas]
    areas["parks"] = [{"center": [p.x, p.y], "half_size": p.half_size, "mission": p.mission}
                      for p in city.parks]
    areas["arterial_roads"] = {"x": list(city.arterial_roads_x), "y": list(city.arterial_roads_y)}
    areas["road_half_width_m"] = {
        "x": [road_half_width(road, city.arterial_roads_x) for road in layout.roads_x],
        "y": [road_half_width(road, city.arterial_roads_y) for road in layout.roads_y],
    }
    areas["sidewalk_width_m"] = URBAN_SIDEWALK_WIDTH_M
    areas["building_count"] = len(layout.buildings)
    return follow, areas, procedural_manifest_fields(layout)


__all__ = ["build_urban_procedural"]
