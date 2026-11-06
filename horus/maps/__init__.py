"""Map providers for the commander: a common schema in local NED (``horus.maps.model``), served as
tiles (``horus.maps.tiles``).

``map.provider`` selects the source:
- ``none``: no third-party map. The drone's own map, and the tools built on it, work in every world.
- ``gta``: the whole GTA V map (Los Santos and Blaine County: road graph, districts, street
  furniture and GTA's ped navmesh as walkable ground), built once at setup by
  ``scripts/maps/build_gta_tiles.py`` into the tile directory at ``map.data_path``. The runtime
  reads only that directory, and keeps only its index, places and a few tiles in memory.
- ``osm``: an OpenStreetMap Overpass extract (``scripts/maps/fetch_osm.py``) converted at start-up
  about ``map.origin_*`` or the flight's global origin (``horus.maps.osm``) and held in memory.

Every provider has ``name``, ``attribution``, ``searchable`` (the places and named areas
find_place ranks), ``area(north_m, east_m, radius_m)``, and the tiles: ``version``,
``index_json()`` and ``tile_json(level, x, y)``.
"""

import json
from pathlib import Path

from horus.config import MapSettings
from horus.maps.model import Area, MapData, Place, Point, Way, contains, load_map_file
from horus.maps.osm import osm_to_map
from horus.maps.tiles import MemoryMap, TiledMap

__all__ = [
    "Area", "MapData", "MapProvider", "MemoryMap", "Place", "Point", "TiledMap", "Way", "contains",
    "create_map_provider", "load_map_file",
]

MapProvider = MemoryMap | TiledMap


def create_map_provider(settings: MapSettings, default_origin: tuple[float, float]) -> MapProvider | None:
    """The configured provider; ``default_origin`` (lat, lon) places an OSM extract when the map
    section names no origin of its own."""
    if settings.provider == "none":
        return None
    if settings.provider == "osm":
        path = Path(settings.data_path)
        if not path.is_file():
            raise FileNotFoundError(f"map.data_path {path} does not exist; fetch it with scripts/maps/fetch_osm.py")
        origin = (
            (settings.origin_latitude_deg, settings.origin_longitude_deg)
            if settings.origin_latitude_deg is not None else default_origin
        )
        meta = path.with_suffix(".meta.json")
        sources = {**(json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}),
                   "origin_lat_lon": list(origin)}
        return MemoryMap("osm", osm_to_map(json.loads(path.read_text(encoding="utf-8")), origin, sources))
    directory = Path(settings.data_path)
    if not (directory / "index.json").is_file():
        raise FileNotFoundError(
            f"map.data_path {directory} is not a tile directory; build it with scripts/maps/build_gta_tiles.py")
    return TiledMap("gta", directory)
