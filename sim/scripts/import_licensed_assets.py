#!/usr/bin/env python3
"""Import pinned, licence-checked Gazebo Fuel meshes and derive neutral tint textures.

Offline maintenance tool (needs Pillow); world generation itself stays standard-library only
and reads only the files this script writes under ``sim/models``.

Every archive is pinned by Fuel owner/name/version and SHA-256. Only listed mesh and texture
members are copied (upstream model.sdf files, plugins, actors and scripts are never used);
archive member paths are checked before reading. OBJ material libraries are rewritten to point
at the copied textures. Each imported model directory gets a PROVENANCE.txt with the source,
version, archive hash, full author, licence and any required attribution or change notice.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "sim" / "models"
FUEL = "https://fuel.gazebosim.org/1.0"

CC0 = "Creative Commons Zero v1.0 Universal (CC0 1.0)"
CC_BY = "Creative Commons Attribution 4.0 International (CC BY 4.0)"

# name, owner/model, version, sha256, licence, author, target, members {zip path: target path}
ARCHIVES = [
    {
        "fuel": "OpenRobotics/Casual female", "version": 4,
        "sha256": "e03944df8f010c7d6cf85fe61b2a9bf2899eed971e73fb76461f4705edeffe5d",
        "license": CC0, "author": "Rohit Salem", "target": "horus_human_female_a",
        "description": "MakeHuman casual female body, clothing and hair meshes",
        "notes": "Created with MakeHuman; the asset-level licences of the bundled MakeHuman clothing, "
                 "hair and skin (female_casualsuit01/02, shoes05, ponytail01, eyebrow002, eyelashes01) "
                 "were not independently verified.",
        "members": {
            "meshes/casual_female.dae": "meshes/casual_female.dae",
            **{f"materials/textures/{name}.png": f"materials/textures/{name}.png" for name in (
                "brown_eye", "eyebrow002", "eyelashes01", "female_casualsuit01_diffuse",
                "female_casualsuit02_diffuse", "ponytail01_diffuse", "shoes05_diffuse",
                "tongue01_diffuse", "young_lightskinned_female_diffuse")},
        },
    },
    {
        "fuel": "OpenRobotics/Hatchback", "version": 3,
        "sha256": "dcd34e68cf43863991768c3ddbdeb322d6ee8bddaa01aa7da8267bbacfdb60ed",
        "license": CC0, "author": "Nate Koenig", "target": "horus_vehicle_hatchback",
        "description": "hatchback car", "mtl": "meshes/hatchback.mtl",
        "members": {"meshes/hatchback.obj": "meshes/hatchback.obj",
                    "materials/textures/hatchback.png": "materials/textures/hatchback.png",
                    "materials/textures/wheels3.png": "materials/textures/wheels3.png"},
        "textures": {"hatchback.png": "hatchback.png", "wheels3.png": "wheels3.png"},
    },
    {
        "fuel": "OpenRobotics/Hatchback red", "version": 4,
        "sha256": "a99e5e16fabdd15e0fb962ff5294493f45f7a29df8615128cecc082f9fc910cd",
        "license": CC0, "author": "Nate Koenig", "target": "horus_vehicle_hatchback_red",
        "description": "red hatchback car (mesh shared with Hatchback)", "mtl": "meshes/hatchback.mtl",
        "members": {"meshes/hatchback.obj": "meshes/hatchback.obj",
                    "materials/textures/hatchback.png": "materials/textures/hatchback_red.png"},
        "textures": {"hatchback.png": "hatchback_red.png", "wheels3.png": "wheels3.png"},
        # The colour variants ship without wheel textures; they reuse the base Hatchback's (CC0).
        "copy_from": {"horus_vehicle_hatchback/materials/textures/wheels3.png": "materials/textures/wheels3.png"},
    },
    {
        "fuel": "OpenRobotics/Hatchback blue", "version": 5,
        "sha256": "1659265c26d026ae5dbb7edf9106acf2a479073ba213da04ca5699fb39b34892",
        "license": CC0, "author": "Nate Koenig", "target": "horus_vehicle_hatchback_blue",
        "description": "blue hatchback car (mesh shared with Hatchback)", "mtl": "meshes/hatchback.mtl",
        "members": {"meshes/hatchback.obj": "meshes/hatchback.obj",
                    "materials/textures/hatchback.png": "materials/textures/hatchback_blue.png"},
        "textures": {"hatchback.png": "hatchback_blue.png", "wheels3.png": "wheels3.png"},
        "copy_from": {"horus_vehicle_hatchback/materials/textures/wheels3.png": "materials/textures/wheels3.png"},
    },
    {
        "fuel": "OpenRobotics/Prius Hybrid", "version": 3,
        "sha256": "ea5d10f1a2c4e89f8a0441f2c9e623575ecd9a7670c81c3f23bb9204ca32b1ab",
        "license": CC0, "author": "Ian Chen", "target": "horus_vehicle_sedan",
        "description": "Toyota Prius hybrid sedan", "mtl": "meshes/Hybrid.mtl",
        "members": {"meshes/Hybrid.obj": "meshes/Hybrid.obj",
                    "materials/textures/Hybrid.png": "materials/textures/Hybrid.png",
                    "materials/textures/Hybrid_Interior.png": "materials/textures/Hybrid_Interior.png",
                    "materials/textures/Wheels3.png": "materials/textures/Wheels3.png"},
        "textures": {"Hybrid.png": "Hybrid.png", "Hybrid_Interior.png": "Hybrid_Interior.png",
                     "Wheels3.png": "Wheels3.png"},
    },
    {
        "fuel": "OpenRobotics/SUV", "version": 4,
        "sha256": "729c066a1a2c1ba75a4453050a3c5022f02ccdfa75a4d4db4bab56104e3a1d54",
        "license": CC0, "author": "Nate Koenig", "target": "horus_vehicle_suv",
        "description": "sport utility vehicle", "mtl": "meshes/suv.mtl",
        "members": {"meshes/suv.obj": "meshes/suv.obj",
                    "materials/textures/suv.png": "materials/textures/suv.png",
                    "materials/textures/wheels_01.png": "materials/textures/wheels_01.png"},
        "textures": {"suv.png": "suv.png", "wheels_01.png": "wheels_01.png"},
    },
    {
        "fuel": "OpenRobotics/Pickup", "version": 4,
        "sha256": "fa4d6702828deaef54e50717ddc341e0b9b487eaaea9a0605c91419c1744ab9e",
        "license": CC0, "author": "Nate Koenig", "target": "horus_vehicle_pickup",
        "description": "pickup truck",
        "notes": "The COLLADA file names its author 'Cole' (OSRF Vehicles.max export); model.config names Nate Koenig.",
        "members": {"meshes/pickup.dae": "meshes/pickup.dae",
                    "materials/textures/pickup_diffuse.jpg": "materials/textures/pickup_diffuse.jpg",
                    "materials/textures/wheels2.jpg": "materials/textures/wheels2.jpg"},
    },
    {
        "fuel": "OpenRobotics/TruckBox", "version": 2,
        "sha256": "0d43837bb6482a408941e0f137bc102bee6afa5c50570f79e20fca69e070da07",
        "license": CC_BY, "author": "Roselle Carmen", "target": "horus_vehicle_box_truck",
        "description": "box truck", "mtl": "meshes/truck.mtl", "attribution": True,
        "members": {"meshes/truck.obj": "meshes/truck.obj",
                    "meshes/TruckBox_Diffuse.png": "materials/textures/TruckBox_Diffuse.png"},
        "textures": {"TruckBox_Diffuse.png": "TruckBox_Diffuse.png"},
    },
    {
        "fuel": "OpenRobotics/TruckDelivery", "version": 2,
        "sha256": "8bfbad7401d9f9d0d819e1ac068efc95ce0b2fd3ba32779c8ac6273a98fb39f0",
        "license": CC_BY, "author": "Roselle Carmen", "target": "horus_vehicle_delivery_truck",
        "description": "delivery truck", "mtl": "meshes/truck.mtl", "attribution": True,
        "members": {"meshes/truck.obj": "meshes/truck.obj",
                    "meshes/TruckDelivery_Diffuse.png": "materials/textures/TruckDelivery_Diffuse.png"},
        "textures": {"TruckDelivery_Diffuse.png": "TruckDelivery_Diffuse.png"},
    },
]

MAX_TEXTURE_PX = 1024


def _archive_url(entry: dict) -> str:
    owner, name = entry["fuel"].split("/", 1)
    quoted = urllib.parse.quote(name)
    return f"{FUEL}/{owner}/models/{quoted}/{entry['version']}/{quoted}.zip"


def _fetch(entry: dict, cache: Path) -> bytes:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{entry['fuel'].replace('/', '__').replace(' ', '_')}-v{entry['version']}.zip"
    if not path.is_file():
        with urllib.request.urlopen(_archive_url(entry), timeout=120) as response:
            path.write_bytes(response.read())
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry["sha256"]:
        raise RuntimeError(f"{entry['fuel']} v{entry['version']}: sha256 {digest} != pinned {entry['sha256']}")
    return data


def _safe_members(archive: zipfile.ZipFile) -> set[str]:
    names = set()
    for info in archive.infolist():
        parts = info.filename.split("/")
        if info.filename.startswith("/") or ".." in parts or "\\" in info.filename:
            raise RuntimeError(f"unsafe archive member {info.filename!r}")
        names.add(info.filename)
    return names


def _save_texture(data: bytes, destination: Path) -> None:
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    if max(image.size) > MAX_TEXTURE_PX:
        scale = MAX_TEXTURE_PX / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".jpg":
        image.convert("RGB").save(destination, quality=92)
    else:
        image.save(destination, optimize=True)


def _rewrite_mtl(text: str, textures: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        basename = match.group(2).rsplit("/", 1)[-1]
        if basename not in textures:
            raise RuntimeError(f"unmapped MTL texture {match.group(2)!r}")
        return f"{match.group(1)} ../materials/textures/{textures[basename]}"
    return re.sub(r"^(\s*map_K[ad])\s+(\S+)\s*$", replace, text, flags=re.MULTILINE)

def _provenance(entry: dict, archive: zipfile.ZipFile) -> str:
    owner, name = entry["fuel"].split("/", 1)
    lines = [
        f"Source: Gazebo Fuel {owner}/{name}, version {entry['version']}",
        f"URL: {_archive_url(entry)}",
        f"Archive SHA-256: {entry['sha256']} (byte-equal to the exact-version Fuel endpoint)",
        f"Author (model.config): {entry['author']}",
        f"Licence: {entry['license']} (Fuel model-level publisher metadata at import time; "
        "the archive itself embeds no licence text)",
        "Changes by HORUS: copied only the mesh and texture files listed below; textures larger than "
        f"{MAX_TEXTURE_PX} px were downscaled; OBJ material texture paths were rewritten; the upstream "
        "model.sdf and any plugins/scripts were not used.",
    ]
    if entry.get("attribution"):
        lines.append(f"Attribution: \"{name}\" by {entry['author']} (Open Robotics), Gazebo Fuel, "
                     "licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Modified as above.")
    if entry.get("notes"):
        lines.append(f"Notes: {entry['notes']}")
    lines.append("Files: " + ", ".join(sorted(entry["members"].values())))
    for source, relative in entry.get("copy_from", {}).items():
        lines.append(f"Copied: {relative} from sim/models/{source} (base Hatchback v3 archive, CC0 1.0)")
    lines.append("")
    lines.append("Upstream model.config (verbatim):")
    lines.append(archive.read("model.config").decode("utf-8", "replace").strip())
    for member in sorted(entry["members"]):
        text = archive.read(member)
        if member.endswith((".obj", ".mtl")):
            header = [line for line in text.decode("utf-8", "replace").splitlines()[:6] if line.startswith("#")]
            if header:
                lines += ["", f"{member} header (verbatim):", *header]
        elif member.endswith(".dae"):
            match = re.search(rb"<contributor>.*?</contributor>", text, re.S)
            if match:
                lines += ["", f"{member} COLLADA contributor (verbatim):", match.group(0).decode("utf-8", "replace")]
    return "\n".join(lines) + "\n"


def _model_config(entry: dict) -> str:
    return ('<?xml version="1.0"?>\n'
            f"<model><name>{entry['target']}</name><version>1.0</version>"
            f"<author><name>{entry['author']}</name></author>"
            f"<description>{entry['description']} from Gazebo Fuel {entry['fuel']} v{entry['version']} "
            f"({entry['license']}); see PROVENANCE.txt. Mesh/texture library only.</description></model>\n")


def import_archive(entry: dict, cache: Path) -> dict:
    data = _fetch(entry, cache)
    archive = zipfile.ZipFile(io.BytesIO(data))
    members = _safe_members(archive)
    target = MODELS / entry["target"]
    target.mkdir(parents=True, exist_ok=True)
    for source, relative in entry["members"].items():
        if source not in members:
            raise RuntimeError(f"{entry['fuel']}: missing member {source}")
        payload = archive.read(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix.lower() in (".png", ".jpg"):
            _save_texture(payload, destination)
        else:
            destination.write_bytes(payload)
    for source, relative in entry.get("copy_from", {}).items():
        shared = MODELS / source
        if not shared.is_file():
            raise RuntimeError(f"{entry['fuel']}: shared file {source} must be imported first")
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        (target / relative).write_bytes(shared.read_bytes())
    if "mtl" in entry:
        text = archive.read(entry["mtl"]).decode("utf-8", "replace")
        rewritten = _rewrite_mtl(text, entry["textures"])
        for line in rewritten.splitlines():
            if line.strip().startswith("map_K"):
                path = (target / entry["mtl"]).parent / line.split()[1]
                if not path.resolve().is_relative_to(target.resolve()) or not path.is_file():
                    raise RuntimeError(f"{entry['target']}: MTL texture {line.split()[1]} does not resolve inside the model")
        (target / entry["mtl"]).write_text(rewritten, encoding="utf-8")
    (target / "PROVENANCE.txt").write_text(_provenance(entry, archive), encoding="utf-8")
    (target / "model.config").write_text(_model_config(entry), encoding="utf-8")
    return {"target": entry["target"], "fuel": entry["fuel"], "version": entry["version"],
            "sha256": entry["sha256"], "license": entry["license"], "author": entry["author"]}


def _neutral(image, *, gain: float, lift: float = 0.0):
    """Grey-scale version brightened so a material diffuse multiplier can recolour it."""
    from PIL import Image, ImageOps

    grey = ImageOps.grayscale(image.convert("RGB"))
    grey = grey.point(lambda value: max(0, min(255, round(lift * 255 + value * gain))))
    if image.mode == "RGBA":
        return Image.merge("RGBA", (grey, grey, grey, image.getchannel("A")))
    return Image.merge("RGB", (grey, grey, grey))


def derive_human_textures() -> list[str]:
    """Neutral hair/denim tints and garment templates for per-actor clothing textures.

    Templates are RGBA: R = region (64 top, 128 bottom, 192 trim, 0 unused), G = smoothed
    fabric luminance, B = 0, A = original alpha.
    """
    from PIL import Image, ImageFilter

    out = MODELS / "horus_human_textures" / "materials" / "textures"
    out.mkdir(parents=True, exist_ok=True)
    standing = MODELS / "person_standing" / "materials" / "textures"
    female = MODELS / "horus_human_female_a" / "materials" / "textures"
    written = []

    hair = Image.open(standing / "male02_diffuse_black.png").resize((512, 512), Image.LANCZOS)
    _neutral(hair, gain=2.6, lift=0.45).save(out / "male_hair_neutral.png", optimize=True)
    denim = Image.open(standing / "jeans_basic_diffuse.png").resize((512, 512), Image.LANCZOS)
    _neutral(denim, gain=1.9, lift=0.2).save(out / "male_denim_neutral.png", optimize=True)
    ponytail = Image.open(female / "ponytail01_diffuse.png").resize((512, 512), Image.LANCZOS)
    _neutral(ponytail, gain=1.8, lift=0.25).save(out / "female_hair_neutral.png", optimize=True)
    written += ["male_hair_neutral.png", "male_denim_neutral.png", "female_hair_neutral.png"]

    size = 256
    shirt = Image.open(standing / "tshirt02_texture.png").convert("RGBA").resize((size, size), Image.LANCZOS)
    luminance = shirt.convert("L").filter(ImageFilter.GaussianBlur(3))
    alpha = shirt.getchannel("A")
    region = alpha.point(lambda value: 64 if value > 16 else 0)
    Image.merge("RGBA", (region, luminance, Image.new("L", (size, size), 0), alpha)).save(
        out / "male_tshirt_template.png")
    written.append("male_tshirt_template.png")

    # The casual-female outfit atlas holds the top in the upper half, trousers in the lower-left
    # and cap sleeves (orange trim) in the lower-right; trim is the saturated orange piping.
    suit = Image.open(female / "female_casualsuit01_diffuse.png").convert("RGBA").resize((size, size), Image.LANCZOS)
    pixels = suit.load()
    region = Image.new("L", (size, size), 0)
    marks = region.load()
    for y in range(size):
        for x in range(size):
            r, g, b, a = pixels[x, y]
            grey_background = abs(r - g) < 12 and abs(g - b) < 12 and r > 170
            if a < 16 or grey_background:
                continue
            if r > 150 and g < 140 and b < 90:
                marks[x, y] = 192
            elif y < size * 0.47 or x > size * 0.72:
                marks[x, y] = 64
            else:
                marks[x, y] = 128
    luminance = suit.convert("L").filter(ImageFilter.GaussianBlur(2))
    Image.merge("RGBA", (region, luminance, Image.new("L", (size, size), 0), suit.getchannel("A"))).save(
        out / "female_garment_template.png")
    written.append("female_garment_template.png")
    (out.parent.parent / "PROVENANCE.txt").write_text(
        "Derived from CC0 MakeHuman textures in sim/models/person_standing (OpenRobotics Standing person, "
        "CC0 1.0) and sim/models/horus_human_female_a (OpenRobotics Casual female v4, CC0 1.0) by "
        "sim/scripts/import_licensed_assets.py: grey-scale tint bases and clothing region/luminance "
        "templates.\n", encoding="utf-8")
    (out.parent.parent / "model.config").write_text(
        '<?xml version="1.0"?>\n<model><name>horus_human_textures</name><version>1.0</version>'
        "<author><name>HORUS</name></author><description>Derived tint bases and garment templates "
        "(CC0 sources); see PROVENANCE.txt.</description></model>\n", encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("/tmp/horus-fuel-cache"),
                        help="download cache; archives are verified against pinned SHA-256")
    args = parser.parse_args(argv)
    imported = [import_archive(entry, args.cache) for entry in ARCHIVES]
    textures = derive_human_textures()
    print(json.dumps({"imported": imported, "derived_textures": textures}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
