from __future__ import annotations

from pathlib import Path
import urllib.request

import mujoco

from horus.config import PerceptionSettings
from horus.maps import TiledMap
from horus.maps.tiles import write_tile_directory
from horus.perception import CameraConfig, OpenCVCameraReader
from horus.perception.gta_stream import parse_frame_stream_uri
from horus.world.camera_server import WorldCameraServer
from horus.world.physics import MuJoCoQuadWorld
from horus.world.scenes import SCENE_NAMES, build_scene_bundle
from horus.world.sitl import SitlProcess, compose_sitl_parms


def test_all_built_in_3d_scenes_compile_and_build_maps(tmp_path: Path) -> None:
    for name in SCENE_NAMES:
        bundle = build_scene_bundle(name, tmp_path / name / "assets", seed=7)
        model = mujoco.MjModel.from_xml_string(bundle.xml)
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        assert model.nsensor == 7
        assert (tmp_path / name / "assets" / "apriltag_id0.png").is_file()

        map_dir = tmp_path / name / "map"
        write_tile_directory(bundle.map_data, map_dir, "builtin_3d")
        tiled = TiledMap("builtin_3d", map_dir)
        matches = tiled.searchable.find_places("Home", limit=2)
        assert matches


def test_sim_uri_streams_frames_through_opencvcambereader_and_spectator_http(tmp_path: Path) -> None:
    assert parse_frame_stream_uri("sim://127.0.0.1:5610") == ("127.0.0.1", 5610)
    assert parse_frame_stream_uri("gta://127.0.0.1") == ("127.0.0.1", 5610)

    import socket
    import threading
    import numpy as np
    from horus.executor.precision_landing import AprilTag36h11Detector
    from horus.perception.gta_stream import GtaFrameStream

    bundle = build_scene_bundle("indoor_sar", tmp_path / "assets")
    world = MuJoCoQuadWorld(bundle)
    # Hover 1.5 m directly above the AprilTag pad with nadir camera (-90 deg) and verify detection.
    world.set_pose([0.0, -0.08, 1.5])
    world.set_gimbal(-90.0, 0.0)
    world._apply_camera_poses()

    server = WorldCameraServer(world, camera_port=0, spectator_port=None)
    try:
        bgr = server.render_drone_bgr()
        assert bgr.shape == (480, 640, 3)
        tag = AprilTag36h11Detector().detect(bgr)
        assert tag is not None
        assert abs(tag.center_x - 320.0) < 40.0
        assert abs(tag.center_y - 240.0) < 40.0

        jpeg = server.render_spectator_jpeg(preset="overhead", show_tof=True)
        assert jpeg.startswith(b"\xff\xd8\xff")

        # Exercise the exact HGTA pull-TCP wire protocol over socketpair.
        srv_sock, cli_sock = socket.socketpair()
        srv_thread = threading.Thread(target=server._serve_client, args=(srv_sock,), daemon=True)
        srv_thread.start()
        try:
            stream = GtaFrameStream.__new__(GtaFrameStream)
            stream.last_header = None
            stream.inactive_skipped = 0
            stream._timeout_s = 2.0
            stream._require_active = True
            stream._allow_legacy_header = False
            stream._socket = cli_sock
            ok, frame = stream.read()
            assert ok and frame.shape == (480, 640, 3)
            assert stream.last_header is not None and stream.last_header.camera_active
        finally:
            server._stop.set()
            cli_sock.close()
            srv_thread.join(timeout=2.0)
    finally:
        server.close()


def test_sitl_process_composes_parms_and_direct_command(tmp_path: Path) -> None:
    fake_bin = tmp_path / "arducopter"
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    parm_path = compose_sitl_parms("indoor_sar", tmp_path / "sitl-overrides.parm")
    text = parm_path.read_text(encoding="utf-8")
    assert "PRX1_TYPE 2" in text
    assert "RTL_ALT_M 2" in text
    assert "SIM_RATE_HZ 800" in text

    proc = SitlProcess(
        tmp_path / "run",
        scene="urban_plaza",
        sitl_binary=fake_bin,
        mavlink_port=15760,
        range_port=15763,
        json_port_out=19002,
        json_port_in=19003,
    )
    argv, cwd = proc.build_command()
    assert argv[0] == str(fake_bin)
    assert "--model" in argv and "JSON:127.0.0.1" in argv
    assert "--sim-port-out" in argv and "19002" in argv
    assert "--sim-port-in" in argv and "19003" in argv
    assert "--serial0" in argv and "tcp:15760:wait" in argv
    assert "--serial2" in argv and "tcp:15763" in argv
    assert cwd == tmp_path / "run" / "sitl"
    assert (cwd / "copter-default.parm").is_file()
