"""Authenticated MCP streamable-HTTP API for bounded HORUS skills and observations."""

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from horus.config import HorusConfig
from horus.event_log import JsonlEventLog, event_log_path_from_environment
from horus.events import EventBus
from horus.executor import HorusExecutor
from horus.flight import FlightLink
from horus.mcp_server import flight as flight_tools
from horus.mcp_server import guidance, scene, tasks
from horus.mcp_server import perception as perception_tools
from horus.mcp_server.context import ToolContext


class BearerTokenASGI:
    """Small ASGI gate that protects the MCP endpoint before request parsing."""

    def __init__(self, app: Any, token: str):
        if not token:
            raise ValueError("MCP bearer token must not be empty")
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self._expected):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _write_current_run_pointer(run_dir: Path) -> None:
    pointer = Path("runs/horus/current-run")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_name(f".{pointer.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(str(run_dir.resolve()), encoding="utf-8")
        os.replace(temporary, pointer)
    finally:
        temporary.unlink(missing_ok=True)


def create_mcp(
    config: HorusConfig,
    executor: HorusExecutor,
    flight: FlightLink,
    perception: Any,
    *,
    perception_unavailable_reason: str | None = None,
    event_log: JsonlEventLog | None = None,
    image_publisher: Any = None,
    event_bus: EventBus | None = None,
) -> Any:
    """Build the pinned MCP SDK server and register the complete must-have tool set."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("HORUS")
    ctx = ToolContext(
        mcp,
        config,
        executor,
        flight,
        perception,
        perception_unavailable_reason=perception_unavailable_reason,
        event_log=event_log,
        image_publisher=image_publisher,
        event_bus=event_bus or EventBus(),
    )
    for module in (guidance, flight_tools, perception_tools, scene, tasks):
        module.register(mcp, ctx)
    return mcp


def create_application(mcp: Any, token: str | None = None) -> Any:
    """Wrap the SDK's MCP streamable HTTP ASGI app with mandatory bearer auth."""

    credential = token or os.environ.get("HORUS_MCP_TOKEN")
    if not credential:
        raise RuntimeError("HORUS_MCP_TOKEN must be set before starting the MCP server")
    app = mcp.streamable_http_app()
    return BearerTokenASGI(app, credential)


def main(argv: list[str] | None = None) -> None:
    import argparse

    import uvicorn

    from horus.config import load_config
    from horus.executor import HorusExecutor
    from horus.flight import MavlinkFlightLink
    from horus.mcp_server.gz_image import LIVE_IMAGE_TOPIC, GazeboImagePublisher

    parser = argparse.ArgumentParser(description="Run the authenticated HORUS MCP server.")
    parser.add_argument(
        "--no-perception",
        action="store_true",
        help="run flight, emergency, and state tools without initializing the camera",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, os.environ.get("HORUS_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    token = os.environ.get(config.server.auth_token_env)
    if not token:
        raise SystemExit(f"{config.server.auth_token_env} must be set before starting HORUS MCP")

    run_dir_override = os.environ.get("HORUS_RUN_DIR")
    run_dir = (
        Path(run_dir_override).expanduser()
        if run_dir_override
        else Path("runs/horus")
        / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}_{uuid4().hex[:8]}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_current_run_pointer(run_dir)
    event_log_path = event_log_path_from_environment()
    print(f"HORUS MCP run directory: {run_dir}")
    event_log = JsonlEventLog(event_log_path)
    event_bus = EventBus()

    flight = MavlinkFlightLink(config.flight)
    perception = None
    executor = None
    unavailable_reason: str | None = None
    image_publisher = None
    live_publisher = None
    try:
        flight.connect()
        if args.no_perception:
            unavailable_reason = "disabled by --no-perception"
        elif not config.perception.enabled:
            unavailable_reason = "disabled by perception.enabled=false"
        else:
            try:
                # Vision dependencies (NumPy, OpenCV, ONNX Runtime) are needed only here, so a
                # flight-only install with the server extra can run --help and --no-perception.
                from horus.mcp_server.live_view import LivePerceptionView
                from horus.perception import create_perception_system

                perception = create_perception_system(
                    config.perception,
                    flight,
                    camera_horizontal_fov_deg=config.executor.camera_horizontal_fov_deg,
                    event_bus=event_bus,
                )
                live_publisher = GazeboImagePublisher(LIVE_IMAGE_TOPIC)
                perception.start(
                    LivePerceptionView(
                        perception,
                        live_publisher,
                        ignore_regions=config.perception.ignore_regions,
                        detector_name=Path(config.perception.detector_model).stem,
                    )
                )
            except Exception as exc:
                unavailable_reason = f"camera initialization failed: {exc}"
                logging.getLogger(__name__).warning(
                    "Perception unavailable; continuing with flight and state tools: %s",
                    exc,
                )
                if perception is not None:
                    perception.close()
                    perception = None

        image_publisher = GazeboImagePublisher()
        executor = HorusExecutor(flight, config.executor, perception, event_bus=event_bus)
        mcp = create_mcp(
            config,
            executor,
            flight,
            perception,
            perception_unavailable_reason=unavailable_reason,
            event_log=event_log,
            image_publisher=image_publisher,
            event_bus=event_bus,
        )
        app = create_application(mcp, token)
        uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")
    finally:
        if live_publisher is not None:
            live_publisher.close()
        if image_publisher is not None:
            image_publisher.close()
        if executor is not None:
            executor.close()
        if perception is not None:
            perception.close()
        flight.close()


if __name__ == "__main__":
    main()
