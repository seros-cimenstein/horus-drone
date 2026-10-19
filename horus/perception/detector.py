"""Lazy YOLOv8/ONNX candidate proposal detector."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .geometry import iou
from .onnx_session import load_onnx_session
from .types import Detection


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Configuration for the CPU YOLO ONNX proposal model."""

    model_name: str = "models/yolov8n.onnx"
    confidence: float = 0.25
    nms_iou: float = 0.45
    input_size: int = 640
    has_objectness: bool | None = None
    max_detections: int = 300
    class_names: tuple[str, ...] | None = None
    onnx_intra_op_num_threads: int = 2
    onnx_inter_op_num_threads: int = 1

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if not 0.0 <= self.nms_iou <= 1.0:
            raise ValueError("nms_iou must be in [0, 1]")
        if self.input_size < 32 or self.max_detections < 1:
            raise ValueError("input_size and max_detections must be positive")


class YOLODetector:
    """Run YOLO ONNX inference on the CPU, loading the model on first use."""

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self.config = config or DetectorConfig()
        self._model: Any = None
        self._input_name: str | None = None

    @property
    def model_id(self) -> str:
        """Configured model path, useful for status reporting."""
        return self.config.model_name

    def _load(self) -> None:
        if self._model is not None:
            return
        model_path = Path(self.model_id)
        if model_path.suffix.lower() != ".onnx":
            raise ValueError(f"YOLODetector requires an ONNX model file, got {str(model_path)!r}")
        if not model_path.is_file():
            raise FileNotFoundError(f"configured YOLO ONNX model does not exist: {model_path}")
        self._model, self._input_name = load_onnx_session(
            str(model_path),
            purpose="YOLO",
            intra_op_num_threads=self.config.onnx_intra_op_num_threads,
            inter_op_num_threads=self.config.onnx_inter_op_num_threads,
        )

    def detect(self, image: Any) -> list[Detection]:
        """Run inference on one OpenCV BGR image and return source-pixel boxes."""
        self._load()
        return self._detect_onnx(image)

    def _detect_onnx(self, image: Any) -> list[Detection]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("OpenCV and NumPy are required for YOLO ONNX preprocessing") from exc
        if image is None or len(image.shape) < 2:
            raise ValueError("image must be a non-empty HxW[xC] array")
        height, width = int(image.shape[0]), int(image.shape[1])
        size = self.config.input_size
        scale = min(size / width, size / height)
        resized_width = max(1, int(round(width * scale)))
        resized_height = max(1, int(round(height * scale)))
        resized = cv2.resize(
            image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR
        )
        pad_left = (size - resized_width) // 2
        pad_top = (size - resized_height) // 2
        canvas = np.full((size, size, 3), 114, dtype=image.dtype)
        canvas[
            pad_top : pad_top + resized_height,
            pad_left : pad_left + resized_width,
        ] = resized
        blob = cv2.dnn.blobFromImage(
            canvas, 1.0 / 255.0, (size, size), swapRB=True, crop=False
        )
        outputs = self._model.run(None, {self._input_name: blob})
        if not outputs:
            raise ValueError("YOLO ONNX model returned no outputs")
        rows = np.asarray(outputs[0])
        if rows.ndim == 3 and rows.shape[0] == 1:
            rows = rows[0]
        if rows.ndim != 2:
            raise ValueError(f"unsupported YOLO ONNX output shape: {np.asarray(outputs[0]).shape}")

        class_names = self.config.class_names
        # Any 80-class YOLO export (v8: 4 box + 80 scores; v5: + objectness) is a COCO model
        # whatever its file name; a custom 80-class model must set class_names explicitly.
        coco_widths = (4 + len(_COCO_CLASSES), 5 + len(_COCO_CLASSES))
        if class_names is None and any(dim in coco_widths for dim in rows.shape):
            class_names = _COCO_CLASSES
        has_objectness = self.config.has_objectness
        if has_objectness is None:
            objectness_features = 5 + len(class_names) if class_names is not None else 85
            has_objectness = objectness_features in rows.shape
        class_offset = 5 if has_objectness else 4
        feature_count = class_offset + len(class_names) if class_names is not None else None
        if feature_count is not None and rows.shape[0] == feature_count and rows.shape[1] != feature_count:
            rows = rows.T
        elif feature_count is None and rows.shape[0] < rows.shape[1]:
            rows = rows.T
        if feature_count is not None and rows.shape[1] != feature_count:
            raise ValueError(f"unsupported YOLO ONNX feature count: {rows.shape[1]}")
        if rows.shape[1] <= class_offset:
            raise ValueError(f"unsupported YOLO ONNX feature count: {rows.shape[1]}")

        proposals: list[tuple[tuple[float, float, float, float], float, int]] = []
        for row in rows:
            class_scores = row[class_offset:]
            class_id = int(np.argmax(class_scores))
            score = float(class_scores[class_id])
            if has_objectness:
                score *= float(row[4])
            if score < self.config.confidence:
                continue
            center_x, center_y, box_w, box_h = (float(value) for value in row[:4])
            x1 = max(0.0, (center_x - box_w / 2.0 - pad_left) / scale)
            y1 = max(0.0, (center_y - box_h / 2.0 - pad_top) / scale)
            x2 = min(float(width), (center_x + box_w / 2.0 - pad_left) / scale)
            y2 = min(float(height), (center_y + box_h / 2.0 - pad_top) / scale)
            if x2 > x1 and y2 > y1:
                proposals.append(((x1, y1, x2, y2), score, class_id))
        proposals.sort(key=lambda item: item[1], reverse=True)
        kept: list[tuple[tuple[float, float, float, float], float, int]] = []
        for proposal in proposals:
            box, _, class_id = proposal
            if any(
                class_id == kept_id and iou(box, kept_box) > self.config.nms_iou
                for kept_box, _, kept_id in kept
            ):
                continue
            kept.append(proposal)
            if len(kept) >= self.config.max_detections:
                break
        return [
            Detection(
                bbox=box,
                confidence=score,
                class_name=(
                    class_names[class_id]
                    if class_names is not None and class_id < len(class_names)
                    else f"class_{class_id}"
                ),
                class_id=class_id,
            )
            for box, score, class_id in kept
        ]


_COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
)
