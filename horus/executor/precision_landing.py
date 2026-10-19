"""AprilTag-guided precision landing for nadir-facing cameras: detection, setpoint math and the
executor-driven descent over the home pad."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from horus.executor.core import HorusExecutor
    from horus.flight import Pose, Telemetry


logger = logging.getLogger(__name__)


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


class PrecisionLanding:
    """Tag-guided GUIDED descent over the home pad while an RTH/LAND emergency is in progress.

    Runs inside the executor's controller tick. Every autopilot command goes through the
    executor's epoch-checked ``_command``, and state an emergency also writes changes only under
    the executor lock after ``_require_current_tick``.
    """

    HOME_RADIUS_M = 3.0
    # Below this altitude the tag overflows the camera view; hand the centred vehicle to vertical LAND.
    HANDOFF_ALTITUDE_M = 0.6
    FRAME_MAX_AGE_S = 1.0
    GIMBAL_SETTLE_S = 0.5

    def __init__(self, executor: HorusExecutor) -> None:
        self._executor = executor
        self.controller = AprilTagPrecisionLandingController()
        # Autopilot mode (RTL/LAND) of the emergency in progress; None when no landing is pending.
        self.fallback_mode: str | None = None
        self.active = False
        # When the camera was turned nadir; it must be restored before any later skill.
        self.gimbal_requested_at: float | None = None
        self.unavailable = False
        self._stall_started_monotonic: float | None = None
        self._stall_start_altitude_m: float | None = None
        self._stall_start_sim_s: float | None = None
        self._target_monotonic: float | None = None
        self._target_velocity: tuple[float, float, float] | None = None
        self._last_log_monotonic = 0.0
        self.stats: dict[str, Any] = {
            "tag_detections": 0, "acquisitions": 0, "completed_landings": 0, "last_fallback": None,
        }

    def reset(self, unavailable: bool, *, release_gimbal: bool = True) -> bool:
        """End precision control; call with the executor lock held.

        Returns whether the camera was turned nadir. ``release_gimbal=False`` keeps that record so
        the next skill still restores the default pitch.
        """
        camera_down = self.gimbal_requested_at is not None
        self.fallback_mode = None
        self.active = False
        if release_gimbal:
            self.gimbal_requested_at = None
        self.unavailable = unavailable
        self._reset_watch()
        return camera_down

    def _reset_watch(self) -> None:
        """Forget the in-progress descent window and tag setpoint."""
        self._stall_started_monotonic = None
        self._stall_start_altitude_m = None
        self._stall_start_sim_s = None
        self._target_monotonic = None
        self._target_velocity = None

    def step(self, mode: str) -> None:
        """One controller tick; ``mode`` is the autopilot mode the emergency commanded."""
        try:
            self._step(mode)
        except Exception as exc:
            logger.warning("precision landing error: %s", exc, exc_info=True)
            self.stats["last_fallback"] = f"error: {exc}"
            if self.active:
                self._controller_failed()
            else:
                # A detector or gimbal failure leaves the existing RTL/LAND mode in control.
                executor = self._executor
                with executor._lock:
                    executor._require_current_tick()
                    self.unavailable = True

    def fall_back_to_land(self, summary: str, *, record: bool = False, **event: Any) -> None:
        """Stop the descent and hand the vehicle to autopilot LAND, which owns its own failsafes.

        ``record`` stores ``summary`` as ``stats["last_fallback"]`` once the hand-off is committed.
        """
        executor = self._executor
        executor._command_zero_velocity()
        executor._command_mode("LAND")
        with executor._lock:
            executor._require_current_tick()
            if record:
                self.stats["last_fallback"] = summary
            self.reset(unavailable=True)
            executor._enter_idle_locked()
        executor._publish_event("precision_landing_stalled", summary, **event)

    def _step(self, mode: str) -> None:
        executor = self._executor
        telemetry = executor._telemetry()
        current = telemetry.pose
        if current is None or not all(
            math.isfinite(value) for value in (current.north_m, current.east_m, current.up_m)
        ):
            return
        if telemetry.pose_updated_monotonic is None:
            return
        if not executor._fresh(telemetry.pose_updated_monotonic):
            if self.active:
                self._controller_failed()
            return
        if not telemetry.armed:
            self._complete()
            return
        self._descent_watch(current, telemetry)
        if self.active and current.up_m <= self.HANDOFF_ALTITUDE_M:
            self.stats["completed_landings"] += 1
            logger.info("precision landing reached hand-off altitude; handing touchdown to LAND")
            executor._command_zero_velocity()
            executor._command_mode("LAND")
            self._complete()
            return
        home_distance = math.hypot(current.north_m, current.east_m)
        if home_distance > self.HOME_RADIUS_M:
            if self.active:
                executor._command_mode(mode)
                self.active = False
                self._reset_watch()
            return
        if self.unavailable:
            return

        frame = self._frame()
        if frame is None:
            self._hold_or_coast("no fresh frame")
            return
        if self.gimbal_requested_at is None:
            executor._command(executor.flight.set_gimbal_pitch_yaw, -90.0, 0.0)
            with executor._lock:
                executor._require_current_tick()
                self.gimbal_requested_at = time.monotonic()
            return
        if frame.timestamp < self.gimbal_requested_at + self.GIMBAL_SETTLE_S:
            if self.active:
                executor._command_zero_velocity()
            return
        heading = telemetry.heading_deg
        if (
            heading is None
            or not math.isfinite(heading)
            or not executor._fresh(telemetry.heading_updated_monotonic)
        ):
            # The tag offset is rotated into NED by the heading; never steer on a stale one.
            if self.active:
                executor._command_zero_velocity()
            return
        detection, velocity = self.controller.observe(frame.image, float(heading))
        if detection is None:
            self._hold_or_coast("tag not detected in the current frame")
            return
        self.stats["tag_detections"] += 1
        if not self.active:
            executor._command_mode("GUIDED")
            with executor._lock:
                executor._require_current_tick()
                self.active = True
                self._reset_watch()
            self.stats["acquisitions"] += 1
            logger.info("precision landing acquired AprilTag at %.2f m from home, %.2f m up",
                        home_distance, current.up_m)
        self._target_monotonic = time.monotonic()
        self._target_velocity = (float(velocity[0]), float(velocity[1]), float(velocity[2]))
        executor._command(executor.flight.set_velocity_ned, *self._target_velocity)
        self._log_control(telemetry, current, detection, self._target_velocity)

    def _frame(self) -> Any | None:
        perception = self._executor.perception
        if perception is None:
            return None
        frame = perception.get_frame()
        if frame is None or getattr(frame, "image", None) is None:
            return None
        try:
            timestamp = float(getattr(frame, "timestamp"))
        except (TypeError, ValueError, OverflowError):
            return None
        age = time.monotonic() - timestamp
        if not math.isfinite(timestamp) or age < -0.25 or age > self.FRAME_MAX_AGE_S:
            return None
        return frame

    def _descent_watch(self, current: Pose, telemetry: Telemetry) -> None:
        """Hand back to autopilot LAND when precision control stops making descent progress.

        No autopilot failsafe runs in GUIDED, so a descent that ignores the setpoints would hover
        armed indefinitely. The window is wall time; the required progress is scaled by the
        autopilot clock when it is fresh, because the commanded descent rate is in autopilot time.
        """
        if not self.active:
            return
        now = time.monotonic()
        if self._stall_started_monotonic is None:
            self._stall_started_monotonic = now
            self._stall_start_altitude_m = current.up_m
            self._stall_start_sim_s = self._fresh_sim_time_s(telemetry)
            return
        elapsed = now - self._stall_started_monotonic
        settings = self._executor.settings
        if elapsed < settings.precision_landing_stall_s:
            return
        descended_m = self._stall_start_altitude_m - current.up_m
        now_sim = self._fresh_sim_time_s(telemetry)
        if (
            now_sim is not None
            and self._stall_start_sim_s is not None
            and now_sim > self._stall_start_sim_s
        ):
            progress_basis_s = now_sim - self._stall_start_sim_s
            progress_basis = "autopilot_time_boot"
        else:
            progress_basis_s = elapsed
            progress_basis = "wall"
        required_m = settings.precision_landing_min_descent_m_s * progress_basis_s
        if descended_m >= required_m:
            # Meaningful progress: start a new window from the current altitude.
            self._stall_started_monotonic = now
            self._stall_start_altitude_m = current.up_m
            self._stall_start_sim_s = now_sim
            return
        descent_m_s = descended_m / progress_basis_s
        summary = (
            f"precision landing made no descent progress for {elapsed:.1f}s wall "
            f"({descended_m:.2f} m over {progress_basis_s:.1f}s of {progress_basis}); "
            "falling back to LAND"
        )
        logger.warning(
            "precision landing stalled at %.2f m: %.2f m of descent in %.1fs of %s "
            "(%.3f m/s < %.2f m/s); falling back to LAND",
            current.up_m, descended_m, progress_basis_s, progress_basis, descent_m_s,
            settings.precision_landing_min_descent_m_s,
        )
        self.fall_back_to_land(
            summary,
            record=True,
            altitude_m=current.up_m,
            elapsed_s=elapsed,
            descent_m=descended_m,
            descent_rate_m_s=descent_m_s,
            progress_basis=progress_basis,
        )

    def _fresh_sim_time_s(self, telemetry: Telemetry) -> float | None:
        """Autopilot time in seconds, or ``None`` when the SYSTEM_TIME sample is stale/absent."""
        if not self._executor._fresh(telemetry.time_boot_updated_monotonic):
            return None
        return self._executor._sim_time_s(telemetry)

    def _log_control(
        self,
        telemetry: Telemetry,
        current: Pose,
        detection: AprilTagDetection,
        velocity: tuple[float, float, float],
    ) -> None:
        """Log tag pixel error, gimbal pitch and commanded velocity, at most once a second."""
        now = time.monotonic()
        if now - self._last_log_monotonic < 1.0:
            return
        self._last_log_monotonic = now
        mount = telemetry.mount_status
        logger.info(
            "precision landing: tag_px=(%.1f,%.1f) error_px=(%.1f,%.1f) gimbal_pitch_deg=%s "
            "commanded_ned=(%.2f,%.2f,%.2f) altitude_m=%.2f",
            detection.center_x,
            detection.center_y,
            detection.center_x - detection.image_width * 0.5,
            detection.center_y - detection.image_height * 0.5,
            None if mount is None else mount.pitch_deg,
            velocity[0],
            velocity[1],
            velocity[2],
            current.up_m,
        )

    def _hold_or_coast(self, reason: str) -> None:
        """Keep the last tag setpoint for ``precision_landing_target_grace_s``, then hold.

        Detections arrive on only a fraction of ticks at range, and commanding zero on every miss
        pulses the descent into a near-hover; the stall watchdog remains the last-resort hand-off.
        """
        if not self.active:
            return
        executor = self._executor
        now = time.monotonic()
        last = self._target_monotonic
        velocity = self._target_velocity
        if (
            last is not None
            and velocity is not None
            and now - last <= executor.settings.precision_landing_target_grace_s
        ):
            logger.debug(
                "precision landing: %s; coasting the last tag setpoint (age %.2fs)",
                reason, now - last,
            )
            executor._command(executor.flight.set_velocity_ned, *velocity)
            return
        executor._command_zero_velocity()
        if last is not None:
            logger.debug(
                "precision landing: %s; tag setpoint expired after %.2fs, holding",
                reason, now - last,
            )

    def _complete(self) -> None:
        executor = self._executor
        if self.gimbal_requested_at is not None:
            executor._command(executor._set_default_gimbal_pitch)
        with executor._lock:
            executor._require_current_tick()
            self.reset(unavailable=False)
            executor._enter_idle_locked()

    def _controller_failed(self) -> None:
        """Hand a failed descent (stale pose or error) to LAND rather than hover armed in GUIDED."""
        logger.warning("precision landing lost control; falling back to LAND")
        if self.stats["last_fallback"] is None:
            self.stats["last_fallback"] = "controller failed (stale pose or error)"
        self.fall_back_to_land(
            "precision landing lost control (stale pose or error); falling back to LAND"
        )
