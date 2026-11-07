"""Mission backends of the HORUS commander: one persistent Claude Code stream-json session, or
an Anthropic/OpenAI tools conversation, each driving the HORUS MCP tools.

:class:`~horus.commander.agent.HorusCommander` picks one per mission through
:class:`CommanderBackend`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from horus.commander.ai_views import decode_image
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
# How long after a turn's result a message written during that turn waits for Claude Code to
# start a new turn with it; without one, the message was folded into the finished turn.
_FOLDED_MESSAGE_GRACE_S = 2.0
# HORUS MCP tools no backend offers the model: the commander's own event pump owns wait_events,
# and the operator's emergency lock tools and the display map reads belong to the control apps.
MODEL_DISALLOWED_TOOLS = frozenset({
    "wait_events", "operator_emergency", "operator_release", "get_map_index", "get_map_tile", "get_live_map",
})
# Claude Code names the HORUS MCP tools mcp__horus__<tool>.
_HORUS_TOOL_PREFIX = "mcp__horus__"
# tool_use ids kept to name the tool behind an image result.
_TOOL_NAMES_KEPT = 256


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
        # The messages written to Claude Code and not yet answered: (write number, future), in
        # write order. Each turn (one system/init, one result) starts with the oldest queued
        # message; a message that arrives during a turn is folded into it at its next tool call,
        # or, after the last one, waits and starts the next turn. So results do not map one to
        # one onto messages.
        self._pending: deque[tuple[int, asyncio.Future[str]]] = deque()
        self._written = 0
        # The message the current turn started with (None when its init came before any
        # message), and the inits read so far.
        self._turn_initiator: int | None = None
        self._turns = 0
        self._init_since_result = False
        self._settle_tasks: set[asyncio.Task[None]] = set()
        # When Claude Code last wrote anything (loop time). commander.timeout_s bounds silence, not
        # a whole response: a long mission keeps streaming tool calls and results for many minutes.
        self._last_output = 0.0
        self._records: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._has_started = False
        self._restart_pending = False
        self._closing = False
        # tool_use id -> tool name, to name the tool whose result carries an image.
        self._tool_names: dict[str, str] = {}

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
            self._written += 1
            self._pending.append((self._written, response))
            self._last_output = loop.time()
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
            while True:
                quiet_for = loop.time() - self._last_output
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(response), timeout=max(0.0, self.settings.timeout_s - quiet_for)
                    )
                except asyncio.TimeoutError:
                    if loop.time() - self._last_output < self.settings.timeout_s:
                        continue  # Claude Code is still working: it wrote something meanwhile
                    raise
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
                f"Claude Code wrote nothing for timeout_s={self.settings.timeout_s:g}"
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
        for task in tuple(self._settle_tasks):
            task.cancel()
        self._init_since_result = False
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
        self._tool_names.clear()
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
                self._last_output = asyncio.get_running_loop().time()
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
                    elif record["kind"] == "tool_use":
                        self._remember_tool(record)
                    elif record["kind"] == "ai_view":
                        # Only images a HORUS tool returned to the model count as what the AI saw.
                        name = self._tool_names.get(str(record.get("tool_use_id")), "")
                        if not name.startswith(_HORUS_TOOL_PREFIX):
                            continue
                        record["tool"] = name.removeprefix(_HORUS_TOOL_PREFIX)
                    self._record(record)
                    _print_stream_record(record)
                if event.get("type") == "system" and event.get("subtype") == "init":
                    # A turn starts with the oldest message no earlier turn answered.
                    self._turns += 1
                    self._turn_initiator = self._pending[0][0] if self._pending else None
                    self._init_since_result = True
                if event.get("type") == "result":
                    if event.get("subtype", "success") != "success":
                        failure = RuntimeError(
                            f"Claude Code turn ended with {event.get('subtype', 'unknown')}"
                        )
                    self._resolve_turn(str(event.get("result", "")), failure)
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

    def _remember_tool(self, record: Mapping[str, Any]) -> None:
        tool_use_id = record.get("tool_use_id")
        if not isinstance(tool_use_id, str):
            return
        self._tool_names[tool_use_id] = str(record.get("tool", ""))
        while len(self._tool_names) > _TOOL_NAMES_KEPT:
            self._tool_names.pop(next(iter(self._tool_names)))

    def _resolve_turn(self, text: str, error: Exception | None) -> None:
        """Answer the message a finished turn started with, and later the ones it folded in."""
        started = [
            entry for entry in self._pending
            if self._init_since_result and entry[0] == self._turn_initiator
        ] or list(self._pending)[:1]  # no init seen for this turn: the oldest message
        self._init_since_result = False
        for entry in started:
            self._settle(entry, text, error)
        written, turns = self._written, self._turns
        if any(seq <= written for seq, _ in self._pending):
            task = asyncio.create_task(self._settle_folded(written, turns, text, error))
            self._settle_tasks.add(task)
            task.add_done_callback(self._settle_tasks.discard)

    async def _settle_folded(self, written: int, turns: int, text: str, error: Exception | None) -> None:
        # Messages written before the result either started a new turn right away (its result
        # answers them) or were folded into the finished one: answer those with its result.
        await asyncio.sleep(_FOLDED_MESSAGE_GRACE_S)
        if self._turns != turns:
            return
        for entry in [entry for entry in self._pending if entry[0] <= written]:
            self._settle(entry, text, error)

    def _settle(self, entry: tuple[int, asyncio.Future[str]], text: str, error: Exception | None) -> None:
        try:
            self._pending.remove(entry)
        except ValueError:
            return
        response = entry[1]
        if response.done():
            return
        if error is not None:
            response.set_exception(error)
        else:
            response.set_result(text)

    def _fail_pending(self, error: Exception) -> None:
        while self._pending:
            _, response = self._pending.popleft()
            if not response.done():
                response.set_exception(error)

    def _remove_pending(self, response: asyncio.Future[str]) -> None:
        for entry in tuple(self._pending):
            if entry[1] is response:
                self._pending.remove(entry)

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


OMP_STREAM_LINE_LIMIT = 32 * 1024 * 1024
# Protocol v2 reassembles one logical stdout frame from rpc_chunk frames; the ready frame
# advertises this cap.
OMP_REASSEMBLED_FRAME_LIMIT = 64 * 1024 * 1024
# How long the process may take to print its ready frame, and how long the HORUS MCP tools may
# take to register before the backend refuses to start.
_OMP_READY_TIMEOUT_S = 60.0
_OMP_TOOL_SURFACE_TIMEOUT_S = 20.0
# omp mounts MCP tools as mcp__<server>_<tool>.
_OMP_HORUS_TOOL_PREFIX = "mcp__horus_"
# omp's own intent metadata travels in the tool arguments as "i"; it is stripped before the
# MCP server is called and must not read as a drone-board argument.
_OMP_INTENT_ARG = "i"


class OmpSession:
    """One persistent ``omp --mode rpc`` process for the current mission."""

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
        self._ready: asyncio.Future[Mapping[str, Any]] | None = None
        self._responses: dict[str, asyncio.Future[Mapping[str, Any]]] = {}
        # Prompt id -> the answer future of its `send`. omp serializes turns per session; one
        # prompt runs at a time, and `send` holds _send_lock for its whole turn.
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._turn_texts: dict[str, list[str]] = {}
        self._turn_counts: dict[str, int] = {}
        self._turn_errors: dict[str, str] = {}
        self._active_prompt: str | None = None
        self._turn_limit_hit = False
        self._chunks: dict[str, dict[str, Any]] = {}
        # tool call id -> HORUS tool name, to name the tool behind an image result.
        self._tool_names: dict[str, str] = {}
        self._last_output = 0.0
        self._records: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()
        self._start_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
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
            loop = asyncio.get_running_loop()
            self._temp_dir = tempfile.TemporaryDirectory(prefix="horus-omp-")
            root = Path(self._temp_dir.name)
            project = root / ".omp"
            project.mkdir()
            mcp_path = project / "mcp.json"
            mcp_config = {
                "mcpServers": {
                    "horus": {
                        "type": "http",
                        "url": self.server_url,
                        "headers": {"Authorization": f"Bearer {self.bearer_token}"},
                    }
                }
            }
            mcp_path.write_text(json.dumps(mcp_config), encoding="utf-8")
            mcp_path.chmod(0o600)
            overlay_path = root / "config.yml"
            overlay_path.write_text(
                "tools:\n"
                "  xdev: false\n"
                "  approvalMode: yolo\n"
                "  approval:\n"
                + "".join(
                    f"    {_omp_mcp_tool_name(name)}: deny\n"
                    for name in sorted(MODEL_DISALLOWED_TOOLS)
                )
                + "mcp:\n"
                "  enableProjectConfig: true\n"
                "disabledProviders:\n"
                + "".join(f"  - {provider}\n" for provider in (
                    "claude", "claude-plugins", "codex", "gemini", "opencode",
                    "cursor", "windsurf", "cline", "github", "vscode",
                )),
                encoding="utf-8",
            )
            overlay_path.chmod(0o600)
            prompt_path = root / "system.md"
            prompt_path.write_text(self.system_prompt, encoding="utf-8")
            prompt_path.chmod(0o600)
            command = [
                self.settings.omp_command,
                "--mode",
                "rpc",
                "--no-ui",
                "--no-session",
                "--no-tools",
                "--no-extensions",
                "--no-skills",
                "--no-rules",
                "--no-context-files",
                "--config",
                str(overlay_path),
                "--system-prompt",
                str(prompt_path),
            ]
            if self.settings.omp_model:
                command += ["--model", self.settings.omp_model]
            if self.settings.omp_thinking:
                command += ["--thinking", self.settings.omp_thinking]
            self._record({
                "kind": "omp_started",
                "timeout_s": self.settings.timeout_s,
                "max_turns": self.settings.max_turns,
                "model": self.settings.omp_model,
                "thinking": self.settings.omp_thinking,
            })
            self._ready = loop.create_future()
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._temp_dir.name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=OMP_STREAM_LINE_LIMIT,
            )
            self._process = process
            self._reader_task = asyncio.create_task(self._read_stdout(process))
            self._stderr_task = asyncio.create_task(
                _forward_stderr(process.stderr, self.bearer_token, label="omp")
            )
            try:
                try:
                    ready = await asyncio.wait_for(
                        asyncio.shield(self._ready), _OMP_READY_TIMEOUT_S
                    )
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        f"OMP wrote no ready frame within {_OMP_READY_TIMEOUT_S:g} s"
                    ) from exc
                versions = ready.get("supportedProtocolVersions")
                if isinstance(versions, list) and 2 in versions:
                    try:
                        await self._command(
                            {"type": "negotiate_protocol", "protocolVersion": 2}, timeout=10.0
                        )
                    except (RuntimeError, TimeoutError):
                        pass  # keep the v1 framing; it truncates only oversized frames
                await self._verify_tool_surface()
            except BaseException:
                await self._kill_process()
                raise
            if self._restart_pending:
                self._record({"kind": "session_restarted"})
                self._restart_pending = False
            self._has_started = True
            return process

    async def _verify_tool_surface(self) -> None:
        """Refuse a session whose only top-level tools are not the HORUS drone board's."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _OMP_TOOL_SURFACE_TIMEOUT_S
        while True:
            try:
                response = await self._command({"type": "get_state"}, timeout=5.0)
            except (RuntimeError, TimeoutError) as exc:
                raise RuntimeError(
                    "OMP did not answer get_state; cannot verify its tool surface"
                ) from exc
            data = response.get("data")
            dump = data.get("dumpTools") if isinstance(data, Mapping) else None
            if dump is None:
                # Older protocol: no tool listing. The isolation flags still apply.
                self._record({"kind": "omp_tool_surface_unknown"})
                return
            names = [
                str(tool.get("name"))
                for tool in dump
                if isinstance(tool, Mapping) and isinstance(tool.get("name"), str)
            ]
            foreign = [name for name in names if not name.startswith(_OMP_HORUS_TOOL_PREFIX)]
            if foreign:
                raise RuntimeError(
                    "OMP must load only the HORUS drone-board tools; it also loaded: "
                    + ", ".join(sorted(foreign))
                )
            if names:
                return
            if loop.time() >= deadline:
                raise RuntimeError(
                    "OMP loaded no HORUS drone-board tools; check the MCP server URL and token"
                )
            await asyncio.sleep(0.2)

    async def _command(
        self, frame: Mapping[str, Any], *, timeout: float
    ) -> Mapping[str, Any]:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise RuntimeError("OMP session exited")
        loop = asyncio.get_running_loop()
        request_id = uuid4().hex
        request = {"id": request_id, **dict(frame)}
        response: asyncio.Future[Mapping[str, Any]] = loop.create_future()
        self._responses[request_id] = response
        try:
            await self._write(request)
            answer = await asyncio.wait_for(asyncio.shield(response), timeout)
        finally:
            self._responses.pop(request_id, None)
        if not answer.get("success"):
            raise RuntimeError(
                f"OMP {frame.get('type')} failed: {answer.get('error', 'unknown error')}"
            )
        return answer

    async def _write(self, frame: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise BrokenPipeError("OMP session exited")
        payload = (json.dumps(dict(frame), ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            if process.returncode is not None or process.stdin is None:
                raise BrokenPipeError("OMP session exited")
            process.stdin.write(payload)
            await process.stdin.drain()

    async def send(self, text: str) -> str:
        async with self._send_lock:
            await self._ensure_started()
            process = self._process
            if process is None or process.returncode is not None or process.stdin is None:
                raise RuntimeError("OMP session exited before receiving the message")
            loop = asyncio.get_running_loop()
            prompt_id = uuid4().hex
            response: asyncio.Future[str] = loop.create_future()
            self._pending[prompt_id] = response
            self._turn_texts[prompt_id] = []
            self._turn_counts[prompt_id] = 0
            self._turn_errors.pop(prompt_id, None)
            self._active_prompt = prompt_id
            self._turn_limit_hit = False
            self._last_output = loop.time()
            try:
                try:
                    await self._write({"id": prompt_id, "type": "prompt", "message": text})
                except (BrokenPipeError, ConnectionResetError) as exc:
                    self._remove_pending(prompt_id)
                    raise RuntimeError("OMP session exited while sending a message") from exc
                while True:
                    quiet_for = loop.time() - self._last_output
                    try:
                        return await asyncio.wait_for(
                            asyncio.shield(response),
                            timeout=max(0.0, self.settings.timeout_s - quiet_for),
                        )
                    except asyncio.TimeoutError:
                        if loop.time() - self._last_output < self.settings.timeout_s:
                            continue  # omp is still working: it wrote something meanwhile
                        raise
            except asyncio.CancelledError:
                self._remove_pending(prompt_id)
                raise
            except asyncio.TimeoutError as exc:
                self._record({"kind": "omp_timeout", "timeout_s": self.settings.timeout_s})
                await self._abort_quietly()
                await self.reset()
                raise TimeoutError(
                    f"OMP wrote nothing for timeout_s={self.settings.timeout_s:g}"
                ) from exc
            finally:
                if self._active_prompt == prompt_id:
                    self._active_prompt = None
                self._turn_texts.pop(prompt_id, None)
                self._turn_counts.pop(prompt_id, None)
                self._turn_errors.pop(prompt_id, None)

    async def interrupt(self) -> None:
        await self._abort_quietly()

    async def _abort_quietly(self) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            return
        try:
            await self._write({"type": "abort"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def reset(self) -> None:
        self._fail_pending(RuntimeError("OMP mission was reset"))
        self._active_prompt = None
        self._turn_texts.clear()
        self._turn_counts.clear()
        self._turn_errors.clear()
        self._tool_names.clear()
        process = self._process
        if process is None or process.returncode is not None:
            self._cleanup_temp_dir()
            return
        try:
            response = await self._command({"type": "new_session"}, timeout=10.0)
            if (response.get("data") or {}).get("cancelled") is True:
                raise RuntimeError("OMP refused to start a new session")
        except (RuntimeError, TimeoutError) as exc:
            self._record({"kind": "omp_reset_failed", "error": str(exc)})
            await self._kill_process()

    async def _kill_process(self) -> None:
        process = self._process
        reader = self._reader_task
        self._closing = True
        self._process = None
        self._reader_task = None
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if reader is not None and reader is not asyncio.current_task():
            await asyncio.gather(reader, return_exceptions=True)
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        self._cleanup_temp_dir()
        self._closing = False
        self._has_started = False
        self._restart_pending = False

    async def close(self) -> None:
        self._fail_pending(RuntimeError("OMP session closed"))
        await self._kill_process()
        self._records.put_nowait(None)

    async def _read_stdout(self, process: asyncio.subprocess.Process) -> None:
        failure: Exception | None = None
        try:
            assert process.stdout is not None
            while line := await process.stdout.readline():
                self._last_output = asyncio.get_running_loop().time()
                raw = line.decode("utf-8", errors="replace").strip()
                if self.bearer_token:
                    raw = raw.replace(self.bearer_token, "[REDACTED]")
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    self._record({"kind": "unparsed_output", "text": raw[:500]})
                    print(f"[omp] {raw}", flush=True)
                    continue
                if not isinstance(event, Mapping):
                    continue
                if event.get("type") == "rpc_chunk":
                    event = self._take_chunk(event)
                    if event is None:
                        continue
                elif self._chunks:
                    self._chunks.clear()
                    self._record({
                        "kind": "omp_frame_error",
                        "error": "rpc_chunk sequence interrupted",
                    })
                self._handle_event(event, process)
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
                    failure or RuntimeError("OMP session exited unexpectedly")
                )
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(
                    failure or RuntimeError("OMP session exited before its ready frame")
                )
            for response in tuple(self._responses.values()):
                if not response.done():
                    response.set_exception(
                        failure or RuntimeError("OMP session exited before answering")
                    )
            self._chunks.clear()
            if self._process is process:
                self._process = None
                self._reader_task = None
            return_code = await process.wait()
            self._record({"kind": "omp_finished", "return_code": return_code})

    def _take_chunk(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        chunk_id = event.get("chunkId")
        index = event.get("index")
        count = event.get("count")
        byte_length = event.get("byteLength")
        data = event.get("data")
        if (
            not isinstance(chunk_id, str)
            or not isinstance(index, int)
            or not isinstance(count, int)
            or not isinstance(byte_length, int)
            or not isinstance(data, str)
            or index < 0
            or count < 1
            or index >= count
            or byte_length < 0
            or byte_length > OMP_REASSEMBLED_FRAME_LIMIT
        ):
            self._record({"kind": "omp_frame_error", "error": "malformed rpc_chunk"})
            return None
        state = self._chunks.get(chunk_id)
        if state is None:
            if index != 0:
                self._record({"kind": "omp_frame_error", "error": "rpc_chunk sequence starts late"})
                return None
            state = {"count": count, "byteLength": byte_length, "parts": {}}
            self._chunks[chunk_id] = state
        elif state["count"] != count or state["byteLength"] != byte_length:
            self._chunks.pop(chunk_id, None)
            self._record({"kind": "omp_frame_error", "error": "rpc_chunk metadata changed"})
            return None
        parts: dict[int, bytes] = state["parts"]
        if index in parts:
            self._chunks.pop(chunk_id, None)
            self._record({"kind": "omp_frame_error", "error": "duplicate rpc_chunk"})
            return None
        try:
            parts[index] = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            self._chunks.pop(chunk_id, None)
            self._record({"kind": "omp_frame_error", "error": "invalid rpc_chunk data"})
            return None
        if len(parts) < count:
            return None
        self._chunks.pop(chunk_id, None)
        if sorted(parts) != list(range(count)):
            self._record({"kind": "omp_frame_error", "error": "incomplete rpc_chunk sequence"})
            return None
        payload = b"".join(parts[i] for i in range(count))
        if len(payload) != byte_length:
            self._record({"kind": "omp_frame_error", "error": "rpc_chunk byte length mismatch"})
            return None
        try:
            assembled = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._record({"kind": "omp_frame_error", "error": "rpc_chunk payload is not JSON"})
            return None
        return assembled if isinstance(assembled, Mapping) else None

    def _handle_event(self, event: Mapping[str, Any], process: asyncio.subprocess.Process) -> None:
        if self._process is not process:
            return
        event_type = event.get("type")
        if event_type == "ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(event)
            return
        if event_type == "response":
            request_id = event.get("id")
            response = self._responses.get(request_id) if isinstance(request_id, str) else None
            if response is not None and not response.done():
                response.set_result(event)
            return
        if event_type == "prompt_result":
            self._finish_prompt(event)
            return
        prompt_id = self._active_prompt
        if event_type == "agent_start":
            if prompt_id is not None:
                self._turn_errors.pop(prompt_id, None)
            return
        if event_type == "turn_start":
            if prompt_id is not None:
                self._turn_counts[prompt_id] = self._turn_counts.get(prompt_id, 0) + 1
                if self._turn_counts[prompt_id] > self.settings.max_turns:
                    self._fail_turn_limit(prompt_id)
            return
        if event_type == "message_update":
            streamed = event.get("assistantMessageEvent")
            if (
                isinstance(streamed, Mapping)
                and streamed.get("type") == "text_delta"
                and isinstance(streamed.get("delta"), str)
            ):
                record = {"kind": "assistant_delta", "text": streamed["delta"]}
                self._record(record)
                _print_stream_record(record, label="omp")
            return
        if event_type == "message_end":
            self._message_end(event, prompt_id)
            return
        if event_type == "tool_execution_start":
            self._tool_start(event)
            return
        if event_type == "tool_execution_end":
            self._tool_end(event)
            return
        if event_type == "rpc_frame_error":
            self._record({
                "kind": "omp_frame_error",
                "error": str(event.get("error", "frame could not be delivered")),
            })
        elif event_type == "extension_error":
            self._record({"kind": "omp_error", "error": str(event.get("error", ""))})

    def _message_end(self, event: Mapping[str, Any], prompt_id: str | None) -> None:
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            return
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "text":
                    text = str(block.get("text", ""))
                    if text:
                        if prompt_id is not None:
                            self._turn_texts.setdefault(prompt_id, []).append(text)
                        record = {"kind": "assistant_text", "text": text}
                        self._record(record)
                        _print_stream_record(record, label="omp")
                elif block.get("type") == "thinking":
                    text = str(block.get("thinking", ""))
                    if text:
                        record = {"kind": "thinking", "text": text}
                        self._record(record)
                        _print_stream_record(record, label="omp")
        if message.get("stopReason") == "error" and prompt_id is not None:
            self._turn_errors[prompt_id] = str(message.get("errorMessage", ""))

    def _tool_start(self, event: Mapping[str, Any]) -> None:
        tool = str(event.get("toolName", ""))
        tool_use_id = event.get("toolCallId")
        if isinstance(tool_use_id, str):
            self._tool_names[tool_use_id] = tool
            while len(self._tool_names) > _TOOL_NAMES_KEPT:
                self._tool_names.pop(next(iter(self._tool_names)))
        args = event.get("args")
        if isinstance(args, Mapping):
            args = {key: value for key, value in args.items() if key != _OMP_INTENT_ARG}
        record = {
            "kind": "tool_use",
            "tool": tool.removeprefix(_OMP_HORUS_TOOL_PREFIX),
            "args": args if isinstance(args, Mapping) else {},
            "tool_use_id": tool_use_id,
        }
        self._record(record)
        _print_stream_record(record, label="omp")

    def _tool_end(self, event: Mapping[str, Any]) -> None:
        result = event.get("result")
        content = result.get("content") if isinstance(result, Mapping) else None
        tool_use_id = event.get("toolCallId")
        record = {
            "kind": "tool_result",
            "tool_use_id": tool_use_id,
            "summary": _summarize_tool_result(content),
            "is_error": bool(event.get("isError", False)),
        }
        self._record(record)
        _print_stream_record(record, label="omp")
        tool = self._tool_names.get(tool_use_id) if isinstance(tool_use_id, str) else ""
        if not str(tool).startswith(_OMP_HORUS_TOOL_PREFIX):
            return
        name = str(tool).removeprefix(_OMP_HORUS_TOOL_PREFIX)
        for mime_type, image in _claude_result_images(content):
            self._record({
                "kind": "ai_view",
                "tool": name,
                "tool_use_id": tool_use_id,
                "mime_type": mime_type,
                "image": image,
            })

    def _fail_turn_limit(self, prompt_id: str) -> None:
        if self._turn_limit_hit:
            return
        self._turn_limit_hit = True
        response = self._pending.pop(prompt_id, None)
        self._record({"kind": "omp_turn_limit", "max_turns": self.settings.max_turns})
        if response is not None and not response.done():
            response.set_exception(
                RuntimeError(
                    "commander reached max_turns "
                    f"({self.settings.max_turns}) consecutive MCP tool-call rounds"
                )
            )
        asyncio.create_task(self._abort_quietly())

    def _finish_prompt(self, event: Mapping[str, Any]) -> None:
        prompt_id = event.get("id")
        if not isinstance(prompt_id, str):
            return
        response = self._pending.pop(prompt_id, None)
        if response is None:
            return
        texts = [text for text in self._turn_texts.get(prompt_id, []) if text.strip()]
        final = texts[-1] if texts else ""
        status = str(event.get("status", "completed"))
        if status == "completed":
            record = {"kind": "final_result", "text": final, "subtype": status}
            self._record(record)
            _print_stream_record(record, label="omp")
            if not response.done():
                response.set_result(final)
        elif status == "aborted":
            if not response.done():
                response.set_exception(RuntimeError("OMP turn was aborted"))
        else:
            error = event.get("error")
            message = error.get("message") if isinstance(error, Mapping) else error
            message = message or self._turn_errors.get(prompt_id) or "unknown error"
            self._record({"kind": "omp_prompt_error", "error": str(message)})
            if not response.done():
                response.set_exception(RuntimeError(f"OMP turn failed: {message}"))

    def _settle(self, prompt_id: str, error: Exception) -> None:
        response = self._pending.pop(prompt_id, None)
        if response is not None and not response.done():
            response.set_exception(error)

    def _fail_pending(self, error: Exception) -> None:
        for prompt_id in list(self._pending):
            self._settle(prompt_id, error)

    def _remove_pending(self, prompt_id: str) -> None:
        self._pending.pop(prompt_id, None)

    def _record(self, event: Mapping[str, Any]) -> None:
        record = redact_secret({"source": "omp", **dict(event)}, self.bearer_token)
        self._records.put_nowait(record)

    async def stream_records(self) -> AsyncIterator[Mapping[str, Any]]:
        while True:
            record = await self._records.get()
            if record is None:
                return
            yield record

    def _cleanup_temp_dir(self) -> None:
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None


def _omp_mcp_tool_name(name: str) -> str:
    """The top-level name omp mints for a HORUS MCP tool."""
    return f"{_OMP_HORUS_TOOL_PREFIX}{name}"



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
            tool_results = []
            for call in calls:
                tool_result = await self.session.call_tool(call.name, call.input)
                tool_results.append(tool_result)
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
            for call, tool_result in zip(calls, tool_results):
                self._record_ai_views(call.name, call.id, tool_result)
        raise self._turn_limit_error()

    async def _send_openai(self, text: str) -> str:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise RuntimeError("Install horus-drone[commander-openai] for OpenAI-compatible API mode") from exc
            client: dict[str, Any] = {"api_key": self.api_key, "base_url": self.settings.base_url}
            if self.settings.extra_headers:
                client["default_headers"] = dict(self.settings.extra_headers)
            self._client = AsyncOpenAI(**client)
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
            request: dict[str, Any] = {
                "model": self.settings.model,
                "max_tokens": self.settings.max_tokens,
                "tools": tools,
                "messages": self._history,
            }
            if self.settings.reasoning_effort:
                request["reasoning_effort"] = self.settings.reasoning_effort
            response = await self._client.chat.completions.create(**request)
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
                    self._record_ai_views(call.function.name, call.id, tool_result)
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

    def _record_ai_views(self, tool: str, tool_use_id: Any, result: Any) -> None:
        """The images this tool result put in the model's history, for the control app."""
        for mime_type, data in _tool_images(result):
            image = decode_image(data)
            if image is not None:
                self._record({
                    "kind": "ai_view",
                    "tool": tool,
                    "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else None,
                    "mime_type": mime_type,
                    "image": image,
                })

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
            elif event_type == "assistant" and block_type == "thinking":
                records.append({"kind": "thinking", "text": str(block.get("thinking", ""))})
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
                # The images the model received with this result; the bytes stay in this record
                # and never reach the event stream (HorusCommander keeps them by id).
                for mime_type, image in _claude_result_images(block.get("content")):
                    records.append({
                        "kind": "ai_view",
                        "tool_use_id": block.get("tool_use_id"),
                        "mime_type": mime_type,
                        "image": image,
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


def _print_stream_record(record: Mapping[str, Any], *, label: str = "claude") -> None:
    kind = record.get("kind")
    if kind == "assistant_delta":
        print(str(record.get("text", "")), end="", flush=True)
    elif kind == "assistant_text":
        print(str(record.get("text", "")), end="", flush=True)
    elif kind == "thinking":
        print(f"\n[{label} thinking] {record.get('text', '')}", flush=True)
    elif kind == "tool_use":
        print(
            f"\n[{label} tool] {record.get('tool', '')} "
            f"{json.dumps(record.get('args', {}), ensure_ascii=False, separators=(',', ':'))}",
            flush=True,
        )
    elif kind == "tool_result":
        print(f"\n[{label} result] {record.get('summary', '')}", flush=True)
    elif kind == "final_result":
        print(f"\n[{label} final] {record.get('text', '')}", flush=True)


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


async def _forward_stderr(stream: Any, secret: str, *, label: str = "claude") -> None:
    if stream is None:
        return
    while line := await stream.readline():
        text = line.decode("utf-8", errors="replace").rstrip()
        if secret:
            text = text.replace(secret, "[REDACTED]")
        if text:
            print(f"[{label}] {text}", file=sys.stderr, flush=True)


def _tool_images(result: Any) -> list[tuple[str, str]]:
    images = []
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) != "image":
            continue
        data = getattr(block, "data", None)
        if isinstance(data, str):
            images.append((str(getattr(block, "mimeType", None) or "image/jpeg"), data))
    return images


def _claude_result_images(content: Any) -> list[tuple[str, bytes]]:
    """Decoded images of a stream-json tool_result: Anthropic ``{"type": "image", "source":
    {"type": "base64", "media_type", "data"}}`` blocks (MCP-style ``data``/``mimeType`` too)."""
    if not isinstance(content, list):
        return []
    images = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "image":
            continue
        source = block.get("source")
        if isinstance(source, Mapping):
            if source.get("type", "base64") != "base64":
                continue
            data, mime_type = source.get("data"), source.get("media_type")
        else:
            data, mime_type = block.get("data"), block.get("mimeType")
        image = decode_image(data)
        if image is not None:
            images.append((str(mime_type or "image/jpeg"), image))
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
