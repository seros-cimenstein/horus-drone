"""Generate copy-ready MCP client configurations for Claude Code, Codex, Pi/OMP, and generic HTTP clients."""

from __future__ import annotations

import json
from typing import Any


def generate_mcp_configs(
    mcp_url: str | None,
    *,
    bearer_token: str | None = None,
    include_token: bool = False,
) -> dict[str, Any]:
    """Return copyable MCP configuration snippets for external AI agent clients."""
    url = (mcp_url or "http://127.0.0.1:8000/mcp").strip()
    has_live_token = bool(include_token and bearer_token)
    token_value = bearer_token if has_live_token else "${HORUS_MCP_TOKEN}"
    auth_header = f"Bearer {token_value}"

    mcp_json = {
        "mcpServers": {
            "horus": {
                "type": "http",
                "url": url,
                "headers": {
                    "Authorization": auth_header,
                },
            }
        }
    }

    codex_cli = (
        f'HORUS_MCP_TOKEN="{token_value}" codex mcp add horus --bearer-token-env-var HORUS_MCP_TOKEN "{url}"'
        if has_live_token
        else f'codex mcp add horus --bearer-token-env-var HORUS_MCP_TOKEN "{url}"'
    )
    omp_cli = (
        f'HORUS_MCP_TOKEN="{token_value}" HORUS_MCP_URL="{url}" omp --mode rpc'
        if has_live_token
        else f'HORUS_MCP_URL="{url}" omp --mode rpc'
    )

    return {
        "mcp_url": url,
        "live_session": mcp_url is not None,
        "clients": [
            {
                "id": "claude_code",
                "name": "Claude Code (.mcp.json)",
                "description": "Drop into project `.mcp.json` or pass via `claude --mcp-config`.",
                "snippet": json.dumps(mcp_json, indent=2),
                "cli": f'claude mcp add --transport http horus "{url}" --header "Authorization: {auth_header}"',
            },
            {
                "id": "codex",
                "name": "Codex CLI",
                "description": "Register HORUS Drone board with Codex using bearer token env var.",
                "snippet": f'export HORUS_MCP_TOKEN="{token_value}"\ncodex mcp add horus --bearer-token-env-var HORUS_MCP_TOKEN "{url}"',
                "cli": codex_cli,
            },
            {
                "id": "omp_pi",
                "name": "OMP / Pi Agent",
                "description": "Connect `omp` or `pi` agent directly to the HORUS Drone board HTTP MCP endpoint, or select `omp` / `pi` in Studio Settings -> AI & Vision.",
                "snippet": json.dumps(
                    {
                        "mcpServers": {
                            "horus": {
                                "transport": "streamable-http",
                                "url": url,
                                "headers": {"Authorization": auth_header},
                            }
                        }
                    },
                    indent=2,
                ),
                "cli": omp_cli,
            },
            {
                "id": "generic",
                "name": "Generic Streamable-HTTP MCP",
                "description": "Standard MCP Streamable HTTP endpoint + bearer header.",
                "snippet": f"URL: {url}\nAuthorization: {auth_header}",
                "cli": f'curl -sS -H "Authorization: {auth_header}" "{url}"',
            },
        ],
    }
