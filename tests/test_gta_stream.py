"""GTA V renderer readback stream (horus_capture.asi protocol) against an in-process fake plugin."""

from __future__ import annotations

import itertools
import socket
import sys
import threading
import time
from typing import Iterable

import numpy as np
import pytest

from horus.perception import CameraConfig, Frame, OpenCVCameraReader
from horus.perception.camera import CameraSupervisor
from horus.perception.gta_stream import (
    HEADER,
    LEGACY_HEADER,
    MAGIC,
    PIXEL_FORMAT_BGR8,
    REQUEST,
    SCRIPT_AGE_NEVER,
    GtaFrameStream,
)


class FakePlugin:
    """Answers each request byte with the next scripted reply, like the plugin's TCP server."""

    def __init__(self, replies: Iterable[bytes]) -> None:
        self.replies = replies
        self.requests = 0
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            client, _ = self._listener.accept()
        except OSError:
            return
        with client:
            for reply in self.replies:
                try:
                    if client.recv(1) != REQUEST:
                        return
                    self.requests += 1
                    client.sendall(reply)
                except OSError:
                    return

    def close(self) -> None:
        self._listener.close()
        self._thread.join(timeout=2)


def frame_reply(
    image: np.ndarray,
    present_index: int,
    *,
    magic: bytes = MAGIC,
    camera_active: int = 1,
    script_age: int = 0,
    pose_seq: int = 0,
    game_frame: int = 0,
) -> bytes:
    height, width, _ = image.shape
    header = HEADER.pack(
        magic, HEADER.size, present_index, 123, width, height, PIXEL_FORMAT_BGR8, image.nbytes,
        camera_active, script_age, pose_seq, game_frame,
    )
    return header + image.tobytes()


def legacy_reply(image: np.ndarray, present_index: int) -> bytes:
    height, width, _ = image.shape
    header = LEGACY_HEADER.pack(
        MAGIC, LEGACY_HEADER.size, present_index, 123, width, height, PIXEL_FORMAT_BGR8, image.nbytes
    )
    return header + image.tobytes()


def gta_reader(port: int, read_timeout_s: float = 2.0) -> OpenCVCameraReader:
    return OpenCVCameraReader(
        CameraConfig(source=f"gta://127.0.0.1:{port}", fps_cap=1000.0, read_timeout_s=read_timeout_s)
    )


@pytest.fixture(autouse=True)
def stub_cv2(monkeypatch) -> None:
    # The gta:// path never touches OpenCV, but the reader imports it before choosing a backend.
    monkeypatch.setitem(sys.modules, "cv2", object())


def test_gta_source_reads_fresh_bgr_frames_through_camera_reader() -> None:
    first = np.zeros((4, 6, 3), dtype=np.uint8)
    first[..., 0] = 200  # blue channel only: a BGR/RGB mix-up changes the pixel
    second = np.arange(4 * 6 * 3, dtype=np.uint8).reshape(4, 6, 3)
    plugin = FakePlugin(
        [frame_reply(first, 41, pose_seq=906), frame_reply(second, 44, script_age=1, pose_seq=907, game_frame=5120)]
    )
    reader = gta_reader(plugin.port)
    try:
        frames = [reader.open().read(), reader.read()]
        header = reader._capture.last_header
    finally:
        reader.release()
        plugin.close()

    assert all(isinstance(frame, Frame) for frame in frames)
    assert [frame.frame_id for frame in frames] == [1, 2]
    np.testing.assert_array_equal(frames[0].image, first)
    np.testing.assert_array_equal(frames[1].image, second)
    assert header.present_index == 44 and (header.width, header.height) == (6, 4)
    assert (header.camera_active, header.script_age_presents, header.pose_seq, header.game_frame) == (
        True, 1, 907, 5120,
    )
    assert plugin.requests == 2


def test_inactive_frames_are_skipped_and_the_active_frame_returned_with_its_pose_seq() -> None:
    pause_menu = np.full((4, 4, 3), 90, dtype=np.uint8)
    drone = np.full((4, 4, 3), 200, dtype=np.uint8)
    plugin = FakePlugin(
        [
            frame_reply(pause_menu, 10, camera_active=0, script_age=SCRIPT_AGE_NEVER),
            frame_reply(pause_menu, 14, camera_active=0, script_age=40, pose_seq=900),
            frame_reply(drone, 18, pose_seq=907, game_frame=7000),
        ]
    )
    reader = gta_reader(plugin.port)
    try:
        frame = reader.open().read()
        stream = reader._capture
    finally:
        reader.release()
        plugin.close()

    np.testing.assert_array_equal(frame.image, drone)
    assert frame.frame_id == 1
    assert stream.last_header.pose_seq == 907 and stream.last_header.present_index == 18
    assert stream.inactive_skipped == 2
    assert plugin.requests == 3


def test_an_endless_inactive_stream_times_out_at_the_read_deadline() -> None:
    pause_menu = frame_reply(np.zeros((4, 4, 3), dtype=np.uint8), 1, camera_active=0, script_age=9)
    plugin = FakePlugin(itertools.repeat(pause_menu))
    stream = GtaFrameStream("127.0.0.1", plugin.port, timeout_s=0.3)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match=r"no active GTA drone camera frame in 0.3 s \(\d+ inactive"):
            stream.read()
    finally:
        elapsed = time.monotonic() - started
        stream.release()
        plugin.close()
    assert 0.3 <= elapsed < 1.0
    assert stream.inactive_skipped > 1 and stream.last_header is None


def test_supervisor_reports_stalled_and_processes_nothing_while_only_inactive_frames_arrive() -> None:
    pause_menu = frame_reply(np.zeros((4, 4, 3), dtype=np.uint8), 1, camera_active=0, script_age=9)
    plugin = FakePlugin(itertools.repeat(pause_menu))
    processed: list[Frame] = []
    stop = threading.Event()
    supervisor = CameraSupervisor(gta_reader(plugin.port, read_timeout_s=0.5), processed.append, stop_event=stop)
    supervisor.start(None)
    try:
        deadline = time.monotonic() + 5.0
        while supervisor.status()["camera"] != "stalled" and time.monotonic() < deadline:
            time.sleep(0.05)
        status = supervisor.status()
    finally:
        stop.set()
        for thread in supervisor.threads():
            if thread is not None:
                thread.join(timeout=3)
        supervisor.camera.release()
        plugin.close()

    assert status["camera"] == "stalled", status
    assert status["last_frame_age_s"] is None
    assert processed == []
    assert plugin.requests > 1


def test_gta_stream_rejects_a_malformed_header() -> None:
    plugin = FakePlugin([frame_reply(np.zeros((2, 2, 3), dtype=np.uint8), 1, magic=b"NOPE")])
    reader = gta_reader(plugin.port)
    try:
        with pytest.raises(RuntimeError, match="invalid header"):
            reader.open().read()
    finally:
        reader.release()
        plugin.close()


def test_gta_stream_rejects_the_40_byte_header_of_an_older_plugin() -> None:
    plugin = FakePlugin([legacy_reply(np.zeros((4, 4, 3), dtype=np.uint8), 1)])
    reader = gta_reader(plugin.port)
    try:
        with pytest.raises(RuntimeError, match="40 header bytes, expected 64"):
            reader.open().read()
    finally:
        reader.release()
        plugin.close()


def test_the_40_byte_header_is_read_only_when_explicitly_allowed() -> None:
    image = np.arange(4 * 4 * 3, dtype=np.uint8).reshape(4, 4, 3)
    plugin = FakePlugin([legacy_reply(image, 5)])
    stream = GtaFrameStream(
        "127.0.0.1", plugin.port, timeout_s=2.0, require_active=False, allow_legacy_header=True
    )
    try:
        ok, frame = stream.read()
    finally:
        stream.release()
        plugin.close()

    assert ok
    np.testing.assert_array_equal(frame, image)
    header = stream.last_header
    assert header.present_index == 5 and not header.camera_active
    assert (header.script_age_presents, header.pose_seq, header.game_frame) == (None, None, None)


def test_gta_stream_reports_a_plugin_that_disconnects_mid_frame() -> None:
    truncated = frame_reply(np.zeros((8, 8, 3), dtype=np.uint8), 1)[: HEADER.size + 10]
    plugin = FakePlugin([truncated])
    reader = gta_reader(plugin.port)
    try:
        with pytest.raises(ConnectionError, match="closed"):
            reader.open().read()
    finally:
        reader.release()
        plugin.close()
