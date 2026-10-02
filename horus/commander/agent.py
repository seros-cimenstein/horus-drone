"""Mission-scoped commander backends for Claude Code and provider APIs."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from horus.config import CommanderSettings
from horus.mcp_server.event_log import (
    JsonlEventLog,
    _safe_value,
    event_log_path_from_environment,
)


@dataclass(slots=True)
class CommanderEvent:
    text: str
    response: asyncio.Future[str]
    # Emergency generation at submission; an emergency discards requests not yet sent.
    generation: int = 0


class CommanderBackend(Protocol):
    """Mission backend accepting user/event messages and streaming records."""

    async def send(self, text: str) -> str: ...

    async def interrupt(self) -> None: ...

    async def reset(self) -> None: ...

    def stream_records(self) -> AsyncIterator[Mapping[str, Any]]: ...

    async def close(self) -> None: ...


CLAUDE_STREAM_LINE_LIMIT = 32 * 1024 * 1024
_EVENT_KINDS = frozenset({
    "task_finished",
    "target_lost",
    "target_reacquired",
    "link_lost",
    "link_restored",
    "auto_rth",
    "emergency",
    "landed",
    "object_detected",
    "target_found",
    "precision_landing_stalled",
    "region_enter",
    "region_exit",
    "region_dwell",
    "region_watch_paused",
    "region_watch_resumed",
    "region_watch_expired",
})
# Kinds that can repeat in bursts (flaky link, occlusions): wake the commander at most once per
# cooldown and report the suppressed count with the next forwarded batch.
_REPEAT_LIMITED_KINDS = frozenset({"link_lost", "link_restored", "target_lost", "target_reacquired"})
# Region transitions wake at most once per kind and watch per REGION_COOLDOWN_S (a busy scene
# otherwise costs a model turn per batch); list_region_watches shows the current occupancy.
# Watch pause/resume/expiry always wake: they change what the watch can report and are already
# rate-bounded by the camera-settling hysteresis and the per-watch event budget.
_REGION_REPEAT_LIMITED_KINDS = frozenset({"region_enter", "region_exit", "region_dwell"})


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
        if kind in _REGION_REPEAT_LIMITED_KINDS:
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


class ClaudeCodeSession:
    """One persistent Claude Code stream-json process for the current mission."""

    def __init__(
        self,
        server_url: str,
        bearer_token: str,
        settings: CommanderSettings,
        system_prompt: str,
    ) -> None:
        self.server_url = server_url
        self.bearer_token = bearer_token
        self.settings = settings
        self.system_prompt = system_prompt
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._config_path: Path | None = None
        self._pending: deque[asyncio.Future[str]] = deque()
        self._records: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._has_started = False
        self._restart_pending = False
        self._closing = False

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        async with self._start_lock:
            process = self._process
            if process is not None and process.returncode is None:
                return process
            self._cleanup_temp_dir()
            self._temp_dir = tempfile.TemporaryDirectory(prefix="horus-claude-mcp-")
            self._config_path = Path(self._temp_dir.name) / "mcp.json"
            config = {
                "mcpServers": {
                    "horus": {
                        "type": "http",
                        "url": self.server_url,
                        "headers": {"Authorization": f"Bearer {self.bearer_token}"},
                    }
                }
            }
            self._config_path.write_text(json.dumps(config), encoding="utf-8")
            self._config_path.chmod(0o600)
            command = [
                self.settings.claude_command,
                "--mcp-config",
                str(self._config_path),
                "--strict-mcp-config",
                "--tools",
                "",
                "--allowedTools",
                "mcp__horus__*",
                "--disallowedTools",
                "Bash",
                "Edit",
                "Write",
                "Read",
                "WebFetch",
                "mcp__horus__wait_events",
                "--append-system-prompt",
                self.system_prompt,
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--permission-mode",
                "dontAsk",
                "--max-turns",
                str(self.settings.max_turns),
            ]
            if self.settings.claude_model:
                command += ["--model", self.settings.claude_model]
            if self.settings.claude_effort:
                command += ["--effort", self.settings.claude_effort]
            self._record({
                "kind": "claude_code_started",
                "timeout_s": self.settings.timeout_s,
                "max_turns": self.settings.max_turns,
            })
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=CLAUDE_STREAM_LINE_LIMIT,
            )
            self._process = process
            self._reader_task = asyncio.create_task(self._read_stdout(process))
            self._stderr_task = asyncio.create_task(_forward_stderr(process.stderr, self.bearer_token))
            if self._restart_pending:
                self._record({"kind": "session_restarted"})
                self._restart_pending = False
            self._has_started = True
            return process

    async def send(self, text: str) -> str:
        process = await self._ensure_started()
        loop = asyncio.get_running_loop()
        response: asyncio.Future[str] = loop.create_future()
        async with self._write_lock:
            if process.returncode is not None or process.stdin is None:
                raise RuntimeError("Claude Code session exited before receiving the message")
            self._pending.append(response)
            message = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
            try:
                process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._remove_pending(response)
                raise RuntimeError("Claude Code session exited while sending a message") from exc
        try:
            return await asyncio.wait_for(asyncio.shield(response), timeout=self.settings.timeout_s)
        except asyncio.CancelledError:
            self._remove_pending(response)
            raise
        except asyncio.TimeoutError as exc:
            self._record({"kind": "claude_code_timeout", "timeout_s": self.settings.timeout_s})
            try:
                await self.interrupt()
            except (BrokenPipeError, ConnectionResetError):
                pass
            await self.reset()
            raise TimeoutError(
                f"Claude Code commander exceeded timeout_s={self.settings.timeout_s:g}"
            ) from exc

    async def interrupt(self) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            return
        request = {
            "type": "control_request",
            "request_id": uuid4().hex,
            "request": {"subtype": "interrupt"},
        }
        async with self._write_lock:
            if process.returncode is None and process.stdin is not None:
                process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                await process.stdin.drain()

    async def reset(self) -> None:
        process = self._process
        reader = self._reader_task
        self._closing = True
        self._process = None
        self._reader_task = None
        self._fail_pending(RuntimeError("Claude Code mission was reset"))
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if reader is not None and reader is not asyncio.current_task():
            await asyncio.gather(reader, return_exceptions=True)
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        self._cleanup_temp_dir()
        self._has_started = False
        self._restart_pending = False
        self._closing = False

    async def close(self) -> None:
        await self.reset()
        self._records.put_nowait(None)

    async def _read_stdout(self, process: asyncio.subprocess.Process) -> None:
        failure: Exception | None = None
        try:
            assert process.stdout is not None
            while line := await process.stdout.readline():
                raw = line.decode("utf-8", errors="replace").strip()
                if self.bearer_token:
                    raw = raw.replace(self.bearer_token, "[REDACTED]")
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    self._record({"kind": "unparsed_output", "text": raw[:500]})
                    print(f"[claude] {raw}", flush=True)
                    continue
                if not isinstance(event, Mapping):
                    continue
                records = _claude_stream_records(event)
                for record in records:
                    if record["kind"] == "mcp_init":
                        self._validate_mcp_init(record)
                    self._record(record)
                    _print_stream_record(record)
                if event.get("type") == "result":
                    if event.get("subtype", "success") != "success":
                        failure = RuntimeError(
                            f"Claude Code turn ended with {event.get('subtype', 'unknown')}"
                        )
                    self._resolve_next(str(event.get("result", "")), failure)
                    failure = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
            if process.returncode is None:
                process.kill()
        finally:
            expected = self._closing or self._process is not process
            if not expected:
                self._restart_pending = self._has_started
                self._fail_pending(
                    failure or RuntimeError("Claude Code session exited unexpectedly")
                )
            if self._process is process:
                self._process = None
                self._reader_task = None
            return_code = await process.wait()
            self._record({"kind": "claude_code_finished", "return_code": return_code})

    @staticmethod
    def _validate_mcp_init(record: Mapping[str, Any]) -> None:
        errors = record.get("mcp_server_errors", [])
        if errors:
            raise RuntimeError(f"Claude Code skipped an MCP server config: {json.dumps(errors)}")
        servers = record.get("mcp_servers")
        if servers is None:
            return
        names = {
            server.get("name")
            for server in servers
            if isinstance(server, Mapping) and isinstance(server.get("name"), str)
        } if isinstance(servers, list) else set()
        if names != {"horus"}:
            raise RuntimeError(
                "Claude Code must load only the HORUS MCP server; "
                f"loaded servers: {sorted(names)}"
            )

    def _resolve_next(self, text: str, error: Exception | None) -> None:
        while self._pending:
            response = self._pending.popleft()
            if response.done():
                continue
            if error is not None:
                response.set_exception(error)
            else:
                response.set_result(text)
            return

    def _fail_pending(self, error: Exception) -> None:
        while self._pending:
            response = self._pending.popleft()
            if not response.done():
                response.set_exception(error)

    def _remove_pending(self, response: asyncio.Future[str]) -> None:
        try:
            self._pending.remove(response)
        except ValueError:
            pass

    def _record(self, event: Mapping[str, Any]) -> None:
        record = _redact_secret({"source": "claude-code", **dict(event)}, self.bearer_token)
        self._records.put_nowait(record)

    async def stream_records(self) -> AsyncIterator[Mapping[str, Any]]:
        while True:
            record = await self._records.get()
            if record is None:
                return
            yield record

    def _cleanup_temp_dir(self) -> None:
        self._config_path = None
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None


class APICommanderBackend:
    """Anthropic/OpenAI tools backend retaining conversation history per mission."""

    def __init__(
        self,
        provider: str,
        settings: CommanderSettings,
        api_key: str,
        session: Any,
        mcp_tools: list[Any],
        system_prompt: str,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.api_key = api_key
        self.session = session
        self.mcp_tools = mcp_tools
        self.system_prompt = system_prompt
        self._lock = asyncio.Lock()
        # Active and lock-waiting sends; interrupt cancels both so no queued turn runs afterwards.
        self._sends: set[asyncio.Task[Any]] = set()
        self._records: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()
        self._history: list[dict[str, Any]] = []
        self._client: Any = None

    async def send(self, text: str) -> str:
        current = asyncio.current_task()
        if current is not None:
            self._sends.add(current)
        try:
            async with self._lock:
                # Checkpoint only once this turn owns the lock: a cancelled waiter never touches
                # another turn's history. An interrupted or failed turn is rolled back entirely so
                # the next request never carries an unanswered tool call or dangling user message.
                checkpoint = len(self._history)
                try:
                    if self.provider == "anthropic":
                        return await self._send_anthropic(text)
                    return await self._send_openai(text)
                except BaseException:
                    del self._history[checkpoint:]
                    raise
        finally:
            self._sends.discard(current)

    async def _send_anthropic(self, text: str) -> str:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("Install horus-drone[commander-anthropic] to use Anthropic") from exc
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                api_key=self.api_key,
                base_url=self.settings.base_url,
            )
        self._history.append({"role": "user", "content": text})
        tools = [
            {"name": tool.name, "description": tool.description or "", "input_schema": tool.inputSchema}
            for tool in self.mcp_tools
        ]
        for _ in range(8):
            response = await self._client.messages.create(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                system=self.system_prompt,
                tools=tools,
                messages=self._history,
            )
            self._history.append({"role": "assistant", "content": response.content})
            calls = [item for item in response.content if getattr(item, "type", None) == "tool_use"]
            for item in response.content:
                if getattr(item, "type", None) == "text":
                    self._record({"kind": "assistant_text", "text": str(item.text)})
                elif getattr(item, "type", None) == "tool_use":
                    self._record({"kind": "tool_use", "tool": item.name, "args": item.input})
            if not calls:
                result = "\n".join(
                    str(item.text) for item in response.content if getattr(item, "type", None) == "text"
                )
                self._record({"kind": "final_result", "text": result})
                return result
            results = []
            for call in calls:
                tool_result = await self.session.call_tool(call.name, call.input)
                summary = _tool_text(tool_result)
                is_error = bool(getattr(tool_result, "isError", False))
                self._record({"kind": "tool_result", "summary": summary[:500], "is_error": is_error})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": _anthropic_tool_content(tool_result),
                    "is_error": is_error,
                })
            self._history.append({"role": "user", "content": results})
        raise RuntimeError("commander reached the maximum consecutive MCP tool-call rounds")

    async def _send_openai(self, text: str) -> str:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("Install horus-drone[commander-openai] for OpenAI-compatible API mode") from exc
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.settings.base_url)
        if not self._history:
            self._history.append({"role": "system", "content": self.system_prompt})
        self._history.append({"role": "user", "content": text})
        tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            }
            for tool in self.mcp_tools
        ]
        for _ in range(8):
            response = await self._client.chat.completions.create(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                tools=tools,
                messages=self._history,
            )
            message = response.choices[0].message
            self._history.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                result = message.content or ""
                self._record({"kind": "assistant_text", "text": result})
                self._record({"kind": "final_result", "text": result})
                return result
            for call in message.tool_calls:
                arguments = json.loads(call.function.arguments or "{}")
                self._record({"kind": "tool_use", "tool": call.function.name, "args": arguments})
                tool_result = await self.session.call_tool(call.function.name, arguments)
                summary = _tool_text(tool_result)
                self._record({
                    "kind": "tool_result",
                    "summary": summary[:500],
                    "is_error": bool(getattr(tool_result, "isError", False)),
                })
                self._history.append({"role": "tool", "tool_call_id": call.id, "content": summary})
                images = _tool_images(tool_result)
                if images:
                    content: list[dict[str, Any]] = [
                        {"type": "text", "text": "Image returned by the preceding HORUS tool."}
                    ]
                    content.extend({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{data}"},
                    } for mime_type, data in images)
                    self._history.append({"role": "user", "content": content})
        raise RuntimeError("commander reached the maximum consecutive MCP tool-call rounds")

    async def interrupt(self) -> None:
        current = asyncio.current_task()
        for send in tuple(self._sends):
            if send is not current:
                send.cancel()

    async def reset(self) -> None:
        await self.interrupt()
        async with self._lock:
            self._history.clear()

    async def close(self) -> None:
        await self.interrupt()
        self._records.put_nowait(None)

    def _record(self, event: Mapping[str, Any]) -> None:
        self._records.put_nowait({"source": self.provider, **dict(event)})

    async def stream_records(self) -> AsyncIterator[Mapping[str, Any]]:
        while True:
            record = await self._records.get()
            if record is None:
                return
            yield record


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
        if settings.provider not in {"claude-code", "anthropic", "openai"}:
            raise ValueError("commander provider must be 'claude-code', 'anthropic', or 'openai'")
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
        if _is_reset_command(text):
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
        if getattr(result, "isError", False):
            raise RuntimeError(_tool_text(result) or f"MCP {action} tool failed")
        result_text = _tool_text(result)
        if not result_text:
            structured = getattr(result, "structuredContent", None)
            result_text = json.dumps(structured, ensure_ascii=False) if structured else "completed"
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
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        try:
            async with streamablehttp_client(self.server_url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    system_prompt = _mcp_prompt_text(await session.get_prompt("horus_commander"))
                    tool_specs = None
                    if self.settings.provider != "claude-code":
                        tools_response = await session.list_tools()
                        tool_specs = list(tools_response.tools)
                        self.backend = APICommanderBackend(
                            self.settings.provider,
                            self.settings,
                            self.api_key or "",
                            session,
                            tool_specs,
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
                result = await session.call_tool(
                    "wait_events",
                    {"after_id": cursor, "timeout_s": timeout_s},
                )
                payload = _tool_json(result)
                for event in payload.get("events", []):
                    if not isinstance(event, Mapping):
                        continue
                    event_id = event.get("id")
                    if isinstance(event_id, int):
                        cursor = max(cursor, event_id)
                    kind = event.get("kind")
                    if kind not in _EVENT_KINDS:
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
        record = _redact_secret(
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


def _is_reset_command(text: str) -> bool:
    return text.strip().lower().rstrip(".!?") in {"new mission", "reset"}


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


def _claude_stream_records(event: Any) -> list[dict[str, Any]]:
    if not isinstance(event, Mapping):
        return []
    event_type = event.get("type")
    records: list[dict[str, Any]] = []
    if event_type == "stream_event":
        streamed = event.get("event")
        if isinstance(streamed, Mapping):
            delta = streamed.get("delta")
            if (
                streamed.get("type") == "content_block_delta"
                and isinstance(delta, Mapping)
                and delta.get("type") == "text_delta"
            ):
                records.append({"kind": "assistant_delta", "text": str(delta.get("text", ""))})
    elif event_type in {"assistant", "user"}:
        message = event.get("message")
        content = message.get("content", []) if isinstance(message, Mapping) else []
        if not isinstance(content, list):
            return records
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if event_type == "assistant" and block_type == "text":
                records.append({"kind": "assistant_text", "text": str(block.get("text", ""))})
            elif event_type == "assistant" and block_type == "tool_use":
                records.append({
                    "kind": "tool_use",
                    "tool": str(block.get("name", "")),
                    "args": block.get("input", {}),
                    "tool_use_id": block.get("id"),
                })
            elif event_type == "user" and block_type == "tool_result":
                records.append({
                    "kind": "tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "summary": _summarize_tool_result(block.get("content")),
                    "is_error": bool(block.get("is_error", False)),
                })
    elif event_type == "result":
        record = {
            "kind": "final_result",
            "text": str(event.get("result", "")),
            "subtype": event.get("subtype", "success"),
        }
        for field in (
            "total_cost_usd",
            "usage",
            "modelUsage",
            "duration_ms",
            "duration_api_ms",
            "num_turns",
        ):
            if field in event:
                record[field] = event[field]
        records.append(record)
    elif event_type == "system" and (
        event.get("subtype") == "init" or event.get("mcp_server_errors")
    ):
        records.append({
            "kind": "mcp_init",
            "subtype": event.get("subtype"),
            "mcp_servers": event.get("mcp_servers"),
            "mcp_server_errors": event.get("mcp_server_errors", []),
        })
    return records


def _print_stream_record(record: Mapping[str, Any]) -> None:
    kind = record.get("kind")
    if kind == "assistant_delta":
        print(str(record.get("text", "")), end="", flush=True)
    elif kind == "assistant_text":
        print(str(record.get("text", "")), end="", flush=True)
    elif kind == "tool_use":
        print(
            f"\n[claude tool] {record.get('tool', '')} "
            f"{json.dumps(record.get('args', {}), ensure_ascii=False, separators=(',', ':'))}",
            flush=True,
        )
    elif kind == "tool_result":
        print(f"\n[claude result] {record.get('summary', '')}", flush=True)
    elif kind == "final_result":
        print(f"\n[claude final] {record.get('text', '')}", flush=True)


def _summarize_tool_result(content: Any) -> str:
    if isinstance(content, str):
        return content[:500]
    if not isinstance(content, list):
        return str(content)[:500] if content is not None else ""
    texts: list[str] = []
    images = 0
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            images += 1
    summary = " ".join(texts).strip()
    if images:
        summary = f"{summary} [{images} image(s), payload omitted]".strip()
    return summary[:500]


def _redact_secret(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, Mapping):
        return {str(key): _redact_secret(item, secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_secret(item, secret) for item in value]
    return value


async def _forward_stderr(stream: Any, secret: str) -> None:
    if stream is None:
        return
    while line := await stream.readline():
        text = line.decode("utf-8", errors="replace").rstrip()
        if secret:
            text = text.replace(secret, "[REDACTED]")
        if text:
            print(f"[claude] {text}", file=sys.stderr, flush=True)


def _tool_text(result: Any) -> str:
    return "\n".join(
        str(block.text) for block in getattr(result, "content", []) if getattr(block, "type", None) == "text"
    )

def _tool_images(result: Any) -> list[tuple[str, str]]:
    images = []
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) != "image":
            continue
        data = getattr(block, "data", None)
        if isinstance(data, str):
            images.append((str(getattr(block, "mimeType", None) or "image/jpeg"), data))
    return images


def _anthropic_tool_content(result: Any) -> str | list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for block in getattr(result, "content", []) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            content.append({"type": "text", "text": str(getattr(block, "text", ""))})
        elif kind == "image":
            data = getattr(block, "data", None)
            if isinstance(data, str):
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": str(getattr(block, "mimeType", None) or "image/jpeg"),
                        "data": data,
                    },
                })
    return content or _tool_text(result)


def _tool_json(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError(_tool_text(result) or "MCP tool call failed")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, Mapping):
        payload: Any = structured.get("result", structured)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise RuntimeError("MCP wait_events returned malformed JSON") from exc
        if isinstance(payload, dict):
            return payload
    text = _tool_text(result)
    if not text:
        raise RuntimeError("MCP tool returned no text metadata")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MCP tool returned malformed JSON metadata") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("MCP tool response must be a JSON object")
    return payload
