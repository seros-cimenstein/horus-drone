"""HORUS perception components with optional vision/model dependencies loaded on demand."""

from .camera import CameraConfig, OpenCVCameraReader
from .detector import DetectorConfig, YOLODetector
from .embedders import ColorHistogramEmbedder, Embedder, ONNXReIDEmbedder, cosine_similarity
from .frame_buffer import FrameBuffer
from .grounding import (
    Grounder,
    GrounderConfig,
    GroundingPointer,
)
from .recognition import TinyEmbeddingClassifier
from .pipeline import PerceptionSystem
from .render import render_set_of_marks
from .tracker import PersistentTracker
from .tracks import TrackerConfig
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
    "PerceptionSystem",
    "PersistentTracker",
    "PointResult",
    "SetOfMarkPayload",
    "StaleFrameError",
    "TrackerConfig",
    "TinyEmbeddingClassifier",
    "YOLODetector",
    "cosine_similarity",
    "create_perception_system",
    "render_set_of_marks",
]
