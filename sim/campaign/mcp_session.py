"""Open the episode's MCP session once the freshly started server answers."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
import threading
import time
from typing import Any

from horus.mcp_client import open_session

# The MCP server boots with the world, so readiness is probed in bounded attempts.
MCP_READY_DEADLINE_S = 120.0
MCP_READY_ATTEMPT_TIMEOUT_S = 30.0


def exception_summary(exc: BaseException) -> str:
    """Flatten ExceptionGroup/TaskGroup wrappers into the underlying causes."""
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, (list, tuple)) and nested:
        inner = "; ".join(exception_summary(item) for item in nested if isinstance(item, BaseException))
        label = f"{type(exc).__name__}: {exc}"
        return f"{label} [{inner}]" if inner else label
    return f"{type(exc).__name__}: {exc}"


async def _ready_session(url: str, token: str, attach: Callable[[Any], Awaitable[Any]]) -> AsyncExitStack:
    """Open an MCP session and ``attach`` it (which must probe it); the caller owns the stack.

    The attempt deadline cancels this task via a watchdog because wrapping the anyio-based
    MCP client in ``asyncio.wait_for`` (a separate task) corrupts its task group, while
    ``anyio.fail_after`` cannot exit its scope around the client's nested task group.
    """
    task = asyncio.current_task()
    watchdog = (asyncio.get_running_loop().call_later(MCP_READY_ATTEMPT_TIMEOUT_S, task.cancel)
                if task is not None else None)
    stack = AsyncExitStack()
    try:
        await attach(await stack.enter_async_context(open_session(url, token)))
    except BaseException as exc:
        if watchdog is not None:
            watchdog.cancel()
        teardown: BaseException | None = None
        try:
            await stack.aclose()
        except BaseException as close_exc:
            teardown = close_exc
        if teardown is not None and getattr(teardown, "exceptions", None):
            # The transport task group's exit carries the real cause (e.g. ConnectError);
            # surface that instead of an opaque "unhandled errors in a TaskGroup".
            raise teardown from exc
        raise
    finally:
        if watchdog is not None:
            watchdog.cancel()
    return stack


async def open_ready_session(url: str, token: str, attach: Callable[[Any], Awaitable[Any]],
                             stop: threading.Event) -> AsyncExitStack:
    """Retry ``_ready_session`` until it succeeds, ``stop`` is set or the readiness deadline passes."""
    deadline = time.monotonic() + MCP_READY_DEADLINE_S
    attempts = 0
    last_error: BaseException | None = None
    while time.monotonic() < deadline and not stop.is_set():
        attempts += 1
        try:
            return await _ready_session(url, token, attach)
        except (Exception, asyncio.CancelledError) as exc:
            if stop.is_set():
                break
            # The MCP client can surface transport failures as an anyio scope
            # cancellation; that is not this task being cancelled, so retry.
            last_error = exc
            print(f"MCP readiness attempt {attempts} failed: {exception_summary(exc)}", flush=True)
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
    if last_error is None:
        raise RuntimeError(f"MCP server did not become ready within {MCP_READY_DEADLINE_S:.0f}s")
    raise RuntimeError(f"MCP server did not become ready after {attempts} attempt(s): "
                       f"{exception_summary(last_error)}")
