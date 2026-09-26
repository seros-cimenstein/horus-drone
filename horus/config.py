"""Typed loading for the small, operator-editable HORUS YAML configuration."""

from __future__ import annotations

import ipaddress
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True, slots=True)
class FlightSettings:
    connection: str = "tcp:127.0.0.1:5760"
    baud: int = 115200
    heartbeat_timeout_s: float = 30.0
    source_system: int = 255
    source_component: int = 190
    gps_free_mode: bool = False
    global_origin_latitude_deg: float = -35.363262
    global_origin_longitude_deg: float = 149.165237
    global_origin_altitude_m: float = 584.0


@dataclass(frozen=True, slots=True)
class ExecutorSettings:
    control_hz: float = 15.0
    command_ttl_s: float = 30.0
    max_speed_m_s: float = 1.0
    max_vertical_speed_m_s: float = 0.5
    max_distance_m: float = 8.0
    min_altitude_m: float = 0.5
    max_altitude_m: float = 5.0
    geofence_radius_m: float = 8.0
    geofence_polygon_ned: tuple[tuple[float, float], ...] | None = None
    geofence_margin_m: float = 0.0
    arrival_tolerance_m: float = 0.25
    follow_distance_m: float = 2.0
    target_width_m: float | None = None
    # Known target height (m) for monocular range from the detection box height; people ~1.7 m.
    target_height_m: float | None = None
    target_match_threshold: float = 0.72
    camera_horizontal_fov_deg: float = 114.59155902616465
    link_loss_rth_delay_s: float = 30.0

    def __post_init__(self) -> None:
        try:
            margin = float(self.geofence_margin_m)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("geofence_margin_m must be a finite non-negative number") from exc
        if not math.isfinite(margin) or margin < 0.0:
            raise ValueError("geofence_margin_m must be a finite non-negative number")
        object.__setattr__(self, "geofence_margin_m", margin)

        polygon = self.geofence_polygon_ned
        if polygon is None:
            if margin != 0.0:
                raise ValueError("geofence_margin_m requires geofence_polygon_ned")
            return
        if not isinstance(polygon, Sequence) or isinstance(polygon, (str, bytes)):
            raise ValueError("geofence_polygon_ned must be a sequence of NED vertex pairs")
        vertices: list[tuple[float, float]] = []
        for vertex in polygon:
            if not isinstance(vertex, Sequence) or isinstance(vertex, (str, bytes)) or len(vertex) != 2:
                raise ValueError("each geofence_polygon_ned vertex must contain north_m and east_m")
            if any(isinstance(coordinate, bool) for coordinate in vertex):
                raise ValueError("geofence_polygon_ned coordinates must be finite numbers")
            try:
                north_m, east_m = (float(coordinate) for coordinate in vertex)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("geofence_polygon_ned coordinates must be finite numbers") from exc
            if not math.isfinite(north_m) or not math.isfinite(east_m):
                raise ValueError("geofence_polygon_ned coordinates must be finite numbers")
            vertices.append((north_m, east_m))
        if len(vertices) > 1 and vertices[0] == vertices[-1]:
            vertices.pop()
        if len(vertices) < 3:
            raise ValueError("geofence_polygon_ned needs at least three distinct vertices")
        object.__setattr__(self, "geofence_polygon_ned", tuple(vertices))


@dataclass(frozen=True, slots=True)
class PerceptionSettings:
    enabled: bool = True
    point_compensation_enabled: bool = True
    source: str = "udp://0.0.0.0:5600"
    detector_model: str = "models/yolov8n.onnx"
    embedding_model: str = "models/osnet_x0_25_msmt17.onnx"
    grounding_model: str = "IDEA-Research/grounding-dino-tiny"
    max_fps: float = 5.0
    onnx_intra_op_num_threads: int = 2
    onnx_inter_op_num_threads: int = 1
    ring_buffer_s: float = 5.0
    render_width_px: int = 640
    ignore_regions: tuple[tuple[float, float, float, float], ...] = ()

    def __post_init__(self) -> None:
        for name in ("onnx_intra_op_num_threads", "onnx_inter_op_num_threads"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"perception.{name} must be a positive integer")

        try:
            regions = tuple(
                tuple(float(coordinate) for coordinate in region)
                for region in self.ignore_regions
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "ignore_regions must contain normalized [x1, y1, x2, y2] boxes"
            ) from exc
        for region in regions:
            if len(region) != 4 or not all(math.isfinite(value) for value in region):
                raise ValueError(
                    "ignore_regions must contain normalized [x1, y1, x2, y2] boxes"
                )
            x1, y1, x2, y2 = region
            if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
                raise ValueError("ignore_regions boxes must be ordered within [0, 1]")
        object.__setattr__(self, "ignore_regions", regions)

@dataclass(frozen=True, slots=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    auth_token_env: str = "HORUS_MCP_TOKEN"

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("server.host must be a loopback address")
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host.casefold() == "localhost"
        if not loopback:
            raise ValueError("server.host must be loopback-only")


@dataclass(frozen=True, slots=True)
class CommanderSettings:
    provider: str = "claude-code"
    model: str = "claude-3-5-sonnet-latest"
    base_url: str | None = None
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 800
    timeout_s: float = 180.0
    max_turns: int = 8
    claude_command: str = "claude"
    # Claude Code backend: model alias or full name ('opus' tracks the newest Opus available to the
    # account) and reasoning effort (low, medium, high, xhigh, max). None keeps the CLI's own default.
    claude_model: str | None = None
    claude_effort: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in {"claude-code", "anthropic", "openai"}:
            raise ValueError("commander provider must be 'claude-code', 'anthropic', or 'openai'")
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or not math.isfinite(self.timeout_s)
            or self.timeout_s <= 0
        ):
            raise ValueError("commander timeout_s must be a finite positive number")
        if self.claude_effort is not None and self.claude_effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("commander claude_effort must be low, medium, high, xhigh, or max")
        if isinstance(self.max_turns, bool) or not isinstance(self.max_turns, int) or self.max_turns < 1:
            raise ValueError("commander max_turns must be a positive integer")
        if not isinstance(self.claude_command, str) or not self.claude_command.strip():
            raise ValueError("commander claude_command must be a non-empty string")


@dataclass(frozen=True, slots=True)
class HorusConfig:
    flight: FlightSettings = FlightSettings()
    executor: ExecutorSettings = ExecutorSettings()
    perception: PerceptionSettings = PerceptionSettings()
    server: ServerSettings = ServerSettings()
    commander: CommanderSettings = CommanderSettings()


def _section(data: Mapping[str, Any], name: str, kind: type) -> Any:
    values = data.get(name, {})
    if not isinstance(values, Mapping):
        raise ValueError(f"configuration section {name!r} must be a mapping")
    known = kind.__dataclass_fields__
    unknown = set(values) - set(known)
    if unknown:
        raise ValueError(f"unknown {name} setting(s): {', '.join(sorted(unknown))}")
    try:
        return kind(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name} configuration: {exc}") from exc


def load_config(path: str | Path | None = None) -> HorusConfig:
    """Load config, preferring an explicit path then HORUS_CONFIG then config/horus.yaml."""
    selected = Path(path or os.environ.get("HORUS_CONFIG", "config/horus.yaml"))
    if not selected.is_absolute():
        selected = Path.cwd() / selected
    with selected.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, Mapping):
        raise ValueError("HORUS configuration root must be a mapping")
    allowed = {"flight", "executor", "perception", "server", "commander"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown configuration section(s): {', '.join(sorted(unknown))}")
    return HorusConfig(
        flight=_section(data, "flight", FlightSettings),
        executor=_section(data, "executor", ExecutorSettings),
        perception=_section(data, "perception", PerceptionSettings),
        server=_section(data, "server", ServerSettings),
        commander=_section(data, "commander", CommanderSettings),
    )
