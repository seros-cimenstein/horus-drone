"""Runner combining MuJoCoQuadWorld, WorldSensors, and WorldCameraServer."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
import threading

from horus.bootstrap import preferred_linux_mujoco_gl

_gl = preferred_linux_mujoco_gl()
if _gl is not None:
    os.environ.setdefault("MUJOCO_GL", _gl)

from horus.maps.tiles import write_tile_directory
from horus.world.camera_server import WorldCameraServer
from horus.world.physics import MuJoCoQuadWorld
from horus.world.scenes import SCENE_NAMES, build_scene_bundle
from horus.world.sensors import WorldSensors


def run_world(
    *,
    scene: str = "indoor_sar",
    seed: int = 42,
    run_dir: Path,
    json_port: int = 9002,
    camera_port: int = 5610,
    spectator_port: int | None = 5615,
    gimbal_port: int = 14600,
    range_port: int = 5763,
    map_dir: Path | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Start and block in the built-in 3D test world until ``stop_event`` or signal."""
    run_dir = Path(run_dir)
    bundle = build_scene_bundle(scene, run_dir / "scene_assets", seed=seed)
    if map_dir is not None:
        write_tile_directory(bundle.map_data, map_dir, "builtin_3d")

    world = MuJoCoQuadWorld(bundle)
    cam = WorldCameraServer(world, camera_port=camera_port, spectator_port=spectator_port)
    sensors = WorldSensors(
        world,
        range_endpoint=f"tcp:127.0.0.1:{range_port}" if range_port > 0 else None,
        gimbal_connection=f"udpout:127.0.0.1:{gimbal_port}" if gimbal_port > 0 else None,
    )
    stop = stop_event or threading.Event()
    cam.start()
    sensors.start()
    logging.getLogger(__name__).info(
        "Built-in 3D world %r ready (json udp:%d, cam sim://127.0.0.1:%d, spectator :%s)",
        scene,
        json_port,
        cam.camera_port,
        cam.spectator_port,
    )
    try:
        world.serve_udp_lockstep(stop, port=json_port)
    finally:
        sensors.close()
        cam.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run HORUS Built-in 3D MuJoCo Test World.")
    parser.add_argument("--scene", choices=SCENE_NAMES, default="indoor_sar")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--json-port", type=int, default=9002)
    parser.add_argument("--camera-port", type=int, default=5610)
    parser.add_argument("--spectator-port", type=int, default=5615)
    parser.add_argument("--gimbal-port", type=int, default=14600)
    parser.add_argument("--range-port", type=int, default=5763)
    parser.add_argument("--map-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: stop.set())

    run_world(
        scene=args.scene,
        seed=args.seed,
        run_dir=args.run_dir,
        json_port=args.json_port,
        camera_port=args.camera_port,
        spectator_port=args.spectator_port if args.spectator_port > 0 else None,
        gimbal_port=args.gimbal_port,
        range_port=args.range_port,
        map_dir=args.map_dir,
        stop_event=stop,
    )
