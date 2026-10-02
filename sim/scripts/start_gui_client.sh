#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
run_dir="${HORUS_SIM_RUN_DIR:-}"
if [[ -z "${run_dir}" ]]; then
  printf 'HORUS_SIM_RUN_DIR must be set for the GUI client.\n' >&2
  exit 2
fi
log_dir="${run_dir}/logs"
mkdir -p "${log_dir}"
source "${SCRIPT_DIR}/gui_follow.sh"

gui_pid=""
bridge_pid=""
stop_child() {
  local pid="$1" attempt
  [[ -n "${pid}" ]] || return 0
  if kill -0 "${pid}" 2>/dev/null; then
    kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "${pid}" 2>/dev/null || true
    for ((attempt = 0; attempt < 5; attempt++)); do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 1
    done
    kill -KILL -- "-${pid}" 2>/dev/null || kill -KILL "${pid}" 2>/dev/null || true
  fi
  wait "${pid}" 2>/dev/null || true
}
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  stop_child "${bridge_pid}"
  stop_child "${gui_pid}"
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 0' INT TERM

if [[ -z "${HORUS_COMMANDER_TOKEN:-}" || -z "${HORUS_MCP_TOKEN:-}" ]]; then
  printf 'HORUS_COMMANDER_TOKEN and HORUS_MCP_TOKEN must be set for the GUI bridge.\n' >&2
  exit 2
fi


# Same mounted layout as start_sim.sh, so layout edits apply without rebuilding the image.
gui_config=/horus/sim/gui/horus_demo.config
if [[ ! -r "${gui_config}" ]]; then
  printf 'Gazebo GUI config is not readable: %s\n' "${gui_config}" >&2
  exit 1
fi
# The demo config binds a few panels to world-scoped topics; retarget them when the remote
# world is not the indoor demo (procedural worlds are named horus_<kind>_p<seed>).
gui_world="${HORUS_GUI_WORLD:-horus_indoor}"
if [[ ! "${gui_world}" =~ ^[A-Za-z0-9_]+$ ]]; then
  printf 'HORUS_GUI_WORLD must be a plain world name (got %s).\n' "${gui_world}" >&2
  exit 2
fi
if [[ "${gui_world}" != horus_indoor ]]; then
  gui_config="${run_dir}/horus_demo-${gui_world}.config"
  if ! sed "s#/world/horus_indoor/#/world/${gui_world}/#g" \
    /horus/sim/gui/horus_demo.config > "${gui_config}"; then
    printf 'Could not retarget the GUI config for world %s.\n' "${gui_world}" >&2
    exit 1
  fi
  printf 'GUI panels bound to /world/%s/ topics.\n' "${gui_world}"
fi

set -m
gz sim -g --gui-config "${gui_config}" > "${log_dir}/gui.log" 2>&1 &
gui_pid=$!
printf '%s\n' "${gui_pid}" > "${log_dir}/gui.pid"
# PYTHONPATH=/horus: run the mounted source, since image console scripts can predate new entry points.
/opt/horus-venv/bin/python -m horus.gz_bridge > "${log_dir}/gz_bridge.log" 2>&1 &
bridge_pid=$!
printf '%s\n' "${bridge_pid}" > "${log_dir}/gz_bridge.pid"

if ! wait_for_gui_follow "${gui_pid}" "${log_dir}" horus_iris; then
  exit 1
fi
if ! kill -0 "${bridge_pid}" 2>/dev/null; then
  printf 'Gazebo commander bridge exited during startup; inspect %s/gz_bridge.log.\n' \
    "${log_dir}" >&2
  exit 1
fi
: > "${log_dir}/gui.ready"
printf 'GUI client and horus-gz-bridge are connected to remote Gazebo partition %s.\n' \
  "${GZ_PARTITION:-default}"

if wait -n "${gui_pid}" "${bridge_pid}"; then
  status=0
else
  status=$?
fi
exit "${status}"
