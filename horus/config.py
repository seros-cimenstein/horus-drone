"""Typed loading for the small, operator-editable HORUS YAML configuration."""

from __future__ import annotations

import ipaddress
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
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
class GimbalSettings:
    """The camera gimbal, driven directly by HORUS over the MAVLink gimbal-device protocol
    (Gimbal Protocol v2 device messages); the autopilot takes no part in it.

    ``connection`` is the gimbal device's MAVLink endpoint (pymavlink form, e.g. ``udpin:...`` or
    ``/dev/ttyS1``); None means a fixed camera, so ``look`` is refused and geometry uses
    ``executor.default_gimbal_pitch_deg``. HORUS speaks as the gimbal manager
    (``source_system``/``source_component``) to the device ``device_system``/``device_component``
    and accepts status only from it.
    """

    connection: str | None = None
    baud: int = 115200
    source_system: int = 1
    # MAV_COMP_ID_ONBOARD_COMPUTER: the drone board is the gimbal manager.
    source_component: int = 191
    device_system: int = 1
    # MAV_COMP_ID_GIMBAL.
    device_component: int = 154
    # Earth-frame pitch (negative down) and nose-relative yaw the device accepts.
    pitch_min_deg: float = -90.0
    pitch_max_deg: float = 45.0
    yaw_limit_deg: float = 160.0
    # GIMBAL_DEVICE_ATTITUDE_STATUS rate requested from the device.
    status_rate_hz: float = 20.0
    # True for a device whose reported roll/pitch are relative to the vehicle body although its
    # flags claim horizon lock; HORUS then composes the vehicle lean back in.
    status_body_frame: bool = False

    def __post_init__(self) -> None:
        if self.connection is not None and (not isinstance(self.connection, str) or not self.connection.strip()):
            raise ValueError("gimbal connection must be a non-empty string or null")
        for name in ("source_system", "source_component", "device_system", "device_component"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
                raise ValueError(f"gimbal {name} must be an integer from 1 to 255")
        for name in ("pitch_min_deg", "pitch_max_deg", "yaw_limit_deg", "status_rate_hz"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"gimbal {name} must be a finite number")
        if not -90.0 <= self.pitch_min_deg < self.pitch_max_deg <= 90.0:
            raise ValueError("gimbal pitch limits must satisfy -90 <= pitch_min_deg < pitch_max_deg <= 90")
        if not 0.0 <= self.yaw_limit_deg <= 180.0:
            raise ValueError("gimbal yaw_limit_deg must be between 0 and 180")
        if self.status_rate_hz <= 0.0:
            raise ValueError("gimbal status_rate_hz must be positive")


@dataclass(frozen=True, slots=True)
class ExecutorSettings:
    control_hz: float = 15.0
    command_ttl_s: float = 30.0
    # Go-to controller speeds. GUIDED velocity setpoints bypass ArduPilot's waypoint speed
    # parameters, so the controller flies its legs at these speeds; they are not safety limits.
    speed_m_s: float = 1.0
    # Altitude-gated cruise speed for open transit legs: the horizontal speed is
    # ``cruise_speed_m_s`` while both the current altitude above home and the commanded target
    # altitude are at/above ``cruise_min_altitude_m``, and ``speed_m_s`` anywhere below it.
    # None (or 0) disables the gate and keeps every leg at ``speed_m_s``.
    cruise_speed_m_s: float | None = None
    cruise_min_altitude_m: float | None = None
    vertical_speed_m_s: float = 0.5
    # Default rate of the ``turn`` skill's turn in place (deg/s, autopilot time) when the
    # commander gives none; ArduPilot's yaw-rate limits bound any rate.
    turn_rate_deg_s: float = 30.0
    arrival_tolerance_m: float = 0.25
    camera_horizontal_fov_deg: float = 114.59155902616465
    default_gimbal_pitch_deg: float = 0.0
    link_loss_rth_delay_s: float = 30.0
    # Wall-clock freshness limit for pose, heading, forward-ToF and camera-frame samples driving
    # motion. Real flight keeps the default; the flight link requests the control messages at
    # 20 Hz (autopilot time) so the wall gap stays below it down to very low sim real-time factors.
    telemetry_max_age_s: float = 1.5
    # Precision landing (RTH/LAND): while precision control owns the descent, hand back to the
    # autopilot LAND if altitude has not fallen at least precision_landing_min_descent_m_s for
    # precision_landing_stall_s; an armed GUIDED hover would otherwise hang over the pad.
    precision_landing_stall_s: float = 12.0
    precision_landing_min_descent_m_s: float = 0.05
    # Keep commanding the last validated tag setpoint (centring + descent) this long after the
    # last fresh detection, so an intermittent detector does not pulse the setpoint to zero.
    precision_landing_target_grace_s: float = 1.0

    def __post_init__(self) -> None:
        _number(self, "control_hz", 10.0, 20.0)
        for name in (
            "command_ttl_s",
            "speed_m_s",
            "vertical_speed_m_s",
            "arrival_tolerance_m",
            "link_loss_rth_delay_s",
            "precision_landing_stall_s",
            "precision_landing_min_descent_m_s",
            "precision_landing_target_grace_s",
            "telemetry_max_age_s",
        ):
            _number(self, name, 0.0, low_open=True)
        _number(self, "camera_horizontal_fov_deg", 1.0, 179.0)
        _number(self, "default_gimbal_pitch_deg", -90.0, 45.0)
        _number(self, "turn_rate_deg_s", 0.0, low_open=True)

        # 0 or None disables the altitude-gated cruise speed.
        if not _number(self, "cruise_speed_m_s", 0.0, optional=True):
            object.__setattr__(self, "cruise_speed_m_s", None)
            if self.cruise_min_altitude_m is not None:
                raise ValueError("cruise_min_altitude_m requires cruise_speed_m_s")
        else:
            if self.cruise_speed_m_s <= self.speed_m_s:
                raise ValueError("cruise_speed_m_s must exceed speed_m_s")
            if self.cruise_min_altitude_m is None:
                raise ValueError("cruise_speed_m_s requires cruise_min_altitude_m")
            _number(self, "cruise_min_altitude_m", 0.0)


def _number(
    settings: Any,
    name: str,
    low: float | None = None,
    high: float | None = None,
    *,
    low_open: bool = False,
    high_open: bool = False,
    optional: bool = False,
    section: str = "",
) -> float | None:
    """Validate ``settings.<name>`` as a finite number in the given range and store it as float.

    ``optional`` admits None. Bounds are inclusive unless ``low_open``/``high_open``; ``section``
    prefixes the field name in the error (``"perception."``).
    """
    value = getattr(settings, name)
    if value is None and optional:
        return None
    lower = "" if low is None else f"{'>' if low_open else '>='} {low:g}"
    upper = "" if high is None else f"{'<' if high_open else '<='} {high:g}"
    bounds = " and ".join(part for part in (lower, upper) if part)
    message = f"{section}{name} must be a finite number{' ' + bounds if bounds else ''}"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    number = float(value)
    if (
        not math.isfinite(number)
        or (low is not None and (number <= low if low_open else number < low))
        or (high is not None and (number >= high if high_open else number > high))
    ):
        raise ValueError(message)
    object.__setattr__(settings, name, number)
    return number


def _count(settings: Any, name: str, high: int | None = None, *, section: str = "") -> int:
    """Validate ``settings.<name>`` as an integer in [1, high]."""
    value = getattr(settings, name)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or (high is not None and value > high)
    ):
        limit = f" between 1 and {high}" if high is not None else ""
        raise ValueError(f"{section}{name} must be a positive integer{limit}")
    return value


@dataclass(frozen=True, slots=True)
class PerceptionSettings:
    enabled: bool = True
    point_compensation_enabled: bool = True
    source: str = "udp://0.0.0.0:5600"
    detector_model: str = "models/yolov8n.onnx"
    embedding_model: str = "models/osnet_x0_25_msmt17.onnx"
    grounder_url: str | None = "http://127.0.0.1:8100"
    max_fps: float = 5.0
    open_vocab_hz: float = 0.5
    vocabulary: list[str] = field(default_factory=list)
    onnx_intra_op_num_threads: int = 2
    onnx_inter_op_num_threads: int = 1
    ring_buffer_s: float = 5.0
    render_width_px: int = 640
    # Tracker identity tuning, passed through to horus.perception.tracks.TrackerConfig (which
    # documents each field).
    reference_views: int = 4
    # Crops below this size are never stored as reference views.
    reference_view_min_width_px: int = 12
    reference_view_min_height_px: int = 30
    reference_view_dedupe_cos: float = 0.98
    ignore_regions: tuple[tuple[float, float, float, float], ...] = ()

    def __post_init__(self) -> None:
        section = "perception."
        for name in ("max_fps", "open_vocab_hz", "ring_buffer_s"):
            _number(self, name, 0.0, low_open=True, section=section)
        _number(self, "reference_view_dedupe_cos", 0.0, 1.0, section=section)
        _count(self, "render_width_px", 640, section=section)
        for name in (
            "reference_views",
            "reference_view_min_width_px",
            "reference_view_min_height_px",
            "onnx_intra_op_num_threads",
            "onnx_inter_op_num_threads",
        ):
            _count(self, name, section=section)
        if not isinstance(self.vocabulary, Sequence) or isinstance(self.vocabulary, (str, bytes)):
            raise ValueError("perception.vocabulary must be a list of terms")
        object.__setattr__(self, "vocabulary", list(self.vocabulary))

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
    # Claude's agentic tool-call budget per stream-json turn, not per mission.
    max_turns: int = 40
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
class MapSettings:
    # none (no third-party map), gta (common-schema JSON at data_path) or osm (an Overpass JSON
    # extract at data_path, converted at start-up); see horus/maps.
    provider: str = "none"
    data_path: str | None = None
    # osm: the latitude/longitude of local NED (0, 0), which must be the vehicle's EKF origin.
    # None takes flight.global_origin_latitude_deg/longitude_deg (SITL's home).
    origin_latitude_deg: float | None = None
    origin_longitude_deg: float | None = None
    # Optional common-schema JSON whose walkable layer is added to data_path's (GTA's ped navmesh,
    # scripts/maps/fetch_gta_walkable.py).
    walkable_path: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in {"none", "gta", "osm"}:
            raise ValueError("map provider must be 'none', 'gta' or 'osm'")
        if (self.origin_latitude_deg is None) != (self.origin_longitude_deg is None):
            raise ValueError("map origin_latitude_deg and origin_longitude_deg go together")
        if self.provider != "none" and (not isinstance(self.data_path, str) or not self.data_path.strip()):
            raise ValueError(f"map provider {self.provider!r} needs a data_path")
        if self.walkable_path is not None and self.provider == "none":
            raise ValueError("map walkable_path needs a provider")


@dataclass(frozen=True, slots=True)
class HorusConfig:
    flight: FlightSettings = FlightSettings()
    gimbal: GimbalSettings = GimbalSettings()
    executor: ExecutorSettings = ExecutorSettings()
    perception: PerceptionSettings = PerceptionSettings()
    server: ServerSettings = ServerSettings()
    commander: CommanderSettings = CommanderSettings()
    map: MapSettings = MapSettings()


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
    allowed = {"flight", "gimbal", "executor", "perception", "server", "commander", "map"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown configuration section(s): {', '.join(sorted(unknown))}")
    return HorusConfig(
        flight=_section(data, "flight", FlightSettings),
        gimbal=_section(data, "gimbal", GimbalSettings),
        executor=_section(data, "executor", ExecutorSettings),
        perception=_section(data, "perception", PerceptionSettings),
        server=_section(data, "server", ServerSettings),
        commander=_section(data, "commander", CommanderSettings),
        map=_section(data, "map", MapSettings),
    )
