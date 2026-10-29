"""Fetch the GTA V map around HORUS's origin into data/maps/gta/map.json (map.provider: gta).

Downloads DurtyFree ``gta-v-data-dumps`` at a pinned commit (road nodes with street names and
flags, districts, bus stops, ATMs, seats), checks each file's SHA-256, crops to a radius around
``HorusGta.ini [origin]`` and converts game x/y to local NED (``horus.maps.gta``). The downloads
are kept under ``data/maps/gta/source/`` (gitignored), so a second run is offline.

The dumps' repository has no licence and the data is extracted from Rockstar's game: local
research use only, never committed or redistributed.

Usage: python scripts/maps/fetch_gta_map.py [--ini gta/horus_mod/HorusGta.ini] [--radius 600]
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from horus.maps.gta import POI_KINDS, build_gta_map  # noqa: E402
from horus.maps.model import map_to_json  # noqa: E402

REPOSITORY = "DurtyFree/gta-v-data-dumps"
COMMIT = "b65684e00f689fdec405c5f1055322c802d3c895"
# Path in the repository -> SHA-256 of the file at COMMIT (checked 2026-10-24).
FILES = {
    "nodes.zip": "39ea47a1cce9b56affc09f76d5b5e3150dfc47824a0788bc3a15578e28d40ca6",
    "zones.json": "63a515c1e37a04135b0e667d6f00b506d29fdde1561911f1ec9479814bb9b08c",
    "objectslocations/worldBusStops.json": "bdf8b5a91788a9468c72d529270e58fecd636103f35f7632ab626aca1a6c6132",
    "objectslocations/worldAtms.json": "f9e923eb816059c06297dd9ef63a0944c14f759b19866d559111b221d780f00a",
    "objectslocations/worldSeats.json": "1fbf013263e77fdfc13e1dd163398b0f99878841e1d0dee755e759b14dc8d15e",
}
ATTRIBUTION = (
    f"GTA V map data from {REPOSITORY} @ {COMMIT[:7]} (no licence; extracted from Rockstar's "
    "Grand Theft Auto V): local research use only"
)


def fetch(path: str, cache: Path) -> bytes:
    local = cache / path
    if local.exists():
        data = local.read_bytes()
    else:
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{path}"
        print(f"downloading {url}", file=sys.stderr)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != FILES[path]:
        raise SystemExit(f"{path}: SHA-256 {digest} does not match the pinned {FILES[path]}")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(data)
    return data


def read_origin(ini: Path) -> tuple[float, float, float]:
    parser = configparser.ConfigParser(inline_comment_prefixes=(";",))
    parser.read(ini, encoding="utf-8")
    section = parser["origin"]
    return float(section["x"]), float(section["y"]), float(section.get("heading_deg", "0") or 0)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ini", type=Path, default=root / "gta/horus_mod/HorusGta.ini")
    parser.add_argument("--radius", type=float, default=600.0, help="crop radius around the origin (m)")
    parser.add_argument("--out", type=Path, default=root / "data/maps/gta/map.json")
    args = parser.parse_args()

    cache = args.out.parent / "source"
    origin = read_origin(args.ini)
    with zipfile.ZipFile(io.BytesIO(fetch("nodes.zip", cache))) as archive:
        nodes = json.loads(archive.read("nodes.json"))
    zones = json.loads(fetch("zones.json", cache))
    pois = {
        Path(path).stem: json.loads(fetch(path, cache))
        for path in FILES
        if Path(path).stem in POI_KINDS
    }
    sources = {
        "repository": REPOSITORY,
        "commit": COMMIT,
        "sha256": FILES,
        "origin_gta": {"x": origin[0], "y": origin[1], "heading_deg": origin[2]},
        "crop_radius_m": args.radius,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data = build_gta_map(nodes, zones, pois, origin, args.radius, ATTRIBUTION, sources)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(map_to_json(data), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{args.out}: {len(data.places)} places, {len(data.ways)} ways, {len(data.areas)} areas "
          f"within {args.radius:g} m of GTA ({origin[0]:g}, {origin[1]:g})")


if __name__ == "__main__":
    main()
