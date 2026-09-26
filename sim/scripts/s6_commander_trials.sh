#!/usr/bin/env bash
# S6: "find the person in the red shirt" end to end with the Claude Code commander (MCP only, no API key).
# Each run: fresh remote simulator, Gazebo ground-truth trace, commander mission from this machine
# through the MCP tunnel, full commander/MCP event log, then an analysis of which person was approached.
# Usage: sim/scripts/s6_commander_trials.sh [RUNS] (default 1). Results: sim/logs/s6/.
set -Eeuo pipefail
runs="${1:-1}"
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "${project_dir}"
remote="${HORUS_REMOTE:-koko}"
remote_dir="${HORUS_REMOTE_DIR:-horus}"
container="${HORUS_REMOTE_SIM_CONTAINER_NAME:-horus-sim}"
horus_bin="${HORUS_HOST_VENV:-${project_dir}/.venv-host}/bin"
token_file="${XDG_RUNTIME_DIR:-/tmp}/horus-$(id -u)/${remote}/mcp-token"
out_dir="sim/logs/s6"
mkdir -p "${out_dir}"
export HORUS_MCP_URL="http://127.0.0.1:${HORUS_MCP_LOCAL_PORT:-8000}/mcp"
command -v claude >/dev/null || { printf 'claude CLI not found on PATH\n' >&2; exit 1; }

stop_remote() { bash scripts/koko/remote_down.sh >/dev/null 2>&1 || true; }
trap stop_remote EXIT

for ((i = 1; i <= runs; i++)); do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  prefix="${out_dir}/run${i}_${stamp}"
  bash scripts/koko/remote_up.sh > "${prefix}_remote_up.log" 2>&1
  HORUS_MCP_TOKEN="$(cat "${token_file}")"
  export HORUS_MCP_TOKEN
  ssh "${remote}" docker exec --detach "${container}" /opt/horus-venv/bin/python \
    /horus/sim/scripts/gt_trace.py "/horus/sim/logs/s6_gt_${stamp}.jsonl"
  sleep 10
  started="$(date +%s)"
  HORUS_EVENT_LOG="${project_dir}/${prefix}_events.jsonl" "${horus_bin}/horus" \
    --server-url "${HORUS_MCP_URL}" --mission missions/S6_find_red_shirt.yaml \
    > "${prefix}_mission.log" 2>&1 || printf 'mission exited non-zero\n' >> "${prefix}_mission.log"
  printf '%s\n' "$(( $(date +%s) - started ))" > "${prefix}_wall_seconds.txt"
  "${horus_bin}/horus-mcp-call" get_state > "${prefix}_final_state.json" 2>&1 || true
  ssh "${remote}" "docker exec ${container} pkill -INT -f gt_trace.py; sleep 1"
  scp -q "${remote}:${remote_dir}/sim/logs/s6_gt_${stamp}.jsonl" "${prefix}_groundtruth.jsonl"
  bash scripts/koko/remote_down.sh > /dev/null 2>&1
  "${horus_bin}/python" sim/scripts/s6_analyze.py "${prefix}" | tee "${prefix}_analysis.json"
done
