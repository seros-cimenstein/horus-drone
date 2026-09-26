"""Publish every processed perception frame, annotated, for human observers.

The set-of-mark image only updates when the commander requests a frame; this view shows the
detector, tracker IDs, shirt-colour attribute, and model timings continuously, and logs a
periodic throughput summary.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import logging
import time
from typing import Any

from horus.perception.render import render_live_view

LOGGER = logging.getLogger(__name__)
_MAX_LOGGED_TRACKS = 8


class LivePerceptionView:
    """Perception-thread observation callback; never raises into the camera loop."""

    def __init__(
        self,
        perception: Any,
        publisher: Any,
        *,
        ignore_regions: Sequence[Sequence[float]] = (),
        detector_name: str = "YOLO",
        log_interval_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._perception = perception
        self._publisher = publisher
        self._ignore_regions = tuple(tuple(region) for region in ignore_regions)
        self._detector_name = detector_name
        self._log_interval_s = log_interval_s
        self._clock = clock
        self._last_log_at: float | None = None
        self._render_failed = False

    def __call__(self, observation: Any) -> None:
        try:
            stats = self._perception.processing_stats()
        except Exception as exc:
            LOGGER.debug("Perception stats unavailable: %s", exc)
            return
        frame = getattr(observation, "frame", None)
        if frame is not None and self._publisher.available and not self._render_failed:
            try:
                image = render_live_view(
                    frame,
                    stats,
                    ignore_regions=self._ignore_regions,
                    detector_name=self._detector_name,
                )
            except Exception as exc:
                self._render_failed = True
                LOGGER.warning("Disabling live perception view rendering: %s", exc)
            else:
                self._publisher.publish_bgr(image)
        self._maybe_log(stats, getattr(observation, "candidates", ()))

    def _maybe_log(self, stats: dict[str, Any], candidates: Sequence[Any]) -> None:
        now = self._clock()
        if self._last_log_at is not None and now - self._last_log_at < self._log_interval_s:
            return
        self._last_log_at = now
        tracks = ", ".join(_track_summary(candidate) for candidate in candidates[:_MAX_LOGGED_TRACKS])
        if len(candidates) > _MAX_LOGGED_TRACKS:
            tracks += f", +{len(candidates) - _MAX_LOGGED_TRACKS} more"
        LOGGER.info(
            "perception %s fps | %s %s ms | track+ReID %s ms | detections %s (ignored %s) | tracks: %s",
            stats.get("fps"),
            self._detector_name,
            stats.get("detector_ms"),
            stats.get("tracker_ms"),
            stats.get("detections"),
            stats.get("ignored"),
            tracks or "none",
        )


def _track_summary(candidate: Any) -> str:
    summary = f"#{candidate.id} {candidate.class_name} {candidate.confidence:.2f}"
    shirt = candidate.attributes.get("shirt_colour")
    return f"{summary} shirt={shirt}" if shirt is not None else summary
