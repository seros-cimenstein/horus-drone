"""Command-line client for HORUS's authenticated MCP streamable-HTTP API."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _jpeg_bytes(image_content: Any) -> bytes:
    encoded = _field(image_content, "data")
    if not isinstance(encoded, str):
        raise RuntimeError("MCP ImageContent did not contain base64 image data")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("MCP ImageContent contained invalid base64 data") from exc
    mime_type = str(_field(image_content, "mimeType", _field(image_content, "mime_type", "")))
    if mime_type.casefold() == "image/jpeg" and raw.startswith(b"\xff\xd8\xff"):
        return raw
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Saving a non-JPEG MCP image as JPEG requires Pillow") from exc
    try:
        with Image.open(io.BytesIO(raw)) as source:
            rgb = source.convert("RGB")
            output = io.BytesIO()
            rgb.save(output, format="JPEG")
            return output.getvalue()
    except Exception as exc:
        raise RuntimeError("MCP ImageContent could not be converted to JPEG") from exc


def _image_path(directory: Path, timestamp: str, tool: str, index: int, count: int) -> Path:
    safe_tool = re.sub(r"[^A-Za-z0-9_-]+", "_", tool).strip("_") or "tool"
    suffix = f"_{index}" if count > 1 else ""
    return directory / f"{timestamp}_{safe_tool}{suffix}.jpg"


def _print_content(
    content: list[Any], *, tool: str, image_root: Path, bearer_token: str
) -> None:
    image_blocks = [block for block in content if _field(block, "type") == "image"]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    for block in content:
        if _field(block, "type") == "text":
            text = _field(block, "text")
            if isinstance(text, str):
                print(text.replace(bearer_token, "[REDACTED]"))
    if image_blocks:
        image_root.mkdir(parents=True, exist_ok=True)
        for index, block in enumerate(image_blocks, start=1):
            path = _image_path(image_root, timestamp, tool, index, len(image_blocks))
            path.write_bytes(_jpeg_bytes(block))
            print(path)


async def _request(
    command: str,
    name: str | None,
    arguments: dict[str, Any],
    url: str,
    token: str,
) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if command == "list-tools":
                tools = (await session.list_tools()).tools
                for tool in tools:
                    description = _field(tool, "description", "") or ""
                    suffix = f": {description}" if description else ""
                    print(f"{tool.name}{suffix}".replace(token, "[REDACTED]"))
                return
            if command == "get-prompt":
                if not name:
                    raise ValueError("get-prompt requires a prompt name (for example horus_commander)")
                result = await session.get_prompt(name)
                for message in result.messages:
                    content = _field(message, "content")
                    if _field(content, "type") == "text":
                        text = _field(content, "text")
                        if isinstance(text, str):
                            print(text.replace(token, "[REDACTED]"))
                return

            result = await session.call_tool(command, arguments)
            content = list(_field(result, "content", []) or [])
            _print_content(
                content,
                tool=command,
                image_root=Path("runs/manager"),
                bearer_token=token,
            )
            if _field(result, "isError", _field(result, "is_error", False)):
                raise RuntimeError("MCP tool call failed")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="horus-mcp-call",
        description="Call a HORUS MCP tool, list server tools, or retrieve a prompt.",
    )
    parser.add_argument("tool", help="tool name, list-tools, or get-prompt")
    parser.add_argument("name", nargs="?", help="prompt name when using get-prompt")
    parser.add_argument("--args", default="{}", metavar="JSON", help="tool arguments as a JSON object")
    parsed = parser.parse_args(argv)

    url = os.environ.get("HORUS_MCP_URL")
    if not url:
        parser.error("HORUS_MCP_URL is required; set it to the HORUS MCP streamable HTTP URL")
    token = os.environ.get("HORUS_MCP_TOKEN")
    if not token:
        parser.error("HORUS_MCP_TOKEN is required; set it to the HORUS MCP bearer token")
    if parsed.tool == "get-prompt" and not parsed.name:
        parser.error("get-prompt requires a prompt name (for example horus_commander)")
    if parsed.tool != "get-prompt" and parsed.name is not None:
        parser.error("a second positional argument is only valid with get-prompt")
    try:
        arguments = json.loads(parsed.args)
    except json.JSONDecodeError as exc:
        parser.error(f"--args must be valid JSON: {exc.msg}")
    if not isinstance(arguments, dict):
        parser.error("--args must decode to a JSON object")
    if parsed.tool in {"list-tools", "get-prompt"} and arguments:
        parser.error(f"--args is not used with {parsed.tool}")

    try:
        asyncio.run(_request(parsed.tool, parsed.name, arguments, url, token))
    except Exception as exc:
        message = str(exc).replace(token, "[REDACTED]")
        print(f"horus-mcp-call: {message or type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
