"""Loopback HTTP/SSE API for the host-side HORUS commander."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
from collections import deque
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

from horus.commander.agent import HorusCommander
from horus.config import load_config
from horus.mcp_server.event_log import _safe_value


DEFAULT_MCP_URL = "http://127.0.0.1:8000/mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_DEFAULT_HISTORY_SIZE = 512


class _EventHub:
    """Bounded replay history with independent wakeups for every SSE client."""

    def __init__(self, history_size: int, secret: str):
        if history_size < 1:
            raise ValueError("history_size must be positive")
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Event] = set()
        self._next_id = 0
        self._secret = secret

    def publish(self, event: Mapping[str, Any]) -> dict[str, Any]:
        safe_event = _safe_value(dict(event))
        if not isinstance(safe_event, dict):
            safe_event = {"kind": "commander_event"}
        safe_event = _replace_secret(safe_event, self._secret)
        self._next_id += 1
        safe_event["id"] = self._next_id
        self._history.append(safe_event)
        for subscriber in tuple(self._subscribers):
            subscriber.set()
        return dict(safe_event)

    def events_after(self, cursor: int) -> list[dict[str, Any]]:
        return [dict(event) for event in self._history if event["id"] > cursor]

    def subscribe(self) -> asyncio.Event:
        signal = asyncio.Event()
        self._subscribers.add(signal)
        return signal

    def unsubscribe(self, signal: asyncio.Event) -> None:
        self._subscribers.discard(signal)


def _replace_secret(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {key: _replace_secret(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_secret(item, secret) for item in value]
    return value


class CommanderASGI:
    """Small ASGI application implementing authenticated task submission and SSE."""

    def __init__(self, commander: Any, token: str, history_size: int):
        self.commander = commander
        self._expected_authorization = f"Bearer {token}".encode("utf-8")
        self.event_hub = _EventHub(history_size, token)
        self._workers: set[asyncio.Task[None]] = set()

        previous_callback = getattr(commander, "event_callback", None)

        def publish_commander_event(event: Mapping[str, Any]) -> None:
            self.event_hub.publish(event)
            if callable(previous_callback):
                previous_callback(event)

        try:
            commander.event_callback = publish_commander_event
        except (AttributeError, TypeError):
            # Injected test doubles may not expose the optional commander hook.
            pass

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        headers = _headers(scope)
        supplied = headers.get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self._expected_authorization):
            await _send_json(
                send,
                401,
                {"error": "unauthorized"},
                [(b"www-authenticate", b"Bearer")],
            )
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")
        if path == "/task":
            if method != "POST":
                await _send_json(send, 405, {"error": "method not allowed"}, [(b"allow", b"POST")])
                return
            await self._submit_task(receive, send)
            return
        if path == "/events":
            if method != "GET":
                await _send_json(send, 405, {"error": "method not allowed"}, [(b"allow", b"GET")])
                return
            cursor = _event_cursor(scope, headers)
            if cursor is None:
                await _send_json(send, 400, {"error": "invalid event cursor"})
                return
            await self._stream_events(cursor, receive, send)
            return
        await _send_json(send, 404, {"error": "not found"})

    async def _submit_task(self, receive: Any, send: Any) -> None:
        try:
            body = await _read_body(receive)
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            await _send_json(send, 400, {"error": "request body must be valid JSON"})
            return
        except _ClientDisconnected:
            return
        if not isinstance(payload, dict):
            await _send_json(send, 400, {"error": "request body must be a JSON object"})
            return
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            await _send_json(send, 400, {"error": "text must be a non-empty string"})
            return

        task_id = uuid4().hex
        self.event_hub.publish({"kind": "task_accepted", "task_id": task_id, "text": text})
        worker = asyncio.create_task(self._process_task(task_id, text))
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        await _send_json(send, 202, {"status": "accepted", "task_id": task_id})

    async def _process_task(self, task_id: str, text: str) -> None:
        try:
            result = await self.commander.submit_task(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.event_hub.publish(
                {"kind": "task_error", "task_id": task_id, "error": str(exc)}
            )
        else:
            self.event_hub.publish(
                {"kind": "task_completed", "task_id": task_id, "result": result}
            )

    async def _stream_events(self, cursor: int, receive: Any, send: Any) -> None:
        signal = self.event_hub.subscribe()
        disconnect = asyncio.create_task(_wait_for_disconnect(receive))
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream; charset=utf-8"),
                        (b"cache-control", b"no-cache"),
                        (b"connection", b"keep-alive"),
                        (b"x-accel-buffering", b"no"),
                    ],
                }
            )
            while not disconnect.done():
                signal.clear()
                available = self.event_hub.events_after(cursor)
                if available:
                    for event in available:
                        data = json.dumps(
                            event,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        frame = f"id: {event['id']}\ndata: {data}\n\n".encode("utf-8")
                        await send(
                            {"type": "http.response.body", "body": frame, "more_body": True}
                        )
                        cursor = event["id"]
                    continue

                wake = asyncio.create_task(signal.wait())
                done, _ = await asyncio.wait(
                    {wake, disconnect}, timeout=15.0, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnect in done:
                    wake.cancel()
                    await asyncio.gather(wake, return_exceptions=True)
                    return
                if wake in done:
                    continue
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
                await send(
                    {"type": "http.response.body", "body": b": keep-alive\n\n", "more_body": True}
                )
        finally:
            self.event_hub.unsubscribe(signal)
            if not disconnect.done():
                disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def create_app(
    commander: Any,
    token: str | None = None,
    *,
    history_size: int = _DEFAULT_HISTORY_SIZE,
) -> CommanderASGI:
    """Create the HTTP/SSE app; a non-empty token is always required."""
    if token is None:
        token = os.environ.get("HORUS_COMMANDER_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("HORUS_COMMANDER_TOKEN must be set to a non-empty value")
    return CommanderASGI(commander, token, history_size)


def _headers(scope: Mapping[str, Any]) -> dict[bytes, bytes]:
    result: dict[bytes, bytes] = {}
    for name, value in scope.get("headers", []):
        result[name.lower()] = value
    return result


async def _read_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _ClientDisconnected
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _wait_for_disconnect(receive: Any) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


class _ClientDisconnected(Exception):
    pass


def _event_cursor(scope: Mapping[str, Any], headers: Mapping[bytes, bytes]) -> int | None:
    raw = headers.get(b"last-event-id")
    if raw is None:
        query = parse_qs(
            scope.get("query_string", b"").decode("ascii", errors="replace"),
            keep_blank_values=True,
        )
        values = query.get("after")
        if not values:
            return 0
        value = values[-1]
    else:
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not value or not value.isascii() or not value.isdigit():
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def _send_json(
    send: Any,
    status: int,
    payload: Mapping[str, Any],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv: list[str] | None = None) -> None:
    """Start the commander MCP session and its loopback HTTP/SSE service."""
    parser = argparse.ArgumentParser(description="Run the authenticated HORUS commander service.")
    parser.add_argument("--config", help="HORUS YAML configuration path")
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    command_token = os.environ.get("HORUS_COMMANDER_TOKEN")
    if not command_token or not command_token.strip():
        parser.error("HORUS_COMMANDER_TOKEN must be set to a non-empty bearer token")
    mcp_token = os.environ.get("HORUS_MCP_TOKEN")
    if not mcp_token or not mcp_token.strip():
        parser.error("HORUS_MCP_TOKEN must be set to the HORUS MCP bearer token")

    config = load_config(args.config)
    server_url = os.environ.get("HORUS_MCP_URL") or DEFAULT_MCP_URL
    commander = HorusCommander(server_url, mcp_token, config.commander)
    app = create_app(commander, command_token)

    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "The commander service requires the uvicorn dependency from the mcp extra"
        ) from exc

    async def serve() -> None:
        server = uvicorn.Server(
            uvicorn.Config(app, host=DEFAULT_HOST, port=args.port, log_level="info")
        )
        commander_task = asyncio.create_task(commander.run())
        server_task = asyncio.create_task(server.serve())
        try:
            done, _ = await asyncio.wait(
                {commander_task, server_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if commander_task in done:
                error = commander_task.exception()
                if error is not None:
                    raise error
                raise RuntimeError("commander MCP session stopped unexpectedly")
            await server_task
        finally:
            server.should_exit = True
            await commander.stop()
            if not commander_task.done():
                commander_task.cancel()
            if not server_task.done():
                server_task.cancel()
            await asyncio.gather(commander_task, server_task, return_exceptions=True)

    asyncio.run(serve())


if __name__ == "__main__":
    main()
