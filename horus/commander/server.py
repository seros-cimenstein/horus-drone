"""Loopback HTTP/SSE API for the host-side HORUS commander, plus the control app page."""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import os
import signal
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote
from uuid import uuid4

from horus.commander.agent import HorusCommander
from horus.commander.backends import redact_secret
from horus.commander.display import DisplayReader, DisplayUnavailable
from horus.commander.intents import emergency_action, is_reset_command
from horus.config import load_config
from horus.event_log import _safe_value
from horus.logging import configure_logging, install_unhandled_exception_hooks
from horus.mcp_client import DEFAULT_MCP_URL


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_DEFAULT_HISTORY_SIZE = 512
_GRACEFUL_SHUTDOWN_S = 2.0
# The control app's static files: served without the token, so they must never carry data or
# secrets. The page reads the token from its own URL fragment and sends it on every API call. The
# manifest, service worker and icons make the page installable as the phone app.
_WEB_DIR = Path(__file__).with_name("web")
_STATIC_FILES = {
    "/": ("index.html", b"text/html; charset=utf-8"),
    "/app.js": ("app.js", b"text/javascript; charset=utf-8"),
    "/app.css": ("app.css", b"text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", b"application/manifest+json"),
    "/sw.js": ("sw.js", b"text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", b"image/svg+xml"),
    "/icon-192.png": ("icon-192.png", b"image/png"),
    "/icon-512.png": ("icon-512.png", b"image/png"),
    "/icon-maskable-512.png": ("icon-maskable-512.png", b"image/png"),
    "/scenes/indoor_sar.jpg": ("scenes/indoor_sar.jpg", b"image/jpeg"),
    "/scenes/forest_sar.jpg": ("scenes/forest_sar.jpg", b"image/jpeg"),
    "/scenes/urban_plaza.jpg": ("scenes/urban_plaza.jpg", b"image/jpeg"),
    "/scenes/procedural.jpg": ("scenes/procedural.jpg", b"image/jpeg"),
    "/scenes/gazebo.jpg": ("scenes/gazebo.jpg", b"image/jpeg"),
    "/scenes/gta.jpg": ("scenes/gta.jpg", b"image/jpeg"),
    "/scenes/direct_usb.jpg": ("scenes/direct_usb.jpg", b"image/jpeg"),
    "/scenes/companion_board.jpg": ("scenes/companion_board.jpg", b"image/jpeg"),
    "/scenes/grounder.jpg": ("scenes/grounder.jpg", b"image/jpeg"),
}
# Answers this large or larger are gzipped for clients that accept it (the phone's map).
_GZIP_MIN_BYTES = 1024
_STATIC_HEADERS = [
    (b"content-security-policy", b"default-src 'self'; img-src 'self' data: blob:"),
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]
# The pictures the AI received: /api/ai-view/<id> (bytes) and /api/ai-view/latest (metadata).
_AI_VIEW_PREFIX = "/api/ai-view/"
# An id always names the same picture, so the page may keep it for a day without asking again.
_AI_VIEW_CACHE = b"private, max-age=86400, immutable"


class _EventHub:
    """Bounded replay history with independent wakeups for every SSE client."""

    def __init__(self, history_size: int, *secrets: str):
        if history_size < 1:
            raise ValueError("history_size must be positive")
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Event] = set()
        self._next_id = 0
        self._closed = False
        self._secrets = tuple(secret for secret in secrets if secret)
        # Event ids restart at 1 with every service run; the stream sends this so a page that
        # reconnects with an old Last-Event-ID notices the restart and starts over from 0.
        self.run_id = uuid4().hex

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True
        for subscriber in tuple(self._subscribers):
            subscriber.set()

    def publish(self, event: Mapping[str, Any]) -> dict[str, Any]:
        safe_event = _safe_value(dict(event))
        if not isinstance(safe_event, dict):
            safe_event = {"kind": "commander_event"}
        for secret in self._secrets:
            safe_event = redact_secret(safe_event, secret)
        if not safe_event.get("time"):
            # Replayed history then shows when each event happened, not when a page loaded it.
            safe_event["time"] = datetime.now(timezone.utc).isoformat()
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


class CommanderASGI:
    """Small ASGI application implementing authenticated task submission and SSE."""

    def __init__(
        self, commander: Any, token: str, history_size: int, display: Any = None,
        phone_token: str | None = None,
        studio: Any = None,
    ):
        self.commander = commander
        self.display = display
        self.studio = studio
        self._token = token
        self._laptop_authorization = f"Bearer {token}".encode("utf-8")
        self._phone_authorization = (
            f"Bearer {phone_token}".encode("utf-8") if phone_token else None
        )
        # The phone app's own token is accepted next to the laptop's; restarting the service
        # without it revokes the phone.
        self._expected_authorizations = tuple(
            f"Bearer {secret}".encode("utf-8") for secret in (token, phone_token) if secret
        )
        self.event_hub = _EventHub(history_size, token, phone_token or "")
        self._workers: set[asyncio.Task[Any]] = set()
        # The map's tile index, fetched from the drone board once per version:
        # (version, JSON body, gzipped body). Tiles are not cached here: each is one passive call.
        self._map_index: tuple[str, bytes, bytes] | None = None
        self._map_index_lock = asyncio.Lock()
        self._routes: dict[str, tuple[str, Any]] = {
            "/task": ("POST", self._submit_task),
            "/emergency": ("POST", self._submit_emergency),
            "/reset": ("POST", self._reset_mission),
            "/events": ("GET", self._stream_events),
            "/api/state": ("GET", self._display_state),
            "/api/frame": ("GET", self._display_frame),
            "/api/frame/stream": ("GET", self._display_frame_stream),
            "/api/map/index": ("GET", self._display_map_index),
            "/api/map/tile": ("GET", self._display_map_tile),
            "/api/map/live": ("GET", self._display_live_map),
        }
        self._laptop_only_paths: frozenset[str] = frozenset({
            "/api/studio/bootstrap",
            "/api/studio/session/start",
            "/api/studio/session/stop",
            "/api/studio/session/restart",
            "/api/studio/settings",
            "/api/studio/ai/test",
            "/api/studio/link-shaper",
            "/api/studio/manual-move",
            "/api/studio/hardware/ports",
            "/api/studio/runs",
            "/api/studio/mcp-configs",
            "/api/studio/grounder",
            "/api/studio/grounder/start",
            "/api/studio/grounder/stop",
            "/api/studio/phone",
            "/api/studio/phone/serve",
        })
        if studio is not None:
            self._routes.update({
                "/api/studio/status": ("GET", self._studio_status),
                "/api/studio/capabilities": ("GET", self._studio_capabilities),
                "/api/studio/bootstrap": ("POST", self._studio_bootstrap),
                "/api/studio/session/start": ("POST", self._studio_session_start),
                "/api/studio/session/stop": ("POST", self._studio_session_stop),
                "/api/studio/session/restart": ("POST", self._studio_session_restart),
                "/api/studio/spectator-frame": ("GET", self._studio_spectator_frame),
                "/api/studio/spectator-stream": ("GET", self._studio_spectator_stream),
                "/api/studio/settings": ("GET|PUT", self._studio_settings),
                "/api/studio/ai/test": ("POST", self._studio_ai_test),
                "/api/studio/link-shaper": ("POST", self._studio_link_shaper),
                "/api/studio/manual-move": ("POST", self._studio_manual_move),
                "/api/studio/preflight": ("GET", self._studio_preflight),
                "/api/studio/hardware/ports": ("GET", self._studio_hardware_ports),
                "/api/studio/runs": ("GET", self._studio_runs),
                "/api/studio/mcp-configs": ("GET", self._studio_mcp_configs),
                "/api/studio/grounder": ("GET", self._studio_grounder),
                "/api/studio/grounder/start": ("POST", self._studio_grounder_start),
                "/api/studio/grounder/stop": ("POST", self._studio_grounder_stop),
                "/api/studio/phone": ("GET", self._studio_phone),
                "/api/studio/phone/serve": ("POST", self._studio_phone_serve),
            })

        previous_callback = commander.event_callback

        def publish_commander_event(event: Mapping[str, Any]) -> None:
            self.event_hub.publish(event)
            if previous_callback is not None:
                previous_callback(event)

        commander.event_callback = publish_commander_event
        if studio is not None and hasattr(studio, "publish_event"):
            if studio.publish_event is None:
                studio.publish_event = self.event_hub.publish

    def begin_shutdown(self) -> None:
        """Close active SSE streams promptly so Uvicorn shuts down without timeout errors."""
        self.event_hub.close()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        if path in _STATIC_FILES:
            await _send_static(scope, send, path)
            return
        if path == "/version":
            await _send_page_version(scope, send)
            return

        headers = _headers(scope)
        supplied = headers.get(b"authorization", b"")
        is_laptop = hmac.compare_digest(supplied, self._laptop_authorization)
        is_phone = (
            self._phone_authorization is not None
            and hmac.compare_digest(supplied, self._phone_authorization)
        )
        if not (is_laptop or is_phone):
            await _send_json(
                send,
                401,
                {"error": "unauthorized"},
                [(b"www-authenticate", b"Bearer")],
            )
            return

        route = self._routes.get(path)
        if route is None and path.startswith(_AI_VIEW_PREFIX):
            route = ("GET", self._ai_view)
        if route is None:
            await _send_json(send, 404, {"error": "not found"})
            return
        allowed, handler = route
        method = scope.get("method", "GET").upper()
        allowed_methods = allowed.split("|")
        if method not in allowed_methods:
            await _send_json(
                send,
                405,
                {"error": "method not allowed"},
                [(b"allow", ", ".join(allowed_methods).encode("ascii"))],
            )
            return
        if path in self._laptop_only_paths and not is_laptop:
            await _send_json(send, 403, {"error": "operator laptop token required"})
            return
        await handler(scope, receive, send)

    async def _submit_task(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
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
        action = emergency_action(text)
        if action is not None:
            self._start_emergency(task_id, action)
        else:
            worker = self._reset(task_id) if is_reset_command(text) else self._process_task(task_id, text)
            self._track_worker(asyncio.create_task(worker))
        await _send_json(send, 202, {"status": "accepted", "task_id": task_id})

    async def _submit_emergency(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        try:
            payload = json.loads(await _read_body(receive))
        except (UnicodeDecodeError, json.JSONDecodeError):
            await _send_json(send, 400, {"error": "request body must be valid JSON"})
            return
        except _ClientDisconnected:
            return
        action = payload.get("action") if isinstance(payload, dict) else None
        if not isinstance(action, str) or action not in {"hold", "land", "rth"}:
            await _send_json(send, 400, {"error": "action must be hold, land, or rth"})
            return
        task_id = uuid4().hex
        self._start_emergency(task_id, action)
        await _send_json(send, 202, {"status": "accepted", "task_id": task_id, "action": action})

    def _start_emergency(self, task_id: str, action: str) -> None:
        self.event_hub.publish({
            "kind": "emergency",
            "action": action,
            "source": "operator",
            "status": "requested",
        })
        self._track_worker(asyncio.create_task(self._process_emergency(task_id, action)))

    async def _reset_mission(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        error = await self._reset()
        if error is not None:
            await _send_json(send, 503, {"error": error})
            return
        await _send_json(send, 200, {"status": "reset"})

    async def _reset(self, task_id: str | None = None) -> str | None:
        """Reset the mission with one event sequence for both paths; return the error on failure."""
        ids = {} if task_id is None else {"task_id": task_id}
        self.event_hub.publish({"kind": "session_reset_requested", **ids})
        try:
            await self.commander.reset()
        except Exception as exc:
            self.event_hub.publish({"kind": "session_reset_error", **ids, "error": str(exc)})
            return str(exc)
        self.event_hub.publish({"kind": "session_reset", **ids})
        return None

    def _track_worker(self, worker: asyncio.Task[Any]) -> None:
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)

    async def _process_emergency(self, task_id: str, action: str) -> None:
        try:
            result = await self.commander.emergency(action)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.event_hub.publish({
                "kind": "emergency",
                "task_id": task_id,
                "action": action,
                "source": "operator",
                "status": "error",
                "error": str(exc),
            })
        else:
            self.event_hub.publish({
                "kind": "emergency",
                "task_id": task_id,
                "action": action,
                "source": "operator",
                "status": "executed",
                "result": result,
            })

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

    async def _stream_events(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        cursor = _event_cursor(scope, _headers(scope))
        if cursor is None:
            await _send_json(send, 400, {"error": "invalid event cursor"})
            return
        signal = self.event_hub.subscribe()
        disconnect = asyncio.create_task(_wait_for_disconnect(receive))
        wake: asyncio.Task[bool] | None = None
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
                        (b"x-horus-run", self.event_hub.run_id.encode("ascii")),
                    ],
                }
            )
            while not disconnect.done() and not self.event_hub.closed:
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

                if self.event_hub.closed:
                    break
                wake = asyncio.create_task(signal.wait())
                done, _ = await asyncio.wait(
                    {wake, disconnect}, timeout=15.0, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnect in done:
                    wake.cancel()
                    await asyncio.gather(wake, return_exceptions=True)
                    wake = None
                    return
                if wake in done:
                    wake = None
                    if self.event_hub.closed:
                        break
                    continue
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
                wake = None
                await send(
                    {"type": "http.response.body", "body": b": keep-alive\n\n", "more_body": True}
                )
            if self.event_hub.closed and not disconnect.done():
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            return
        finally:
            if wake is not None and not wake.done():
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            self.event_hub.unsubscribe(signal)
            if not disconnect.done():
                disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)

    async def _ai_view(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        """A picture the AI received, by id; ``latest`` answers the newest one's metadata."""
        store = getattr(self.commander, "ai_views", None)
        name = scope.get("path", "")[len(_AI_VIEW_PREFIX):]
        if name == "latest":
            view = store.latest() if store is not None else None
            if view is None:
                await _send_json(send, 404, {"error": "the AI has not looked at the camera yet"})
                return
            await _send_json(send, 200, view.metadata(), [(b"cache-control", b"no-store")])
            return
        view = store.get(name) if store is not None else None
        if view is None:
            await _send_json(send, 404, {"error": "no such AI view: unknown, or no longer kept"})
            return
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", view.mime_type.encode("ascii")),
                (b"content-length", str(len(view.data)).encode("ascii")),
                (b"cache-control", _AI_VIEW_CACHE),
                (b"x-content-type-options", b"nosniff"),
            ],
        })
        await send({"type": "http.response.body", "body": view.data})

    # Display reads for the control app: the separate, passive DisplayReader session only.
    async def _display_state(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await self._display_reply(send, lambda display: display.state())

    async def _display_frame(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await self._display_reply(send, lambda display: display.frame())

    async def _display_frame_stream(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if self.display is None:
            await _send_json(send, 503, {"error": "display reads are not configured"})
            return
        query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="replace"))
        try:
            fps = max(1, min(30, int((query.get("fps") or ["15"])[0])))
        except ValueError:
            fps = 15
        period = 1.0 / fps
        try:
            first_payload = await self.display.frame()
        except DisplayUnavailable as exc:
            await _send_json(send, 503, {"error": redact_secret(str(exc), self._token)})
            return

        disconnect = asyncio.create_task(_wait_for_disconnect(receive))
        try:
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"multipart/x-mixed-replace; boundary=horusframe"),
                    (b"cache-control", b"no-store"),
                    (b"x-accel-buffering", b"no"),
                ],
            })
            last_frame_id: Any = None
            last_sent_at = 0.0
            payload: dict[str, Any] | None = first_payload
            while not disconnect.done() and not self.event_hub.closed:
                started = time.monotonic()
                if payload is None:
                    try:
                        payload = await self.display.frame()
                    except DisplayUnavailable:
                        break
                frame_id = payload.get("frame_id")
                now = time.monotonic()
                if frame_id is None or frame_id != last_frame_id or (now - last_sent_at) >= 1.0:
                    img_b64 = payload.get("image_base64")
                    if isinstance(img_b64, str) and img_b64:
                        jpeg = base64.b64decode(img_b64)
                        meta = json.dumps(
                            {
                                "frame_id": frame_id,
                                "candidates": payload.get("candidates") or payload.get("candidate_table") or [],
                                "width": payload.get("width"),
                                "height": payload.get("height"),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        part = (
                            b"--horusframe\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            + f"Content-Length: {len(jpeg)}\r\nX-Horus-Meta: {meta}\r\n\r\n".encode("utf-8")
                            + jpeg
                            + b"\r\n"
                        )
                        await send({"type": "http.response.body", "body": part, "more_body": True})
                        last_frame_id = frame_id
                        last_sent_at = now
                payload = None
                remaining = period - (time.monotonic() - started)
                if remaining > 0:
                    try:
                        await asyncio.wait_for(asyncio.shield(disconnect), timeout=remaining)
                    except asyncio.TimeoutError:
                        pass
            if not disconnect.done():
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            return
        finally:
            if not disconnect.done():
                disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)

    async def _display_live_map(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await self._display_reply(send, lambda display: display.live_map(), gzip_ok=_accepts_gzip(scope))

    async def _display_map_index(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        # Stored by the phone: ETag/304 so it downloads once per map version, gzipped when the
        # client accepts it; the drone board is asked only whether the cached version still holds.
        if self.display is None:
            await _send_json(send, 503, {"error": "display reads are not configured"})
            return
        try:
            async with self._map_index_lock:
                cached = self._map_index
                answer = await self.display.map_index(cached[0] if cached else "")
                if not (cached and answer.get("unchanged") and answer.get("version") == cached[0]):
                    version = answer.get("version")
                    if not isinstance(version, str) or not version or answer.get("unchanged"):
                        raise DisplayUnavailable("get_map_index returned no index")
                    body = json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    cached = (version, body, gzip.compress(body, 6))
                    self._map_index = cached
        except DisplayUnavailable as exc:
            await _send_json(send, 503, {"error": redact_secret(str(exc), self._token)})
            return
        version, body, packed = cached
        await _send_versioned(scope, send, f'"{version}"', body, packed)

    async def _display_map_tile(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        # Tiles are stored by the phone too: ETag "<version>/<level>/<x>/<y>", so a revalidation
        # passes the stored version to the drone board and comes back as a 304.
        if self.display is None:
            await _send_json(send, 503, {"error": "display reads are not configured"})
            return
        tile = _tile_query(scope)
        if tile is None:
            await _send_json(send, 400, {"error": "level must be 0, 1 or 2 and x, y integers"})
            return
        suffix = "/" + "/".join(map(str, tile))
        known = ""
        for tag in _headers(scope).get(b"if-none-match", b"").decode("ascii", errors="replace").split(","):
            tag = tag.strip().strip('"')
            if tag.endswith(suffix) and len(tag) > len(suffix):
                known = tag[: -len(suffix)]
        try:
            answer = await self.display.map_tile(*tile, known)
        except DisplayUnavailable as exc:
            await _send_json(send, 503, {"error": redact_secret(str(exc), self._token)})
            return
        version = answer.get("version")
        if not isinstance(version, str) or not version:
            await _send_json(send, 503, {"error": "get_map_tile returned no tile"})
            return
        if answer.get("missing"):
            await _send_json(send, 404, {"error": "no such tile"})
            return
        body = b"" if answer.get("unchanged") else json.dumps(
            answer, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        await _send_versioned(scope, send, f'"{version}{suffix}"', body, gzip.compress(body, 6) if body else b"")

    async def _display_reply(self, send: Any, read: Any, *, gzip_ok: bool = False) -> None:
        if self.display is None:
            await _send_json(send, 503, {"error": "display reads are not configured"})
            return
        try:
            payload = await read(self.display)
        except DisplayUnavailable as exc:
            await _send_json(send, 503, {"error": redact_secret(str(exc), self._token)})
            return
        await _send_json(send, 200, payload, gzip_ok=gzip_ok)

    # Operator-only HORUS Studio runtime management routes (never exposed as MCP tools).
    async def _read_json_object(self, receive: Any, send: Any, *, allow_empty: bool = False) -> dict[str, Any] | None:
        try:
            body = await _read_body(receive)
        except _ClientDisconnected:
            return None
        if not body.strip() and allow_empty:
            return {}
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            await _send_json(send, 400, {"error": "request body must be valid JSON"})
            return None
        if not isinstance(payload, dict):
            await _send_json(send, 400, {"error": "request body must be a JSON object"})
            return None
        return payload

    async def _studio_status(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await _send_json(send, 200, self.studio.status())

    async def _studio_bootstrap(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        payload = await self._read_json_object(receive, send, allow_empty=True)
        if payload is None:
            return
        try:
            result = await self.studio.bootstrap_assets(
                include_models=bool(payload.get("include_models", True)),
                include_sitl=bool(payload.get("include_sitl", True)),
            )
        except Exception as exc:
            await _send_json(send, 500, {"error": redact_secret(str(exc), self._token)})
            return
        await _send_json(send, 200, result)

    async def _studio_session_start(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        payload = await self._read_json_object(receive, send, allow_empty=True)
        if payload is None:
            return
        try:
            result = await self.studio.start_session(payload)
        except ValueError as exc:
            await _send_json(send, 400, {"error": str(exc)})
            return
        except Exception as exc:
            await _send_json(send, 503, {"error": redact_secret(str(_unwrap_error(exc)), self._token)})
            return
        await _send_json(send, 200, result)

    async def _studio_session_stop(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await _send_json(send, 200, await self.studio.stop_session())

    async def _studio_session_restart(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        try:
            result = await self.studio.restart_session()
        except Exception as exc:
            await _send_json(send, 503, {"error": redact_secret(str(_unwrap_error(exc)), self._token)})
            return
        await _send_json(send, 200, result)

    async def _studio_spectator_frame(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="replace"))
        preset = (query.get("preset") or ["follow"])[0]
        show_tof = (query.get("tof") or ["1"])[0] not in {"0", "false", "no"}
        try:
            jpeg = await self.studio.spectator_frame(preset=preset, show_tof=show_tof)
        except Exception as exc:
            await _send_json(send, 503, {"error": str(exc)})
            return
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"image/jpeg"),
                (b"content-length", str(len(jpeg)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        })
        await send({"type": "http.response.body", "body": jpeg})

    async def _studio_spectator_stream(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="replace"))
        preset = (query.get("preset") or ["follow"])[0]
        show_tof = (query.get("tof") or ["1"])[0] not in {"0", "false", "no"}
        try:
            fps = max(1, min(30, int((query.get("fps") or ["25"])[0])))
        except ValueError:
            fps = 25
        if not self.studio.status().get("spectator_url"):
            await _send_json(send, 503, {"error": "No 3D spectator stream is active"})
            return

        disconnect = asyncio.create_task(_wait_for_disconnect(receive))
        stream_gen = self.studio.stream_spectator_mjpeg(preset=preset, show_tof=show_tof, fps=fps)
        try:
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"multipart/x-mixed-replace; boundary=horusframe"),
                    (b"cache-control", b"no-store"),
                    (b"x-accel-buffering", b"no"),
                ],
            })
            async for chunk in stream_gen:
                if disconnect.done() or self.event_hub.closed:
                    break
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            if not disconnect.done():
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            return
        except Exception:
            if not disconnect.done():
                try:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                except Exception:
                    pass
        finally:
            await stream_gen.aclose()
            if not disconnect.done():
                disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)

    async def _studio_settings(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("method", "GET").upper() == "GET":
            await _send_json(send, 200, self.studio.settings_store.public_view())
            return
        payload = await self._read_json_object(receive, send)
        if payload is None:
            return
        try:
            view = self.studio.update_settings(payload)
        except ValueError as exc:
            await _send_json(send, 400, {"error": str(exc)})
            return
        await _send_json(send, 200, view)

    async def _studio_ai_test(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await _send_json(send, 200, await self.studio.test_ai())

    async def _studio_link_shaper(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        payload = await self._read_json_object(receive, send)
        if payload is None:
            return
        try:
            updated = self.studio.link_shaper.configure(
                delay_ms=payload.get("delay_ms"),
                jitter_ms=payload.get("jitter_ms"),
                link_cut=payload.get("link_cut"),
            )
        except (TypeError, ValueError) as exc:
            await _send_json(send, 400, {"error": str(exc)})
            return
        state = {"delay_ms": updated.delay_ms, "jitter_ms": updated.jitter_ms, "link_cut": updated.link_cut}
        self.event_hub.publish({"kind": "link_shaper_updated", **state})
        await _send_json(send, 200, state)

    async def _studio_manual_move(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        payload = await self._read_json_object(receive, send)
        if payload is None:
            return
        action = payload.get("action")
        params = payload.get("params") if isinstance(payload.get("params"), Mapping) else {}
        if not isinstance(action, str) or not action.strip():
            await _send_json(send, 400, {"error": "action must be a non-empty string"})
            return
        try:
            result = await self.studio.manual_move(action.strip(), params)
        except ValueError as exc:
            await _send_json(send, 400, {"error": str(exc)})
            return
        except Exception as exc:
            await _send_json(send, 503, {"error": redact_secret(str(_unwrap_error(exc)), self._token)})
            return
        await _send_json(send, 200, result)

    async def _studio_preflight(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await _send_json(send, 200, {"items": await self.studio.preflight()})

    async def _studio_hardware_ports(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await _send_json(send, 200, {"ports": self.studio.hardware_ports()})

    async def _studio_runs(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await _send_json(send, 200, {"runs": self.studio.list_runs()})

    async def _studio_capabilities(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="replace"))
        probe = (query.get("probe") or ["1"])[0] not in {"0", "false", "no"}
        caps = await asyncio.to_thread(self.studio.capabilities, probe_external_tools=probe)
        await _send_json(send, 200, {"capabilities": caps})

    async def _studio_mcp_configs(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="replace"))
        include_token = (query.get("include_token") or ["0"])[0] in {"1", "true", "yes"}
        await _send_json(send, 200, self.studio.mcp_configs(include_token=include_token))

    async def _studio_grounder(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await _send_json(send, 200, self.studio.grounder_status())

    async def _studio_grounder_start(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        payload = await self._read_json_object(receive, send, allow_empty=True)
        if payload is None:
            return
        backend = str(payload.get("backend") or "auto")
        port = int(payload.get("port") or 8100)
        try:
            res = self.studio.start_grounder(backend=backend, port=port)
        except Exception as exc:
            await _send_json(send, 503, {"error": redact_secret(str(_unwrap_error(exc)), self._token)})
            return
        await _send_json(send, 200, res)

    async def _studio_grounder_stop(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        await _send_json(send, 200, self.studio.stop_grounder())

    async def _studio_phone(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        status = await asyncio.to_thread(self.studio.phone_status)
        await _send_json(send, 200, status)

    async def _studio_phone_serve(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        payload = await self._read_json_object(receive, send)
        if payload is None:
            return
        action = str(payload.get("action") or "status").strip().lower()
        try:
            res = await asyncio.to_thread(self.studio.phone_serve_action, action)
        except ValueError as exc:
            await _send_json(send, 400, {"error": str(exc)})
            return
        except Exception as exc:
            await _send_json(send, 503, {"error": redact_secret(str(_unwrap_error(exc)), self._token)})
            return
        await _send_json(send, 200, res)

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
    display: Any = None,
    phone_token: str | None = None,
    studio: Any = None,
) -> CommanderASGI:
    """Create the HTTP/SSE app; a non-empty token is always required.

    ``display`` (a :class:`DisplayReader`) answers the control app's ``/api/*`` reads; without
    one they return 503. ``phone_token``, when given, is accepted as a second bearer token for
    the phone app. ``studio`` attaches operator-only runtime management routes under
    ``/api/studio/*``.
    """
    if token is None:
        token = os.environ.get("HORUS_COMMANDER_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("HORUS_COMMANDER_TOKEN must be set to a non-empty value")
    if phone_token is not None and not phone_token.strip():
        phone_token = None
    return CommanderASGI(commander, token, history_size, display, phone_token, studio=studio)


def _accepts_gzip(scope: Mapping[str, Any]) -> bool:
    return b"gzip" in _headers(scope).get(b"accept-encoding", b"").lower()


async def _send_versioned(scope: Mapping[str, Any], send: Any, etag: str, body: bytes, packed: bytes) -> None:
    """A stored map answer: 304 when the client holds ``etag``, else ``body`` (``packed`` when it
    accepts gzip), revalidated on every use (``no-cache``)."""
    tag = etag.encode("ascii")
    headers = [(b"etag", tag), (b"cache-control", b"no-cache"), (b"vary", b"accept-encoding")]
    if tag in [held.strip() for held in _headers(scope).get(b"if-none-match", b"").split(b",")]:
        await send({"type": "http.response.start", "status": 304, "headers": headers})
        await send({"type": "http.response.body", "body": b""})
        return
    if _accepts_gzip(scope):
        body = packed
        headers.append((b"content-encoding", b"gzip"))
    headers += [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _tile_query(scope: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """``(level, x, y)`` from ``?level=L&x=X&y=Y``, or None when malformed."""
    query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="replace"), keep_blank_values=True)
    values = []
    for name in ("level", "x", "y"):
        given = query.get(name, [])
        if len(given) != 1 or not given[0].removeprefix("-").isdigit() or len(given[0]) > 7:
            return None
        values.append(int(given[0]))
    return (values[0], values[1], values[2]) if 0 <= values[0] <= 2 else None


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


def _unwrap_error(exc: BaseException) -> BaseException:
    sub = getattr(exc, "exceptions", None)
    while isinstance(sub, (list, tuple)) and sub and isinstance(sub[0], BaseException):
        exc = sub[0]
        sub = getattr(exc, "exceptions", None)
    return exc


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
    *,
    gzip_ok: bool = False,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [(b"content-type", b"application/json; charset=utf-8")]
    if gzip_ok and len(body) >= _GZIP_MIN_BYTES:
        body = gzip.compress(body, 6)
        headers += [(b"content-encoding", b"gzip"), (b"vary", b"accept-encoding")]
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _send_static(scope: Mapping[str, Any], send: Any, path: str) -> None:
    if scope.get("method", "GET").upper() != "GET":
        await _send_json(send, 405, {"error": "method not allowed"}, [(b"allow", b"GET")])
        return
    name, content_type = _STATIC_FILES[path]
    try:
        body = (_WEB_DIR / name).read_bytes()
    except OSError:
        await _send_json(send, 404, {"error": "not found"})
        return
    headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode("ascii"))]
    headers.extend(_STATIC_HEADERS)
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# The files an open page runs; a page compares this hash with its own to notice a new version
# (an installed phone app can stay open for days without reloading).
_PAGE_FILES = ("index.html", "app.js", "app.css")


async def _send_page_version(scope: Mapping[str, Any], send: Any) -> None:
    if scope.get("method", "GET").upper() != "GET":
        await _send_json(send, 405, {"error": "method not allowed"}, [(b"allow", b"GET")])
        return
    digest = hashlib.sha256()
    try:
        for name in _PAGE_FILES:
            digest.update((_WEB_DIR / name).read_bytes())
    except OSError:
        await _send_json(send, 404, {"error": "not found"})
        return
    await _send_json(send, 200, {"version": digest.hexdigest()[:16]}, _STATIC_HEADERS)


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
    config = load_config(args.config)
    token_env = config.server.auth_token_env
    mcp_token = os.environ.get(token_env)
    if not mcp_token or not mcp_token.strip():
        parser.error(f"{token_env} must be set to the HORUS MCP bearer token")
    server_url = os.environ.get("HORUS_MCP_URL") or DEFAULT_MCP_URL
    # The phone app's own token (optional); scripts/phone_serve.sh prints the phone address.
    phone_token = (os.environ.get("HORUS_COMMANDER_PHONE_TOKEN") or "").strip() or None
    configure_logging(
        component="commander",
        extra_secrets=[command_token, mcp_token, phone_token],
    )
    install_unhandled_exception_hooks("horus.commander")
    commander = HorusCommander(server_url, mcp_token, config.commander)
    # The control app's reads use their own session, never the AI pilot's.
    display = DisplayReader(server_url, mcp_token)
    app = create_app(commander, command_token, display=display, phone_token=phone_token)

    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "The commander service requires the uvicorn dependency from the mcp extra"
        ) from exc

    async def serve() -> None:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=DEFAULT_HOST,
                port=args.port,
                log_level="info",
                log_config=None,
                access_log=False,
                # An open control app keeps its /events stream open forever; without a bound,
                # shutdown would wait for that page and the page would never see the restart.
                timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
            )
        )
        loop = asyncio.get_running_loop()
        orig_handle_exit = server.handle_exit

        def _on_exit(sig: int, frame: Any) -> Any:
            try:
                loop.call_soon_threadsafe(app.begin_shutdown)
            except RuntimeError:
                pass
            return orig_handle_exit(sig, frame)

        server.handle_exit = _on_exit  # type: ignore[method-assign]

        def _on_sigterm(signum: int, frame: Any) -> None:
            try:
                loop.call_soon_threadsafe(app.begin_shutdown)
            except RuntimeError:
                pass
            server.should_exit = True

        # uvicorn re-raises a captured SIGTERM after its own shutdown, which would kill the process
        # before the cleanup below ends the Claude Code process; stop gracefully instead, as on
        # Ctrl-C (also while uvicorn's own handlers are not installed yet or no longer).
        previous_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)
        commander_task = asyncio.create_task(commander.run())
        display_task = asyncio.create_task(display.run())
        server_task = asyncio.create_task(server.serve())
        announce_task = asyncio.create_task(
            _announce_when_listening(server, args.port, command_token, phone=phone_token is not None)
        )
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
            app.begin_shutdown()
            server.should_exit = True
            await commander.stop()
            display_task.cancel()
            announce_task.cancel()
            if not commander_task.done():
                commander_task.cancel()
            if not server_task.done():
                server_task.cancel()
            await asyncio.gather(
                commander_task, display_task, server_task, announce_task, return_exceptions=True
            )
            signal.signal(signal.SIGTERM, previous_sigterm)

    asyncio.run(serve())


def control_app_url(port: int, token: str) -> str:
    """The page address with the token in the fragment, which browsers never send to a server."""
    return f"http://{DEFAULT_HOST}:{port}/#token={quote(token, safe='')}"


async def _announce_when_listening(server: Any, port: int, token: str, *, phone: bool = False) -> None:
    # Only once the socket is bound: a busy port must not print an address that is someone else's.
    # stdout only, for the terminal that started the service; never the event log or /events.
    while not server.started:
        await asyncio.sleep(0.05)
    print(f"HORUS control app: {control_app_url(port, token)}", flush=True)
    if phone:
        print(
            "HORUS phone app: phone token accepted; `scripts/phone_serve.sh start` publishes this "
            "service on your tailnet and prints the phone address",
            flush=True,
        )


if __name__ == "__main__":
    main()
