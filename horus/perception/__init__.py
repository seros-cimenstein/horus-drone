"""HORUS perception components with optional vision/model dependencies loaded on demand."""

from .camera import CameraConfig, OpenCVCameraReader
from .detector import DetectorConfig, YOLODetector
from .grounding import (
    FrameBuffer,
    Grounder,
    GrounderConfig,
    GroundingPointer,
)
from .recognition import TinyEmbeddingClassifier
from .pipeline import PerceptionEngine, PerceptionSystem
from .render import render_set_of_marks
from .tracker import (
    ColorHistogramEmbedder,
    Embedder,
    ONNXReIDEmbedder,
    PersistentTracker,
    TrackerConfig,
    box_iou,
    cosine_similarity,
)
from .types import (
    BBox,
    Candidate,
    Detection,
    Embedding,
    Frame,
    FrameObservation,
    GroundingBox,
    NoGroundingMatch,
    PointResult,
    SetOfMarkPayload,
    StaleFrameError,
)
from .factory import create_perception_system

__all__ = [
    "BBox",
    "CameraConfig",
    "Candidate",
    "ColorHistogramEmbedder",
    "Detection",
    "DetectorConfig",
    "Embedder",
    "Embedding",
    "Frame",
    "FrameBuffer",
    "FrameObservation",
    "Grounder",
    "GrounderConfig",
    "GroundingBox",
    "GroundingPointer",
    "NoGroundingMatch",
    "ONNXReIDEmbedder",
    "OpenCVCameraReader",
    "PerceptionEngine",
    "PerceptionSystem",
    "PersistentTracker",
    "PointResult",
    "SetOfMarkPayload",
    "StaleFrameError",
    "TrackerConfig",
    "TinyEmbeddingClassifier",
    "YOLODetector",
    "box_iou",
    "cosine_similarity",
    "create_perception_system",
    "render_set_of_marks",
]
