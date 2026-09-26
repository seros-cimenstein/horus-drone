"""Bridge the host commander and MCP state into Gazebo Transport topics.

Gazebo and MCP bindings are imported only by the runtime entry point so this
module remains importable on host installations without those dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_COMMANDER_URL = "http://127.0.0.1:8765"
DEFAULT_MCP_URL = "http://127.0.0.1:8000/mcp"
DEFAULT_TASK_TOPIC = "/horus/task"
DEFAULT_EVENTS_TOPIC = "/horus/events"
DEFAULT_STATE_TOPIC = "/horus/state"
STATE_POLL_INTERVAL = 0.5
RECONNECT_INTERVAL = 1.0


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    commander_url: str = DEFAULT_COMMANDER_URL
    commander_token: str = ""
    mcp_url: str = DEFAULT_MCP_URL
    mcp_token: str = ""
    task_topic: str = DEFAULT_TASK_TOPIC
    events_topic: str = DEFAULT_EVENTS_TOPIC
    state_topic: str = DEFAULT_STATE_TOPIC

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BridgeConfig:
        """Build settings from the documented environment variables."""
        values = os.environ if environ is None else environ
        return cls(
            commander_url=values.get("HORUS_COMMANDER_URL", DEFAULT_COMMANDER_URL),
            commander_token=values.get("HORUS_COMMANDER_TOKEN", ""),
            mcp_url=values.get("HORUS_MCP_URL", DEFAULT_MCP_URL),
            mcp_token=values.get("HORUS_MCP_TOKEN", ""),
        )


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """One complete server-sent event, with its current reconnect cursor."""

    event_id: str | None
    data: str


class SSEDecoder:
    """Incrementally decode SSE lines and retain the latest valid event ID."""

    def __init__(self, last_event_id: str | None = None) -> None:
        self.last_event_id = last_event_id
        self._data: list[str] = []
        self._pending_event_id: str | None = None
        self._has_pending_event_id = False

    def feed_line(self, line: str) -> SSEEvent | None:
        """Consume a line from ``aiter_lines`` and return completed data events."""
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "id" and "\x00" not in value:
            self._pending_event_id = value
            self._has_pending_event_id = True
        # Event type and retry are intentionally ignored; this bridge forwards
        # the host's JSON payload and uses only SSE's resumable event ID.
        return None

    def _dispatch(self) -> SSEEvent | None:
        if self._has_pending_event_id:
            self.last_event_id = self._pending_event_id
        event = None
        if self._data:
            event = SSEEvent(self.last_event_id, "\n".join(self._data))
        self._data.clear()
        self._pending_event_id = None
        self._has_pending_event_id = False
        return event


def compact_json(value: Any) -> str:
    """Serialize a JSON value without insignificant whitespace."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


_PANEL_TEXT_LIMIT = 400
_HIDDEN_EVENT_KINDS = frozenset({"assistant_delta", "mcp_init", "claude_code_finished"})


def _clip(text: Any, limit: int = _PANEL_TEXT_LIMIT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def format_commander_event(event: SSEEvent) -> str | None:
    """Render one commander SSE event as a readable panel line, or ``None`` to skip it.

    Per-token ``assistant_delta`` records are dropped; the complete ``assistant_text``
    blocks carry the same text.
    """
    try:
        value = json.loads(event.data)
    except json.JSONDecodeError:
        return _clip(event.data)
    if not isinstance(value, Mapping):
        return _clip(compact_json(value))
    kind = value.get("kind")
    if kind in _HIDDEN_EVENT_KINDS:
        return None
    if kind == "task_accepted":
        return f"▶ Task: {_clip(value.get('text', ''))}"
    if kind == "claude_code_started":
        return "Claude Code started"
    if kind == "assistant_text":
        return f"Claude: {_clip(value.get('text', ''))}"
    if kind == "tool_use":
        tool = str(value.get("tool", "")).removeprefix("mcp__horus__")
        return f"→ {tool} {_clip(compact_json(value.get('args', {})), 200)}"
    if kind == "tool_result":
        marker = "✗" if value.get("is_error") else "←"
        return f"{marker} {_clip(value.get('summary', ''), 300)}"
    if kind == "final_result":
        return f"✔ Final: {_clip(value.get('text', ''), 800)}"
    if kind == "task_completed":
        return "■ Task finished"
    if kind == "task_error":
        return f"✗ Task failed: {_clip(value.get('error', ''))}"
    if kind == "claude_code_timeout":
        return f"✗ Claude Code timed out after {value.get('timeout_s')} s"
    if kind == "unparsed_output":
        return f"claude: {_clip(value.get('text', ''))}"
    return _clip(compact_json(value))


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def state_from_tool_result(result: Any) -> Any:
    """Decode the JSON result returned by MCP's ``get_state`` tool.

    FastMCP wraps a ``str`` tool return as ``{"result": "<json>"}`` structured content, so
    the JSON text block is preferred and a wrapped string is decoded.
    """
    if _field(result, "isError", False):
        raise RuntimeError("MCP get_state returned an error")

    for block in _field(result, "content", ()) or ():
        if _field(block, "type") != "text":
            continue
        text = _field(block, "text")
        if isinstance(text, str):
            return json.loads(text)
    structured = _field(result, "structuredContent")
    if isinstance(structured, Mapping) and set(structured) == {"result"}:
        wrapped = structured["result"]
        return json.loads(wrapped) if isinstance(wrapped, str) else wrapped
    if structured is not None:
        return structured
    raise ValueError("MCP get_state result contained no JSON text")


def publish_string(publisher: Any, message_type: Any, text: str) -> None:
    """Publish a string payload using a Gazebo StringMsg-compatible type."""
    message = message_type()
    message.data = text
    if publisher.publish(message) is False:
        raise RuntimeError("Gazebo Transport rejected a StringMsg publication")


async def submit_task(client: Any, config: BridgeConfig, text: str) -> None:
    """Forward a Gazebo task message to the authenticated host endpoint."""
    response = await client.post(
        f"{config.commander_url.rstrip('/')}/task",
        headers={"Authorization": f"Bearer {config.commander_token}"},
        json={"text": text},
    )
    response.raise_for_status()


async def publish_state_once(session: Any, publisher: Any, message_type: Any) -> Any:
    """Fetch and publish one decoded MCP state result."""
    result = await session.call_tool("get_state", {})
    state = state_from_tool_result(result)
    publish_string(publisher, message_type, compact_json(state))
    return state


async def stream_commander_events(
    client: Any,
    publisher: Any,
    message_type: Any,
    config: BridgeConfig,
    *,
    reconnect_delay: float = RECONNECT_INTERVAL,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Stream commander SSE events, resuming from the last complete SSE ID."""
    last_event_id: str | None = None
    url = f"{config.commander_url.rstrip('/')}/events"
    headers = {"Authorization": f"Bearer {config.commander_token}"}

    while True:
        decoder = SSEDecoder(last_event_id)
        request_headers = dict(headers)
        if last_event_id:
            request_headers["Last-Event-ID"] = last_event_id
        try:
            async with client.stream("GET", url, headers=request_headers, timeout=None) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    event = decoder.feed_line(line)
                    if event is not None:
                        panel_line = format_commander_event(event)
                        if panel_line is not None:
                            publish_string(publisher, message_type, panel_line)
                    last_event_id = decoder.last_event_id
                last_event_id = decoder.last_event_id
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Commander event stream disconnected; reconnecting")
        await sleep(reconnect_delay)


async def poll_mcp_state(
    config: BridgeConfig,
    publisher: Any,
    message_type: Any,
    *,
    session_factory: Callable[..., Any] | None = None,
    streamable_http_factory: Callable[..., Any] | None = None,
    poll_interval: float = STATE_POLL_INTERVAL,
    reconnect_delay: float = RECONNECT_INTERVAL,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Independently reconnect to MCP and publish ``get_state`` at 2 Hz."""
    if session_factory is None or streamable_http_factory is None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        session_factory = session_factory or ClientSession
        streamable_http_factory = streamable_http_factory or streamablehttp_client

    request_headers = {"Authorization": f"Bearer {config.mcp_token}"}
    while True:
        try:
            async with streamable_http_factory(config.mcp_url, headers=request_headers) as (
                read_stream,
                write_stream,
                _,
            ):
                async with session_factory(read_stream, write_stream) as session:
                    await session.initialize()
                    while True:
                        started = time.monotonic()
                        await publish_state_once(session, publisher, message_type)
                        await sleep(max(0.0, poll_interval - (time.monotonic() - started)))
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("MCP state connection failed; reconnecting")
            await sleep(reconnect_delay)


class GazeboBridge:
    """Own task subscription and event/state StringMsg publishers."""

    def __init__(
        self,
        node: Any,
        message_type: Any,
        http_client: Any,
        config: BridgeConfig,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.config = config
        self.node = node
        self.message_type = message_type
        self.http_client = http_client
        self.loop = loop
        self.events_publisher = node.advertise(config.events_topic, message_type)
        self.state_publisher = node.advertise(config.state_topic, message_type)
        if self.events_publisher is None or self.state_publisher is None:
            raise RuntimeError("Gazebo Transport could not advertise commander bridge topics")
        subscribed = node.subscribe(message_type, config.task_topic, self._on_task)
        if not subscribed:
            raise RuntimeError(f"Gazebo Transport rejected task topic {config.task_topic!r}")
        self._task_requests: set[asyncio.Task[None]] = set()

    def _on_task(self, message: Any) -> None:
        """Schedule HTTP work safely even if Gazebo invokes us off-loop."""
        text = message.data
        self.loop.call_soon_threadsafe(self._schedule_task, text)

    def _schedule_task(self, text: str) -> None:
        task = self.loop.create_task(self._submit_task(text))
        self._task_requests.add(task)
        task.add_done_callback(self._task_requests.discard)

    async def _submit_task(self, text: str) -> None:
        try:
            await submit_task(self.http_client, self.config, text)
        except Exception:
            LOGGER.exception("Could not submit task to commander service")


def _load_gazebo_bindings() -> tuple[Any, Any]:
    """Load Gazebo bindings only when starting the in-container executable."""
    try:
        from gz.msgs10.stringmsg_pb2 import StringMsg
        from gz.transport13 import Node
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Gazebo Python bindings are unavailable; install python3-gz-transport13 "
            "and python3-gz-msgs10 in the simulation container"
        ) from exc
    return Node, StringMsg


async def run(config: BridgeConfig | None = None) -> None:
    """Run the host commander and MCP bridge until cancelled."""
    import httpx

    config = config or BridgeConfig.from_env()
    if not config.commander_token:
        raise RuntimeError("HORUS_COMMANDER_TOKEN must be set before starting the Gazebo bridge")
    if not config.mcp_token:
        raise RuntimeError("HORUS_MCP_TOKEN must be set before starting the Gazebo bridge")
    node_type, message_type = _load_gazebo_bindings()
    node = node_type()
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient() as client:
        bridge = GazeboBridge(node, message_type, client, config, loop)
        await asyncio.gather(
            stream_commander_events(
                client,
                bridge.events_publisher,
                message_type,
                config,
            ),
            poll_mcp_state(config, bridge.state_publisher, message_type),
        )


def main() -> None:
    """Run the bridge as ``python -m horus.gz_bridge`` or a console script."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    logging.basicConfig(level=os.environ.get("HORUS_LOG_LEVEL", "INFO"))
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
