"""Fetch an OpenStreetMap extract around a point into data/maps/osm/ (map.provider: osm).

One Overpass API request for the walkable network, roads, buildings, entrances, crossings, bus
stops, amenities, shops and named parks/squares within a box around ``--lat``/``--lon`` (default
the simulators' home, ``horus.config.FlightSettings``). Overpass usage policy: one modest request
with an identifying User-Agent, and an existing extract is reused unless ``--refresh``. Nominatim
is not used.

Writes ``extract.json`` (the raw Overpass JSON, converted to NED by the provider at start-up) and
``extract.meta.json`` (query, URL, time, SHA-256). Data © OpenStreetMap contributors, ODbL; it
stays under the gitignored ``data/`` and is never committed.

Usage: python scripts/maps/fetch_osm.py [--lat -35.363262 --lon 149.165237] [--half-size 1000]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from horus.config import FlightSettings  # noqa: E402
from horus.geo import latlon_from_ned  # noqa: E402

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "HORUS-drone-research/1.0 (scripts/maps/fetch_osm.py; one extract per site, cached)"
QUERY = """[out:json][timeout:90];
(
  way["highway"]({bbox});
  way["area:highway"]({bbox});
  way["building"]({bbox});
  node["entrance"]({bbox});
  node["highway"~"^(crossing|bus_stop)$"]({bbox});
  nwr["amenity"]({bbox});
  nwr["shop"]({bbox});
  way["leisure"]["name"]({bbox});
  way["landuse"]["name"]({bbox});
  way["place"]["name"]({bbox});
);
out body;
>;
out skel qt;
"""


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    home = FlightSettings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lat", type=float, default=home.global_origin_latitude_deg)
    parser.add_argument("--lon", type=float, default=home.global_origin_longitude_deg)
    parser.add_argument("--half-size", type=float, default=1000.0, help="metres from the point to each box edge")
    parser.add_argument("--out", type=Path, default=root / "data/maps/osm/extract.json")
    parser.add_argument("--refresh", action="store_true", help="fetch again even if an extract exists")
    args = parser.parse_args()

    if args.out.exists() and not args.refresh:
        print(f"{args.out} exists; reusing it (--refresh to fetch again)")
        return
    south, west = latlon_from_ned(-args.half_size, -args.half_size, (args.lat, args.lon))
    north, east = latlon_from_ned(args.half_size, args.half_size, (args.lat, args.lon))
    query = QUERY.replace("{bbox}", f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}")
    request = urllib.request.Request(
        OVERPASS_URL, data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = response.read()
    extract = json.loads(body)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(body)
    meta = {
        "source": OVERPASS_URL,
        "query": query,
        "centre_lat_lon": [args.lat, args.lon],
        "half_size_m": args.half_size,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "osm_timestamp": extract.get("osm3s", {}).get("timestamp_osm_base"),
        "sha256": hashlib.sha256(body).hexdigest(),
        "licence": "© OpenStreetMap contributors, ODbL 1.0",
    }
    args.out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    kinds = {}
    for element in extract.get("elements", ()):
        kinds[element["type"]] = kinds.get(element["type"], 0) + 1
    print(f"{args.out}: {kinds} ({len(body) >> 10} KiB, OSM data as of {meta['osm_timestamp']})")


if __name__ == "__main__":
    main()
