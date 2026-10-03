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
class ExecutorSettings:
    control_hz: float = 15.0
    command_ttl_s: float = 30.0
    max_speed_m_s: float = 1.0
    # Altitude-gated cruise speed for open transit legs: the horizontal speed limit is
    # ``cruise_speed_m_s`` while both the current altitude above home and the commanded target
    # altitude are at/above ``cruise_min_altitude_m``, and ``max_speed_m_s`` anywhere below it.
    # None (or 0) disables the gate and keeps every leg at ``max_speed_m_s``.
    cruise_speed_m_s: float | None = None
    cruise_min_altitude_m: float | None = None
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
    # Known person height (m) for monocular range from the detection box height; applied only to
    # person targets (person label, shirt-colour attribute, or remembered person label). People ~1.7 m.
    target_height_m: float | None = None
    target_match_threshold: float = 0.72
    camera_horizontal_fov_deg: float = 114.59155902616465
    default_gimbal_pitch_deg: float = 0.0
    # Ground-plane monocular range: the ray through the box's ground-contact row is intersected
    # with the ground plane. Below this ray depression a small camera-pitch error dominates the
    # intersection, so the estimate is refused instead of returned (10 deg keeps it bounded).
    min_ground_range_depression_deg: float = 10.0
    # The payload's forward lidars (sim/models/horus_sensor_payload/model.sdf) are single-ray
    # beams mounted level with the airframe: zero vertical field of view, so they only measure
    # what sits on the vehicle's horizontal plane at flight altitude. A target whose ray leaves
    # that plane by more than this half-angle (0 deg plus a margin for attitude jitter) must not
    # use the forward ToF: outdoors at -45 deg gimbal pitch the beam flies over the person and
    # returns whatever is behind it.
    forward_tof_vertical_half_fov_deg: float = 5.0
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
        try:
            margin = float(self.geofence_margin_m)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("geofence_margin_m must be a finite non-negative number") from exc
        if not math.isfinite(margin) or margin < 0.0:
            raise ValueError("geofence_margin_m must be a finite non-negative number")
        object.__setattr__(self, "geofence_margin_m", margin)
        pitch_value = self.default_gimbal_pitch_deg
        try:
            pitch = float(pitch_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "default_gimbal_pitch_deg must be between -90 and 45 degrees"
            ) from exc
        if (
            isinstance(pitch_value, bool)
            or not math.isfinite(pitch)
            or not -90.0 <= pitch <= 45.0
        ):
            raise ValueError(
                "default_gimbal_pitch_deg must be between -90 and 45 degrees"
            )
        object.__setattr__(self, "default_gimbal_pitch_deg", pitch)
        depression_value = self.min_ground_range_depression_deg
        try:
            depression = float(depression_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "min_ground_range_depression_deg must be between 0 and 90 degrees"
            ) from exc
        if (
            isinstance(depression_value, bool)
            or not math.isfinite(depression)
            or not 0.0 < depression < 90.0
        ):
            raise ValueError("min_ground_range_depression_deg must be between 0 and 90 degrees")
        object.__setattr__(self, "min_ground_range_depression_deg", depression)
        beam_value = self.forward_tof_vertical_half_fov_deg
        try:
            beam = float(beam_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "forward_tof_vertical_half_fov_deg must be between 0 and 45 degrees"
            ) from exc
        if (
            isinstance(beam_value, bool)
            or not math.isfinite(beam)
            or not 0.0 <= beam <= 45.0
        ):
            raise ValueError("forward_tof_vertical_half_fov_deg must be between 0 and 45 degrees")
        object.__setattr__(self, "forward_tof_vertical_half_fov_deg", beam)

        cruise_value = self.cruise_speed_m_s
        if cruise_value is not None and (
            isinstance(cruise_value, bool)
            or not isinstance(cruise_value, (int, float))
            or not math.isfinite(cruise_value)
            or cruise_value < 0.0
        ):
            raise ValueError("cruise_speed_m_s must be a finite non-negative number or null")
        cruise = None if cruise_value in (None, 0, 0.0) else float(cruise_value)
        if cruise is None:
            if self.cruise_min_altitude_m is not None:
                raise ValueError("cruise_min_altitude_m requires cruise_speed_m_s")
        else:
            if cruise <= self.max_speed_m_s:
                raise ValueError("cruise_speed_m_s must exceed max_speed_m_s")
            gate_value = self.cruise_min_altitude_m
            if (
                gate_value is None
                or isinstance(gate_value, bool)
                or not isinstance(gate_value, (int, float))
                or not math.isfinite(gate_value)
            ):
                raise ValueError("cruise_speed_m_s requires a finite cruise_min_altitude_m")
            gate = float(gate_value)
            if not self.min_altitude_m <= gate <= self.max_altitude_m:
                raise ValueError(
                    "cruise_min_altitude_m must lie within [min_altitude_m, max_altitude_m]"
                )
            object.__setattr__(self, "cruise_min_altitude_m", gate)
        object.__setattr__(self, "cruise_speed_m_s", cruise)

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
    grounder_url: str | None = "http://127.0.0.1:8100"
    max_fps: float = 5.0
    open_vocab_hz: float = 0.5
    vocabulary: list[str] = field(default_factory=list)
    onnx_intra_op_num_threads: int = 2
    onnx_inter_op_num_threads: int = 1
    ring_buffer_s: float = 5.0
    render_width_px: int = 640
    # Keep a live track's identity through a brief appearance-score dip (see TrackerConfig);
    # 0 disables the grace and requires every validated frame to pass the threshold.
    revalidation_grace_s: float = 1.0
    # Multi-view references (R1): a live, continuously associated track stores up to
    # ``reference_views`` confirmed views in total and validates against the best of them, so an
    # appearance dip does not lose a correctly tracked target. Admission is anchored on the
    # creation view at the full threshold and gated by class, shirt colour and crop size.
    # Re-acquisition after a loss stays creation-view-only with the full threshold and gates.
    reference_views: int = 4
    reference_view_min_width_px: int = 12
    reference_view_min_height_px: int = 30
    reference_view_dedupe_cos: float = 0.98
    # Re-acquisition must pass on two distinct processed frames within this many seconds
    # (0 disables the confirmation).
    reacquisition_confirm_s: float = 1.0
    # The published shirt colour is a bounded vote over the track's recent accepted
    # classifications, so one misread frame cannot label a track: a colour needs at least
    # ``shirt_colour_min_votes`` votes within the last ``shirt_colour_window`` and strictly
    # more votes than any other colour, and an undecided track publishes no colour.
    shirt_colour_window: int = 5
    shirt_colour_min_votes: int = 3
    ignore_regions: tuple[tuple[float, float, float, float], ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.open_vocab_hz, bool)
            or not isinstance(self.open_vocab_hz, (int, float))
            or not math.isfinite(self.open_vocab_hz)
            or self.open_vocab_hz <= 0
        ):
            raise ValueError("perception.open_vocab_hz must be a finite positive number")
        if (
            isinstance(self.revalidation_grace_s, bool)
            or not isinstance(self.revalidation_grace_s, (int, float))
            or not math.isfinite(self.revalidation_grace_s)
            or self.revalidation_grace_s < 0
        ):
            raise ValueError("perception.revalidation_grace_s must be a finite non-negative number")
        if isinstance(self.reference_views, bool) or not isinstance(self.reference_views, int) or self.reference_views < 1:
            raise ValueError("perception.reference_views must be a positive integer")
        for name in ("reference_view_min_width_px", "reference_view_min_height_px"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"perception.{name} must be a positive integer")
        if (
            isinstance(self.reference_view_dedupe_cos, bool)
            or not isinstance(self.reference_view_dedupe_cos, (int, float))
            or not math.isfinite(self.reference_view_dedupe_cos)
            or not 0.0 <= self.reference_view_dedupe_cos <= 1.0
        ):
            raise ValueError("perception.reference_view_dedupe_cos must be in [0, 1]")
        if (
            isinstance(self.reacquisition_confirm_s, bool)
            or not isinstance(self.reacquisition_confirm_s, (int, float))
            or not math.isfinite(self.reacquisition_confirm_s)
            or self.reacquisition_confirm_s < 0
        ):
            raise ValueError("perception.reacquisition_confirm_s must be a finite non-negative number")
        for name in ("shirt_colour_window", "shirt_colour_min_votes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"perception.{name} must be a positive integer")
        if self.shirt_colour_min_votes > self.shirt_colour_window:
            raise ValueError(
                "perception.shirt_colour_min_votes must not exceed perception.shirt_colour_window"
            )
        if not isinstance(self.vocabulary, Sequence) or isinstance(self.vocabulary, (str, bytes)):
            raise ValueError("perception.vocabulary must be a list of terms")
        object.__setattr__(self, "vocabulary", list(self.vocabulary))

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
