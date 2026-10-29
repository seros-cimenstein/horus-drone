"""Map providers for the commander: a common schema in local NED (``horus.maps.model``).

``map.provider`` selects the source:
- ``none``: no third-party map. The drone's own map, and the tools built on it, work in every world.
- ``gta``: the GTA V map, converted once at setup by ``scripts/maps/fetch_gta_map.py`` into a
  common-schema JSON at ``map.data_path`` (the runtime only reads that file), plus GTA's ped
  navmesh as the walkable layer at ``map.walkable_path`` (``scripts/maps/fetch_gta_walkable.py``).
- ``osm``: an OpenStreetMap Overpass extract (``scripts/maps/fetch_osm.py``) converted at start-up
  about ``map.origin_*`` or the flight's global origin (``horus.maps.osm``).
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path

from horus.config import MapSettings
from horus.maps.model import Area, MapData, Place, Point, Way, contains, load_map_file
from horus.maps.osm import osm_to_map

__all__ = [
    "Area", "MapData", "MapProvider", "Place", "Point", "Way", "contains", "create_map_provider", "load_map_file",
]


@dataclass(frozen=True, slots=True)
class MapProvider:
    name: str
    data: MapData


def create_map_provider(settings: MapSettings, default_origin: tuple[float, float]) -> MapProvider | None:
    """The configured provider; ``default_origin`` (lat, lon) places an OSM extract when the map
    section names no origin of its own."""
    if settings.provider == "none":
        return None
    if settings.provider == "osm":
        path = Path(_existing(settings.data_path, "data_path", "fetch_osm.py"))
        origin = (
            (settings.origin_latitude_deg, settings.origin_longitude_deg)
            if settings.origin_latitude_deg is not None else default_origin
        )
        meta = path.with_suffix(".meta.json")
        sources = {**(json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}),
                   "origin_lat_lon": list(origin)}
        data = osm_to_map(json.loads(path.read_text(encoding="utf-8")), origin, sources)
    else:
        data = load_map_file(_existing(settings.data_path, "data_path", "fetch_gta_map.py"))
    if settings.walkable_path is not None:
        walkable = load_map_file(_existing(settings.walkable_path, "walkable_path", "fetch_gta_walkable.py"))
        data = replace(
            data,
            walkable=data.walkable + walkable.walkable,
            attribution="; ".join(text for text in (data.attribution, walkable.attribution) if text),
            sources={**data.sources, "walkable": walkable.sources},
        )
    return MapProvider(settings.provider, data)


def _existing(path: str, setting: str, fetcher: str) -> str:
    if not Path(path).is_file():
        raise FileNotFoundError(f"map.{setting} {path} does not exist; build it with scripts/maps/{fetcher}")
    return path
