"""Asynchronous task and event MCP tools."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from horus.events import EVENT_KINDS, TERMINAL_TASK_STATUSES
from horus.mcp_server.context import ToolContext, to_json

_WAIT_MAX_S = 60.0
_WAIT_POLL_S = 0.5
# wait_for_event never blocks longer than this, nor longer than 80 % of the link-loss RTH delay:
# contact is noted only when a call starts and ends, so back-to-back waits keep a hold supervised.
_EVENT_WAIT_MAX_S = 25.0


async def _poll_until(
    read: Callable[[], dict[str, Any]], done: Callable[[dict[str, Any]], bool], timeout_s: float
) -> str:
    """Poll ``read`` until ``done`` or the 1-60 s timeout; report the last status as JSON."""
    timeout = min(_WAIT_MAX_S, max(1.0, float(timeout_s)))
    started = time.monotonic()
    while True:
        status = read()
        waited = time.monotonic() - started
        if done(status) or waited >= timeout:
            break
        await asyncio.sleep(min(_WAIT_POLL_S, timeout - waited))
    return to_json({**status, "waited_s": round(waited, 2), "timed_out": not done(status)})


def get_task_status(ctx: ToolContext, task_id: str) -> str:
    """Inspect an asynchronous task's state, TTL, errors, and requested/executed targets."""
    return to_json(ctx.executor.get_task_status(task_id))


async def wait_for_task(ctx: ToolContext, task_id: str, timeout_s: float = 30.0) -> str:
    """Block until a task reaches a terminal status or timeout_s (1-60 s) elapses.

    Returns the task status plus ``waited_s`` and ``timed_out``; call again to keep
    waiting on a long move.
    """
    return await _poll_until(
        lambda: ctx.executor.get_task_status(task_id),
        lambda status: status.get("status") in TERMINAL_TASK_STATUSES,
        timeout_s,
    )


async def wait_events(ctx: ToolContext, after_id: int = 0, timeout_s: float = 25.0) -> str:
    """Wait for executor and perception events newer than after_id, up to 60 seconds.

    Perception events include object_detected and the region_* events of watch_region;
    event labels are quoted untrusted detector strings.
    """
    cursor = int(after_id)
    timeout = min(60.0, max(0.0, float(timeout_s)))
    events = await ctx.event_bus.wait(cursor, timeout)
    last_id = events[-1]["id"] if events else cursor
    return to_json({"events": events, "last_id": last_id})


async def wait_for_event(
    ctx: ToolContext, kinds: list[str], after_id: int | None = None, timeout_s: float = 20.0
) -> str:
    """Block until an event of one of ``kinds`` arrives or timeout_s (1-25 s) elapses.

    Use it instead of polling get_state: after rth or land wait for ["landed"]; while a skill
    runs wait for e.g. ["task_finished"].
    Without ``after_id`` only events published after this call starts count, so an event that
    arrives between two calls is missed: when calling again, pass the previous result's
    ``last_id`` as ``after_id``. Returns the matching ``events``, ``last_id``, ``waited_s`` and
    ``timed_out``; call again to keep waiting.
    """
    if isinstance(kinds, str) or not kinds or not all(isinstance(kind, str) for kind in kinds):
        raise ValueError("kinds must be a non-empty list of event kinds")
    unknown = sorted(set(kinds) - EVENT_KINDS)
    if unknown:
        raise ValueError(f"unknown event kinds {unknown}; known: {sorted(EVENT_KINDS)}")
    wanted = frozenset(kinds)
    upper = min(_EVENT_WAIT_MAX_S, 0.8 * ctx.config.executor.link_loss_rth_delay_s)
    timeout = min(upper, max(1.0, float(timeout_s)))
    cursor = ctx.event_bus.latest_id() if after_id is None else int(after_id)
    loop = asyncio.get_running_loop()
    started = loop.time()
    matched: list[dict[str, Any]] = []
    while not matched:
        remaining = timeout - (loop.time() - started)
        if remaining <= 0.0:
            break
        events = await ctx.event_bus.wait(cursor, remaining)
        if not events:
            break
        cursor = events[-1]["id"]
        matched = [event for event in events if event["kind"] in wanted]
    return to_json({
        "events": matched,
        "last_id": cursor,
        "waited_s": round(loop.time() - started, 2),
        "timed_out": not matched,
    })


def cancel(ctx: ToolContext, task_id: str) -> str:
    """Cancel a queued or running skill by task ID and inspect the resulting status."""
    return to_json(ctx.executor.cancel(task_id))


def register(mcp: Any, ctx: ToolContext) -> None:
    for function in (get_task_status, wait_for_task, wait_events, wait_for_event, cancel):
        ctx.tool(function)
