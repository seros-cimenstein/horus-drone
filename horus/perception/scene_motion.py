"""Per-track image-motion samples and the least-squares motion fit behind ``candidate_motion``.

Rates are fractions of the processed frame per second (x right, y down). They mix target motion
with camera ego-motion; ``camera_motion`` classifies the measured camera movement over the
samples and never proves a still camera.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
import math
from typing import Any, NamedTuple

from .camera_state import CameraState, camera_deviation, camera_tolerances, unmeasured_camera_components

# Motion history: samples inside this window (ending at a track's newest sample) are kept.
MOTION_WINDOW_S = 2.0
MAX_SAMPLES_PER_TRACK = 32
MIN_MOTION_SAMPLES = 3
MIN_MOTION_SPAN_S = 0.25
# Motion fit confidence halves when the centre fit residual reaches this fraction of the frame.
RESIDUAL_HALF_CONFIDENCE = 0.02


class MotionSample(NamedTuple):
    timestamp: float
    x: float
    y: float
    width: float
    height: float
    camera: CameraState | None
    camera_reason: str | None


class TrackHistory:
    __slots__ = ("samples", "class_name", "last_frame_id", "last_timestamp", "epoch")

    def __init__(self, epoch: int) -> None:
        self.samples: deque[MotionSample] = deque(maxlen=MAX_SAMPLES_PER_TRACK)
        self.class_name = ""
        self.last_frame_id = 0
        self.last_timestamp = 0.0
        self.epoch = epoch


def fit_motion(samples: Sequence[MotionSample], span: float) -> dict[str, Any]:
    """Least-squares rates of centre, size and log-scale over the retained samples."""
    count = len(samples)
    t_mean = sum(sample.timestamp for sample in samples) / count
    variance = sum((sample.timestamp - t_mean) ** 2 for sample in samples)

    def slope(values: Sequence[float]) -> tuple[float, float]:
        mean = sum(values) / count
        rate = sum((sample.timestamp - t_mean) * (value - mean) for sample, value in zip(samples, values))
        return rate / variance, mean

    xs = [sample.x for sample in samples]
    ys = [sample.y for sample in samples]
    vx, mean_x = slope(xs)
    vy, mean_y = slope(ys)
    vw, _ = slope([sample.width for sample in samples])
    vh, _ = slope([sample.height for sample in samples])
    scale_rate, _ = slope(
        [0.5 * math.log(max(sample.width, 1e-6) * max(sample.height, 1e-6)) for sample in samples]
    )
    residual = math.sqrt(
        sum(
            (x - (mean_x + vx * (sample.timestamp - t_mean))) ** 2
            + (y - (mean_y + vy * (sample.timestamp - t_mean))) ** 2
            for sample, x, y in zip(samples, xs, ys)
        )
        / count
    )
    confidence = min(1.0, (count - 1) / 5.0) * min(1.0, span) / (1.0 + residual / RESIDUAL_HALF_CONFIDENCE)
    return {
        "center_velocity_per_s": [round(vx, 4), round(vy, 4)],
        "center_speed_per_s": round(math.hypot(vx, vy), 4),
        "size_velocity_per_s": [round(vw, 4), round(vh, 4)],
        "scale_rate_per_s": round(scale_rate, 4),
        "residual_rms": round(residual, 4),
        "fit_confidence": round(confidence, 3),
    }


def camera_motion(samples: Sequence[MotionSample]) -> dict[str, Any]:
    """Classify measured camera movement over the motion window; it never proves a still camera."""
    for sample in samples:
        if sample.camera is None:
            return {
                "status": "unknown",
                "reason": sample.camera_reason,
                "interpretation": "camera state unavailable for part of the window: the image motion "
                "may be partly or entirely camera ego-motion",
            }
    reference = samples[0].camera
    worst: dict[str, float] = {}
    exceeded: set[str] = set()
    unmeasured: set[str] = set()
    for sample in samples:
        deviation, over = camera_deviation(reference, sample.camera)  # type: ignore[arg-type]
        exceeded.update(over)
        unmeasured.update(unmeasured_camera_components(sample.camera))  # type: ignore[arg-type]
        for name, value in deviation.items():
            worst[name] = max(worst.get(name, 0.0), value)
    common = {"max_deviation": worst, "tolerances": camera_tolerances(), "unmeasured": sorted(unmeasured)}
    if exceeded:
        return {
            "status": "moving",
            "exceeded": sorted(exceeded),
            **common,
            "interpretation": "the camera moved during the window: the image-relative rates include "
            "camera ego-motion and are not the target's own motion",
        }
    return {
        "status": "within_tolerance",
        **common,
        "interpretation": "measured camera components stayed within tolerance; sub-tolerance "
        "ego-motion and any unmeasured components can still move the image, so the rates remain "
        "image-relative approximations of target motion, never a metric speed",
    }
