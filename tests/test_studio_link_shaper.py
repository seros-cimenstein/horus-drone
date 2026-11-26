from __future__ import annotations

import asyncio
import time

import pytest

from horus.studio.link_shaper import LinkShaper, LinkShaperState, ShapedMCPSession


class _FakeRawSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        return {"ok": True, "tool": name}

    async def list_tools(self):
        return ["get_state", "goto"]

    async def get_prompt(self, name: str, arguments=None):
        return f"prompt:{name}"


def test_link_shaper_bounds_validation() -> None:
    shaper = LinkShaper()
    st = shaper.configure(delay_ms=250, jitter_ms=50, link_cut=False)
    assert st == LinkShaperState(delay_ms=250, jitter_ms=50, link_cut=False)

    with pytest.raises(ValueError):
        shaper.configure(delay_ms=6000)
    with pytest.raises(ValueError):
        shaper.configure(jitter_ms=-1)


def test_shaped_session_delays_and_cuts_ai_calls_but_never_operator_safety() -> None:
    async def run() -> None:
        raw = _FakeRawSession()
        shaper = LinkShaper()
        shaped = ShapedMCPSession(raw, shaper)

        shaper.configure(delay_ms=40, jitter_ms=0, link_cut=False)
        t0 = time.monotonic()
        res = await shaped.call_tool("goto", {"north_m": 2.0})
        assert res["ok"] is True
        assert time.monotonic() - t0 >= 0.03

        shaper.configure(link_cut=True)
        with pytest.raises(ConnectionError, match="cut"):
            await shaped.call_tool("goto", {"north_m": 3.0})
        with pytest.raises(ConnectionError, match="cut"):
            await shaped.call_tool("wait_events", {"after_id": 0})

        # Operator emergency and release bypass the AI radio link shaper.
        op_res = await shaped.call_tool("operator_emergency", {"action": "hold"})
        assert op_res["tool"] == "operator_emergency"

    asyncio.run(run())
