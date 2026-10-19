"""Mission-scoped HORUS commander: dispatches operator intent and executor events through one
backend from :mod:`horus.commander.backends` (Claude Code session or provider API)."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from horus.commander.backends import (
    APICommanderBackend, ClaudeCodeSession, CommanderBackend, redact_secret,
)
from horus.commander.intents import is_reset_command
from horus.config import CommanderSettings
from horus.event_log import JsonlEventLog, _safe_value, event_log_path_from_environment
from horus.events import EVENT_KINDS, MODEL_WAKE_EVENT_KINDS, REGION_TRANSITION_KINDS
from horus.mcp_client import call_json, open_session, raise_for_error, tool_text


@dataclass(slots=True)
class CommanderEvent:
    text: str
    response: asyncio.Future[str]
    # Emergency generation at submission; an emergency discards requests not yet sent.
    generation: int = 0


# Kinds that can repeat in bursts (flaky link, occlusions): wake the commander at most once per
# cooldown and report the suppressed count with the next forwarded batch.
_REPEAT_LIMITED_KINDS = frozenset({"link_lost", "link_restored", "target_lost", "target_reacquired"})
# Emergency events whose requester already informs the model; mirrored to the log/GUI only.
_ECHOED_EMERGENCY_SOURCES = frozenset({"mcp", "plan"})


def _event_source(event: Mapping[str, Any]) -> Any:
    data = event.get("data")
    return data.get("source") if isinstance(data, Mapping) else None


class _EventWakePolicy:
    """Decide which executor events wake the commander; every event still reaches the GUI log."""

    COOLDOWN_S = 30.0
    REGION_COOLDOWN_S = 10.0

    def __init__(self) -> None:
        self._last_sent: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._labels_seen: set[str] = set()

    def admit(self, event: Mapping[str, Any], now: float) -> bool:
        kind = str(event.get("kind", ""))
        if kind == "object_detected":
            data = event.get("data")
            label = str(data.get("label", "")).casefold() if isinstance(data, Mapping) else ""
            if label in self._labels_seen:
                self._suppress(kind)
                return False
            self._labels_seen.add(label)
            return True
        if kind in _REPEAT_LIMITED_KINDS:
            return self._admit_after_cooldown(kind, now, self.COOLDOWN_S)
        # Region transitions wake at most once per kind and watch per REGION_COOLDOWN_S (a busy
        # scene otherwise costs a model turn per batch); list_region_watches shows the occupancy.
        # Watch pause/resume/expiry always wake: they change what the watch can report and are
        # already rate-bounded by the camera-settling hysteresis and the per-watch event budget.
        if kind in REGION_TRANSITION_KINDS:
            data = event.get("data")
            watch_id = str(data.get("watch_id", "")) if isinstance(data, Mapping) else ""
            return self._admit_after_cooldown(f"{kind}[{watch_id}]", now, self.REGION_COOLDOWN_S)
        return True

    def _admit_after_cooldown(self, key: str, now: float, cooldown_s: float) -> bool:
        last = self._last_sent.get(key)
        if last is not None and now - last < cooldown_s:
            self._suppress(key)
            return False
        self._last_sent[key] = now
        return True

    def drain_suppressed(self) -> str | None:
        if not self._suppressed:
            return None
        counts = ", ".join(f"{kind} x{count}" for kind, count in sorted(self._suppressed.items()))
        self._suppressed.clear()
        return f"[HORUS events suppressed as repeats since the last message: {counts}]"

    def reset(self) -> None:
        self._last_sent.clear()
        self._suppressed.clear()
        self._labels_seen.clear()

    def _suppress(self, kind: str) -> None:
        self._suppressed[kind] = self._suppressed.get(kind, 0) + 1


class HorusCommander:
    """Dispatch operator intent and MCP events through one mission-scoped backend."""

    def __init__(
        self,
        server_url: str,
        bearer_token: str,
        settings: CommanderSettings,
        *,
        api_key: str | None = None,
        event_log: JsonlEventLog | None = None,
        event_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.bearer_token = bearer_token
        self.settings = settings
        self.api_key = (
            (api_key or os.environ.get(settings.api_key_env))
            if settings.provider in {"anthropic", "openai"}
            else None
        )
        if settings.provider in {"anthropic", "openai"} and not self.api_key:
            raise RuntimeError(
                f"API commander provider {settings.provider!r} requires {settings.api_key_env}"
            )
        self.event_log = event_log or _event_log_from_environment()
        self.event_callback = event_callback
        self._events: asyncio.Queue[CommanderEvent | None] = asyncio.Queue()
        self._failure: Exception | None = None
        self._stopped = asyncio.Event()
        self._session_ready = asyncio.Event()
        self._session_ready.set()
        self.backend: CommanderBackend | None = None
        self._mcp_session: Any = None
        self._mission_active = False
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._event_message_tasks: set[asyncio.Task[None]] = set()
        self._wake_policy = _EventWakePolicy()
        self._emergency_generation = 0

    async def submit_task(self, text: str) -> str:
        """Queue a free-text operator request for the current mission."""
        if is_reset_command(text):
            await self.reset()
            return "Mission reset."
        if self._failure is not None:
            raise RuntimeError("commander MCP session failed") from self._failure
        if self._stopped.is_set():
            raise RuntimeError("commander has been stopped")
        loop = asyncio.get_running_loop()
        response: asyncio.Future[str] = loop.create_future()
        await self._events.put(
            CommanderEvent(text=text, response=response, generation=self._emergency_generation)
        )
        return await response

    async def reset(self) -> None:
        """End the mission and discard queued, active, and backend conversation state."""
        self._session_ready.clear()
        self._mission_active = False
        self._wake_policy.reset()
        current = asyncio.current_task()
        active_tasks = self._message_tasks | self._event_message_tasks
        for task in tuple(active_tasks):
            if task is not current:
                task.cancel()
        if active_tasks:
            await asyncio.gather(
                *(task for task in active_tasks if task is not current),
                return_exceptions=True,
            )
        while True:
            try:
                pending = self._events.get_nowait()
            except asyncio.QueueEmpty:
                break
            if pending is None:
                await self._events.put(None)
                break
            if not pending.response.done():
                pending.response.set_exception(RuntimeError("operator task was discarded by mission reset"))
        try:
            if self.backend is not None:
                await self.backend.reset()
        finally:
            self._session_ready.set()

    async def emergency(self, action: str, *, session: Any = None) -> str:
        """Execute an emergency MCP tool directly, independently of Claude/API work.

        ``session`` is the caller's own MCP session, used until the commander's is connected, so
        an emergency never waits for commander startup.
        """
        if action not in {"hold", "land", "rth"}:
            raise ValueError("emergency action must be hold, land, or rth")
        mcp_session = self._mcp_session if self._mcp_session is not None else session
        if mcp_session is None:
            raise RuntimeError("commander MCP session is not ready")
        # Operator requests, event batches and notices not yet sent to the model are now stale.
        self._emergency_generation += 1
        generation = self._emergency_generation
        backend = self.backend
        # Interrupt the active model turn alongside, never ahead of, the emergency tool call.
        interrupting = None if backend is None else asyncio.create_task(backend.interrupt())
        try:
            result = await mcp_session.call_tool(action, {})
        except BaseException:
            if interrupting is not None:
                await asyncio.gather(interrupting, return_exceptions=True)
            raise
        if interrupting is not None:
            await interrupting
        raise_for_error(result, action)
        result_text = tool_text(result) or "completed"
        if backend is not None and self._mission_active:
            notice = f"[operator emergency] {action} executed: {result_text}"
            task = asyncio.create_task(self._send_emergency_notice(backend, notice, generation))
            self._track_task(task, self._event_message_tasks)
        return result_text

    async def _send_emergency_notice(
        self, backend: CommanderBackend, text: str, generation: int
    ) -> None:
        try:
            await self._session_ready.wait()
            if (
                not self._mission_active
                or self.backend is not backend
                or generation != self._emergency_generation
            ):
                return
            await backend.send(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._append_event({"kind": "session_message_error", "error": str(exc)})

    async def stop(self) -> None:
        self._stopped.set()
        await self._events.put(None)

    async def run(self) -> None:
        """Serve one MCP connection and mission backend until stopped."""
        try:
            async with open_session(self.server_url, self.bearer_token) as session:
                system_prompt = _mcp_prompt_text(await session.get_prompt("horus_commander"))
                if self.settings.provider != "claude-code":
                    tools_response = await session.list_tools()
                    self.backend = APICommanderBackend(
                        self.settings.provider,
                        self.settings,
                        self.api_key or "",
                        session,
                        list(tools_response.tools),
                        system_prompt,
                    )
                else:
                    self.backend = ClaudeCodeSession(
                        self.server_url,
                        self.bearer_token,
                        self.settings,
                        system_prompt,
                    )
                self._mcp_session = session
                record_task = asyncio.create_task(self._forward_backend_records(self.backend))
                event_task = asyncio.create_task(self._pump_events(session))
                try:
                    while not self._stopped.is_set():
                        event = await self._events.get()
                        if event is None or self._stopped.is_set():
                            break
                        task = asyncio.create_task(self._process_message(event))
                        self._track_task(task, self._message_tasks)
                finally:
                    event_task.cancel()
                    for task in tuple(self._message_tasks | self._event_message_tasks):
                        task.cancel()
                    await asyncio.gather(
                        event_task,
                        *self._message_tasks,
                        *self._event_message_tasks,
                        return_exceptions=True,
                    )
                    record_task.cancel()
                    await asyncio.gather(record_task, return_exceptions=True)
                    await self.backend.close()
                    self.backend = None
                    self._mcp_session = None
        except Exception as exc:
            self._failure = exc
            while True:
                try:
                    pending = self._events.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if pending is not None and not pending.response.done():
                    pending.response.set_exception(
                        RuntimeError("commander MCP session failed before processing the request")
                    )
            raise

    async def _process_message(self, event: CommanderEvent) -> None:
        try:
            await self._session_ready.wait()
            if event.generation != self._emergency_generation:
                # Queued before an operator emergency (e.g. during commander startup): never send it.
                raise RuntimeError("operator task was interrupted by an emergency")
            backend = self.backend
            if backend is None:
                raise RuntimeError("commander backend is not ready")
            self._mission_active = True
            result = await backend.send(f"[operator] {event.text.strip()}")
            if not event.response.done():
                event.response.set_result(result)
        except asyncio.CancelledError:
            if not event.response.done():
                event.response.set_exception(RuntimeError("operator task was interrupted"))
            raise
        except Exception as exc:
            if not event.response.done():
                event.response.set_exception(exc)

    async def _forward_backend_records(self, backend: CommanderBackend) -> None:
        async for event in backend.stream_records():
            self._append_event(event)

    async def _pump_events(self, session: Any) -> None:
        cursor = 0
        pending: list[Mapping[str, Any]] = []
        last_flush = float("-inf")
        loop = asyncio.get_running_loop()
        while True:
            timeout_s = 25.0
            if pending:
                timeout_s = min(timeout_s, max(0.0, last_flush + 1.0 - loop.time()))
                if timeout_s == 0:
                    self._forward_event_batch(pending)
                    pending.clear()
                    last_flush = loop.time()
                    continue
            try:
                payload = await call_json(
                    session, "wait_events", {"after_id": cursor, "timeout_s": timeout_s}
                )
                if not isinstance(payload, Mapping):
                    raise ValueError("MCP wait_events response must be a JSON object")
                for event in payload.get("events", []):
                    if not isinstance(event, Mapping):
                        continue
                    event_id = event.get("id")
                    if isinstance(event_id, int):
                        cursor = max(cursor, event_id)
                    kind = event.get("kind")
                    if kind not in EVENT_KINDS:
                        continue
                    mirrored = {
                        "kind": kind,
                        "source": "mcp-event",
                        "mcp_event_id": event_id,
                        "time": event.get("time"),
                        "summary": event.get("summary", ""),
                        "data": event.get("data", {}),
                    }
                    self._append_event(mirrored)
                    if kind not in MODEL_WAKE_EVENT_KINDS:
                        continue
                    if kind == "emergency" and _event_source(event) in _ECHOED_EMERGENCY_SOURCES:
                        # An MCP-requested emergency was sent by this commander (which already
                        # sends its own notice) or by the model itself; a plan reports through
                        # plan_finished. Waking the model again would cost a second turn.
                        continue
                    if self._wake_policy.admit(event, loop.time()):
                        pending.append(event)
                next_id = payload.get("last_id")
                if isinstance(next_id, int):
                    cursor = max(cursor, next_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._append_event({"kind": "event_pump_error", "error": str(exc)})
                await asyncio.sleep(1.0)
                continue
            if pending and loop.time() - last_flush >= 1.0:
                self._forward_event_batch(pending)
                pending.clear()
                last_flush = loop.time()

    def _forward_event_batch(self, events: list[Mapping[str, Any]]) -> None:
        if not self._mission_active or self.backend is None:
            return
        lines = [
            f"[HORUS event #{event.get('id')} {event.get('time', '')}] "
            f"{event.get('kind', '')}: {event.get('summary', '')}"
            for event in events
        ]
        suppressed = self._wake_policy.drain_suppressed()
        if suppressed:
            lines.append(suppressed)
        task = asyncio.create_task(self._send_event_message("\n".join(lines)))
        self._track_task(task, self._event_message_tasks)

    async def _send_event_message(self, text: str) -> None:
        try:
            await self._session_ready.wait()
            backend = self.backend
            if not self._mission_active or backend is None:
                return
            await backend.send(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._append_event({"kind": "event_message_error", "error": str(exc)})

    @staticmethod
    def _track_task(task: asyncio.Task[None], tasks: set[asyncio.Task[None]]) -> None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _append_event(self, event: Mapping[str, Any]) -> None:
        if self.event_log is None and self.event_callback is None:
            return
        provider = self.settings.provider
        record = redact_secret(
            {"source": provider, **dict(event)},
            self.bearer_token,
        )
        if self.event_log is not None:
            try:
                self.event_log.append(record)
            except (OSError, TypeError) as exc:
                print(f"warning: could not append commander event log: {exc}", file=sys.stderr)
        if self.event_callback is not None:
            try:
                self.event_callback(_safe_value(record))
            except Exception as exc:
                print(f"warning: commander event callback failed: {exc}", file=sys.stderr)


def _event_log_from_environment() -> JsonlEventLog | None:
    try:
        path = event_log_path_from_environment()
    except RuntimeError:
        return None
    return JsonlEventLog(path)


def _mcp_prompt_text(result: Any) -> str:
    texts: list[str] = []
    for message in getattr(result, "messages", []):
        content = getattr(message, "content", None)
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            text = block if isinstance(block, str) else getattr(block, "text", None)
            if isinstance(text, str) and text:
                texts.append(text)
    if not texts:
        raise RuntimeError("MCP prompt horus_commander returned no text instructions")
    return "\n".join(texts)

