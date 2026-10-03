#!/usr/bin/env python3
"""Derive the primitive-collision airframe used by generated 1.2 km worlds.

The outdoor airframe (``horus_iris_with_gimbal_outdoor``) collides through triangle meshes: the Iris
body (``iris_collision.stl``) and three gimbal arms. Against a generated world's large terrain
mesh those mesh-vs-mesh contacts stall dartsim (RTF 0.00 on Koko), while the same world with box
proxies runs at RTF ~0.47. This script writes procedural copies whose mesh collisions are replaced
by their mesh-bound boxes; visuals, joints, plugins and include names are unchanged, so camera
topic paths stay identical to the outdoor chain. It also writes
``horus_sensor_payload_procedural``: the ToF payload with its six single-beam proximity sensors
replaced by one 8-beam ring, which generated worlds render in a single lidar scan. The procedural
airframe includes the ring payload under the unchanged include name ``horus_sensor_payload``, so
the world-scoped ToF topic chain stays identical too.

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


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"expected exactly one occurrence of {old!r}")
    return text.replace(old, new)


# Generated worlds render camera and ToF at 5 Hz instead of 10: measured live on Koko (1.2 km
# forest, civilians moving) the drone's render sensors bound the world at RTF 0.14 at 10 Hz and
# 0.28 at 5 Hz. Perception already processes at most 5 fps (perception.max_fps), ArduPilot's
# proximity timeout is 500 ms of sim time, and HORUS's 1.5 s telemetry age holds above RTF 0.14.
SENSOR_RATE_HZ = 5


def _sensor_rate(text: str, rate_hz: int) -> str:
    """Set ``<update_rate>`` of every ``<sensor>`` in ``text``; plugin/model rates are untouched."""
    def one(match: re.Match[str]) -> str:
        block, count = re.subn(r"<update_rate>[^<]*</update_rate>",
                               f"<update_rate>{rate_hz}</update_rate>", match.group(0))
        if count != 1:
            raise ValueError("every sensor must declare exactly one update_rate")
        return block
    text, sensors = re.subn(r"<sensor\b.*?</sensor>", one, text, flags=re.S)
    if sensors == 0:
        raise ValueError("no <sensor> found")
    return text


_PROXIMITY_SENSOR = re.compile(
    r'\n      <sensor name="proximity_[^"]+" type="gpu_lidar">.*?</sensor>', re.S)
# -pi (dead astern) through 3*pi/4 with eight samples: beams every 45 degrees at
# -180/-135/-90/-45/0/45/90/135, the directions RING_BEAMS bridges in horus/tof_bridge/bridge.py.
_RING_SENSOR = """      <sensor name="proximity_ring" type="gpu_lidar">
        <pose>0 0 0.08 0 0 0</pose>
        <update_rate>10</update_rate><always_on>true</always_on><visualize>false</visualize>
        <lidar>
          <scan>
            <horizontal><samples>8</samples><resolution>1</resolution><min_angle>-3.1415926536</min_angle><max_angle>2.3561944902</max_angle></horizontal>
            <vertical><samples>1</samples><resolution>1</resolution><min_angle>0</min_angle><max_angle>0</max_angle></vertical>
          </scan>
          <range><min>0.05</min><max>4.0</max><resolution>0.01</resolution></range>
        </lidar>
      </sensor>"""

_PAYLOAD_COMMENT = (
    """      <!-- Virtual sensors only: no payload visuals or collisions can occlude the camera.
           Each single-beam ToF is yawed to the sector it is bridged as (scripts/tof_bridges.sh):
           MAVLink orientation k (clockwise, 45 deg steps) is body yaw -k*45 deg here.
           Sensor topics are deliberately unset: gz-sim scopes them under the world and every
           enclosing model, so the same payload works in the indoor and outdoor airframes. -->""",
    """      <!-- Virtual sensors only: no payload visuals or collisions can occlude the camera.
           One 8-beam horizontal ring replaces the six single-beam proximity ToFs, so generated
           1.2 km worlds render one lidar scan; scripts/tof_bridges.sh fans the beams out to the
           same MAVLink DISTANCE_SENSOR sectors (RING_BEAMS in horus/tof_bridge/bridge.py).
           Sensor topics are deliberately unset: gz-sim scopes them under the world and every
           enclosing model, so the same payload works in the indoor and outdoor airframes. -->""")

_AIRFRAME_PAYLOAD_COMMENT = (
    """    <!-- Virtual ToF payload: the same sensor set the indoor airframe carries (sensor topics are
         world-scoped by gz-sim, so the bridge derives them from the world/model names). -->""",
    """    <!-- Virtual ToF payload: generated worlds carry the single-ring payload
         (model://horus_sensor_payload_procedural); the include name stays horus_sensor_payload,
         so the world-scoped ToF topic chain and the downward rangefinder are unchanged. -->""")


def _ring_payload() -> str:
    payload = (MODELS / "horus_sensor_payload" / "model.sdf").read_text(encoding="utf-8")
    matches = _PROXIMITY_SENSOR.findall(payload)
    if len(matches) != 6:
        raise ValueError(f"expected six proximity sensors in horus_sensor_payload, found {len(matches)}")
    payload = _PROXIMITY_SENSOR.sub("", payload)
    payload = _replace_once(payload, '      <sensor name="range_down"',
                            _RING_SENSOR + '\n      <sensor name="range_down"')
    return _replace_once(payload, *_PAYLOAD_COMMENT)


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
    gimbal = _sensor_rate(gimbal, SENSOR_RATE_HZ)
    _write("horus_gimbal_small_3d_procedural", gimbal,
           "Outdoor 84 degree gimbal camera with box arm collisions.",
           "Derived by sim/scripts/derive_procedural_airframe.py from horus_gimbal_small_3d_outdoor.\n"
           "Change: yaw/roll/pitch arm mesh collisions replaced by their mesh-bound boxes; camera\n"
           f"update rate {SENSOR_RATE_HZ} Hz (generated-world render budget).\n")

    airframe = (MODELS / "horus_iris_with_gimbal_outdoor" / "model.sdf").read_text(encoding="utf-8")
    for old, new in (("model://iris_with_standoffs<", "model://horus_iris_standoffs_procedural<"),
                     ("model://horus_gimbal_small_3d_outdoor<", "model://horus_gimbal_small_3d_procedural<"),
                     ("model://horus_sensor_payload<", "model://horus_sensor_payload_procedural<")):
        if old not in airframe:
            raise ValueError(f"{old!r} not found in the outdoor airframe")
        airframe = airframe.replace(old, new)
    airframe = _replace_once(airframe, *_AIRFRAME_PAYLOAD_COMMENT)
    _write("horus_iris_with_gimbal_procedural", airframe,
           "Outdoor Iris with gimbal and single-ring ToF payload (primitive collisions).",
           "Derived by sim/scripts/derive_procedural_airframe.py from horus_iris_with_gimbal_outdoor.\n"
           "Change: includes the box-collision Iris body and gimbal copies, and the single-ring\n"
           "horus_sensor_payload_procedural under the include name horus_sensor_payload.\n")

    _write("horus_sensor_payload_procedural", _sensor_rate(_ring_payload(), SENSOR_RATE_HZ),
           "Generated-world ToF payload: one 8-beam proximity ring and the downward rangefinder.",
           "Derived by sim/scripts/derive_procedural_airframe.py from horus_sensor_payload.\n"
           "Change: the six single-beam proximity_* ToF sensors are replaced by one 8-beam\n"
           "proximity_ring gpu_lidar at the payload origin (beams every 45 degrees from -180 to\n"
           f"135, 0.05-4.0 m range); ring and range_down run at {SENSOR_RATE_HZ} Hz (generated-world\n"
           "render budget); links and inertial are unchanged.\n")

    _write("horus_iris_procedural",
           '<?xml version="1.0"?>\n<sdf version="1.9"><model name="horus_iris_procedural"><include>'
           "<uri>model://horus_iris_with_gimbal_procedural</uri><name>iris_with_gimbal</name>"
           "</include></model></sdf>\n",
           "Generated-world Iris camera, gimbal and ToF proximity payload (primitive collisions).",
           "Written by sim/scripts/derive_procedural_airframe.py; wrapper mirroring horus_iris_outdoor.\n")
    print("wrote horus_iris_procedural, horus_iris_with_gimbal_procedural, "
          "horus_gimbal_small_3d_procedural, horus_iris_standoffs_procedural, "
          "horus_sensor_payload_procedural")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
