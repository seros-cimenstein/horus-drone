"""Construct the lazy perception stack from shared HORUS settings."""

from __future__ import annotations

import math
from pathlib import Path
import time
from typing import Any

from horus.config import PerceptionSettings
from horus.flight import MavlinkFlightLink
from .camera import CameraConfig, OpenCVCameraReader
from .detector import DetectorConfig, YOLODetector
from .grounding import FrameBuffer, GrounderConfig
from .pipeline import PerceptionSystem
from .tracker import (
    ColorHistogramEmbedder,
    ONNXReIDEmbedder,
    PersistentTracker,
    TrackerConfig,
)


# Lost tracks keep their ID this long, independent of how long frames stay buffered for point().
_MAX_LOST_TRACK_S = 10.0


def create_perception_system(
    settings: PerceptionSettings,
    flight: MavlinkFlightLink,
    *,
    event_bus: Any = None,
) -> PerceptionSystem:
    """Build the camera, detector, tracker, latency buffer, and external grounder client."""
    if not math.isfinite(settings.max_fps) or settings.max_fps <= 0:
        raise ValueError("perception.max_fps must be a finite positive number")
    if not math.isfinite(settings.open_vocab_hz) or settings.open_vocab_hz <= 0:
        raise ValueError("perception.open_vocab_hz must be a finite positive number")
    if not math.isfinite(settings.ring_buffer_s) or settings.ring_buffer_s <= 0:
        raise ValueError("perception.ring_buffer_s must be a finite positive number")
    if settings.render_width_px < 1 or settings.render_width_px > 640:
        raise ValueError("perception.render_width_px must be between 1 and 640")

    def current_pose(_: float) -> Any:
        return camera_pose_from_telemetry(flight.telemetry())

    camera = OpenCVCameraReader(
        CameraConfig(source=settings.source, fps_cap=settings.max_fps),
        pose_provider=current_pose,
    )
    detector = YOLODetector(
        DetectorConfig(
            model_name=settings.detector_model,
            onnx_intra_op_num_threads=settings.onnx_intra_op_num_threads,
            onnx_inter_op_num_threads=settings.onnx_inter_op_num_threads,
        )
    )
    embedder = _create_embedder(
        settings.embedding_model,
        onnx_intra_op_num_threads=settings.onnx_intra_op_num_threads,
        onnx_inter_op_num_threads=settings.onnx_inter_op_num_threads,
    )
    tracker = PersistentTracker(
        TrackerConfig(
            max_lost_seconds=max(2.5, min(settings.ring_buffer_s, _MAX_LOST_TRACK_S)),
            revalidation_grace_s=float(settings.revalidation_grace_s),
        ),
        embedder=embedder,
    )
    buffer = FrameBuffer(
        max_seconds=settings.ring_buffer_s,
        max_frames=max(1, math.ceil(settings.max_fps * settings.ring_buffer_s)),
    )
    grounder_config = GrounderConfig()
    grounder = None
    if settings.grounder_url:
        from horus.grounder.client import GrounderClient

        grounder = GrounderClient(settings.grounder_url)
    return PerceptionSystem(
        detector,
        tracker,
        camera=camera,
        buffer=buffer,
        grounder=grounder,
        grounder_config=grounder_config,
        open_vocab_hz=settings.open_vocab_hz,
        vocabulary=settings.vocabulary,
        event_bus=event_bus,
        ignore_regions=settings.ignore_regions,
    )


def _create_embedder(
    model_name: str,
    *,
    onnx_intra_op_num_threads: int = 2,
    onnx_inter_op_num_threads: int = 1,
):
    if model_name == "color_histogram":
        return ColorHistogramEmbedder()
    path = Path(model_name)
    if path.suffix.lower() == ".onnx":
        if not path.is_file():
            raise FileNotFoundError(f"configured ReID ONNX model does not exist: {path}")
        return ONNXReIDEmbedder(
            str(path),
            onnx_intra_op_num_threads=onnx_intra_op_num_threads,
            onnx_inter_op_num_threads=onnx_inter_op_num_threads,
        )
    raise ValueError(
        "perception.embedding_model must be 'color_histogram' or the path to a ReID .onnx model"
    )


def camera_pose_from_telemetry(telemetry: Any, now: float | None = None) -> dict[str, Any] | None:
    """Frame pose at capture: position, heading, vehicle roll/pitch and gimbal attitude with ages.

    Every group carries the age of its own latest MAVLink sample (LOCAL_POSITION_NED/GPS,
    heading, ATTITUDE, MOUNT_STATUS) so scene monitoring rejects stale camera state instead of
    assuming it held still. Groups never received are omitted, i.e. reported unmeasured.
    """
    monotonic = time.monotonic() if now is None else now

    def age(updated: float | None) -> float | None:
        return None if updated is None else max(0.0, monotonic - updated)

    result: dict[str, Any] = {}
    pose = telemetry.pose
    if pose is not None:
        result.update(pose.as_dict())
        result["pose_age_s"] = age(telemetry.pose_updated_monotonic)
    if telemetry.heading_deg is not None:
        result["heading_deg"] = telemetry.heading_deg
        result["heading_age_s"] = age(telemetry.heading_updated_monotonic)
    if telemetry.vehicle_roll_deg is not None and telemetry.vehicle_pitch_deg is not None:
        result["vehicle_roll_deg"] = telemetry.vehicle_roll_deg
        result["vehicle_pitch_deg"] = telemetry.vehicle_pitch_deg
        result["attitude_age_s"] = age(telemetry.attitude_updated_monotonic)
    mount = telemetry.mount_status
    if mount is not None:
        result["gimbal_pitch_deg"] = mount.pitch_deg
        result["gimbal_roll_deg"] = mount.roll_deg
        result["gimbal_yaw_deg"] = mount.yaw_deg
        result["gimbal_age_s"] = age(mount.updated_monotonic)
    if telemetry.time_boot_ms is not None:
        # Autopilot (in SITL: simulation) clock: capture ≈ time_boot_ms + time_boot_age_s, the
        # age being host monotonic time, so the estimate drifts by age·|1 − real-time factor|.
        result["time_boot_ms"] = telemetry.time_boot_ms
        result["time_boot_age_s"] = age(telemetry.time_boot_updated_monotonic)
    return result or None


