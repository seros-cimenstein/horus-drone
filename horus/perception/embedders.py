"""Appearance embedders for re-identification and the unit-vector helpers they share."""

from __future__ import annotations

import math
from typing import Any, Protocol, Sequence

from .geometry import clamp_box
from .onnx_session import load_onnx_session
from .types import BBox, Embedding


class Embedder(Protocol):
    """Appearance encoder contract; ``PersistentTracker.encode`` normalises whatever it returns."""

    def embed(self, image: Any, bbox: BBox) -> Sequence[float] | None: ...


def normalize_embedding(values: Any) -> Embedding | None:
    """Unit-length copy of a finite numeric vector, or ``None`` when its norm is at most 1e-12."""
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError):
        return None
    if not vector or not all(math.isfinite(value) for value in vector):
        return None
    norm = math.sqrt(sum(value * value for value in vector))
    if math.isinf(norm):
        # The squares overflowed: scale by the largest magnitude first.
        scale = max(abs(value) for value in vector)
        vector = tuple(value / scale for value in vector)
        norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        return None
    return tuple(value / norm for value in vector)


def cosine_similarity(left: Embedding | None, right: Embedding | None) -> float | None:
    """Cosine similarity of two ``normalize_embedding`` vectors, i.e. their dot product."""
    if left is None or right is None or len(left) != len(right) or not left:
        return None
    return sum(a * b for a, b in zip(left, right))


class ColorHistogramEmbedder:
    """Dependency-lazy HSV histogram embedding useful as a no-training ReID baseline."""

    def embed(self, image: Any, bbox: BBox) -> Embedding | None:
        """Return the crop's hue/saturation histogram; missing OpenCV/NumPy gives no embedding."""
        if image is None:
            return None
        try:
            import cv2
            import numpy as np
        except ImportError:
            return None
        x1, y1, x2, y2 = clamp_box(bbox, int(image.shape[1]), int(image.shape[0]))
        if x2 <= x1 or y2 <= y1:
            return None
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
        return tuple(float(value) for value in np.asarray(hist, dtype=np.float32).reshape(-1))


class ONNXReIDEmbedder:
    """Run a person-ReID ONNX model with ONNX Runtime's CPU provider."""

    def __init__(
        self,
        model_path: str,
        *,
        input_size: tuple[int, int] = (128, 256),
        onnx_intra_op_num_threads: int = 2,
        onnx_inter_op_num_threads: int = 1,
    ) -> None:
        self.model_path = model_path
        self.input_size = input_size
        self.onnx_intra_op_num_threads = onnx_intra_op_num_threads
        self.onnx_inter_op_num_threads = onnx_inter_op_num_threads
        self._session: Any = None
        self._input_name: str | None = None

    def _load(self) -> None:
        if self._session is None:
            self._session, self._input_name = load_onnx_session(
                self.model_path,
                purpose="ReID",
                intra_op_num_threads=self.onnx_intra_op_num_threads,
                inter_op_num_threads=self.onnx_inter_op_num_threads,
            )

    def embed(self, image: Any, bbox: BBox) -> Embedding | None:
        """Crop a person box, apply Torchreid ImageNet preprocessing, and return the raw feature."""
        if image is None:
            return None
        self._load()
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("OpenCV and NumPy are required to preprocess ReID crops") from exc

        x1, y1, x2, y2 = clamp_box(bbox, int(image.shape[1]), int(image.shape[0]))
        if x2 <= x1 or y2 <= y1:
            return None
        crop = image[y1:y2, x1:x2]
        resized = cv2.resize(crop, self.input_size, interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        values = np.asarray(rgb, dtype=np.float32) / 255.0
        values -= np.asarray((0.485, 0.456, 0.406), dtype=np.float32)
        values /= np.asarray((0.229, 0.224, 0.225), dtype=np.float32)
        tensor = np.transpose(values, (2, 0, 1))[None, ...]
        outputs = self._session.run(None, {self._input_name: tensor})
        if not outputs:
            raise RuntimeError("ReID ONNX model returned no feature output")
        vector = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        if vector.size == 0 or not np.isfinite(vector).all():
            return None
        return tuple(float(value) for value in vector)
