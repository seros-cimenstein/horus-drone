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

from horus.config import load_config
from horus.mcp_client import open_session, raise_for_error


def _jpeg_bytes(image_content: Any) -> bytes:
    encoded = getattr(image_content, "data", None)
    if not isinstance(encoded, str):
        raise RuntimeError("MCP ImageContent did not contain base64 image data")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("MCP ImageContent contained invalid base64 data") from exc
    mime_type = str(getattr(image_content, "mimeType", "") or "")
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
    image_blocks = [block for block in content if getattr(block, "type", None) == "image"]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
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
    async with open_session(url, token) as session:
        if command == "list-tools":
            tools = (await session.list_tools()).tools
            for tool in tools:
                description = getattr(tool, "description", "") or ""
                suffix = f": {description}" if description else ""
                print(f"{tool.name}{suffix}".replace(token, "[REDACTED]"))
            return
        if command == "get-prompt":
            if not name:
                raise ValueError("get-prompt requires a prompt name (for example horus_commander)")
            result = await session.get_prompt(name)
            for message in result.messages:
                content = getattr(message, "content", None)
                if getattr(content, "type", None) == "text":
                    text = getattr(content, "text", None)
                    if isinstance(text, str):
                        print(text.replace(token, "[REDACTED]"))
            return

        result = await session.call_tool(command, arguments)
        _print_content(
            list(getattr(result, "content", None) or []),
            tool=command,
            image_root=Path("runs/manager"),
            bearer_token=token,
        )
        raise_for_error(result, command)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="horus-mcp-call",
        description="Call a HORUS MCP tool, list server tools, or retrieve a prompt.",
    )
    parser.add_argument("tool", help="tool name, list-tools, or get-prompt")
    parser.add_argument("name", nargs="?", help="prompt name when using get-prompt")
    parser.add_argument("--args", default="{}", metavar="JSON", help="tool arguments as a JSON object")
    parser.add_argument("--config", help="HORUS YAML config naming the token variable (server.auth_token_env)")
    parsed = parser.parse_args(argv)

    url = os.environ.get("HORUS_MCP_URL")
    if not url:
        parser.error("HORUS_MCP_URL is required; set it to the HORUS MCP streamable HTTP URL")
    try:
        token_env = load_config(parsed.config).server.auth_token_env
    except (OSError, ValueError) as exc:
        parser.error(f"could not load the HORUS config: {exc}")
    token = os.environ.get(token_env)
    if not token:
        parser.error(f"{token_env} is required; set it to the HORUS MCP bearer token")
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
