#!/usr/bin/env python3
"""Derive the primitive-collision airframe used by generated 1.2 km worlds.

The outdoor airframe (``horus_iris_with_gimbal_outdoor``) collides through triangle meshes: the Iris
body (``iris_collision.stl``) and three gimbal arms. Against a generated world's large terrain
mesh those mesh-vs-mesh contacts stall dartsim (RTF 0.00 on Koko), while the same world with box
proxies runs at RTF ~0.47. This script writes procedural copies whose mesh collisions are replaced
by their mesh-bound boxes; visuals, joints, sensors, plugins and include names are unchanged, so
camera/ToF topic paths stay identical to the outdoor chain.

Run inside the simulation image (it reads the upstream Iris model from /opt/ardupilot_gazebo):

  docker run --rm -v "$PWD":/horus -w /horus --entrypoint /opt/horus-venv/bin/python \\
    horus-sim:local-s1s2 sim/scripts/derive_procedural_airframe.py
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "sim" / "models"
UPSTREAM = Path("/opt/ardupilot_gazebo/models")


def _stl_points(path: Path) -> list[tuple[float, float, float]]:
    data = path.read_bytes()
    count = struct.unpack("<I", data[80:84])[0]
    return [struct.unpack("<3f", data[84 + 50 * i + 12 + 12 * k:84 + 50 * i + 24 + 12 * k])
            for i in range(count) for k in range(3)]


def _dae_points(path: Path) -> list[tuple[float, float, float]]:
    root = ET.parse(path).getroot()
    ns = root.tag.split("}")[0].strip("{")
    unit = root.find(f"{{{ns}}}asset/{{{ns}}}unit")
    scale = float(unit.get("meter")) if unit is not None else 1.0
    points: list[tuple[float, float, float]] = []
    for array in root.iter(f"{{{ns}}}float_array"):
        if "position" in (array.get("id") or "").lower():
            values = [float(value) * scale for value in array.text.split()]
            points += [tuple(values[i:i + 3]) for i in range(0, len(values), 3)]
    return points


def _box(points: list[tuple[float, float, float]]) -> tuple[str, str]:
    low = [min(point[i] for point in points) for i in range(3)]
    high = [max(point[i] for point in points) for i in range(3)]
    size = " ".join(f"{high[i] - low[i]:.4f}" for i in range(3))
    centre = " ".join(f"{(high[i] + low[i]) / 2:.4f}" for i in range(3))
    return size, centre


def _boxify(text: str, name: str, points: list[tuple[float, float, float]]) -> str:
    size, centre = _box(points)
    pattern = re.compile(rf"(<collision name=[\"']{name}[\"']>)(.*?)(</collision>)", re.S)
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"collision {name!r} not found")
    body = re.sub(r"<pose>[^<]*</pose>\s*", "", match.group(2), count=1)
    body = re.sub(r"<mesh>.*?</mesh>", f"<box><size>{size}</size></box>", body, count=1, flags=re.S)
    if "<box>" not in body:
        raise ValueError(f"collision {name!r} has no mesh geometry")
    replacement = f"{match.group(1)}\n        <pose>{centre} 0 0 0</pose>{body}{match.group(3)}"
    return text[:match.start()] + replacement + text[match.end():]


def _write(name: str, sdf: str, description: str, provenance: str) -> None:
    directory = MODELS / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.sdf").write_text(sdf, encoding="utf-8")
    (directory / "model.config").write_text(
        '<?xml version="1.0"?>\n<model><name>' + name + '</name><version>1.0</version>'
        '<sdf version="1.9">model.sdf</sdf><author><name>HORUS</name></author>'
        f"<description>{description}</description></model>\n", encoding="utf-8")
    (directory / "PROVENANCE.txt").write_text(provenance, encoding="utf-8")


def main() -> int:
    iris = (UPSTREAM / "iris_with_standoffs" / "model.sdf").read_text(encoding="utf-8")
    iris = _boxify(iris, "base_collision",
                   _stl_points(UPSTREAM / "iris_with_standoffs" / "meshes" / "iris_collision.stl"))
    _write("horus_iris_standoffs_procedural", iris,
           "Iris with Standoffs with a box body collision (visuals reference model://iris_with_standoffs).",
           "Derived by sim/scripts/derive_procedural_airframe.py from ardupilot_gazebo's\n"
           "models/iris_with_standoffs/model.sdf (the RotorS/OpenRobotics 'Iris with Standoffs' model,\n"
           "published on Gazebo Fuel under CC0 1.0). Change: base_collision mesh replaced by its\n"
           "mesh-bound box; the model name, links, joints and visuals are unchanged.\n")

    gimbal = (MODELS / "horus_gimbal_small_3d_outdoor" / "model.sdf").read_text(encoding="utf-8")
    meshes = UPSTREAM / "gimbal_small_3d" / "meshes"
    for collision, mesh in (("yaw_collision", "yaw_arm"), ("roll_collision", "roll_arm"),
                            ("pitch_collision", "camera_enclosure")):
        gimbal = _boxify(gimbal, collision, _dae_points(meshes / f"{mesh}.dae"))
    _write("horus_gimbal_small_3d_procedural", gimbal,
           "Outdoor 84 degree gimbal camera with box arm collisions.",
           "Derived by sim/scripts/derive_procedural_airframe.py from horus_gimbal_small_3d_outdoor.\n"
           "Change: yaw/roll/pitch arm mesh collisions replaced by their mesh-bound boxes.\n")

    airframe = (MODELS / "horus_iris_with_gimbal_outdoor" / "model.sdf").read_text(encoding="utf-8")
    for old, new in (("model://iris_with_standoffs<", "model://horus_iris_standoffs_procedural<"),
                     ("model://horus_gimbal_small_3d_outdoor<", "model://horus_gimbal_small_3d_procedural<")):
        if old not in airframe:
            raise ValueError(f"{old!r} not found in the outdoor airframe")
        airframe = airframe.replace(old, new)
    _write("horus_iris_with_gimbal_procedural", airframe,
           "Outdoor Iris with gimbal and ToF payload using primitive collisions for generated worlds.",
           "Derived by sim/scripts/derive_procedural_airframe.py from horus_iris_with_gimbal_outdoor.\n"
           "Change: includes the box-collision Iris body and gimbal copies.\n")

    _write("horus_iris_procedural",
           '<?xml version="1.0"?>\n<sdf version="1.9"><model name="horus_iris_procedural"><include>'
           "<uri>model://horus_iris_with_gimbal_procedural</uri><name>iris_with_gimbal</name>"
           "</include></model></sdf>\n",
           "Generated-world Iris camera, gimbal and ToF proximity payload (primitive collisions).",
           "Written by sim/scripts/derive_procedural_airframe.py; wrapper mirroring horus_iris_outdoor.\n")
    print("wrote horus_iris_procedural, horus_iris_with_gimbal_procedural, "
          "horus_gimbal_small_3d_procedural, horus_iris_standoffs_procedural")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
