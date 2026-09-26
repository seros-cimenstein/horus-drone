#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/gui_follow.sh"

if [[ "${HORUS_SIM_CONTAINER:-0}" != 1 ]]; then
  exec "${PROJECT_DIR}/sim/run.sh" start "$@"
fi

world="${1:-iris_runway.sdf}"
plugin_root=/opt/ardupilot_gazebo
gui_enabled="${HORUS_SIM_GUI:-0}"
if [[ "${gui_enabled}" != 0 && "${gui_enabled}" != 1 ]]; then
  printf 'HORUS_SIM_GUI must be 0 or 1 (got %s).\n' "${gui_enabled}" >&2
  exit 2
fi

if [[ "${world}" = /* ]]; then
  world_file="${world}"
elif [[ -f "/horus/sim/worlds/${world}" ]]; then
  world_file="/horus/sim/worlds/${world}"
elif [[ -f "${world}" ]]; then
  world_file="$(realpath -- "${world}")"
else
  world_file="${plugin_root}/worlds/${world}"
fi
if [[ ! -f "${world_file}" ]]; then
  printf 'World does not exist: %s\n' "${world_file}" >&2
  exit 1
fi
if ! command -v gz >/dev/null || ! command -v sim_vehicle.py >/dev/null; then
  printf 'Required tools gz and sim_vehicle.py must be present in the image.\n' >&2
  exit 1
fi
param_file="${HORUS_SITL_PARAM_FILE:-}"
if [[ -n "${param_file}" && ! -r "${param_file}" ]]; then
  printf 'SITL parameter file is not readable: %s\n' "${param_file}" >&2
  exit 1
fi

run_id="${HORUS_SIM_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
if [[ ! "${run_id}" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]]; then
  printf 'Invalid simulation run ID: %s\n' "${run_id}" >&2
  exit 1
fi
run_root="${HORUS_SIM_RUN_ROOT:-/horus/sim/run}"
run_dir="${run_root}/${run_id}"
mkdir -p "${run_root}"
if [[ -e "${run_dir}" || -L "${run_dir}" ]]; then
  printf 'Simulation run directory already exists: %s\n' "${run_dir}" >&2
  exit 1
fi
mkdir "${run_dir}"
log_dir="${run_dir}/logs"
mkdir -p "${log_dir}" "${run_dir}/terrain" "${run_dir}/.gz" \
  "${run_dir}/.cache/ogre2" "${run_dir}/.config" "${run_dir}/.local/share"
: > "${run_dir}/eeprom.bin"
printf 'LOG_DISARMED 0\n' > "${run_dir}/sitl-overrides.parm"
if [[ -n "${param_file}" ]]; then
  printf '\n# Loaded from %s\n' "${param_file}" >> "${run_dir}/sitl-overrides.parm"
  cat -- "${param_file}" >> "${run_dir}/sitl-overrides.parm"
fi
printf '%s\n' "${run_id}" > "${run_root}/.active"

# Gazebo maps its GZ home to HOME; XDG_CACHE_HOME also keeps the Ogre2 cache run-local.
export HORUS_SIM_RUN_DIR="${run_dir}"
export HOME="${run_dir}"
export XDG_CACHE_HOME="${run_dir}/.cache"
export XDG_CONFIG_HOME="${run_dir}/.config"
export XDG_DATA_HOME="${run_dir}/.local/share"
export GZ_SIM_SYSTEM_PLUGIN_PATH="${plugin_root}/build"
export GZ_SIM_RESOURCE_PATH="${plugin_root}/models:${plugin_root}/worlds:/horus/sim/models:/horus/sim/worlds"

# Job control gives Gazebo and SITL separate process groups so stop_sim.sh can
# terminate each command and its children without touching unrelated processes.
set -m
rm -f "${log_dir}/gazebo.pid" "${log_dir}/sitl.pid" \
  "${log_dir}/tof_bridge.pid" "${log_dir}/gui.pid"

cleanup() {
  local pid_file pid alive attempt
  for pid_file in "${log_dir}/tof_bridge.pid" "${log_dir}/sitl.pid" \
    "${log_dir}/gui.pid" "${log_dir}/gazebo.pid"; do
    [[ -f "${pid_file}" ]] || continue
    read -r pid < "${pid_file}" || true
    [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] || continue
    kill -TERM -- "-${pid}" 2>/dev/null || true
  done
  for ((attempt = 0; attempt < 10; attempt++)); do
    alive=0
    for pid_file in "${log_dir}/tof_bridge.pid" "${log_dir}/sitl.pid" \
      "${log_dir}/gui.pid" "${log_dir}/gazebo.pid"; do
      [[ -f "${pid_file}" ]] || continue
      read -r pid < "${pid_file}" || true
      [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] || continue
      if kill -0 -- "-${pid}" 2>/dev/null; then
        alive=1
      fi
    done
    (( alive == 0 )) && break
    sleep 1
  done
  for pid_file in "${log_dir}/tof_bridge.pid" "${log_dir}/sitl.pid" \
    "${log_dir}/gui.pid" "${log_dir}/gazebo.pid"; do
    [[ -f "${pid_file}" ]] || continue
    read -r pid < "${pid_file}" || true
    [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] || continue
    kill -KILL -- "-${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  rm -f "${log_dir}/gazebo.pid" "${log_dir}/sitl.pid" \
    "${log_dir}/tof_bridge.pid" "${log_dir}/gui.pid"
}
trap 'exit 0' INT TERM
trap cleanup EXIT

wait_for_tcp_port() {
  local port="$1"
  local deadline=$((SECONDS + 60))
  while (( SECONDS < deadline )); do
    if python3 -c 'import socket, sys; s = socket.socket(); s.settimeout(0.2); result = s.connect_ex(("127.0.0.1", int(sys.argv[1]))); s.close(); sys.exit(result)' "${port}"; then
      return 0
    fi
    if ! kill -0 "${sitl_pid}" 2>/dev/null; then
      printf 'SITL exited while waiting for TCP port %s; inspect %s/sitl.log.\n' \
        "${port}" "${log_dir}" >&2
      return 1
    fi
    sleep 0.25
  done
  printf 'Timed out waiting for SITL TCP port %s; inspect %s/sitl.log.\n' \
    "${port}" "${log_dir}" >&2
  return 1
}


cd -- "${run_dir}"
gz sim -v4 -s -r --headless-rendering "${world_file}" \
  > "${log_dir}/gazebo.log" 2>&1 &
gazebo_pid=$!
printf '%s\n' "${gazebo_pid}" > "${log_dir}/gazebo.pid"
sleep 4
if ! kill -0 "${gazebo_pid}" 2>/dev/null; then
  printf 'Gazebo exited during startup; inspect %s/gazebo.log.\n' "${log_dir}" >&2
  exit 1
fi

sitl_args=(
  -v ArduCopter -f gazebo-iris --model JSON
  --add-param-file "${run_dir}/sitl-overrides.parm" --no-rebuild --no-mavproxy
)
if [[ "${world_file##*/}" == "horus_indoor.sdf" ]]; then
  # sim_vehicle.py forwards this to the SITL binary via its instance arguments.
  sitl_args+=(--sitl-instance-args=--serial2=tcp:5763)
fi
sim_vehicle.py "${sitl_args[@]}" > "${log_dir}/sitl.log" 2>&1 &
sitl_pid=$!
printf '%s\n' "${sitl_pid}" > "${log_dir}/sitl.pid"
if [[ "${world_file##*/}" == "horus_indoor.sdf" ]]; then
  # A brief serial0 client makes ArduCopter open serial1/serial2 before the ToF bridge waits.
  if ! wait_for_tcp_port 5760; then
    exit 1
  fi
  printf 'SITL serial0 TCP port 5760 accepted a bootstrap client; serial2 can now start.\n'
fi
bridge_pid=""
if [[ "${world_file##*/}" == "horus_indoor.sdf" ]]; then
  (
    if ! wait_for_tcp_port 5763; then
      printf 'Timed out waiting for SITL TCP port 5763; inspect %s/sitl.log.\n' "${log_dir}" >&2
      exit 1
    fi
    printf 'SITL serial2 TCP port 5763 is listening; starting the indoor ToF bridge.\n'
    export HORUS_PYTHON=/opt/horus-venv/bin/python
    export HORUS_MAVLINK_ENDPOINT=tcp:127.0.0.1:5763
    export HORUS_TOF_RATE_HZ=20
    exec /horus/scripts/tof_bridges_indoor.sh
  ) > "${log_dir}/tof_bridge.log" 2>&1 &
  bridge_pid=$!
  printf '%s\n' "${bridge_pid}" > "${log_dir}/tof_bridge.pid"
fi

gui_pid=""
if [[ "${gui_enabled}" == 1 ]]; then
  gui_config=/horus/sim/gui/horus_demo.config
  if [[ ! -r "${gui_config}" ]]; then
    printf 'Gazebo GUI config is not readable: %s\n' "${gui_config}" >&2
    exit 1
  fi
  gz sim -g --gui-config "${gui_config}" > "${log_dir}/gui.log" 2>&1 &
  gui_pid=$!
  printf '%s\n' "${gui_pid}" > "${log_dir}/gui.pid"
  if [[ "${world_file##*/}" == "horus_indoor.sdf" ]] \
    && ! wait_for_gui_follow "${gui_pid}" "${log_dir}" horus_iris; then
    exit 1
  fi
fi
printf 'Gazebo server PID %s; ArduCopter SITL PID %s. Run data: %s\n' \
  "${gazebo_pid}" "${sitl_pid}" "${run_dir}"

disk_check_deadline=$((SECONDS + 5))
while true; do
  if ! kill -0 "${gazebo_pid}" 2>/dev/null; then
    printf 'Gazebo exited; inspect %s/gazebo.log.\n' "${log_dir}" >&2
    exit 1
  fi
  if ! kill -0 "${sitl_pid}" 2>/dev/null; then
    printf 'SITL exited; inspect %s/sitl.log.\n' "${log_dir}" >&2
    exit 1
  fi
  if [[ -n "${gui_pid}" ]] && ! kill -0 "${gui_pid}" 2>/dev/null; then
    printf 'Gazebo GUI exited; inspect %s/gui.log.\n' "${log_dir}" >&2
    exit 1
  fi
  if [[ -n "${bridge_pid}" ]] && ! kill -0 "${bridge_pid}" 2>/dev/null; then
    printf 'Indoor ToF bridge exited; inspect %s/tof_bridge.log.\n' "${log_dir}" >&2
    exit 1
  fi
  if (( SECONDS >= disk_check_deadline )); then
    available_kb="$(df --output=avail -k /)"
    available_kb="${available_kb#*$'\n'}"
    available_kb="${available_kb//[[:space:]]/}"
    if [[ ! "${available_kb}" =~ ^[0-9]+$ ]] || (( available_kb < 1000000 )); then
      printf 'Root free space is %s KiB; stopping Gazebo and SITL below the 1000000 KiB limit.\n' \
        "${available_kb:-unknown}" >&2
      exit 1
    fi
    disk_check_deadline=$((SECONDS + 5))
  fi
  sleep 1
done
