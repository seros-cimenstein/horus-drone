"""AprilTag-guided precision landing primitives for nadir-facing cameras."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True, slots=True)
class AprilTagDetection:
    """Pixel-space center of a detected AprilTag in its source frame."""

    center_x: float
    center_y: float
    image_width: int
    image_height: int


class AprilTag36h11Detector:
    """Lazily construct an OpenCV detector for AprilTag 36h11 marker id 0."""

    def __init__(self) -> None:
        self._cv2: Any | None = None
        self._dictionary: Any | None = None
        self._detector: Any | None = None
        self._parameters: Any | None = None

    def _initialize(self) -> None:
        if self._cv2 is not None:
            return
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("OpenCV with the aruco module is required for precision landing") from exc
        aruco = getattr(cv2, "aruco", None)
        if aruco is None:
            raise RuntimeError("OpenCV was built without the aruco module")
        dictionary_id = getattr(aruco, "DICT_APRILTAG_36h11", None)
        if dictionary_id is None:
            raise RuntimeError("OpenCV aruco does not provide DICT_APRILTAG_36h11")
        get_dictionary = getattr(aruco, "getPredefinedDictionary", None)
        if not callable(get_dictionary):
            get_dictionary = getattr(aruco, "Dictionary_get", None)
        if not callable(get_dictionary):
            raise RuntimeError("OpenCV aruco does not provide predefined dictionaries")
        dictionary = get_dictionary(dictionary_id)
        parameters_factory = getattr(aruco, "DetectorParameters_create", None)
        if not callable(parameters_factory):
            parameters_factory = getattr(aruco, "DetectorParameters", None)
        parameters = parameters_factory() if callable(parameters_factory) else None
        detector_factory = getattr(aruco, "ArucoDetector", None)
        detector = detector_factory(dictionary, parameters) if callable(detector_factory) else None
        self._cv2 = cv2
        self._dictionary = dictionary
        self._parameters = parameters
        self._detector = detector

    def detect(self, image: Any) -> AprilTagDetection | None:
        """Find marker id 0 without copying already-grayscale input images."""
        shape = getattr(image, "shape", None)
        if shape is None or len(shape) not in (2, 3):
            raise ValueError("AprilTag input must be a grayscale or color image array")
        height, width = int(shape[0]), int(shape[1])
        if height <= 0 or width <= 0:
            return None
        self._initialize()
        cv2 = self._cv2
        if len(shape) == 2:
            grayscale = image
        else:
            channels = int(shape[2])
            if channels == 1:
                grayscale = image[:, :, 0]
            elif channels == 3:
                grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            elif channels == 4:
                grayscale = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
            else:
                raise ValueError("AprilTag color input must have 1, 3, or 4 channels")
        if self._detector is not None:
            corners, identifiers, _ = self._detector.detectMarkers(grayscale)
        else:
            detect_markers = getattr(cv2.aruco, "detectMarkers", None)
            if not callable(detect_markers):
                raise RuntimeError("OpenCV aruco does not provide marker detection")
            kwargs = {} if self._parameters is None else {"parameters": self._parameters}
            corners, identifiers, _ = detect_markers(
                grayscale, self._dictionary, **kwargs
            )
        if identifiers is None:
            return None
        for corner_set, marker_id in zip(corners, identifiers):
            if int(marker_id[0]) != 0:
                continue
            points = corner_set.reshape(4, 2)
            center_x = sum(float(points[index, 0]) for index in range(4)) * 0.25
            center_y = sum(float(points[index, 1]) for index in range(4)) * 0.25
            return AprilTagDetection(center_x, center_y, width, height)
        return None


class AprilTagPrecisionLandingController:
    """Combine id-0 image detection with deterministic bounded NED setpoint math."""

    def __init__(self, detector: AprilTag36h11Detector | None = None) -> None:
        self.detector = detector if detector is not None else AprilTag36h11Detector()

    def observe(
        self,
        image: Any,
        heading_deg: float,
        *,
        max_horizontal_speed_m_s: float = 0.35,
        descent_speed_m_s: float = 0.15,
        deadband: float = 0.05,
    ) -> tuple[AprilTagDetection | None, tuple[float, float, float]]:
        """Return the id-0 detection and a (north, east, down) velocity setpoint."""
        detection = self.detector.detect(image)
        if detection is None:
            return None, (0.0, 0.0, 0.0)
        velocity = centering_velocity_ned(
            detection.center_x,
            detection.center_y,
            detection.image_width,
            detection.image_height,
            heading_deg,
            max_horizontal_speed_m_s=max_horizontal_speed_m_s,
            descent_speed_m_s=descent_speed_m_s,
            deadband=deadband,
        )
        return detection, velocity


def centering_velocity_ned(
    center_x: float,
    center_y: float,
    image_width: int,
    image_height: int,
    heading_deg: float,
    *,
    max_horizontal_speed_m_s: float = 0.35,
    descent_speed_m_s: float = 0.15,
    deadband: float = 0.05,
    descent_cone: float = 0.15,
) -> tuple[float, float, float]:
    """Map tag-center error to bounded NED velocity; descend while the tag is near center.

    Horizontal correction is proportional to the normalized image error (full speed at a
    half-frame offset) and is zeroed per axis only inside ``deadband``, so small residual
    offsets still produce a usable correction instead of stalling at the deadband edge.
    Descent runs while both axis errors are within ``descent_cone``, centering and descending
    together. Image-right maps to vehicle-right and image-up to vehicle-forward for a
    nadir-facing camera; vehicle-relative commands are rotated by heading into NED.
    Positive down velocity is MAVLink's descent direction.
    """
    values = (
        center_x, center_y, heading_deg, max_horizontal_speed_m_s, descent_speed_m_s, deadband, descent_cone,
    )
    try:
        (
            center_x, center_y, heading_deg, max_horizontal_speed_m_s, descent_speed_m_s, deadband, descent_cone,
        ) = (float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("centering inputs must be finite numbers") from exc
    if not all(
        math.isfinite(value)
        for value in (
            center_x, center_y, heading_deg, max_horizontal_speed_m_s, descent_speed_m_s, deadband, descent_cone,
        )
    ):
        raise ValueError("centering inputs must be finite numbers")
    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")
    if max_horizontal_speed_m_s <= 0 or descent_speed_m_s < 0:
        raise ValueError("velocity limits must be positive, with nonnegative descent speed")
    if not 0.0 <= deadband < 1.0 or not deadband <= descent_cone <= 1.0:
        raise ValueError("deadband must be in [0, 1) and no larger than descent_cone <= 1")

    error_right = (center_x - image_width * 0.5) / (image_width * 0.5)
    error_down = (center_y - image_height * 0.5) / (image_height * 0.5)
    down = descent_speed_m_s if abs(error_right) <= descent_cone and abs(error_down) <= descent_cone else 0.0
    if abs(error_right) <= deadband:
        error_right = 0.0
    if abs(error_down) <= deadband:
        error_down = 0.0

    gain = max_horizontal_speed_m_s / 0.5
    forward = -error_down * gain
    right = error_right * gain
    heading = math.radians(heading_deg)
    north = forward * math.cos(heading) - right * math.sin(heading)
    east = forward * math.sin(heading) + right * math.cos(heading)
    speed = math.hypot(north, east)
    if speed > max_horizontal_speed_m_s:
        scale = max_horizontal_speed_m_s / speed
        north *= scale
        east *= scale
    return north + 0.0, east + 0.0, down
