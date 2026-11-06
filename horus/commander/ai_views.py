"""The camera images the AI pilot's tools returned to the AI model, kept in memory for the control
app ("what the AI saw"). Only the newest few are kept, bounded by count and by total bytes; the
bytes never go into the event stream, the event log or the commander's history: the feed gets a
small ``ai_view`` event and the page fetches the picture by its id when it shows it."""

from __future__ import annotations

import base64
import binascii
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

AI_VIEW_MAX_COUNT = 30
AI_VIEW_MAX_BYTES = 8 * 1024 * 1024
# Served as their own content type; anything else a tool might return is not kept.
AI_VIEW_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_VIEW_ID = re.compile(r"[0-9a-f]{16}")


@dataclass(frozen=True, slots=True)
class AIView:
    view_id: str
    time: str
    tool: str
    mime_type: str
    data: bytes
    tool_use_id: str | None = None

    def metadata(self) -> dict[str, Any]:
        """The small description the feed and ``/api/ai-view/latest`` carry; never the bytes."""
        meta: dict[str, Any] = {
            "view_id": self.view_id,
            "time": self.time,
            "tool": self.tool,
            "mime_type": self.mime_type,
            "bytes": len(self.data),
        }
        if self.tool_use_id:
            meta["tool_use_id"] = self.tool_use_id
        return meta


class AIViewStore:
    """Ring buffer of the newest AI views: at most ``max_count`` and ``max_bytes`` together.

    Ids are random, so an id never names a different picture, not even after a service restart
    (the page may cache a picture by its id for a day).
    """

    def __init__(self, max_count: int = AI_VIEW_MAX_COUNT, max_bytes: int = AI_VIEW_MAX_BYTES) -> None:
        if max_count < 1 or max_bytes < 1:
            raise ValueError("max_count and max_bytes must be positive")
        self.max_count = max_count
        self.max_bytes = max_bytes
        self._views: OrderedDict[str, AIView] = OrderedDict()
        self._bytes = 0

    def add(
        self, data: bytes, mime_type: str, tool: str, tool_use_id: str | None = None
    ) -> AIView | None:
        """Keep one picture, dropping the oldest ones over the limits; None when it is not kept
        (empty, an unknown type, or larger than the whole budget)."""
        mime_type = mime_type.lower()
        if not data or mime_type not in AI_VIEW_MIME_TYPES or len(data) > self.max_bytes:
            return None
        view_id = secrets.token_hex(8)
        while view_id in self._views:
            view_id = secrets.token_hex(8)
        view = AIView(
            view_id=view_id,
            time=datetime.now(timezone.utc).isoformat(),
            tool=tool,
            mime_type=mime_type,
            data=bytes(data),
            tool_use_id=tool_use_id or None,
        )
        self._views[view_id] = view
        self._bytes += len(view.data)
        while len(self._views) > self.max_count or self._bytes > self.max_bytes:
            _, dropped = self._views.popitem(last=False)
            self._bytes -= len(dropped.data)
        return view

    def get(self, view_id: str) -> AIView | None:
        return self._views.get(view_id) if _VIEW_ID.fullmatch(view_id) else None

    def latest(self) -> AIView | None:
        return next(reversed(self._views.values()), None)

    def __len__(self) -> int:
        return len(self._views)

    @property
    def total_bytes(self) -> int:
        return self._bytes


def decode_image(data: Any) -> bytes | None:
    """The bytes of a base64 image payload, or None when it is not valid base64."""
    if not isinstance(data, str) or not data:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
