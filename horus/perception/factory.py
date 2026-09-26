"""Construct the lazy perception stack from shared HORUS settings."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from horus.config import PerceptionSettings
from horus.flight import MavlinkFlightLink
from .camera import CameraConfig, OpenCVCameraReader
from .detector import DetectorConfig, YOLODetector
from .grounding import FrameBuffer, GrounderConfig, TransformersGrounder
from .pipeline import PerceptionSystem
from .tracker import (
    ColorHistogramEmbedder,
    ONNXReIDEmbedder,
    PersistentTracker,
    TrackerConfig,
)


def create_perception_system(
    settings: PerceptionSettings,
    flight: MavlinkFlightLink,
) -> PerceptionSystem:
    """Build camera, detector, ReID tracker, latency buffer, and lazy grounding fallback."""
    if not math.isfinite(settings.max_fps) or settings.max_fps <= 0:
        raise ValueError("perception.max_fps must be a finite positive number")
    if not math.isfinite(settings.ring_buffer_s) or settings.ring_buffer_s <= 0:
        raise ValueError("perception.ring_buffer_s must be a finite positive number")
    if settings.render_width_px < 1 or settings.render_width_px > 640:
        raise ValueError("perception.render_width_px must be between 1 and 640")

    def current_pose(_: float) -> Any:
        telemetry = flight.telemetry()
        pose = telemetry.pose
        return pose.as_dict() if pose is not None else None

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
        TrackerConfig(max_lost_seconds=max(2.5, settings.ring_buffer_s)),
        embedder=embedder,
    )
    buffer = FrameBuffer(
        max_seconds=settings.ring_buffer_s,
        max_frames=max(1, math.ceil(settings.max_fps * settings.ring_buffer_s)),
    )
    backend, model_name = _grounder_model(settings.grounding_model)
    grounder_config = GrounderConfig(backend=backend, model_name=model_name)
    grounder = TransformersGrounder(grounder_config)
    return PerceptionSystem(
        detector,
        tracker,
        camera=camera,
        buffer=buffer,
        grounder=grounder,
        grounder_config=grounder_config,
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


def _grounder_model(model: str) -> tuple[str, str]:
    if model.startswith("owlv2:"):
        name = model.partition(":")[2]
        if not name:
            raise ValueError("owlv2 grounding model name cannot be empty")
        return "owlv2", name
    if model.startswith("grounding-dino:"):
        name = model.partition(":")[2]
        if not name:
            raise ValueError("grounding-dino model name cannot be empty")
        return "grounding-dino", name
    return "grounding-dino", model
