"""The perception interface the executor calls; ``horus.perception.PerceptionSystem`` implements it.

Kept free of vision imports so the executor loads on a flight-only install.
"""

from __future__ import annotations

from typing import Any, Protocol


class PerceptionPort(Protocol):
    def latest_observation(self) -> Any | None:
        """Newest processed observation (``frame_id``, ``timestamp``, ``pose``, ``candidates``)."""

    def get_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        """A candidate visible in the newest frame, unvalidated."""

    def validate_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        """The candidate after fresh reference-appearance validation, or None."""

    def validate_candidate_failure(self, candidate_id: int) -> str | None:
        """Why ``validate_candidate`` rejects the ID (``not_visible``, ``stale``, ...)."""

    def reidentify_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        """Match a lost ID's stored appearance to a fresh visible candidate."""

    def get_frame(self, frame_id: int | None = None) -> Any | None:
        """Newest (or a retained) frame with ``image`` and ``timestamp``."""
