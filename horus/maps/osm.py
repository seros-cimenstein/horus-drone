"""OpenStreetMap (an Overpass JSON extract) converted to the common schema in local NED.

The provider reads the raw extract ``scripts/maps/fetch_osm.py`` stores and converts it at
start-up about the map's origin (``horus.geo``, the flight link's flat-earth formula), so a moved
origin needs no refetch. Mapped:

- ways: footways, paths, steps, cycleways and pedestrian streets by their ``highway`` value,
  ``footway=crossing`` as ``crossing``, every other highway as ``road``;
- walkable: closed pedestrian ways and footway areas (``area=yes``), and ``area:highway`` areas;
- obstacles: buildings;
- places: crossings, entrances, bus stops, amenities and shops (nodes, or a closed way at its
  outline's centroid);
- areas: named parks, squares and land uses.

Relations (multipolygons) are left out. Data © OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from horus.geo import ned_from_latlon
from horus.maps.model import Area, MapData, Place, Way

ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
_FOOT_HIGHWAYS = {"footway", "path", "pedestrian", "steps", "cycleway", "living_street", "track", "bridleway"}
_AREA_TAGS = ("leisure", "landuse", "place")


def osm_to_map(extract: Mapping[str, Any], origin: tuple[float, float], sources: dict[str, Any]) -> MapData:
    elements = extract.get("elements", ())
    nodes = {e["id"]: ned_from_latlon(e["lat"], e["lon"], origin) for e in elements if e.get("type") == "node"}
    places: list[Place] = []
    ways: list[Way] = []
    walkable: list[Area] = []
    obstacles: list[Area] = []
    areas: list[Area] = []
    for element in elements:
        tags = element.get("tags") or {}
        name = str(tags.get("name", ""))
        if element.get("type") == "node":
            place = _place(tags, name, *nodes[element["id"]])
            if place is not None:
                places.append(place)
            continue
        if element.get("type") != "way":
            continue
        points = tuple(nodes[n] for n in element.get("nodes", ()) if n in nodes)
        if len(points) < 2:
            continue
        closed = len(points) >= 4 and points[0] == points[-1]
        outline = points[:-1] if closed else ()
        highway = tags.get("highway")
        if closed and "building" in tags:
            obstacles.append(Area("building", outline, name))
        elif closed and "area:highway" in tags:
            walkable.append(Area(str(tags["area:highway"]), outline, name))
        elif highway and closed and (tags.get("area") == "yes" or highway == "pedestrian"):
            walkable.append(Area(str(highway), outline, name))
        elif highway:
            if tags.get("footway") == "crossing":
                kind = "crossing"
            elif highway in _FOOT_HIGHWAYS:
                kind = str(highway)
            else:
                kind = "road"
            ways.append(Way(kind, points, name))
        elif closed and name and any(key in tags for key in _AREA_TAGS):
            key = next(key for key in _AREA_TAGS if key in tags)
            areas.append(Area(str(tags[key]), outline, name))
        if closed and not highway:
            n = len(outline)
            centroid = (sum(p[0] for p in outline) / n, sum(p[1] for p in outline) / n)
            place = _place(tags, name, *centroid)
            if place is not None:
                places.append(Place(place.name, place.kind, place.north_m, place.east_m, outline))
    return MapData(
        places=tuple(places), ways=tuple(ways), areas=tuple(areas), walkable=tuple(walkable),
        obstacles=tuple(obstacles), attribution=ATTRIBUTION, sources=sources,
    )


def _place(tags: Mapping[str, Any], name: str, north: float, east: float) -> Place | None:
    if tags.get("highway") == "crossing" or tags.get("railway") == "crossing":
        return Place(name or "Crossing", "crossing", north, east)
    if "entrance" in tags:
        return Place(name or str(tags.get("ref") or "Entrance"), "entrance", north, east)
    if tags.get("highway") == "bus_stop":
        return Place(name or "Bus stop", "bus_stop", north, east)
    for key in ("amenity", "shop"):
        if key in tags:
            kind = str(tags[key])
            return Place(name or kind.replace("_", " "), kind, north, east)
    return None
