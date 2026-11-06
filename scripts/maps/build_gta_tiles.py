"""Build the whole GTA V map as tiles into data/maps/gta/tiles/ (map.provider: gta).

Sources, each pinned by SHA-256 and kept under ``source/`` beside the output (data/ is
gitignored), so a second run is offline:
- DurtyFree ``gta-v-data-dumps`` at a pinned commit: road nodes with street names and flags,
  districts, bus stops, ATMs, seats (converted by ``horus.maps.gta``);
- the same author's ped navmesh dump (4,921 meshes, 7.4 M polygons; a 466 MB 7z on mega.nz holding
  LZ4 MessagePack, extracted with ``7z``): its footpath and road polygons (neither underground,
  interior nor water) become the walkable layer, merged into outlines with holes per 250 m tile
  (``horus.maps.model.merge_polygons``).

Game x/y become local NED about ``HorusGta.ini [origin]``, and ``horus.maps.tiles`` writes the tile
directory: ``index.json``, ``search.json.gz`` and ``<level>/<x>_<y>.json.gz``. The same inputs
give the same bytes and version.

The navmesh needs msgpack, lz4 and cryptography, so the full build runs in a throwaway venv (HORUS
itself gains no dependency); ``--no-walkable`` leaves the navmesh out and needs only HORUS's own
environment:
    python -m venv /tmp/navmesh-venv
    /tmp/navmesh-venv/bin/pip install msgpack lz4 cryptography pyyaml
    /tmp/navmesh-venv/bin/python scripts/maps/build_gta_tiles.py [--out DIR] [--no-walkable]
The 659 MB extracted navmesh file is removed after the build. Neither dump has a licence and the
data is extracted from Rockstar's game: local research use only, never committed or redistributed.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import io
import json
import math
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from horus.maps.gta import POI_KINDS, build_gta_map, game_to_ned, overview_rule  # noqa: E402
from horus.maps.model import Area, merge_polygons  # noqa: E402
from horus.maps.tiles import LEVEL_SIZES_M, write_tile_directory  # noqa: E402

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
MEGA_URL = "https://mega.nz/file/lLZWwJbR#T3Xm3nQKRTP1Z0XrkzgbbINo54d6wK2Na4Nj6tYfbXI"
ARCHIVE_NAME = "navmeshes.7z"
ARCHIVE_SHA256 = "3e3792605a33a541fa16fdeaf011f95900516d85497318d6a2019104c208e605"
MESSAGEPACK_NAME = "navigationMeshes.msgpack"
MESSAGEPACK_SHA256 = "e7d308924f83e4ba5a0398647c03d3a245572eeb5bc453af1d82b344291c07d5"
ATTRIBUTION = (
    f"GTA V map data from {REPOSITORY} @ {COMMIT[:7]} (no licence; extracted from Rockstar's "
    "Grand Theft Auto V): local research use only"
)
NAVMESH_ATTRIBUTION = (
    "GTA V ped navmesh from DurtyFree/gta-v-data-dumps (mega.nz dump; no licence; extracted from "
    "Rockstar's Grand Theft Auto V): local research use only"
)
# NavigationMeshPoly MessagePack keys (navigationmesh.md in the dumps).
FOOTPATH, UNDERGROUND, WATER, INTERIOR, ROAD, POSITION, VERTICES = 2, 3, 5, 7, 9, 26, 27
WALKABLE_TOLERANCE_M = 0.3


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


def download_mega(url: str, out: Path) -> None:
    """A public mega.nz file: AES-128-CTR with the key and nonce folded into the link's fragment."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    file_id, key_b64 = url.split("/file/")[1].split("#")
    a = struct.unpack(">8I", base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4)))
    key = struct.pack(">4I", a[0] ^ a[4], a[1] ^ a[5], a[2] ^ a[6], a[3] ^ a[7])
    nonce = struct.pack(">4I", a[4], a[5], 0, 0)
    request = urllib.request.Request(
        "https://g.api.mega.co.nz/cs?id=1", data=json.dumps([{"a": "g", "g": 1, "p": file_id}]).encode(),
        headers={"Content-Type": "application/json"},
    )
    meta = json.load(urllib.request.urlopen(request, timeout=60))[0]
    print(f"downloading {meta['s'] >> 20} MiB from mega.nz", file=sys.stderr)
    decryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    out.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(meta["g"], timeout=120) as response, open(out, "wb") as stream:
        while chunk := response.read(1 << 20):
            stream.write(decryptor.update(chunk))


def check(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise SystemExit(f"{path}: SHA-256 {digest.hexdigest()} does not match the pinned {expected}")


def meshes(path: Path):
    """Stream ``List<NavigationMesh>`` from MessagePack-CSharp's Lz4BlockArray framing: an array of
    an ext 98 header (the uncompressed length of each block) and the LZ4 blocks themselves."""
    import lz4.block
    import msgpack

    with open(path, "rb") as stream:
        outer = msgpack.Unpacker(stream, raw=False, max_buffer_size=0, read_size=1 << 20)
        outer.read_array_header()
        lengths = msgpack.Unpacker(raw=False)
        lengths.feed(outer.unpack().data)
        blocks = iter(list(lengths))
        inner = msgpack.Unpacker(raw=False, strict_map_key=False, max_buffer_size=0)

        def read(next_item):
            while True:
                try:
                    return next_item()
                except msgpack.OutOfData:
                    inner.feed(lz4.block.decompress(outer.unpack(), uncompressed_size=next(blocks)))

        for _ in range(read(inner.read_array_header)):
            yield read(inner.unpack)


def navmesh_walkable(cache: Path, origin: tuple[float, float, float]) -> tuple[tuple[Area, ...], dict[str, int]]:
    """The navmesh's footpath and road polygons, merged per level-0 tile (by polygon centre)."""
    archive, extracted = cache / ARCHIVE_NAME, cache / MESSAGEPACK_NAME
    if not archive.exists():
        download_mega(MEGA_URL, archive)
    check(archive, ARCHIVE_SHA256)
    if not extracted.exists():
        subprocess.run(["7z", "x", "-y", f"-o{cache}", str(archive), MESSAGEPACK_NAME], check=True,
                       stdout=subprocess.DEVNULL)
    check(extracted, MESSAGEPACK_SHA256)
    ox, oy, heading = origin
    size = LEVEL_SIZES_M[0]
    by_tile: dict[tuple[int, int], list[tuple[str, tuple[tuple[float, float], ...]]]] = {}
    counts = {"footpath": 0, "road": 0}
    for _, _, _, mesh_polygons in meshes(extracted):
        for poly in mesh_polygons:
            if poly[UNDERGROUND] or poly[INTERIOR] or poly[WATER]:
                continue
            kind = "footpath" if poly[FOOTPATH] else "road" if poly[ROAD] else None
            if kind is None:
                continue
            counts[kind] += 1
            north, east = game_to_ned(poly[POSITION][0], poly[POSITION][1], ox, oy, heading)
            outline = tuple(game_to_ned(v[0], v[1], ox, oy, heading) for v in poly[VERTICES])
            by_tile.setdefault((math.floor(east / size), math.floor(north / size)), []).append((kind, outline))
    extracted.unlink()
    walkable: list[Area] = []
    for key in sorted(by_tile):
        walkable.extend(merge_polygons(by_tile.pop(key), WALKABLE_TOLERANCE_M))
    return tuple(walkable), counts


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ini", type=Path, default=root / "gta/horus_mod/HorusGta.ini")
    parser.add_argument("--out", type=Path, default=root / "data/maps/gta/tiles", help="tile directory")
    parser.add_argument("--no-walkable", action="store_true", help="leave GTA's ped navmesh out")
    args = parser.parse_args()

    started = time.monotonic()
    cache = args.out.parent / "source"
    origin = read_origin(args.ini)
    with zipfile.ZipFile(io.BytesIO(fetch("nodes.zip", cache))) as archive:
        nodes = json.loads(archive.read("nodes.json"))
    zones = json.loads(fetch("zones.json", cache))
    pois = {Path(path).stem: json.loads(fetch(path, cache)) for path in FILES if Path(path).stem in POI_KINDS}
    sources = {
        "repository": REPOSITORY,
        "commit": COMMIT,
        "sha256": FILES,
        "origin_gta": {"x": origin[0], "y": origin[1], "heading_deg": origin[2]},
    }
    data = build_gta_map(nodes, zones, pois, origin, ATTRIBUTION, sources)
    if not args.no_walkable:
        walkable, counts = navmesh_walkable(cache, origin)
        sources["navmesh"] = {
            "url": MEGA_URL.split("#")[0],
            "sha256": {ARCHIVE_NAME: ARCHIVE_SHA256, MESSAGEPACK_NAME: MESSAGEPACK_SHA256},
            "polygons": counts,
            "merge_tolerance_m": WALKABLE_TOLERANCE_M,
        }
        data = replace(data, walkable=walkable, attribution=f"{ATTRIBUTION}; {NAVMESH_ATTRIBUTION}")
    index = write_tile_directory(data, args.out, "gta", overview_rule(nodes, origin))

    print(f"{args.out}: version {index['version']}, {len(data.places)} places, {len(data.ways)} ways, "
          f"{len(data.areas)} areas, {len(data.walkable)} walkable outlines; NED bounds {index['bounds']}")
    for level, size in enumerate(LEVEL_SIZES_M):
        entries = index["tiles"][str(level)]
        largest = max(entries, key=lambda entry: entry[2])
        print(f"level {level} ({size} m): {len(entries)} tiles, {index['bytes'][str(level)] / 1e6:.2f} MB gzipped, "
              f"largest {largest[2] / 1e3:.1f} kB at x={largest[0]} y={largest[1]}")
    print(f"built in {time.monotonic() - started:.0f} s")


if __name__ == "__main__":
    main()
