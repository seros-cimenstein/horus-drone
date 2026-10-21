"""Client for frames read back from GTA V's renderer by ``horus_capture.asi`` (see gta/capture).

Pull protocol over TCP: the client sends one ``b"F"`` byte per frame it wants; the plugin replies
with the newest back buffer it has not sent yet, so a slow consumer never builds a backlog.
Reply = 64-byte little-endian header + tightly packed BGR8 pixels::

    magic "HGTA" | u32 header_bytes | u64 present_index | u64 capture_unix_ns
    | u32 width | u32 height | u32 pixel_format (1 = BGR8) | u32 payload_bytes
    | u32 camera_active | u32 script_age_presents | u64 pose_seq | u64 game_frame

The camera fields come from HorusGta's shared-memory block at that frame's Present:
``camera_active`` is 1 only while HorusGta drives the drone camera and its script ticked within the
last two presents (a paused game stops script ticks, so pause-menu frames read 0);
``script_age_presents`` is 0xFFFFFFFF until the script has written the block.

By default ``read()`` returns only drone-camera frames: inactive frames (landing page, pause menu,
normal gameplay camera, no bridge) are skipped, and if no active frame arrives within ``timeout_s``
the read raises ``TimeoutError``, so the camera supervisor reports a stall instead of feeding those
frames to perception. Plugins built before the camera fields sent a 40-byte header; it is rejected
unless ``allow_legacy_header`` is set (only the capture selftest's ``--legacy-header`` does that).
"""

from __future__ import annotations

from dataclasses import dataclass
import socket
import struct
import time
from typing import Any
from urllib.parse import urlsplit

HEADER = struct.Struct("<4sIQQIIIIIIQQ")
LEGACY_HEADER = struct.Struct("<4sIQQIIII")
_PREFIX = struct.Struct("<4sI")
MAGIC = b"HGTA"
PIXEL_FORMAT_BGR8 = 1
DEFAULT_PORT = 5610
REQUEST = b"F"
SCRIPT_AGE_NEVER = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class GtaFrameHeader:
    present_index: int
    capture_unix_ns: int
    width: int
    height: int
    camera_active: bool
    # None for a legacy 40-byte header, which carries no camera fields.
    script_age_presents: int | None
    pose_seq: int | None
    game_frame: int | None


def parse_gta_uri(source: str) -> tuple[str, int]:
    """``gta://host:port`` -> (host, port); host defaults to 127.0.0.1, port to 5610."""
    endpoint = urlsplit(source)
    if endpoint.scheme.lower() != "gta":
        raise ValueError(f"not a gta:// source: {source!r}")
    return endpoint.hostname or "127.0.0.1", endpoint.port or DEFAULT_PORT


class GtaFrameStream:
    """``cv2.VideoCapture``-shaped reader (``isOpened``/``read``/``release``) for the capture plugin.

    ``require_active`` (default) skips frames whose ``camera_active`` is 0 within one ``timeout_s``
    deadline per ``read()``; ``False`` returns every frame (the capture selftest checks the camera
    fields itself). ``inactive_skipped`` counts the frames skipped so far.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float,
        require_active: bool = True,
        allow_legacy_header: bool = False,
    ) -> None:
        self.last_header: GtaFrameHeader | None = None
        self.inactive_skipped = 0
        self._timeout_s = timeout_s
        self._require_active = require_active
        self._allow_legacy_header = allow_legacy_header
        self._socket: socket.socket | None = socket.create_connection((host, port), timeout=timeout_s)
        self._socket.settimeout(timeout_s)

    def isOpened(self) -> bool:  # noqa: N802 - mirrors cv2.VideoCapture
        return self._socket is not None

    def read(self) -> tuple[bool, Any]:
        """Request frames until an acceptable one arrives; return it as an HxWx3 uint8 BGR array."""
        sock = self._socket
        if sock is None:
            return False, None
        deadline = time.monotonic() + self._timeout_s
        skipped = 0
        last_age: int | None = None
        while True:
            try:
                header, image = self._read_frame(sock, deadline)
            except TimeoutError as exc:
                if not skipped:
                    raise
                raise self._inactive_timeout(skipped, last_age) from exc
            if header.camera_active or not self._require_active:
                self.last_header = header
                return True, image
            skipped += 1
            self.inactive_skipped += 1
            last_age = header.script_age_presents
            if time.monotonic() >= deadline:
                raise self._inactive_timeout(skipped, last_age)

    def _inactive_timeout(self, skipped: int, last_age: int | None) -> TimeoutError:
        age = "never written" if last_age in (None, SCRIPT_AGE_NEVER) else f"{last_age} presents"
        return TimeoutError(
            f"no active GTA drone camera frame in {self._timeout_s:g} s ({skipped} inactive frames skipped, "
            f"script age {age}): HorusGta is idle or the game is paused"
        )

    def release(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def _read_frame(self, sock: socket.socket, deadline: float) -> tuple[GtaFrameHeader, Any]:
        import numpy as np

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("GTA capture stream sent no complete frame before the read deadline")
        sock.settimeout(remaining)
        sock.sendall(REQUEST)
        prefix = self._recv_exact(sock, _PREFIX.size, deadline)
        magic, header_bytes = _PREFIX.unpack(prefix)
        legacy = header_bytes == LEGACY_HEADER.size and self._allow_legacy_header
        if magic != MAGIC or (header_bytes != HEADER.size and not legacy):
            raise RuntimeError(
                f"GTA capture stream sent an invalid header (magic {magic!r}, {header_bytes} header bytes, "
                f"expected {HEADER.size})"
            )
        layout = LEGACY_HEADER if legacy else HEADER
        fields = layout.unpack(prefix + self._recv_exact(sock, layout.size - _PREFIX.size, deadline))
        present_index, capture_ns, width, height, pixel_format, payload_bytes = fields[2:8]
        if pixel_format != PIXEL_FORMAT_BGR8 or payload_bytes != width * height * 3:
            raise RuntimeError(
                f"GTA capture stream sent unsupported frame format {pixel_format} "
                f"({width}x{height}, {payload_bytes} bytes)"
            )
        payload = self._recv_exact(sock, payload_bytes, deadline)
        if legacy:
            header = GtaFrameHeader(present_index, capture_ns, width, height, False, None, None, None)
        else:
            camera_active, script_age, pose_seq, game_frame = fields[8:]
            header = GtaFrameHeader(
                present_index, capture_ns, width, height, camera_active == 1, script_age, pose_seq, game_frame
            )
        return header, np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 3)

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int, deadline: float) -> bytearray:
        buffer = bytearray(size)
        view = memoryview(buffer)
        received = 0
        while received < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("GTA capture stream sent no complete frame before the read deadline")
            sock.settimeout(remaining)
            count = sock.recv_into(view[received:])
            if count == 0:
                raise ConnectionError("GTA capture stream closed")
            received += count
        return buffer
