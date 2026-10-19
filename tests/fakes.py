"""Shared test doubles: one flight link, executor settings, an MCP test server and detectors."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

from horus.config import ExecutorSettings
from horus.flight import Pose
from horus.perception.types import Detection, Frame, FrameObservation


class FakeTelemetry:
    """Fresh, mutable stand-in for ``horus.flight.Telemetry`` whose fields tests set directly."""

    def __init__(self, pose: Pose, *, armed: bool = False, mode: str = "STABILIZE"):
        now = time.monotonic()
        self.pose = pose
        self.observed_at = "2026-09-24T00:00:00+00:00"
        self.received_monotonic = now
        self.pose_updated_monotonic = now
        self.heading_deg = 0.0
        self.heading_updated_monotonic = now
        self.armed = armed
        self.mode = mode
        self.proximity: tuple[Any, ...] = ()
        self.ground_distance: Any = None
        self.mount_status: Any = None
        self.time_boot_ms: int | None = None
        self.time_boot_updated_monotonic: float | None = None
        self.vehicle_roll_deg: float | None = None
        self.vehicle_pitch_deg: float | None = None
        self.attitude_updated_monotonic: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"pose": self.pose.as_dict(), "observed_at": self.observed_at}


class FakeFlight:
    """Every flight-link operation the executor and MCP server use, recorded in ``commands``."""

    def __init__(self, pose: Pose = Pose(0.0, 0.0, 2.0), *, armed: bool = False, mode: str = "STABILIZE"):
        self._telemetry = FakeTelemetry(pose, armed=armed, mode=mode)
        self.commands: list[tuple] = []
        self.heartbeat = True

    def telemetry(self) -> FakeTelemetry:
        return self._telemetry

    def heartbeat_ok(self, max_age_s: float = 3.0) -> bool:
        return self.heartbeat

    def set_guided(self) -> None:
        self.commands.append(("guided",))

    def arm(self) -> None:
        self.commands.append(("arm",))

    def takeoff(self, altitude: float) -> None:
        self.commands.append(("takeoff", altitude))

    def set_velocity_ned(self, north, east, down, yaw_deg=None) -> None:
        self.commands.append(
            ("velocity", north, east, down) if yaw_deg is None else ("velocity", north, east, down, yaw_deg)
        )

    def set_gimbal_pitch_yaw(self, pitch_deg: float, yaw_deg: float = 0.0) -> None:
        self.commands.append(("gimbal", pitch_deg, yaw_deg))

    def hold(self) -> None:
        self.commands.append(("hold",))

    def rth(self) -> None:
        self.commands.append(("rth",))

    def land(self) -> None:
        self.commands.append(("land",))

    def flight_commands(self) -> list[tuple]:
        """Commands that move the vehicle or change its mode (camera aiming excluded)."""
        return [command for command in self.commands if command[0] != "gimbal"]


def executor_settings(**overrides: Any) -> ExecutorSettings:
    """Small indoor envelope with fast TTLs, shared by executor-level tests."""
    values: dict[str, Any] = {
        "control_hz": 15.0,
        "command_ttl_s": 1.0,
        "max_speed_m_s": 1.0,
        "max_vertical_speed_m_s": 0.5,
        "max_distance_m": 5.0,
        "min_altitude_m": 0.5,
        "max_altitude_m": 4.0,
        "geofence_radius_m": 3.0,
        "arrival_tolerance_m": 0.25,
        "follow_distance_m": 2.0,
        "target_width_m": None,
    }
    values.update(overrides)
    return ExecutorSettings(**values)


class FixedDetector:
    """Returns the same detection(s) for every frame."""

    def __init__(self, *detections: Detection):
        self.detections = list(detections)

    def detect(self, image: Any) -> list[Detection]:
        return list(self.detections)


def feed_frame(system: Any, image: Any, timestamp: float, pose: Any = None) -> FrameObservation:
    """Process ``image`` as the system's next camera frame; frame IDs count up from 1."""
    latest = system.buffer.latest()
    frame_id = latest.frame_id + 1 if latest is not None else 1
    return system.process_frame(Frame(frame_id, timestamp, image, pose=pose))


class RunningServer:
    """Serve an ASGI app on an ephemeral loopback port in a background thread."""

    def __init__(self, app: Any):
        import uvicorn

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.socket.setblocking(False)
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="critical", access_log=False)
        )
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10.0
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            self.close()
            raise RuntimeError("MCP test server did not start")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5.0)
        self.socket.close()


def call_tool(mcp: Any, name: str, arguments: dict | None = None, *, convert_result: bool = False) -> Any:
    """Call a FastMCP tool in-process (no transport)."""
    return asyncio.run(mcp._tool_manager.call_tool(name, arguments or {}, convert_result=convert_result))


class PerceptionStub:
    """Every ``PerceptionPort`` method with a neutral answer; executor fakes override what they need."""

    def latest_observation(self) -> Any | None:
        return None

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return None

    def validate_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return None

    def validate_candidate_failure(self, candidate_id: int) -> str | None:
        return None

    def reidentify_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return None

    def get_frame(self, frame_id: int | None = None) -> Any | None:
        return None
