"""Bridge the host commander and MCP state into Gazebo Transport topics.

Gazebo and MCP bindings are imported only by the runtime entry point so this
module remains importable on host installations without those dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, fields
from typing import Any

from horus.config import ServerSettings, load_config
from horus.events import EVENT_KINDS
from horus.mcp_client import DEFAULT_MCP_URL, call_json, open_session

LOGGER = logging.getLogger(__name__)

DEFAULT_COMMANDER_URL = "http://127.0.0.1:8765"
STATE_POLL_INTERVAL = 0.5
RECONNECT_INTERVAL = 1.0


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Bridge settings; ``from_env`` reads each field from ``HORUS_<FIELD>``.

    - ``HORUS_COMMANDER_URL`` / ``HORUS_COMMANDER_TOKEN``: commander base URL and bearer token.
    - ``HORUS_MCP_URL``: MCP streamable-HTTP endpoint; its bearer token is read from the
      variable named by the HORUS config's ``server.auth_token_env`` (``mcp_token_env``).
    - ``HORUS_TASK_TOPIC``: Gazebo topic forwarded to commander ``POST /task``.
    - ``HORUS_EMERGENCY_TOPIC``: Gazebo topic forwarded to commander ``POST /emergency``.
    - ``HORUS_RESET_TOPIC``: Gazebo topic forwarded to commander ``POST /reset``.
    - ``HORUS_EVENTS_TOPIC``: Gazebo topic publishing readable commander events.
    - ``HORUS_STATE_TOPIC``: Gazebo topic publishing compact MCP ``get_state`` JSON.
    """

    commander_url: str = DEFAULT_COMMANDER_URL
    commander_token: str = ""
    mcp_url: str = DEFAULT_MCP_URL
    mcp_token: str = ""
    mcp_token_env: str = ServerSettings().auth_token_env
    task_topic: str = "/horus/task"
    emergency_topic: str = "/horus/emergency"
    reset_topic: str = "/horus/reset"
    events_topic: str = "/horus/events"
    state_topic: str = "/horus/state"

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, server: ServerSettings | None = None
    ) -> BridgeConfig:
        """Build settings from the variables documented on the class."""
        values = os.environ if environ is None else environ
        token_env = (server or ServerSettings()).auth_token_env
        settings = {
            f.name: values.get(f"HORUS_{f.name.upper()}", f.default)
            for f in fields(cls)
            if f.name not in {"mcp_token", "mcp_token_env"}
        }
        return cls(**settings, mcp_token=values.get(token_env, ""), mcp_token_env=token_env)


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
    if kind == "session_restarted":
        return "↻ Claude session restarted"
    if kind == "session_reset_requested":
        return None  # the outcome (session_reset / session_reset_error) gets the panel line
    if kind == "session_reset":
        return "↻ New mission session"
    if kind == "session_reset_error":
        return f"✗ Could not reset mission: {_clip(value.get('error', ''))}"
    if kind == "emergency":
        data = value.get("data", {})
        data = data if isinstance(data, Mapping) else {}
        action = str(value.get("action", data.get("action", ""))).upper()
        status = str(value.get("status", data.get("status", "")))
        detail = value.get("error") or value.get("result") or value.get("summary") or ""
        suffix = f": {_clip(detail, 240)}" if detail else ""
        return f"!! Emergency {action} {status}{suffix}".strip()
    if kind in EVENT_KINDS:
        return f"• {_clip(kind.replace('_', ' '))}: {_clip(value.get('summary', ''))}"
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


def publish_string(publisher: Any, message_type: Any, text: str) -> None:
    """Publish a string payload using a Gazebo StringMsg-compatible type."""
    message = message_type()
    message.data = text
    if publisher.publish(message) is False:
        raise RuntimeError("Gazebo Transport rejected a StringMsg publication")


async def publish_state_once(session: Any, publisher: Any, message_type: Any) -> Any:
    """Fetch and publish one decoded MCP state result.

    The poll is passive: GUI telemetry must not count as supervisory-link contact.
    """
    state = await call_json(session, "get_state", {"passive": True})
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
    open_mcp: Callable[[str, str], AbstractAsyncContextManager[Any]] = open_session,
    poll_interval: float = STATE_POLL_INTERVAL,
    reconnect_delay: float = RECONNECT_INTERVAL,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Independently reconnect to MCP and publish ``get_state`` at 2 Hz."""
    while True:
        try:
            async with open_mcp(config.mcp_url, config.mcp_token) as session:
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
    """Own the commander topic subscriptions and event/state StringMsg publishers."""

    def __init__(
        self,
        node: Any,
        message_type: Any,
        http_client: Any,
        config: BridgeConfig,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.config = config
        self.node = node  # Gazebo subscriptions live only as long as their node.
        self.http_client = http_client
        self.loop = loop
        self.events_publisher = node.advertise(config.events_topic, message_type)
        self.state_publisher = node.advertise(config.state_topic, message_type)
        if self.events_publisher is None or self.state_publisher is None:
            raise RuntimeError("Gazebo Transport could not advertise commander bridge topics")
        # (Gazebo topic, commander endpoint, JSON field carrying the message text or None).
        routes = (
            (config.task_topic, "/task", "text"),
            (config.emergency_topic, "/emergency", "action"),
            (config.reset_topic, "/reset", None),
        )
        for topic, path, field in routes:
            callback = functools.partial(self._on_message, path, field)
            if not node.subscribe(message_type, topic, callback):
                raise RuntimeError(f"Gazebo Transport rejected commander topic {topic!r}")
        self._task_requests: set[asyncio.Task[None]] = set()

    def _on_message(self, path: str, field: str | None, message: Any) -> None:
        """Schedule HTTP work safely even if Gazebo invokes us off-loop."""
        payload = None if field is None else {field: message.data}
        self.loop.call_soon_threadsafe(self._schedule, self._post(path, payload))

    def _schedule(self, coro: Coroutine[Any, Any, None]) -> None:
        task = self.loop.create_task(coro)
        self._task_requests.add(task)
        task.add_done_callback(self._task_requests.discard)

    async def _post(self, path: str, payload: dict[str, Any] | None) -> None:
        """POST one forwarded Gazebo message to the authenticated commander endpoint."""
        try:
            response = await self.http_client.post(
                f"{self.config.commander_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {self.config.commander_token}"},
                json=payload,
            )
            response.raise_for_status()
        except Exception:
            LOGGER.exception("Could not forward %s to commander service", path)


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


async def run(config: BridgeConfig) -> None:
    """Run the host commander and MCP bridge until cancelled."""
    import httpx

    if not config.commander_token:
        raise RuntimeError("HORUS_COMMANDER_TOKEN must be set before starting the Gazebo bridge")
    if not config.mcp_token:
        raise RuntimeError(f"{config.mcp_token_env} must be set before starting the Gazebo bridge")
    node_type, message_type = _load_gazebo_bindings()
    async with httpx.AsyncClient() as client:
        bridge = GazeboBridge(node_type(), message_type, client, config, asyncio.get_running_loop())
        await asyncio.gather(
            stream_commander_events(client, bridge.events_publisher, message_type, config),
            poll_mcp_state(config, bridge.state_publisher, message_type),
        )


def main() -> None:
    """Run the bridge as ``python -m horus.gz_bridge`` or a console script."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="HORUS YAML config naming the MCP token variable")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("HORUS_LOG_LEVEL", "INFO"))
    config = BridgeConfig.from_env(server=load_config(args.config).server)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
