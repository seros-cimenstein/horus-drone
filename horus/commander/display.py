"""The control app's own drone board session, for read-only display calls.

It is separate from the AI pilot's session (``HorusCommander``), so a slow or failing page never
delays the model or an emergency, and every call it makes is ``passive``: it never counts as AI
contact (the hold-then-home failsafe still fires when the AI goes quiet) and never replaces the
model's last set-of-mark view.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from horus.mcp_client import open_session, tool_json, tool_text

_RECONNECT_DELAY_S = 1.0


class DisplayUnavailable(Exception):
    """The drone board cannot answer a display read right now (HTTP 503 for the page)."""


class DisplayReader:
    """One self-reconnecting MCP session that only ever calls passive read tools."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = 5.0,
        opener: Callable[[str, str], Any] = open_session,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout_s = timeout_s
        self._opener = opener
        self._session: Any = None
        self._reconnect = asyncio.Event()
        self._last_error = "drone board not connected yet"

    async def run(self) -> None:
        """Hold the session open, reopening it after a failure, until cancelled."""
        while True:
            try:
                async with self._opener(self._url, self._token) as session:
                    self._reconnect.clear()
                    self._session = session
                    await self._reconnect.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"drone board unavailable: {_reason(exc)}"
            finally:
                self._session = None
            await asyncio.sleep(_RECONNECT_DELAY_S)

    async def _call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        session = self._session
        if session is None:
            raise DisplayUnavailable(self._last_error)
        try:
            result = await asyncio.wait_for(
                session.call_tool(name, {**arguments, "passive": True}), self._timeout_s
            )
        except asyncio.TimeoutError:
            self._last_error = f"drone board did not answer {name} within {self._timeout_s:g} s"
            self._reconnect.set()
            raise DisplayUnavailable(self._last_error) from None
        except Exception as exc:
            self._last_error = f"drone board unavailable: {_reason(exc)}"
            self._reconnect.set()
            raise DisplayUnavailable(self._last_error) from exc
        if getattr(result, "isError", False):
            # A tool-level error (e.g. no camera frames yet): the session itself is fine.
            raise DisplayUnavailable(tool_text(result) or f"{name} failed")
        return result

    async def _json(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        result = await self._call(name, arguments or {})
        try:
            return tool_json(result, name)
        except ValueError as exc:
            raise DisplayUnavailable(str(exc)) from exc

    async def state(self) -> Any:
        return await self._json("get_state")

    async def frame(self) -> dict[str, Any]:
        """The latest set-of-mark frame: base64 JPEG plus its frame metadata and candidates."""
        result = await self._call("list_candidates", {})
        image = next(
            (block for block in result.content if getattr(block, "type", None) == "image"), None
        )
        if image is None:
            raise DisplayUnavailable("list_candidates returned no image")
        try:
            metadata = tool_json(result, "list_candidates")
        except ValueError as exc:
            raise DisplayUnavailable(str(exc)) from exc
        return {**metadata, "image_base64": image.data, "mime_type": image.mimeType}

    async def map_index(self, known_version: str = "") -> dict[str, Any]:
        """The map's tile index, or only ``{"version", "unchanged": True}`` while it is still
        ``known_version``, so a cached copy costs the drone link almost nothing."""
        return await self._json("get_map_index", {"known_version": known_version} if known_version else {})

    async def map_tile(self, level: int, x: int, y: int, known_version: str = "") -> dict[str, Any]:
        """One map tile; ``{"version", "unchanged": True}`` while the map is still
        ``known_version``, ``{"version", "missing": True}`` where the map has nothing."""
        arguments = {"level": level, "x": x, "y": y, **({"known_version": known_version} if known_version else {})}
        return await self._json("get_map_tile", arguments)

    async def live_map(self) -> Any:
        """Pose, heading, marks and positioned candidates: the small, changing part of the map."""
        return await self._json("get_live_map")


def _reason(exc: BaseException) -> str:
    """The innermost error, not anyio's "unhandled errors in a TaskGroup" wrapper around it.

    Exception groups are matched by their ``exceptions`` list, so the ``exceptiongroup`` backport
    used on Python 3.10 is unwrapped too.
    """
    inner = getattr(exc, "exceptions", None)
    while isinstance(inner, (list, tuple)) and inner and isinstance(inner[0], BaseException):
        exc = inner[0]
        inner = getattr(exc, "exceptions", None)
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
