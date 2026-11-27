"""Pull-TCP drone camera server (sim:// / HGTA protocol) + 3D spectator JPEG endpoint."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import math
import os
import socket
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import cv2
import mujoco
import numpy as np

from horus.perception.gta_stream import HEADER, MAGIC, PIXEL_FORMAT_BGR8, REQUEST
from horus.world.physics import MuJoCoQuadWorld

logger = logging.getLogger(__name__)

DEFAULT_CAMERA_WIDTH = 640
DEFAULT_CAMERA_HEIGHT = 480


def _draw_spectator_overlay(
    bgr: np.ndarray,
    world: MuJoCoQuadWorld,
    *,
    show_tof: bool = True,
) -> np.ndarray:
    """Annotate the 3D spectator view with mini ToF radar and altitude/attitude HUD."""
    if not show_tof:
        return bgr
    out = bgr.copy()
    h, w, _ = out.shape
    cx, cy = w - 62, 62
    radius = 42
    cv2.circle(out, (cx, cy), radius, (20, 26, 34), -1)
    cv2.circle(out, (cx, cy), radius, (90, 120, 150), 1)
    readings = world.read_rangefinders_m()
    sector_angles = [
        ("range_front", -90.0),
        ("range_front_right", -45.0),
        ("range_back_right", 45.0),
        ("range_back", 90.0),
        ("range_back_left", 135.0),
        ("range_front_left", -135.0),
    ]
    for name, deg in sector_angles:
        dist = readings.get(name, 4.0)
        frac = max(0.1, min(1.0, dist / 4.0))
        rad = math.radians(deg)
        ex = int(cx + math.cos(rad) * radius * frac)
        ey = int(cy + math.sin(rad) * radius * frac)
        color = (70, 80, 240) if dist < 1.2 else (60, 200, 240) if dist < 2.2 else (90, 220, 130)
        cv2.line(out, (cx, cy), (ex, ey), color, 2)
    cv2.circle(out, (cx, cy), 4, (240, 240, 240), -1)
    down = readings.get("range_down", 0.0)
    alt = -world.last_state.pos_ned[2]
    cv2.putText(
        out,
        f"ALT {alt:4.2f}m  RNG {down:4.2f}m",
        (12, h - 14),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (230, 240, 250),
        1,
        cv2.LINE_AA,
    )
    return out


class WorldCameraServer:
    """Serves 640x480 BGR8 drone camera frames via pull-TCP and 3D spectator JPEG frames via HTTP."""

    def __init__(
        self,
        world: MuJoCoQuadWorld,
        *,
        host: str = "127.0.0.1",
        camera_port: int = 5610,
        spectator_port: int | None = None,
        width: int = DEFAULT_CAMERA_WIDTH,
        height: int = DEFAULT_CAMERA_HEIGHT,
    ) -> None:
        self.world = world
        self.host = host
        self.camera_port = int(camera_port)
        self.spectator_port = int(spectator_port) if spectator_port is not None else None
        self.width = int(width)
        self.height = int(height)

        self._render_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="horus-world-render")
        self._renderer: mujoco.Renderer | None = None
        self._render_error: str | None = None
        self._present_index = 0
        self._stop = threading.Event()
        self._tcp_server: socket.socket | None = None
        self._tcp_thread: threading.Thread | None = None
        self._client_threads: list[threading.Thread] = []
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None

    def _ensure_renderer(self) -> mujoco.Renderer | None:
        if self._renderer is not None:
            return self._renderer
        if self._render_error is not None:
            return None
        try:
            self._renderer = mujoco.Renderer(self.world.model, height=self.height, width=self.width)
            logger.info("MuJoCo offscreen renderer ready (MUJOCO_GL=%s)", os.environ.get("MUJOCO_GL", "default"))
            return self._renderer
        except Exception as exc:
            if sys.platform.startswith("linux") and os.environ.get("MUJOCO_GL") == "egl":
                try:
                    os.environ["MUJOCO_GL"] = "osmesa"
                    self._renderer = mujoco.Renderer(self.world.model, height=self.height, width=self.width)
                    logger.warning("EGL offscreen failed (%s); using OSMesa fallback", exc)
                    return self._renderer
                except Exception as fallback_exc:
                    exc = fallback_exc
            self._render_error = str(exc)
            logger.warning("MuJoCo offscreen renderer unavailable, using synthetic frame fallback: %s", exc)
            return None

    def _render_on_gl_thread(self, camera: str) -> np.ndarray | None:
        renderer = self._ensure_renderer()
        if renderer is None:
            return None
        return self.world.render_camera(renderer, camera)

    def render_drone_bgr(self) -> np.ndarray:
        """Render the drone camera as HxWx3 uint8 BGR."""
        rgb: np.ndarray | None = None
        try:
            rgb = self._render_executor.submit(self._render_on_gl_thread, "drone_cam").result()
        except RuntimeError:
            rgb = None
        if rgb is not None:
            return np.ascontiguousarray(rgb[:, :, ::-1])
        # Fallback synthetic frame if headless GL is missing on restricted runners.
        frame = np.full((self.height, self.width, 3), 28, dtype=np.uint8)
        cv2.putText(frame, "HORUS 3D (no GL)", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (220, 220, 220), 2)
        return frame

    def render_spectator_jpeg(self, *, preset: str = "follow", show_tof: bool = True) -> bytes:
        """Render the 3D spectator camera as JPEG bytes."""
        self.world.set_spectator_view(preset=preset, show_tof=show_tof)
        rgb: np.ndarray | None = None
        try:
            rgb = self._render_executor.submit(self._render_on_gl_thread, "spectator_cam").result()
        except RuntimeError:
            rgb = None
        if rgb is not None:
            bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        else:
            bgr = np.full((self.height, self.width, 3), 24, dtype=np.uint8)
        bgr = _draw_spectator_overlay(bgr, self.world, show_tof=show_tof)
        ok, encoded = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            raise RuntimeError("failed to encode spectator JPEG")
        return encoded.tobytes()

    def start(self) -> None:
        self._stop.clear()
        tcp_sock = socket.create_server((self.host, self.camera_port), backlog=16)
        tcp_sock.settimeout(0.2)
        self.camera_port = int(tcp_sock.getsockname()[1])
        self._tcp_server = tcp_sock
        self._tcp_thread = threading.Thread(target=self._accept_loop, name="horus-world-cam-tcp", daemon=True)
        self._tcp_thread.start()

        if self.spectator_port is not None:
            cam_server = self

            class _SpectatorHandler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802
                    parsed = urlsplit(self.path)
                    qs = parse_qs(parsed.query)
                    preset = (qs.get("preset") or ["follow"])[0]
                    show_tof = (qs.get("tof") or ["1"])[0] not in {"0", "false", "no"}
                    if parsed.path == "/stream.mjpg":
                        try:
                            fps = max(1, min(30, int((qs.get("fps") or ["25"])[0])))
                        except ValueError:
                            fps = 25
                        period = 1.0 / fps
                        self.send_response(200)
                        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=horusframe")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        while not cam_server._stop.is_set():
                            started = time.monotonic()
                            try:
                                jpeg = cam_server.render_spectator_jpeg(preset=preset, show_tof=show_tof)
                                part = (
                                    b"--horusframe\r\n"
                                    b"Content-Type: image/jpeg\r\n"
                                    + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                                    + jpeg
                                    + b"\r\n"
                                )
                                self.wfile.write(part)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                break
                            except Exception:
                                break
                            remaining = period - (time.monotonic() - started)
                            if remaining > 0 and cam_server._stop.wait(remaining):
                                break
                        return
                    if parsed.path != "/frame.jpg":
                        self.send_response(404)
                        self.end_headers()
                        return
                    try:
                        jpeg = cam_server.render_spectator_jpeg(preset=preset, show_tof=show_tof)
                    except Exception as exc:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(str(exc).encode("utf-8"))
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpeg)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(jpeg)

                def log_message(self, format: str, *args: Any) -> None:
                    return

            httpd = ThreadingHTTPServer((self.host, self.spectator_port), _SpectatorHandler)
            self.spectator_port = int(httpd.server_address[1])
            self._http_server = httpd
            self._http_thread = threading.Thread(
                target=httpd.serve_forever, name="horus-world-spectator", daemon=True
            )
            self._http_thread.start()

    def _accept_loop(self) -> None:
        assert self._tcp_server is not None
        while not self._stop.is_set():
            try:
                client, _ = self._tcp_server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._serve_client, args=(client,), daemon=True)
            t.start()
            self._client_threads.append(t)

    def _serve_client(self, client: socket.socket) -> None:
        client.settimeout(1.0)
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        with client:
            while not self._stop.is_set():
                try:
                    req = client.recv(1)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not req:
                    return
                if req != REQUEST:
                    continue
                bgr = self.render_drone_bgr()
                self._present_index += 1
                header = HEADER.pack(
                    MAGIC,
                    HEADER.size,
                    self._present_index,
                    time.time_ns(),
                    self.width,
                    self.height,
                    PIXEL_FORMAT_BGR8,
                    bgr.nbytes,
                    1,
                    0,
                    self._present_index,
                    self._present_index,
                )
                try:
                    client.sendall(header + bgr.tobytes())
                except OSError:
                    return

    def close(self) -> None:
        self._stop.set()
        if self._tcp_server is not None:
            try:
                self._tcp_server.close()
            except OSError:
                pass
            self._tcp_server = None
        if self._tcp_thread is not None and self._tcp_thread.is_alive():
            self._tcp_thread.join(timeout=2.0)
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._http_thread is not None and self._http_thread.is_alive():
            self._http_thread.join(timeout=2.0)
        def _close_renderer() -> None:
            if self._renderer is not None:
                try:
                    self._renderer.close()
                except Exception:
                    pass
                self._renderer = None

        try:
            self._render_executor.submit(_close_renderer).result(timeout=2.0)
        except Exception:
            pass
        self._render_executor.shutdown(wait=True)
