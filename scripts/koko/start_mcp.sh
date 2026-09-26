#!/usr/bin/env bash
set -Eeuo pipefail

container="${1:-}"
if [[ ! "${container}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  printf 'Usage: %s CONTAINER_NAME (MCP token is read from stdin).\n' "${0##*/}" >&2
  exit 2
fi
if ! IFS= read -r HORUS_MCP_TOKEN || [[ -z "${HORUS_MCP_TOKEN}" ]]; then
  printf 'A non-empty MCP bearer token must be provided on stdin.\n' >&2
  exit 2
fi
export HORUS_MCP_TOKEN
# Optional second argument: container path of the HORUS config (default config/horus.yaml).
export HORUS_CONFIG="${2:-/horus/config/horus.yaml}"
if [[ ! "${HORUS_CONFIG}" =~ ^/horus/[A-Za-z0-9_./-]+\.yaml$ ]]; then
  printf 'HORUS config must be a YAML path under /horus.\n' >&2
  exit 2
fi
# Optional third argument: Python log level for horus-mcp (default INFO; DEBUG logs every skill decision).
export HORUS_LOG_LEVEL="${3:-INFO}"
exec docker exec --detach --workdir /horus --env HORUS_MCP_TOKEN --env HORUS_CONFIG --env HORUS_LOG_LEVEL \
  "${container}" bash -lc \
  'exec /opt/horus-venv/bin/horus-mcp >>/horus/sim/logs/horus-mcp.log 2>&1'
