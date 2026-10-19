"""World assembly shared by every outdoor builder.

``assemble_world`` dresses a layout's people (and an urban layout's circulating vehicles),
adds the procedural dressing (fauna, clutter placement, the event schedule), emits the
builder's scenery and closes the world with the home pad and the drone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import math
from pathlib import Path
from typing import Any

from sim.worldgen.dressing import (
    Dressing, add_clutter, add_fauna, animal_models, dress_person, fauna_areas, person_files,
    sample_events,
)
from sim.worldgen.layout import VehiclePlacement
from sim.worldgen.scenario import person_record
from sim.worldgen.sdf import (
    PAD_COLLIDE_BITMASK, PROCEDURAL_DRONE_MODEL, home_pad, include, pedestrian_include, world_header,
)
from sim.worldgen.vehicles import vehicle_model, vehicle_record

Files = dict[str, bytes | str]
# Called with the placed clutter items; returns the scenery SDF text and its files.
SceneryEmitter = Callable[[list[dict]], tuple[str, Files]]


def _flat_ground(x: float, y: float) -> float:
    return 0.0


def assemble_world(kind: str, layout: Any, dressing: Dressing, world_dir: Path, *,
                   world_name: str, static_models: Sequence[str], textures: bool,
                   height: Callable[[float, float], float] | None = None,
                   paint: Callable[[VehiclePlacement], tuple[float, float, float] | None] | None = None,
                   scenery: SceneryEmitter | None = None,
                   scenery_first: bool = False) -> tuple[str, list[dict], Files]:
    """The world SDF, its agent records and the generated actor and scenery files.

    ``height`` stands people on the terrain (flat ground leaves them at z 0); ``paint`` picks
    a vehicle's primitive body colour. Models follow ``static_models``, then the actors
    (people, vehicles, animals) and the ``scenery`` text, or the scenery before the actors
    with ``scenery_first``.
    """
    agents: list[dict] = []
    people: list[str] = []
    for person in layout.people:
        record = dress_person(dressing, person_record(person))
        if height is not None:
            record["z"] = height(*person.position)
        people.append(pedestrian_include(record))
        agents.append(record)
    files: Files = {} if dressing.canonical else person_files(dressing, agents, textures=textures)
    vehicles: list[str] = []
    for vehicle in layout.vehicles if kind == "urban" else ():
        vehicles.append(vehicle_model(vehicle.model, vehicle.type, vehicle.position[0],
                                      vehicle.position[1], 0.0, vehicle.yaw, static=False,
                                      variant=vehicle.variant,
                                      paint=None if paint is None else paint(vehicle)))
        agents.append(vehicle_record(vehicle))
    animals: list[str] = []
    if not dressing.canonical:
        ground = height or _flat_ground
        fauna = add_fauna(dressing, kind, layout, agents, fauna_areas(kind, layout), ground)
        animals = animal_models(fauna, ground)
        agents.extend(fauna)
        add_clutter(dressing, kind, layout, agents, ground)
        sample_events(dressing, agents, layout.seed)
    scenery_models: list[str] = []
    if scenery is not None:
        scenery_sdf, scenery_files = scenery(dressing.clutter)
        scenery_models.append(scenery_sdf)
        files.update(scenery_files)
    actors = [*people, *vehicles, *animals]
    models = ([*static_models, *scenery_models, *actors] if scenery_first
              else [*static_models, *actors, *scenery_models])
    if dressing.canonical:
        pad, drone_model = home_pad(world_dir), "horus_iris_outdoor"
    else:
        pad, drone_model = home_pad(world_dir, PAD_COLLIDE_BITMASK), PROCEDURAL_DRONE_MODEL
    drone = include(drone_model, "horus_iris", 0, 0, 0.195, math.pi / 2)
    world = (world_header(world_name, dressing.environment) + "\n".join(models) + "\n" + pad
             + "\n" + drone + "\n  </world></sdf>\n")
    return world, agents, files
