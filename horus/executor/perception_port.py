"""The perception interface the executor calls; ``horus.perception.PerceptionSystem`` implements it.

Only precision landing reads perception (the newest frame, for the AprilTag pad). Kept free of
vision imports so the executor loads on a flight-only install.
"""

from __future__ import annotations

from typing import Any, Protocol


class PerceptionPort(Protocol):
    def get_frame(self, frame_id: int | None = None) -> Any | None:
        """Newest (or a retained) frame with ``image`` and ``timestamp``."""
