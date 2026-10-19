#!/usr/bin/env bash
# Shared setup for the PreStudy trial scripts (s3-s7) that drive the remote simulator through
# scripts/koko/remote_up.sh. Sourced, not executed: the caller cds to the project root, gets
# remote/remote_dir/container/horus_bin/token_file and HORUS_MCP_URL, and remote_down.sh runs
# on exit. The MCP token comes from remote_up.sh's state directory (HORUS_REMOTE_STATE_DIR).

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "${project_dir}"
source scripts/koko/common.sh
horus_koko_init
remote="${HORUS_REMOTE}"
remote_dir="${HORUS_REMOTE_DIR}"
container="${HORUS_REMOTE_SIM_CONTAINER_NAME}"
horus_bin="${HORUS_HOST_VENV:-${project_dir}/.venv-host}/bin"
token_file="${HORUS_REMOTE_STATE_DIR}/mcp-token"
export HORUS_MCP_URL="http://127.0.0.1:${HORUS_MCP_LOCAL_PORT}/mcp"

trial_stop_remote() { bash scripts/koko/remote_down.sh >/dev/null 2>&1 || true; }
trap trial_stop_remote EXIT

trial_remote_up() {
  # Start the remote stack (remote_up.sh output to LOG; HORUS_REMOTE_CONFIG and
  # HORUS_SITL_PARAM_FILE pass through) and export its MCP token. Usage: trial_remote_up LOG
  bash scripts/koko/remote_up.sh > "$1" 2>&1
  IFS= read -r HORUS_MCP_TOKEN < "${token_file}"
  export HORUS_MCP_TOKEN
}

trial_wait_disarmed() {
  # Poll get_state until the vehicle has armed and then disarmed. Leaves the last state in
  # trial_state and whether it ever armed in trial_armed_seen; returns 1 on timeout.
  # Usage: trial_wait_disarmed TIMEOUT_S
  local deadline=$((SECONDS + $1))
  trial_armed_seen=0
  trial_state=""
  while (( SECONDS < deadline )); do
    trial_state="$("${horus_bin}/horus-mcp-call" get_state 2>/dev/null)" || { sleep 2; continue; }
    grep -q '"armed": *true' <<< "${trial_state}" && trial_armed_seen=1
    if (( trial_armed_seen )) && grep -q '"armed": *false' <<< "${trial_state}"; then return 0; fi
    sleep 2
  done
  return 1
}

trial_gt_start() {
  # Start the Gazebo ground-truth trace into the remote sim/logs/NAME.
  # Usage: trial_gt_start NAME [gt_trace.py ARGS...]
  local name="$1"
  shift
  ssh "${remote}" docker exec --detach "${container}" /opt/horus-venv/bin/python \
    /horus/sim/scripts/gt_trace.py "/horus/sim/logs/${name}" "$@"
}

trial_gt_stop() {
  # Stop the ground-truth trace and copy the remote sim/logs/NAME to LOCAL.
  # Usage: trial_gt_stop NAME LOCAL
  ssh "${remote}" "docker exec ${container} pkill -INT -f gt_trace.py; sleep 1"
  scp -q "${remote}:${remote_dir}/sim/logs/$1" "$2"
}
