"""Authenticated MCP client session and tool-result decoding shared by every HORUS front-end.

The MCP SDK is imported only when a session is opened, so importing this module stays cheap.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

DEFAULT_MCP_URL = "http://127.0.0.1:8000/mcp"


@asynccontextmanager
async def open_session(url: str, token: str) -> AsyncIterator[Any]:
    """Open and initialize one bearer-authenticated streamable-HTTP MCP client session."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def tool_text(result: Any) -> str:
    """Join the text blocks of a tool result (image and other blocks are skipped)."""
    return "\n".join(
        str(block.text)
        for block in getattr(result, "content", None) or ()
        if getattr(block, "type", None) == "text"
    )


def raise_for_error(result: Any, name: str = "tool call") -> None:
    """Raise ``RuntimeError`` carrying the tool's error text when the result is an error."""
    if getattr(result, "isError", False):
        raise RuntimeError(tool_text(result) or f"MCP {name} failed")


def tool_json(result: Any, name: str = "tool call") -> Any:
    """Decode a HORUS tool result: its JSON text block, else FastMCP structured content.

    FastMCP wraps a ``str`` return as ``{"result": "<json>"}`` structured content next to the
    same text block. Errors raise ``RuntimeError``; missing or malformed JSON raises ``ValueError``.
    """
    raise_for_error(result, name)
    text = tool_text(result)
    if not text:
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, Mapping) and set(structured) == {"result"}:
            structured = structured["result"]
        if not isinstance(structured, str):
            if structured is None:
                raise ValueError(f"MCP {name} returned no JSON result")
            return structured
        text = structured
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"MCP {name} returned malformed JSON") from exc


async def call_json(session: Any, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
    """Call one MCP tool and decode its JSON result with :func:`tool_json`."""
    return tool_json(await session.call_tool(name, dict(arguments or {})), name)
