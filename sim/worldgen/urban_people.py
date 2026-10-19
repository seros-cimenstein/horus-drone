"""People of the procedural city: the follow-jogger route with its bus-shelter occluder, bus
stops, the plaza crowd, park sitters, commuters, zebra crossers, joggers and the collapsed
sidewalk person.
"""

from __future__ import annotations

import math
import random

from sim.worldgen.layout import (
    URBAN_SIDEWALK_WIDTH_M, Bench, BusStopSpec, ParkSpec, PersonPlacement, PlazaSpec,
    person_placement, road_half_width,
)
from sim.worldgen.urban_grid import (
    ACTOR_INSET_M, Block, Point, inside_roads, segment_clear_of, sidewalk_interval,
)
from sim.worldgen.validation import SampleRejected


def mission_crossings(rng: random.Random, arms: list[dict], sites: list[Point]) -> list[dict]:
    """Two to four crossing arms near mission sites (the zebra the crossing people use)."""
    def inside(arm: dict) -> bool:
        cx, cy = arm["center"]
        reach = arm["half"] + 0.9 + 1.0
        x = abs(cx) + (reach if arm["axis"] == "x" else 0.0)
        y = abs(cy) + (reach if arm["axis"] == "y" else 0.0)
        return x <= ACTOR_INSET_M and y <= ACTOR_INSET_M

    usable = [arm for arm in arms if inside(arm)]
    scored = sorted(usable, key=lambda arm: min(
        math.hypot(arm["center"][0] - site[0], arm["center"][1] - site[1]) for site in sites))
    pool = scored[:12]
    rng.shuffle(pool)
    count = rng.randint(2, min(4, len(pool)))
    chosen: list[dict] = []
    for arm in pool:
        if all(math.hypot(arm["center"][0] - other["center"][0],
                          arm["center"][1] - other["center"][1]) > 30.0 for other in chosen):
            chosen.append(arm)
        if len(chosen) == count:
            break
    if not chosen:
        raise SampleRejected("no crossing arms inside the actor inset")
    return chosen


def sample_follow(rng: random.Random, roads_x: tuple[float, ...], roads_y: tuple[float, ...],
                  arterials_x: tuple[float, ...], arterials_y: tuple[float, ...]) -> dict:
    """A sidewalk follow route >= 100 m from home with a bus-shelter occluder on it."""
    for _ in range(400):
        vertical = rng.random() < 0.5
        arterials = arterials_x if vertical else arterials_y
        perpendicular = roads_y if vertical else roads_x
        roads = inside_roads(roads_x if vertical else roads_y, arterials)
        road = rng.choice(roads)
        half = road_half_width(road, arterials)
        lane = road + rng.choice((-1.0, 1.0)) * (half + 0.9)
        interval = sidewalk_interval(rng, perpendicular, min_length=64.0)
        if interval is None:
            continue
        low, high = interval
        route = ((lane, low), (lane, high)) if vertical else ((low, lane), (high, lane))
        if min(math.hypot(*route[0]), math.hypot(*route[1])) < 100.0:
            continue
        fraction = rng.uniform(0.45, 0.60)
        sx = route[0][0] + (route[1][0] - route[0][0]) * fraction
        sy = route[0][1] + (route[1][1] - route[0][1]) * fraction
        side = 1.0 if lane > road else -1.0
        back = (side, 0.0) if vertical else (0.0, side)
        return {"route": route, "start": route[0], "shelter": (sx, sy),
                "shelter_yaw": math.atan2(back[1], back[0])}
    raise SampleRejected("no follow route")


def sample_crowd(rng: random.Random, plaza: PlazaSpec,
                 count: int) -> tuple[PersonPlacement, ...]:
    assets = ("person_blue_kinematic", "person_green_kinematic", "person_dark",
              "person_orange", "person_hivis")
    edge = plaza.half_size - 1.0
    people = []
    for index in range(count):
        x = plaza.x + rng.uniform(-edge, edge)
        y = plaza.y + rng.uniform(-edge, edge)
        people.append(person_placement(f"civ_plaza_crowd_{index + 1:02d}", "plaza_crowd",
                              assets[index % len(assets)], (x, y),
                              rng.uniform(-math.pi, math.pi)))
    return tuple(people)


def bus_stop_at(rng: random.Random, block: Block, roads_x: tuple[float, ...],
                arterials_x: tuple[float, ...], roads_y: tuple[float, ...],
                arterials_y: tuple[float, ...], avoid: list[Point]) -> BusStopSpec:
    """A shelter on the block's outer sidewalk platform: back wall away from the road.

    ``avoid`` points (the collapsed casualty, the follow occluder) keep 14 m: a shelter's
    glass end walls and roof must never crowd a mission target's vantage column.
    """
    candidates = []
    for vertical, road, arterials in ((True, block.rx0, arterials_x), (True, block.rx1, arterials_x),
                                      (False, block.ry0, arterials_y), (False, block.ry1, arterials_y)):
        half = road_half_width(road, arterials)
        center_offset = half + URBAN_SIDEWALK_WIDTH_M + 1.55
        for side in (-1.0, 1.0):
            if vertical:
                x = road + side * center_offset
                y = (block.y0 + block.y1) / 2 + rng.uniform(-6.0, 6.0)
                back = (side, 0.0)
            else:
                x = (block.x0 + block.x1) / 2 + rng.uniform(-6.0, 6.0)
                y = road + side * center_offset
                back = (0.0, side)
            if any(math.hypot(x - px, y - py) < 14.0 for px, py in avoid):
                continue
            candidates.append(BusStopSpec(round(x, 3), round(y, 3),
                                          round(math.atan2(back[1], back[0]), 4)))
    if not candidates:
        raise SampleRejected("no bus stop clear of mission targets")
    return rng.choice(candidates)


def sample_bus_waiting(rng: random.Random, stop: BusStopSpec) -> tuple[PersonPlacement, ...]:
    assets = ("person_blue_kinematic", "person_green_kinematic", "person_dark", "person_orange")
    back = (math.cos(stop.yaw), math.sin(stop.yaw))
    along = (-back[1], back[0])
    facing = math.atan2(-back[1], -back[0])
    count = rng.randint(3, 5)
    people = []
    for index in range(count):
        u = -1.6 + 3.2 * (index + 0.5) / count + rng.uniform(-0.15, 0.15)
        offset = rng.uniform(0.72, 1.05)
        x = stop.x - back[0] * offset + along[0] * u
        y = stop.y - back[1] * offset + along[1] * u
        people.append(person_placement(f"civ_bus_waiting_{index + 1:02d}", "bus_waiting",
                              assets[index % len(assets)], (x, y),
                              facing + rng.uniform(-0.3, 0.3)))
    return tuple(people)


def sample_sitters(rng: random.Random, mission_park: ParkSpec,
                   parks: tuple[ParkSpec, ...]) -> tuple[tuple[PersonPlacement, ...], list[Bench]]:
    """Sitting people beside the mission park's paths, each on its own bench."""
    count = rng.randint(2, 4)
    sitters: list[PersonPlacement] = []
    benches: list[Bench] = []
    slots: list[Point] = []
    edge = mission_park.half_size - 1.5
    for index in range(count):
        for _ in range(200):
            side = rng.choice((-1.0, 1.0))
            if rng.random() < 0.5:
                x = mission_park.x + side * rng.uniform(2.0, edge)
                y = mission_park.y + rng.uniform(-edge, edge)
            else:
                x = mission_park.x + rng.uniform(-edge, edge)
                y = mission_park.y + side * rng.uniform(2.0, edge)
            if any(math.hypot(x - px, y - py) < 4.0 for px, py in slots):
                continue
            slots.append((x, y))
            dx, dy = mission_park.x - x, mission_park.y - y
            norm = math.hypot(dx, dy) or 1.0
            facing = math.atan2(dy / norm, dx / norm)
            sitters.append(person_placement(f"civ_park_sitting_{index + 1:02d}", "park_sitting",
                                   "person_sitting", (x, y), facing))
            benches.append(Bench(round(x - math.cos(facing) * 0.55, 3),
                                 round(y - math.sin(facing) * 0.55, 3), round(facing, 4)))
            break
    if len(sitters) < 2:
        raise SampleRejected("no park sitter slots")
    for park in parks[1:]:
        for _ in range(rng.randint(1, 2)):
            for _ in range(50):
                bx = park.x + rng.uniform(-park.half_size + 2.0, park.half_size - 2.0)
                by = park.y + rng.uniform(-park.half_size + 2.0, park.half_size - 2.0)
                if abs(bx - park.x) < 2.5 or abs(by - park.y) < 2.5:
                    continue
                benches.append(Bench(round(bx, 3), round(by, 3), round(rng.uniform(-math.pi, math.pi), 4)))
                break
    return tuple(sitters), benches


def sample_commuters(rng: random.Random, roads_x: tuple[float, ...], roads_y: tuple[float, ...],
                     arterials_x: tuple[float, ...], arterials_y: tuple[float, ...],
                     sites: list[Point]) -> tuple[PersonPlacement, ...]:
    assets = ("person_blue_kinematic", "person_green_kinematic", "person_dark",
              "person_orange", "person_hivis")
    people = []
    count = rng.randint(6, 10)
    for index in range(count):
        for _ in range(200):
            vertical = rng.random() < 0.5
            arterials = arterials_x if vertical else arterials_y
            perpendicular = roads_y if vertical else roads_x
            roads = inside_roads(roads_x if vertical else roads_y, arterials)
            road = rng.choice(roads)
            half = road_half_width(road, arterials)
            lane = road + rng.choice((-1.0, 1.0)) * (half + 0.9)
            interval = sidewalk_interval(rng, perpendicular, min_length=24.0)
            if interval is None:
                continue
            low, high = interval
            if rng.random() < 0.6:
                site = sites[rng.randrange(len(sites))]
                site_axis = site[1] if vertical else site[0]
                if not (low - 70.0 <= site_axis <= high + 70.0):
                    continue
            length = rng.uniform(24.0, min(48.0, high - low))
            if rng.random() < 0.5:
                start, end = low, low + length
            else:
                start, end = high, high - length
            route = ((lane, start), (lane, end)) if vertical else ((start, lane), (end, lane))
            yaw = math.atan2(route[1][1] - route[0][1], route[1][0] - route[0][0])
            people.append(person_placement(f"civ_commuter_{index + 1:02d}", "commuter",
                                  assets[rng.randrange(len(assets))], route[0], yaw, "route",
                                  route=route, speed=rng.uniform(1.1, 1.6), mode="pingpong"))
            break
    if len(people) < 6:
        raise SampleRejected("too few commuters")
    return tuple(people)


def sample_crossings(rng: random.Random, arms: list[dict]) -> tuple[PersonPlacement, ...]:
    assets = ("person_blue_kinematic", "person_green_kinematic", "person_orange")
    people = []
    for index, arm in enumerate(arms, 1):
        cx, cy = arm["center"]
        reach = arm["half"] + 0.9
        if arm["axis"] == "x":
            start, end = (cx - reach, cy), (cx + reach, cy)
        else:
            start, end = (cx, cy - reach), (cx, cy + reach)
        if rng.random() < 0.5:
            start, end = end, start
        people.append(person_placement(f"civ_crossing_{index:02d}", "crossing",
                              assets[index % len(assets)], start, 0.0, "route",
                              route=(start, end), speed=1.0, mode="pingpong",
                              offset=rng.choice((0.0, 12.0, 20.0))))
    return tuple(people)


def sample_joggers(rng: random.Random, roads_x: tuple[float, ...], roads_y: tuple[float, ...],
                   arterials_x: tuple[float, ...], arterials_y: tuple[float, ...]
                   ) -> tuple[PersonPlacement, ...]:
    assets = ("person_orange", "person_blue_kinematic", "person_hivis")
    people = []
    for index in range(2):
        for _ in range(200):
            vertical = rng.random() < 0.5
            arterials = arterials_x if vertical else arterials_y
            perpendicular = roads_y if vertical else roads_x
            roads = inside_roads(roads_x if vertical else roads_y, arterials)
            road = rng.choice(roads)
            half = road_half_width(road, arterials)
            lane = road + rng.choice((-1.0, 1.0)) * (half + 0.9)
            interval = sidewalk_interval(rng, perpendicular, min_length=24.0)
            if interval is None:
                continue
            low, high = interval
            route = ((lane, low), (lane, high)) if vertical else ((low, lane), (high, lane))
            yaw = math.atan2(route[1][1] - route[0][1], route[1][0] - route[0][0])
            people.append(person_placement(f"civ_jogger_{index + 1:02d}", "jogger",
                                  assets[index % len(assets)], route[0], yaw, "route",
                                  route=route, speed=2.5, mode="pingpong"))
            break
    if len(people) < 2:
        raise SampleRejected("too few joggers")
    return tuple(people)


def sample_collapsed(rng: random.Random, roads_x: tuple[float, ...], roads_y: tuple[float, ...],
                     arterials_x: tuple[float, ...], arterials_y: tuple[float, ...],
                     follow_route: tuple[Point, Point]) -> PersonPlacement:
    for _ in range(400):
        vertical = rng.random() < 0.5
        arterials = arterials_x if vertical else arterials_y
        perpendicular = roads_y if vertical else roads_x
        roads = inside_roads(roads_x if vertical else roads_y, arterials)
        road = rng.choice(roads)
        half = road_half_width(road, arterials)
        lane = road + rng.choice((-1.0, 1.0)) * (half + 0.9)
        interval = sidewalk_interval(rng, perpendicular, min_length=4.0, margin=14.0)
        if interval is None:
            continue
        along = rng.uniform(*interval)
        point = (lane, along) if vertical else (along, lane)
        if math.hypot(*point) < 120.0:
            continue
        if not segment_clear_of(point, [follow_route], 30.0):
            continue
        return person_placement("civ_collapsed_01", "collapsed", "person_collapsed_kinematic", point,
                       rng.uniform(-math.pi, math.pi))
    raise SampleRejected("no collapsed location")
