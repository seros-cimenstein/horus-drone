"""Fetch GTA's pedestrian navmesh around HORUS's origin into data/maps/gta/walkable.json.

GTA's own ped navmesh is the GTA counterpart of OSM footways. This downloads DurtyFree's navmesh
dump (``gta-v-data-dumps`` README: 4,921 meshes, 7.4 M polygons; a 7z on mega.nz holding LZ4
MessagePack), checks its SHA-256, extracts it with ``7z``, streams the meshes, keeps the footpath
and road polygons within a radius of ``HorusGta.ini [origin]`` (neither underground, interior nor
water), converts them to local NED and merges each kind into outlines with holes
(``horus.maps.model.merge_polygons``). ``map.walkable_path`` adds the result to the GTA provider.

Setup only, in a throwaway venv; HORUS itself gains no dependency:
    python -m venv /tmp/navmesh-venv
    /tmp/navmesh-venv/bin/pip install msgpack lz4 cryptography pyyaml
    /tmp/navmesh-venv/bin/python scripts/maps/fetch_gta_walkable.py [--radius 600]
The 466 MB archive is kept in data/maps/gta/source/ (gitignored); the 659 MB extracted file is
removed after the conversion. Same terms as the rest of the dumps: no licence, extracted from
Rockstar's game, local research use only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import struct
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import lz4.block
import msgpack
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from horus.maps.gta import game_to_ned  # noqa: E402
from horus.maps.model import MapData, map_to_json, merge_polygons  # noqa: E402
from fetch_gta_map import read_origin  # noqa: E402

MEGA_URL = "https://mega.nz/file/lLZWwJbR#T3Xm3nQKRTP1Z0XrkzgbbINo54d6wK2Na4Nj6tYfbXI"
ARCHIVE_SHA256 = "3e3792605a33a541fa16fdeaf011f95900516d85497318d6a2019104c208e605"
MESSAGEPACK_NAME = "navigationMeshes.msgpack"
MESSAGEPACK_SHA256 = "e7d308924f83e4ba5a0398647c03d3a245572eeb5bc453af1d82b344291c07d5"
ATTRIBUTION = (
    "GTA V ped navmesh from DurtyFree/gta-v-data-dumps (mega.nz dump; no licence; extracted from "
    "Rockstar's Grand Theft Auto V): local research use only"
)
# NavigationMeshPoly MessagePack keys (navigationmesh.md in the dumps).
FOOTPATH, UNDERGROUND, WATER, INTERIOR, ROAD, POSITION, VERTICES = 2, 3, 5, 7, 9, 26, 27


def download_mega(url: str, out: Path) -> None:
    """A public mega.nz file: AES-128-CTR with the key and nonce folded into the link's fragment."""
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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def check(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise SystemExit(f"{path}: SHA-256 {actual} does not match the pinned {expected}")


def meshes(path: Path):
    """Stream ``List<NavigationMesh>`` from MessagePack-CSharp's Lz4BlockArray framing: an array of
    an ext 98 header (the uncompressed length of each block) and the LZ4 blocks themselves."""
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


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ini", type=Path, default=root / "gta/horus_mod/HorusGta.ini")
    parser.add_argument("--radius", type=float, default=600.0, help="crop radius around the origin (m)")
    parser.add_argument("--out", type=Path, default=root / "data/maps/gta/walkable.json")
    args = parser.parse_args()

    cache = args.out.parent / "source"
    archive, extracted = cache / "navmeshes.7z", cache / MESSAGEPACK_NAME
    if not archive.exists():
        download_mega(MEGA_URL, archive)
    check(archive, ARCHIVE_SHA256)
    if not extracted.exists():
        subprocess.run(["7z", "x", "-y", f"-o{cache}", str(archive), MESSAGEPACK_NAME], check=True,
                       stdout=subprocess.DEVNULL)
    check(extracted, MESSAGEPACK_SHA256)

    ox, oy, heading = origin = read_origin(args.ini)
    polygons, counts = [], {"footpath": 0, "road": 0}
    for _, _, _, mesh_polygons in meshes(extracted):
        for poly in mesh_polygons:
            x, y = poly[POSITION][0], poly[POSITION][1]
            if math.hypot(x - ox, y - oy) > args.radius or poly[UNDERGROUND] or poly[INTERIOR] or poly[WATER]:
                continue
            kind = "footpath" if poly[FOOTPATH] else "road" if poly[ROAD] else None
            if kind is None:
                continue
            counts[kind] += 1
            polygons.append((kind, tuple(game_to_ned(v[0], v[1], ox, oy, heading) for v in poly[VERTICES])))
    walkable = merge_polygons(polygons)
    sources = {
        "navmesh": MEGA_URL.split("#")[0],
        "sha256": {"navmeshes.7z": ARCHIVE_SHA256, MESSAGEPACK_NAME: MESSAGEPACK_SHA256},
        "origin_gta": {"x": ox, "y": oy, "heading_deg": heading},
        "crop_radius_m": args.radius,
        "navmesh_polygons": counts,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data = MapData(walkable=walkable, attribution=ATTRIBUTION, sources=sources)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(map_to_json(data), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    extracted.unlink()
    print(f"{args.out}: {counts['footpath']} footpath and {counts['road']} road navmesh polygons within "
          f"{args.radius:g} m of GTA ({origin[0]:g}, {origin[1]:g}) merged into {len(walkable)} outlines")


if __name__ == "__main__":
    main()
