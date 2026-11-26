"""Runtime AI-link fault shaper (latency, jitter, and radio link-cut simulator)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import random
import threading
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class LinkShaperState:
    delay_ms: int = 0
    jitter_ms: int = 0
    link_cut: bool = False

    def __post_init__(self) -> None:
        if not 0 <= int(self.delay_ms) <= 5000:
            raise ValueError("delay_ms must be between 0 and 5000")
        if not 0 <= int(self.jitter_ms) <= 2500:
            raise ValueError("jitter_ms must be between 0 and 2500")


class LinkShaper:
    """Thread-safe runtime fault injector attached ONLY to the AI pilot's MCP session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = LinkShaperState()
        self._rng = random.Random()

    def state(self) -> LinkShaperState:
        with self._lock:
            return self._state

    def configure(
        self,
        *,
        delay_ms: int | None = None,
        jitter_ms: int | None = None,
        link_cut: bool | None = None,
    ) -> LinkShaperState:
        with self._lock:
            current = self._state
            updated = LinkShaperState(
                delay_ms=current.delay_ms if delay_ms is None else int(delay_ms),
                jitter_ms=current.jitter_ms if jitter_ms is None else int(jitter_ms),
                link_cut=current.link_cut if link_cut is None else bool(link_cut),
            )
            self._state = updated
            return updated

    def to_dict(self) -> dict[str, Any]:
        st = self.state()
        return {"delay_ms": st.delay_ms, "jitter_ms": st.jitter_ms, "link_cut": st.link_cut}

    async def apply(self) -> None:
        """Apply configured delay/jitter or raise ``ConnectionError`` when ``link_cut`` is active."""
        st = self.state()
        if st.link_cut:
            raise ConnectionError("AI radio link cut by Studio Fault Lab")
        if st.delay_ms > 0 or st.jitter_ms > 0:
            jitter = self._rng.uniform(-st.jitter_ms, st.jitter_ms) if st.jitter_ms > 0 else 0.0
            wait_s = max(0.0, (st.delay_ms + jitter) / 1000.0)
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            if self.state().link_cut:
                raise ConnectionError("AI radio link cut by Studio Fault Lab")


class ShapedMCPSession:
    """Proxy wrapping an MCP ``ClientSession`` for the AI pilot (never ``DisplayReader``)."""

    def __init__(self, inner: Any, shaper: LinkShaper) -> None:
        self._inner = inner
        self._shaper = shaper

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        # Operator safety tools (`operator_emergency`, `operator_release`) are never blocked by the
        # AI link fault lab; only AI pilot traffic passes through the simulated AI radio link.
        if name not in {"operator_emergency", "operator_release"}:
            await self._shaper.apply()
        return await self._inner.call_tool(name, dict(arguments or {}))

    async def list_tools(self) -> Any:
        await self._shaper.apply()
        return await self._inner.list_tools()

    async def get_prompt(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        await self._shaper.apply()
        return await self._inner.get_prompt(name, arguments or {})

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)
