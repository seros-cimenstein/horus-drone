"""OpenCV camera input for RTP/H.264, local video files, or camera indices."""

from __future__ import annotations

import math

from dataclasses import dataclass
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from .types import Frame


def _has_gstreamer(cv2_module: Any) -> bool:
    try:
        build_info = cv2_module.getBuildInformation()
    except (AttributeError, RuntimeError):
        return False
    if not isinstance(build_info, str):
        return False
    for line in build_info.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "gstreamer":
            return value.strip().upper().startswith("YES")
    return False


@dataclass(frozen=True, slots=True)
class CameraConfig:
    """Camera source options; ``source='udp'`` reads the simulation's RTP stream."""

    source: str | int = "udp"
    udp_host: str = "0.0.0.0"
    udp_port: int = 5600
    fps_cap: float = 10.0
    gstreamer: bool = True
    open_timeout_s: float = 5.0
    read_timeout_s: float = 5.0
    udp_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if self.udp_port < 1 or self.udp_port > 65535:
            raise ValueError("udp_port must be between 1 and 65535")
        if self.fps_cap <= 0:
            raise ValueError("fps_cap must be positive")
        if not all(
            math.isfinite(timeout) and timeout > 0
            for timeout in (self.open_timeout_s, self.read_timeout_s, self.udp_timeout_s)
        ):
            raise ValueError("camera timeouts must be finite and positive")

    def gstreamer_pipeline(self, *, host: str | None = None, port: int | None = None) -> str:
        """Build an OpenCV GStreamer pipeline for RTP payload type 96 H.264."""
        host = self.udp_host if host is None else host
        port = self.udp_port if port is None else port
        address = f"address={host} " if host not in ("", "0.0.0.0") else ""
        timeout_ns = int(self.udp_timeout_s * 1_000_000_000)
        return (
            f'udpsrc {address}port={port} timeout={timeout_ns} '
            'caps="application/x-rtp,media=(string)video,clock-rate=(int)90000,'
            'encoding-name=(string)H264,payload=(int)96" ! rtph264depay ! h264parse ! avdec_h264 ! '
            'videoconvert ! video/x-raw,format=(string)BGR ! '
            'appsink drop=true max-buffers=1 sync=false'
        )


class OpenCVCameraReader:
    """Read timestamped frames through OpenCV; OpenCV/GStreamer loads only on ``open``."""

    def __init__(
        self,
        config: CameraConfig | None = None,
        *,
        pose_provider: Callable[[float], Any] | None = None,
    ) -> None:
        self.config = config or CameraConfig()
        self.pose_provider = pose_provider
        self._capture: Any = None
        self._frame_id = 0
        self._next_read_at = 0.0

    def open(self) -> "OpenCVCameraReader":
        """Open the configured stream, raising a useful error on backend failure."""
        if self._capture is not None:
            return self
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("OpenCV is required to open a camera or video source") from exc

        source = self.config.source
        is_rtp_uri = isinstance(source, str) and source.lower().startswith(("udp://", "rtp://"))
        use_rtp = isinstance(source, str) and (
            source.lower() in {"udp", "rtp", "udp://", "rtp://"} or is_rtp_uri
        )
        if use_rtp:
            if not self.config.gstreamer:
                raise ValueError("RTP/H.264 input requires OpenCV's GStreamer backend")
            if not _has_gstreamer(cv2):
                raise RuntimeError(
                    "UDP/RTP camera input requires OpenCV built with GStreamer, but this build has none. "
                    "In the simulation container use Ubuntu's system python3-opencv; do not install "
                    "the opencv-headless extra (opencv-contrib-python-headless), which lacks GStreamer "
                    "and shadows the system package."
                )
            host, port = self.config.udp_host, self.config.udp_port
            if is_rtp_uri:
                endpoint = urlsplit(source)
                host = endpoint.hostname or host
                port = endpoint.port or port
            pipeline = self.config.gstreamer_pipeline(host=host, port=port)
            # GStreamer rejects CAP_PROP_* open parameters; the pipeline and watchdog enforce timeouts.
            capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        else:
            capture = cv2.VideoCapture(source)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"Unable to open camera/video source {source!r}")
        self._capture = capture
        self._next_read_at = time.monotonic()
        return self

    def read(self) -> Frame | None:
        """Return the next frame, respecting ``fps_cap``; end-of-file returns ``None``."""
        if self._capture is None:
            self.open()
        interval = 1.0 / self.config.fps_cap
        now = time.monotonic()
        delay = self._next_read_at - now
        if delay > 0:
            time.sleep(delay)
        ok, image = self._capture.read()
        timestamp = time.monotonic()
        self._next_read_at = max(self._next_read_at + interval, timestamp)
        if not ok or image is None:
            return None
        self._frame_id += 1
        pose = self.pose_provider(timestamp) if self.pose_provider is not None else None
        return Frame(frame_id=self._frame_id, timestamp=timestamp, image=image, pose=pose)

    def release(self) -> None:
        """Release the native capture resource; safe to call repeatedly."""
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def __enter__(self) -> "OpenCVCameraReader":
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.release()
