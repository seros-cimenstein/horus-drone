"""Bounded structured logging of appearance (re-identification) comparisons."""

from __future__ import annotations

from collections import OrderedDict
import json
import logging
import time
from typing import Any

from .types import Candidate


def _log_json(log: Any, record: dict[str, Any]) -> None:
    log(json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True), extra=record)


class ReidSimilarityLog:
    """One DEBUG record per comparison and one INFO summary per (mode, reference) per interval.

    A hold re-identifies the same target against every visible candidate on every control tick,
    so per-comparison INFO lines would flood the log. The summary keeps every field a diagnosis
    needs (comparison counts, the best score and candidate, whether the reference's own track
    was among them) at a bounded volume. Windows are kept for at most ``max_references``
    reference IDs; the least recently compared reference is forgotten first.
    """

    SUMMARY_INTERVAL_S = 1.0
    MAX_REFERENCES = 64

    def __init__(self, logger: logging.Logger, *, max_references: int = MAX_REFERENCES) -> None:
        if max_references < 1:
            raise ValueError("max_references must be positive")
        self._logger = logger
        self._max_references = max_references
        # reference id -> mode -> summary window, least recently compared reference first.
        self._windows: OrderedDict[int, dict[str, dict[str, Any]]] = OrderedDict()

    def record(
        self,
        *,
        mode: str,
        similarity: float | None,
        reference_id: int,
        candidate: Candidate,
        threshold: float,
        crop_px: tuple[int, int],
        primary_similarity: float | None = None,
        matched_view: int = 0,
        view_count: int = 1,
    ) -> None:
        """Log one comparison at DEBUG and fold it into the reference's summary window.

        ``matched_view`` is the stored view that produced the best score (0 = creation view),
        ``view_count`` the stored views, ``primary_similarity`` the creation view's own score.
        """
        record = {
            "event": "perception_reid_similarity",
            "mode": mode,
            "similarity": similarity,
            "threshold": threshold,
            "configured_threshold": threshold,
            "reference_id": reference_id,
            "candidate_id": candidate.id,
            "shirt_colour": candidate.attributes.get("shirt_colour"),
            "bbox": list(candidate.bbox),
            "crop_width_px": crop_px[0],
            "crop_height_px": crop_px[1],
            "frame_id": candidate.frame_id,
            "matched_view": matched_view,
            "reference_view_count": view_count,
            "primary_similarity": primary_similarity,
        }
        _log_json(self._logger.debug, record)
        self._summarize(record)

    def _window(self, mode: str, reference_id: int, now: float) -> dict[str, Any]:
        windows = self._windows.get(reference_id)
        if windows is None:
            windows = self._windows[reference_id] = {}
            while len(self._windows) > self._max_references:
                self._windows.popitem(last=False)
        else:
            self._windows.move_to_end(reference_id)
        window = windows.get(mode)
        if window is None:
            window = windows[mode] = {
                "started": now,
                "comparisons": 0,
                "total": 0,
                "best_similarity": None,
                "best_candidate_id": None,
                "best_crop_px": None,
                "best_view_index": 0,
                "reference_view_count": 1,
                "last_similarity": None,
                "own_track_seen": False,
            }
        return window

    def _summarize(self, record: dict[str, Any]) -> None:
        now = time.monotonic()
        window = self._window(str(record["mode"]), int(record["reference_id"]), now)
        window["comparisons"] += 1
        window["total"] += 1
        similarity = record.get("similarity")
        if isinstance(similarity, float):
            window["last_similarity"] = similarity
            if window["best_similarity"] is None or similarity > window["best_similarity"]:
                window["best_similarity"] = similarity
                window["best_candidate_id"] = record.get("candidate_id")
                window["best_crop_px"] = (record.get("crop_width_px"), record.get("crop_height_px"))
                window["best_view_index"] = record.get("matched_view", 0)
                window["reference_view_count"] = record.get("reference_view_count", 1)
        if record.get("candidate_id") == record.get("reference_id"):
            window["own_track_seen"] = True
        elapsed = now - window["started"]
        if elapsed < self.SUMMARY_INTERVAL_S:
            return
        _log_json(
            self._logger.info,
            {
                "event": "perception_reid_summary",
                "mode": record["mode"],
                "reference_id": record["reference_id"],
                "threshold": record["threshold"],
                "window_s": round(elapsed, 3),
                "comparisons": window["comparisons"],
                "total_comparisons": window["total"],
                "best_similarity": window["best_similarity"],
                "best_candidate_id": window["best_candidate_id"],
                "best_crop_px": list(window["best_crop_px"]) if window["best_crop_px"] else None,
                "best_view_index": window["best_view_index"],
                "reference_view_count": window["reference_view_count"],
                "last_similarity": window["last_similarity"],
                "own_track_seen": window["own_track_seen"],
            },
        )
        window.update(
            {
                "started": now,
                "comparisons": 0,
                "best_similarity": None,
                "best_candidate_id": None,
                "best_crop_px": None,
                "own_track_seen": False,
            }
        )


class ReacquisitionGate:
    """Require the same candidate to pass re-identification on two distinct processed frames.

    The first passing frame records a pending confirmation and reports no match; a later frame
    with the same candidate id inside ``window_s`` confirms it. A different candidate, an expired
    window, or the same frame repeated (the executor asks many times per frame) does not count.
    A ``window_s`` of 0 disables the rule. At most ``MAX_PENDING`` references are remembered.
    """

    MAX_PENDING = 128

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        # reference_id -> {"candidate_id", "frame_id", "monotonic"}
        self._pending: dict[int, dict[str, Any]] = {}

    def reset(self, reference_id: int) -> None:
        """A frame without a passing candidate breaks the confirmation."""
        self._pending.pop(reference_id, None)

    def confirm(self, reference_id: int, candidate_id: int, frame_id: Any) -> bool:
        if self.window_s <= 0:
            return True
        now = time.monotonic()
        if len(self._pending) > self.MAX_PENDING:
            oldest = sorted(self._pending.items(), key=lambda item: item[1]["monotonic"])
            for reference, _ in oldest[: len(oldest) - self.MAX_PENDING]:
                del self._pending[reference]
        pending = self._pending.get(reference_id)
        if (
            pending is not None
            and pending["candidate_id"] == candidate_id
            and pending["frame_id"] != frame_id
            and now - pending["monotonic"] <= self.window_s
        ):
            del self._pending[reference_id]
            return True
        if pending is None or pending["candidate_id"] != candidate_id or pending["frame_id"] != frame_id:
            self._pending[reference_id] = {
                "candidate_id": candidate_id,
                "frame_id": frame_id,
                "monotonic": now,
            }
        return False
