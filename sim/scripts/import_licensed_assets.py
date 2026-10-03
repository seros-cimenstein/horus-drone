#!/usr/bin/env python3
"""Import pinned, licence-checked Gazebo Fuel meshes and derive neutral tint textures.

Offline maintenance tool (needs Pillow); world generation itself stays standard-library only
and reads only the files this script writes under ``sim/models``.

Every archive is pinned by Fuel owner/name/version and SHA-256. Only listed mesh and texture
members are copied (upstream model.sdf files, plugins, actors and scripts are never used);
archive member paths are checked before reading. OBJ material libraries and COLLADA ``<image>``
references are rewritten to point at the copied textures. Each imported model directory gets a
PROVENANCE.txt with the source, version, archive hash, full author, licence and any required
attribution or change notice.
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
        "description": "hatchback car", "mtl": "meshes/hatchback.mtl", "standalone_sdf": True,
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
        "standalone_sdf": True,
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
        "standalone_sdf": True,
        "members": {"meshes/hatchback.obj": "meshes/hatchback.obj",
                    "materials/textures/hatchback.png": "materials/textures/hatchback_blue.png"},
        "textures": {"hatchback.png": "hatchback_blue.png", "wheels3.png": "wheels3.png"},
        "copy_from": {"horus_vehicle_hatchback/materials/textures/wheels3.png": "materials/textures/wheels3.png"},
    },
    {
        "fuel": "OpenRobotics/Prius Hybrid", "version": 3,
        "sha256": "ea5d10f1a2c4e89f8a0441f2c9e623575ecd9a7670c81c3f23bb9204ca32b1ab",
        "license": CC0, "author": "Ian Chen", "target": "horus_vehicle_sedan",
        "description": "Toyota Prius hybrid sedan", "mtl": "meshes/Hybrid.mtl", "standalone_sdf": True,
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
        "description": "sport utility vehicle", "mtl": "meshes/suv.mtl", "standalone_sdf": True,
        "members": {"meshes/suv.obj": "meshes/suv.obj",
                    "materials/textures/suv.png": "materials/textures/suv.png",
                    "materials/textures/wheels_01.png": "materials/textures/wheels_01.png"},
        "textures": {"suv.png": "suv.png", "wheels_01.png": "wheels_01.png"},
    },
    {
        "fuel": "OpenRobotics/Pickup", "version": 4,
        "sha256": "fa4d6702828deaef54e50717ddc341e0b9b487eaaea9a0605c91419c1744ab9e",
        "license": CC0, "author": "Nate Koenig", "target": "horus_vehicle_pickup",
        "description": "pickup truck", "standalone_sdf": True,
        "notes": "The COLLADA file names its author 'Cole' (OSRF Vehicles.max export); model.config names Nate Koenig.",
        "members": {"meshes/pickup.dae": "meshes/pickup.dae",
                    "materials/textures/pickup_diffuse.jpg": "materials/textures/pickup_diffuse.jpg",
                    "materials/textures/wheels2.jpg": "materials/textures/wheels2.jpg"},
    },
    {
        "fuel": "OpenRobotics/TruckBox", "version": 2,
        "sha256": "0d43837bb6482a408941e0f137bc102bee6afa5c50570f79e20fca69e070da07",
        "license": CC_BY, "author": "Roselle Carmen", "target": "horus_vehicle_box_truck",
        "description": "box truck", "mtl": "meshes/truck.mtl", "attribution": True, "standalone_sdf": True,
        "members": {"meshes/truck.obj": "meshes/truck.obj",
                    "meshes/TruckBox_Diffuse.png": "materials/textures/TruckBox_Diffuse.png"},
        "textures": {"TruckBox_Diffuse.png": "TruckBox_Diffuse.png"},
    },
    {
        "fuel": "OpenRobotics/TruckDelivery", "version": 2,
        "sha256": "8bfbad7401d9f9d0d819e1ac068efc95ce0b2fd3ba32779c8ac6273a98fb39f0",
        "license": CC_BY, "author": "Roselle Carmen", "target": "horus_vehicle_delivery_truck",
        "description": "delivery truck", "mtl": "meshes/truck.mtl", "attribution": True, "standalone_sdf": True,
        "members": {"meshes/truck.obj": "meshes/truck.obj",
                    "meshes/TruckDelivery_Diffuse.png": "materials/textures/TruckDelivery_Diffuse.png"},
        "textures": {"TruckDelivery_Diffuse.png": "TruckDelivery_Diffuse.png"},
    },
    # -- Trees, boulders, street furniture and a seated person for procedural scenery. The tree
    # meshes carry no image references and their Ogre material scripts (alpha rejection) are not
    # copied, so the importer embeds the branch/bark textures into the COLLADA effects (diffuse
    # + alpha). Generated worlds then place each tree as one material-free visual whose instances
    # share the mesh materials: thousands of per-visual SDF materials exhausted Koko's iGPU.
    {
        "fuel": "OpenRobotics/Pine Tree", "version": 6,
        "sha256": "d436a44229321314e79e4c2ba709dc926e833a5b5d90d16c5d085c2a74576e25",
        "license": CC0, "author": "Louise Poubel", "target": "horus_tree_pine",
        "description": "conifer pine tree (branch and bark sub-meshes)", "standalone_sdf": True,
        "notes": "Upstream materials/scripts (Ogre alpha rejection) are not copied; the COLLADA "
                 "effects reference the copied branch (with alpha) and bark textures directly.",
        "members": {
            "meshes/pine_tree.dae": "meshes/pine_tree.dae",
            "materials/textures/bark_diffuse.png": "materials/textures/bark_diffuse.png",
            "materials/textures/branch_2_diffuse.png": "materials/textures/branch_2_diffuse.png",
        },
        "textures": {"bark_diffuse.png": "bark_diffuse.png", "branch_2_diffuse.png": "branch_2_diffuse.png"},
        "embed_textures": {"Branch_2-effect": "branch_2_diffuse.png", "Bark_001-effect": "bark_diffuse.png"},
    },
    {
        "fuel": "OpenRobotics/Oak tree", "version": 7,
        "sha256": "ae128176f707b571d2a2a47a1ab6b80596e81d96402501a64f4f28a49543daa5",
        "license": CC0, "author": "Louise Poubel", "target": "horus_tree_oak",
        "description": "broad-crowned oak tree (branch and bark sub-meshes)", "standalone_sdf": True,
        "notes": "Upstream materials/scripts (Ogre alpha rejection) are not copied; the COLLADA "
                 "effects reference the copied branch (with alpha) and bark textures directly.",
        "members": {
            "meshes/oak_tree.dae": "meshes/oak_tree.dae",
            "materials/textures/bark_diffuse.png": "materials/textures/bark_diffuse.png",
            "materials/textures/branch_diffuse.png": "materials/textures/branch_diffuse.png",
        },
        "textures": {"bark_diffuse.png": "bark_diffuse.png", "branch_diffuse.png": "branch_diffuse.png"},
        "embed_textures": {"Branch_1-effect": "branch_diffuse.png", "Bark-effect": "bark_diffuse.png"},
    },
    {
        "fuel": "OpenRobotics/Falling Rock 1", "version": 6,
        "sha256": "88b38504a76db87266409d658006fe18eb3220937bb0de2d90e945cc0e1ee317",
        "license": CC_BY, "author": "Addisu Taddese, Cole Biesemeyer", "target": "horus_rock_a",
        "description": "boulder (albedo and normal map; decimated collider hull)",
        "attribution": True, "standalone_sdf": True,
        "notes": "The roughness map is not copied (not used); FallingRock01_Collider.dae (the "
                 "upstream decimated collision hull) is kept for reference, the HORUS model.sdf "
                 "uses a box proxy for standalone collision.",
        "members": {
            "meshes/FallingRock01.dae": "meshes/FallingRock01.dae",
            "meshes/FallingRock01_Collider.dae": "meshes/FallingRock01_Collider.dae",
            "materials/textures/FallingRocks_Albedo.jpg": "materials/textures/FallingRocks_Albedo.jpg",
            "materials/textures/FallingRocks_Normal.jpg": "materials/textures/FallingRocks_Normal.jpg",
        },
        "textures": {"FallingRocks_Albedo.jpg": "FallingRocks_Albedo.jpg",
                     "FallingRocks_Normal.jpg": "FallingRocks_Normal.jpg"},
    },
    {
        "fuel": "OpenRobotics/Lamp Post", "version": 3,
        "sha256": "b58e3b135fe981ee889426d7cc7c122c69aa416a4316386320443b431ce27165",
        "license": CC0, "author": "Cole Biesemeyer, Nate Koenig", "target": "horus_lamp_post",
        "description": "street lamp post (diffuse, emissive and specular maps)", "standalone_sdf": True,
        "notes": "The upstream model.sdf scales the mesh by 3 (9.27 m tall); the HORUS registry "
                 "applies the same canonical scale.",
        "members": {
            "meshes/lamp_post.dae": "meshes/lamp_post.dae",
            "materials/textures/Lamp_Post_Diffuse.png": "materials/textures/Lamp_Post_Diffuse.png",
            "materials/textures/Lamp_Post_Emissive.png": "materials/textures/Lamp_Post_Emissive.png",
            "materials/textures/Lamp_Post_Spec.png": "materials/textures/Lamp_Post_Spec.png",
        },
        "textures": {"Lamp_Post_Diffuse.png": "Lamp_Post_Diffuse.png",
                     "Lamp_Post_Emissive.png": "Lamp_Post_Emissive.png",
                     "Lamp_Post_Spec.png": "Lamp_Post_Spec.png"},
    },
    {
        "fuel": "OpenRobotics/Bus", "version": 3,
        "sha256": "39027081c04751263076816193a48bad2927e061bfe6219bcca8f17289c4f1ab",
        "license": CC0, "author": "Nate Koenig", "target": "horus_vehicle_bus",
        "description": "coach bus", "mtl": "meshes/bus.mtl", "standalone_sdf": True,
        "notes": "The upstream model.sdf scales the mesh by 0.01 (12.49 m long); the HORUS registry "
                 "applies the same canonical scale."
                 " The bus material library names the SUV's wheel texture, so it is copied from "
                 "sim/models/horus_vehicle_suv (same author and licence).",
        "members": {"meshes/bus.obj": "meshes/bus.obj", "meshes/bus.mtl": "meshes/bus.mtl",
                    "materials/textures/bus.png": "materials/textures/bus.png"},
        "textures": {"bus.png": "bus.png", "wheels_01.png": "wheels_01.png"},
        "copy_from": {"horus_vehicle_suv/materials/textures/wheels_01.png": "materials/textures/wheels_01.png"},
        "copy_from_note": "OpenRobotics/SUV v4 archive, CC0 1.0 (the Bus model depends on the SUV's wheel texture)",
    },
    {
        "fuel": "OpenRobotics/Rescue Randy Sitting", "version": 8,
        "sha256": "611ef9ddadfba16be25963f2b79967fec6493c638ca77d0652f18fbbc5202fb3",
        "license": CC0, "author": "Ian Chen, Cole Biesemeyer", "target": "horus_person_sitting_a",
        "description": "rescue dummy in a seated pose", "standalone_sdf": True,
        "notes": "The thermal texture and the upstream model.sdf's Artifact Proximity Detector "
                 "include are not copied; the upstream link pose (-0.624 m) that puts the mesh base "
                 "on the ground is recorded in the HORUS registry instead.",
        "members": {
            "meshes/rescue_randy.dae": "meshes/rescue_randy.dae",
            "materials/textures/RescueRandy_Diffuse.jpg": "materials/textures/RescueRandy_Diffuse.jpg",
        },
    },
    {
        "fuel": "OpenRobotics/FoodCourtBenchShort", "version": 2,
        "sha256": "c3e9d82e0fb5c286c43494ab34e41a79940d9ff6afe2913b8a9e8f7d310216d1",
        "license": CC0, "author": "Wan Yi Seow", "target": "horus_bench_a",
        "description": "short park bench (outdoor-plausible two-seater)", "standalone_sdf": True,
        "notes": "The upstream collision mesh (_Col.obj) and the metal/roughness maps are not "
                 "copied; the HORUS model.sdf uses a box proxy for standalone collision.",
        "mtl": "meshes/FoodCourtBenchShort.mtl",
        "members": {
            "meshes/FoodCourtBenchShort.obj": "meshes/FoodCourtBenchShort.obj",
            "meshes/FoodCourtBenchShort.mtl": "meshes/FoodCourtBenchShort.mtl",
            "meshes/FoodCourtBenchShort_Diffuse.png": "materials/textures/FoodCourtBenchShort_Diffuse.png",
        },
        "textures": {"FoodCourtBenchShort_Diffuse.png": "FoodCourtBenchShort_Diffuse.png"},
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


def _rewrite_dae(text: str, textures: dict[str, str]) -> str:
    """Point COLLADA ``<image>`` file references at the copied textures layout.

    gz-common resolves a COLLADA image path relative to the mesh file, so ``meshes/*.dae``
    must reference ``../materials/textures/<name>``. ``<init_from>`` elements inside
    ``<surface>`` are image ids, not paths, and are left untouched.
    """
    def replace_image(match: re.Match[str]) -> str:
        def replace_reference(reference: re.Match[str]) -> str:
            basename = reference.group(1).strip().rsplit("/", 1)[-1]
            mapped = textures.get(basename)
            if mapped is None:
                return reference.group(0)
            return f"<init_from>../materials/textures/{mapped}</init_from>"

        return re.sub(r"<init_from>([^<]*)</init_from>", replace_reference, match.group(0))

    return re.sub(r"<image\b[^>]*>.*?</image>", replace_image, text, flags=re.DOTALL)


def _embed_dae_textures(text: str, effects: dict[str, str]) -> str:
    """Reference a copied texture from each named COLLADA effect (diffuse and A_ONE alpha).

    gz-common's COLLADA loader turns a textured ``<diffuse>`` into the mesh material's texture
    and a textured ``<transparent opaque="A_ONE">`` into alpha-from-texture, so foliage renders
    alpha-tested without any SDF ``<material>``.
    """
    if "<library_images>" in text:
        raise RuntimeError("COLLADA file already has <library_images>; embedding expects none")
    images = "".join(f'<image id="{effect}-image" name="{effect}-image">'
                     f"<init_from>../materials/textures/{texture}</init_from></image>"
                     for effect, texture in effects.items())
    text = text.replace("<library_effects>", f"<library_images>{images}</library_images>\n  <library_effects>", 1)
    for effect in effects:
        match = re.search(rf'<effect id="{re.escape(effect)}">.*?</effect>', text, re.DOTALL)
        if match is None:
            raise RuntimeError(f"COLLADA effect {effect!r} not found")
        reference = f'<texture texture="{effect}-sampler" texcoord="UVMap"/>'
        body = match.group(0).replace(
            "<profile_COMMON>",
            f'<profile_COMMON><newparam sid="{effect}-surface"><surface type="2D">'
            f"<init_from>{effect}-image</init_from></surface></newparam>"
            f'<newparam sid="{effect}-sampler"><sampler2D><source>{effect}-surface</source>'
            "</sampler2D></newparam>", 1)
        body, diffuse = re.subn(r"<diffuse>\s*<color[^>]*>[^<]*</color>\s*</diffuse>",
                                f"<diffuse>{reference}</diffuse>", body)
        body, alpha = re.subn(r'(<transparent opaque="A_ONE">)\s*<color>[^<]*</color>',
                              rf"\1{reference}", body)
        if diffuse != 1 or alpha != 1:
            raise RuntimeError(f"COLLADA effect {effect!r}: expected one diffuse and one A_ONE colour")
        text = text.replace(match.group(0), body)
    return text


def _validate_dae_images(path: Path, target: Path) -> None:
    """Every COLLADA image reference must resolve to a copied file inside the model."""
    for block in re.findall(r"<image\b[^>]*>.*?</image>", path.read_text(encoding="utf-8"), re.DOTALL):
        for value in re.findall(r"<init_from>([^<]*)</init_from>", block):
            resolved = (path.parent / value.strip()).resolve()
            if not resolved.is_relative_to(target.resolve()) or not resolved.is_file():
                raise RuntimeError(f"{path.name}: image reference {value!r} does not resolve inside the model")

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
        "model.sdf and any plugins/scripts were not used."
        + (" COLLADA <image> references were rewritten to the copied meshes/ and materials/textures/ layout."
           if entry.get("textures") and any(rel.endswith(".dae") for rel in entry["members"].values()) else "")
        + (" COLLADA effects " + ", ".join(sorted(entry["embed_textures"]))
           + " were given diffuse and A_ONE alpha textures from the copied files."
           if entry.get("embed_textures") else ""),
    ]
    if entry.get("attribution"):
        lines.append(f"Attribution: \"{name}\" by {entry['author']} (Open Robotics), Gazebo Fuel, "
                     "licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Modified as above.")
    if entry.get("notes"):
        lines.append(f"Notes: {entry['notes']}")
    lines.append("Files: " + ", ".join(sorted(entry["members"].values())))
    for source, relative in entry.get("copy_from", {}).items():
        note = entry.get("copy_from_note", "base Hatchback v3 archive, CC0 1.0")
        lines.append(f"Copied: {relative} from sim/models/{source} ({note})")
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
    if entry.get("standalone_sdf"):
        tail = "Mesh/texture library with a HORUS-authored model.sdf (visual mesh + proxy collisions)."
        sdf = '<sdf version="1.9">model.sdf</sdf>'
    else:
        tail = "Mesh/texture library only."
        sdf = ""
    return ('<?xml version="1.0"?>\n'
            f"<model><name>{entry['target']}</name><version>1.0</version>{sdf}"
            f"<author><name>{entry['author']}</name></author>"
            f"<description>{entry['description']} from Gazebo Fuel {entry['fuel']} v{entry['version']} "
            f"({entry['license']}); see PROVENANCE.txt. {tail}</description></model>\n")


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
        elif destination.suffix.lower() == ".dae" and entry.get("textures"):
            dae = _rewrite_dae(payload.decode("utf-8"), entry["textures"])
            if entry.get("embed_textures"):
                dae = _embed_dae_textures(dae, entry["embed_textures"])
            destination.write_text(dae, encoding="utf-8")
        else:
            destination.write_bytes(payload)
    for relative in entry["members"].values():
        if relative.endswith(".dae"):
            _validate_dae_images(target / relative, target)
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
    parser.add_argument("--select", action="append", default=[], metavar="TARGET",
                        help="import only these model directories (repeatable); derived human "
                             "textures are skipped because they depend on the full import")
    args = parser.parse_args(argv)
    entries = [entry for entry in ARCHIVES if not args.select or entry["target"] in args.select]
    if args.select and not entries:
        parser.error(f"no archive matches {args.select}")
    imported = [import_archive(entry, args.cache) for entry in entries]
    textures = [] if args.select else derive_human_textures()
    print(json.dumps({"imported": imported, "derived_textures": textures}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
