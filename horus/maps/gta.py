"""GTA V map data (DurtyFree ``gta-v-data-dumps``) converted to the common schema in local NED.

Setup-time only (``scripts/maps/build_gta_tiles.py``); the runtime reads the tiles it writes.

GTA's world frame has X east, Y north and Z up, and headings turn counter-clockwise. The mod
places NED (north, east) at ``x = ox - north*sin(h) + east*cos(h)``, ``y = oy + north*cos(h) +
east*sin(h)`` (``gta/horus_mod/README.md``); ``game_to_ned`` is its inverse.

Layers built:
- ways: the road graph as polylines per street, broken at junctions (path nodes flagged on
  water are left out);
- places: junctions (named by the streets meeting there), pedestrian crossings, traffic-light
  nodes, and props from the object dumps (bus stops, ATMs, seats);
- areas: districts (zones, one rectangle per bound box).

The whole map is converted (Los Santos and Blaine County); ``overview_rule`` picks the freeways
and main roads the overview tiles keep.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from horus.maps.model import Area, MapData, Place, Way

# Object dump file stem -> (place kind, place name).
POI_KINDS = {
    "worldBusStops": ("bus_stop", "Bus stop"),
    "worldAtms": ("atm", "ATM"),
    "worldSeats": ("seat", "Seat"),
}


def game_to_ned(x: float, y: float, origin_x: float, origin_y: float, heading_deg: float) -> tuple[float, float]:
    h = math.radians(heading_deg)
    dx, dy = x - origin_x, y - origin_y
    return (dy * math.cos(h) - dx * math.sin(h), dx * math.cos(h) + dy * math.sin(h))


def build_gta_map(
    node_areas: Iterable[Mapping[str, Any]],
    zones: Iterable[Mapping[str, Any]],
    pois: Mapping[str, Iterable[Mapping[str, Any]]],
    origin: tuple[float, float, float],
    attribution: str,
    sources: dict[str, Any],
) -> MapData:
    """``origin`` is GTA ``(x, y, heading_deg)`` of NED (0, 0); see the module docstring."""
    ox, oy, heading = origin

    def ned(position: Mapping[str, Any]) -> tuple[float, float]:
        return game_to_ned(float(position["X"]), float(position["Y"]), ox, oy, heading)

    ways: dict[frozenset, Way] = {}
    places: list[Place] = []
    for area in node_areas:
        for node in area["Nodes"]:
            if node.get("IsOnWater"):
                continue
            here, name = ned(node["Position"]), _street(node)
            neighbour_names = set()
            for link in node.get("ConnectedNodes") or ():
                other = link["Node"]
                if other.get("IsOnWater"):
                    continue
                there, other_name = ned(other["Position"]), _street(other)
                if other_name:
                    neighbour_names.add(other_name)
                key = frozenset((_key(here), _key(there)))
                if len(key) == 2 and key not in ways:
                    kind = "freeway" if node.get("IsFreeway") and other.get("IsFreeway") else "road"
                    ways[key] = Way(kind, (here, there), name or other_name)
            if node.get("IsJunction"):
                streets = sorted({name, *neighbour_names} - {""})
                places.append(Place(" / ".join(streets) or "Junction", "junction", *here))
            if node.get("IsPedCrossway"):
                places.append(Place(f"Crossing on {name}" if name else "Crossing", "crossing", *here))
            if node.get("TrafficlightExists"):
                places.append(Place(f"Traffic lights on {name}" if name else "Traffic lights", "traffic_light", *here))
    for stem, objects in pois.items():
        kind, label = POI_KINDS[stem]
        places.extend(Place(label, kind, *ned(o["Position"])) for o in objects)
    areas = []
    for zone in zones:
        for bound in zone.get("Bounds") or ():
            low, high = bound["Minimum"], bound["Maximum"]
            corners = ((low["X"], low["Y"]), (low["X"], high["Y"]), (high["X"], high["Y"]), (high["X"], low["Y"]))
            outline = tuple(game_to_ned(float(x), float(y), ox, oy, heading) for x, y in corners)
            areas.append(Area("district", outline, str(zone.get("DisplayName") or zone.get("Name") or "")))
    return MapData(
        places=tuple(places), ways=_chain(tuple(ways.values())), areas=tuple(areas),
        attribution=attribution, sources=sources,
    )


def overview_rule(node_areas: Iterable[Mapping[str, Any]], origin: tuple[float, float, float]) -> Callable[[Way], bool]:
    """The overview tiles' rule: freeways, and roads at least half of whose points are main-road
    nodes (neither back road nor gravel)."""
    ox, oy, heading = origin
    main = frozenset(
        _key(game_to_ned(float(node["Position"]["X"]), float(node["Position"]["Y"]), ox, oy, heading))
        for area in node_areas for node in area["Nodes"]
        if not node.get("IsBackroad") and not node.get("IsGravelRoad")
    )

    def keep(way: Way) -> bool:
        return way.kind == "freeway" or 2 * sum(_key(p) in main for p in way.points) >= len(way.points)

    return keep


def _chain(edges: tuple[Way, ...]) -> tuple[Way, ...]:
    """Join two-point edges of the same street and kind into polylines, breaking at junctions
    (a point where other than two of that street's edges meet) and at dead ends."""
    by_street: dict[tuple[str, str], list[Way]] = {}
    for edge in edges:
        by_street.setdefault((edge.kind, edge.name), []).append(edge)
    chained: list[Way] = []
    for (kind, name), group in by_street.items():
        links: dict[tuple[int, int], list[int]] = {}
        for index, edge in enumerate(group):
            for point in edge.points:
                links.setdefault(_key(point), []).append(index)
        used = [False] * len(group)

        def walk(start: tuple[float, float], index: int) -> list[tuple[float, float]]:
            line = [start]
            while True:
                used[index] = True
                a, b = group[index].points
                end = b if _key(a) == _key(line[-1]) else a
                line.append(end)
                onward = [i for i in links[_key(end)] if not used[i]]
                if len(links[_key(end)]) != 2 or not onward:
                    return line
                index = onward[0]

        # Start at ends and junctions first, then whatever is left (closed rings).
        starts = sorted(range(len(group)), key=lambda i: min(len(links[_key(p)]) == 2 for p in group[i].points))
        for index in starts:
            if used[index]:
                continue
            a, b = group[index].points
            start = a if len(links[_key(a)]) != 2 else b if len(links[_key(b)]) != 2 else a
            chained.append(Way(kind, tuple(walk(start, index)), name))
    return tuple(chained)


def _street(node: Mapping[str, Any]) -> str:
    name = str(node.get("StreetName") or "")
    return "" if name == "0" else name


def _key(point: tuple[float, float]) -> tuple[int, int]:
    return (round(point[0] * 100), round(point[1] * 100))
