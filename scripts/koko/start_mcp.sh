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
# Optional second argument: container path of the HORUS config. Without it (or with ""), the
# container's own HORUS_CONFIG applies: the world profile `sim/run.sh start` set (horus_gta.yaml
# for --gta, the procedural profile for --procedural), else horus-mcp's config/horus.yaml.
config_env_args=()
if [[ -n "${2:-}" ]]; then
  if [[ ! "$2" =~ ^/horus/[A-Za-z0-9_./-]+\.yaml$ ]]; then
    printf 'HORUS config must be a YAML path under /horus.\n' >&2
    exit 2
  fi
  export HORUS_CONFIG="$2"
  config_env_args=(--env HORUS_CONFIG)
fi
# Optional third argument: Python log level for horus-mcp (default INFO; DEBUG logs every skill decision).
export HORUS_LOG_LEVEL="${3:-INFO}"
exec docker exec --detach --workdir /horus --env HORUS_MCP_TOKEN "${config_env_args[@]}" --env HORUS_LOG_LEVEL \
  "${container}" bash -lc \
  'exec /opt/horus-venv/bin/horus-mcp >>/horus/sim/logs/horus-mcp.log 2>&1'
