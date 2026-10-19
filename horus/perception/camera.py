"""OpenCV camera input (RTP/H.264, video files, camera indices) and its supervised capture loop."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
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


class CameraSupervisor:
    """Run camera capture on a worker thread with a watchdog that releases a stalled source.

    Each capture operation (open/read) is timed against the camera's ``open_timeout_s`` /
    ``read_timeout_s``; a stalled operation marks the camera ``stalled`` and releases it
    asynchronously so the blocked call returns. Failures retry with exponential backoff.
    ``on_watchdog_tick`` runs on every watchdog iteration, including while capture stalls.
    """

    def __init__(
        self,
        camera: Any,
        process: Callable[[Frame], Any],
        *,
        stop_event: threading.Event,
        on_watchdog_tick: Callable[[], None] = lambda: None,
    ) -> None:
        self.camera = camera
        self._process = process
        self._stop_event = stop_event
        self._on_watchdog_tick = on_watchdog_tick
        self._lock = threading.Lock()
        self._state = "connecting" if camera is not None else "disabled"
        self._reason: str | None = None if camera is not None else "perception is disabled"
        self._last_frame_at: float | None = None
        self._operation: str | None = None
        self._operation_started: float | None = None
        self._watchdog_fired = False
        self._worker: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._release_lock = threading.Lock()
        self._release_pending = False
        self._release_complete = threading.Event()
        self._release_complete.set()

    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def start(self, callback: Callable[[Any], Any] | None) -> None:
        """Start the capture and watchdog threads (the caller checks ``running`` first)."""
        with self._lock:
            self._state = "connecting"
            self._reason = None
            self._operation = None
            self._operation_started = None
            self._watchdog_fired = False
        self._watchdog = threading.Thread(
            target=self._watch, name="horus-camera-watchdog", daemon=True
        )
        self._worker = threading.Thread(
            target=self._run, args=(callback,), name="horus-perception", daemon=True
        )
        self._watchdog.start()
        self._worker.start()

    def threads(self) -> tuple[threading.Thread | None, ...]:
        return self._worker, self._watchdog

    def stop(self) -> None:
        """Mark the camera stopped and forget finished threads (call after ``stop_event`` is set)."""
        with self._lock:
            self._state = "disabled"
            self._reason = "perception is stopped"
            self._operation = None
            self._operation_started = None
        if self._worker is not None and not self._worker.is_alive():
            self._worker = None
        if self._watchdog is not None and not self._watchdog.is_alive():
            self._watchdog = None

    def status(self) -> dict[str, Any]:
        """Capture state, monotonic age of the latest frame and the failure reason."""
        now = time.monotonic()
        with self._lock:
            age = None if self._last_frame_at is None else max(0.0, now - self._last_frame_at)
            return {"camera": self._state, "last_frame_age_s": age, "reason": self._reason}

    def set_status(self, state: str, reason: str | None, frame_time: float | None = None) -> None:
        with self._lock:
            self._state = state
            self._reason = reason
            if frame_time is not None:
                self._last_frame_at = frame_time

    def _begin(self, operation: str) -> None:
        with self._lock:
            self._operation = operation
            self._operation_started = time.monotonic()
            self._watchdog_fired = False

    def _end(self) -> bool:
        with self._lock:
            timed_out = self._watchdog_fired
            self._operation = None
            self._operation_started = None
            self._watchdog_fired = False
        return timed_out

    def _timeout(self, operation: str) -> float:
        config = getattr(self.camera, "config", None)
        name = "open_timeout_s" if operation == "open" else "read_timeout_s"
        try:
            timeout = float(getattr(config, name, 5.0))
        except (TypeError, ValueError, OverflowError):
            return 5.0
        return timeout if math.isfinite(timeout) and timeout > 0 else 5.0

    def _timed(self, operation: str, call: Callable[[], Any]) -> Any:
        """Run one capture call under the watchdog; raise when the watchdog fired meanwhile."""
        self._begin(operation)
        try:
            result = call()
        finally:
            timed_out = self._end()
        if timed_out and not self._stop_event.is_set():
            raise RuntimeError(
                f"camera {operation} timed out after {self._timeout(operation):g} seconds"
            )
        return result

    def release_async(self) -> None:
        """Release the camera on a helper thread; a release already in flight is not repeated."""
        if self.camera is None:
            return
        with self._release_lock:
            if self._release_pending:
                return
            self._release_pending = True
            self._release_complete.clear()

        def release() -> None:
            try:
                self.camera.release()
            finally:
                with self._release_lock:
                    self._release_pending = False
                    self._release_complete.set()

        threading.Thread(target=release, name="horus-camera-release", daemon=True).start()

    def _wait_for_release(self) -> bool:
        while not self._stop_event.is_set():
            with self._release_lock:
                if not self._release_pending:
                    return True
            self._release_complete.wait(0.1)
        return False

    def _watch(self) -> None:
        while not self._stop_event.is_set():
            self._on_watchdog_tick()
            with self._lock:
                operation = self._operation
                started = self._operation_started
            if operation is None or started is None:
                self._stop_event.wait(0.1)
                continue
            timeout = self._timeout(operation)
            interval = max(0.01, min(0.25, timeout / 4.0))
            if self._stop_event.wait(interval):
                return
            now = time.monotonic()
            should_release = False
            with self._lock:
                if (
                    self._operation == operation
                    and self._operation_started == started
                    and not self._watchdog_fired
                    and now - started >= timeout
                ):
                    self._watchdog_fired = True
                    self._state = "stalled"
                    self._reason = f"camera {operation} timed out after {timeout:g} seconds"
                    should_release = True
            if should_release:
                self.release_async()

    def _run(self, callback: Callable[[Any], Any] | None) -> None:
        backoff_s = 0.5
        while not self._stop_event.is_set():
            if not self._wait_for_release():
                return
            self.set_status("connecting", None)
            try:
                self._timed("open", self.camera.open)
                while not self._stop_event.is_set():
                    frame = self._timed("read", self.camera.read)
                    if self._stop_event.is_set():
                        return
                    if frame is None:
                        raise RuntimeError("camera source returned no frame")
                    timestamp = getattr(frame, "timestamp", time.monotonic())
                    self.set_status("streaming", None, float(timestamp))
                    backoff_s = 0.5
                    observation = self._process(frame)
                    if callback is not None:
                        callback(observation)
                return
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                self.set_status("stalled", str(exc))
                self.release_async()
                if self._stop_event.wait(backoff_s):
                    return
                backoff_s = min(backoff_s * 2.0, 8.0)
