"""Mission backends of the HORUS commander: one persistent Claude Code stream-json session, or
an Anthropic/OpenAI tools conversation, each driving the HORUS MCP tools.

:class:`~horus.commander.agent.HorusCommander` picks one per mission through
:class:`CommanderBackend`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from horus.config import CommanderSettings
from horus.mcp_client import tool_text


class CommanderBackend(Protocol):
    """Mission backend accepting user/event messages and streaming records."""

    async def send(self, text: str) -> str: ...

    async def interrupt(self) -> None: ...

    async def reset(self) -> None: ...

    def stream_records(self) -> AsyncIterator[Mapping[str, Any]]: ...

    async def close(self) -> None: ...


CLAUDE_STREAM_LINE_LIMIT = 32 * 1024 * 1024
# HORUS MCP tools no backend offers the model: the commander's own event pump owns wait_events.
MODEL_DISALLOWED_TOOLS = frozenset({"wait_events"})


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
                *(f"mcp__horus__{name}" for name in sorted(MODEL_DISALLOWED_TOOLS)),
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
        record = redact_secret({"source": "claude-code", **dict(event)}, self.bearer_token)
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
        self.mcp_tools = [tool for tool in mcp_tools if tool.name not in MODEL_DISALLOWED_TOOLS]
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
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise RuntimeError("Install horus-drone[commander-anthropic] to use Anthropic") from exc
            self._client = anthropic.AsyncAnthropic(
                api_key=self.api_key,
                base_url=self.settings.base_url,
            )
        self._history.append({"role": "user", "content": text})
        tools = [
            {"name": tool.name, "description": tool.description or "", "input_schema": tool.inputSchema}
            for tool in self.mcp_tools
        ]
        for _ in range(self.settings.max_turns):
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
                summary = tool_text(tool_result)
                is_error = bool(getattr(tool_result, "isError", False))
                self._record({"kind": "tool_result", "summary": summary[:500], "is_error": is_error})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": _anthropic_tool_content(tool_result),
                    "is_error": is_error,
                })
            self._history.append({"role": "user", "content": results})
        raise self._turn_limit_error()

    async def _send_openai(self, text: str) -> str:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise RuntimeError("Install horus-drone[commander-openai] for OpenAI-compatible API mode") from exc
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
        for _ in range(self.settings.max_turns):
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
                summary = tool_text(tool_result)
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
        raise self._turn_limit_error()

    def _turn_limit_error(self) -> RuntimeError:
        return RuntimeError(
            f"commander reached max_turns ({self.settings.max_turns}) consecutive MCP tool-call rounds"
        )

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


def redact_secret(value: Any, secret: str) -> Any:
    """Replace ``secret`` in every string of a JSON-like value; keys become strings."""
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, Mapping):
        return {str(key): redact_secret(item, secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secret(item, secret) for item in value]
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
    return content or tool_text(result)
