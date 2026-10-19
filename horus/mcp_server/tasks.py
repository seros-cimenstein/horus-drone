"""Asynchronous task, event and onboard-plan MCP tools."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from horus.events import TERMINAL_TASK_STATUSES
from horus.executor.plan import (
    DEFAULT_PLAN_TTL_S,
    MAX_LOOP_ITERS,
    MAX_PLAN_CHARS,
    MAX_PLAN_TTL_S,
    MAX_TOTAL_LOOP_ITERS,
    function_reference,
)
from horus.mcp_server.context import ToolContext, to_json

_WAIT_MAX_S = 60.0
_WAIT_POLL_S = 0.5


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
    """Inspect an asynchronous task's state, TTL, errors, and requested/clamped targets."""
    return to_json(ctx.executor.get_task_status(task_id))


async def wait_for_task(ctx: ToolContext, task_id: str, timeout_s: float = 30.0) -> str:
    """Block until a task reaches a terminal status or timeout_s (1-60 s) elapses.

    Returns the task status plus ``waited_s`` and ``timed_out``; call again to keep
    waiting on long skills such as follow.
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


def cancel(ctx: ToolContext, task_id: str) -> str:
    """Cancel a queued or running skill by task ID and inspect the resulting status."""
    return to_json(ctx.executor.cancel(task_id))


_RUN_PLAN_DESCRIPTION = (
    "Run a whole mission onboard as one plan, so it continues without further tool calls "
    "or a live link. Returns a plan_id immediately; wait_for_plan / get_plan_status report "
    "progress, a plan_finished event reports the end, cancel_plan stops it. While a plan "
    "runs it owns the skill queue: direct skill tools are refused, while hold/rth/land "
    "always work and abort it, as do link-loss RTH and a pilot taking over.\n\n"
    "Language (TypeFly-style restricted Python): calls to the functions below, "
    "single-name assignment, if/elif/else, `for i in range(<integer literals>)` "
    f"(at most {MAX_LOOP_ITERS} iterations, {MAX_TOTAL_LOOP_ITERS} nested), "
    "break/continue/pass, "
    "comparisons, and/or/not, float + - * /, f-strings. Nothing else: no imports, "
    "attributes, indexing, while or def. Every flight call is a normal clamped HORUS "
    "skill and blocks until it ends; its return value lets the plan branch.\n"
    f"Limits: {MAX_PLAN_CHARS} characters; ttl_s defaults to {DEFAULT_PLAN_TTL_S:g} s "
    f"(max {MAX_PLAN_TTL_S:g} s).\n\n"
    "Functions:\n" + function_reference() + "\n\n"
    "Example:\n"
    "take_off(3)\n"
    "t = search(0, 8, -4, 4, class_name='person', attribute='shirt_colour', equals='red')\n"
    "if t >= 0:\n"
    "    approach(t, 2)\n"
    "    log(f'found person {t}')\n"
    "else:\n"
    "    rth()"
)


def run_plan(ctx: ToolContext, plan: str, ttl_s: float | None = None) -> str:
    return to_json(ctx.plans.submit(plan, ttl_s))


def get_plan_status(ctx: ToolContext, plan_id: str) -> str:
    """Inspect a plan: status, current task, finished steps, log lines and final variables."""
    return to_json(ctx.plans.status(plan_id))


async def wait_for_plan(ctx: ToolContext, plan_id: str, timeout_s: float = 30.0) -> str:
    """Block until a plan ends or timeout_s (1-60 s) elapses; call again for long plans."""
    return await _poll_until(
        lambda: ctx.plans.status(plan_id), lambda status: status["status"] != "running", timeout_s
    )


def cancel_plan(ctx: ToolContext, plan_id: str) -> str:
    """Stop a running plan; its current skill is cancelled and the vehicle holds."""
    return to_json(ctx.plans.cancel(plan_id))


def register(mcp: Any, ctx: ToolContext) -> None:
    for function in (get_task_status, wait_for_task, wait_events, cancel):
        ctx.tool(function)
    ctx.tool(run_plan, description=_RUN_PLAN_DESCRIPTION)
    for function in (get_plan_status, wait_for_plan, cancel_plan):
        ctx.tool(function)
