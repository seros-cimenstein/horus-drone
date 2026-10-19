#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/gui_follow.sh"
source "${SCRIPT_DIR}/procedural_world.sh"

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
world_stem="${world_file##*/}"
world_stem="${world_stem%.sdf}"
world_dir="${world_file%/*}"
# Canonical scenarios live in sim/scenarios; procedural worlds keep theirs beside the SDF.
scenario_file="/horus/sim/scenarios/${world_stem}.yaml"
[[ -f "${scenario_file}" ]] || scenario_file="${world_dir}/${world_stem}.yaml"
# Worlds that carry the ToF payload also bridge it to MAVLink so ArduPilot proximity avoidance
# has range data; the payload sits deeper in the outdoor airframe, so the scoped topic chain
# differs per world. Generated worlds (horus_<kind>_p<seed>) carry the single-ring payload; the
# indoor and canonical outdoor worlds keep the seven single-beam sensors.
payload_chain=""
payload_layout=""
case "${world_stem}" in
  horus_indoor)
    payload_chain="horus_iris/horus_sensor_payload"
    payload_layout=seven
    ;;
  horus_forest_p*|horus_urban_p*)
    payload_chain="horus_iris/iris_with_gimbal/horus_sensor_payload"
    payload_layout=ring
    ;;
  horus_forest*|horus_urban*)
    payload_chain="horus_iris/iris_with_gimbal/horus_sensor_payload"
    payload_layout=seven
    ;;
esac
# The SITL parameter fragments follow the world (sim/scripts/world_profiles.py: indoor
# proximity avoidance, canonical outdoor, or generated-world fence/speed). An explicit
# HORUS_SITL_PARAM_FILE (one file or a colon-separated list) always wins.
param_files="${HORUS_SITL_PARAM_FILE:-}"
if [[ -z "${param_files}" ]]; then
  world_profile="$(horus_world_profile "${world_stem}")"
  read -r _ param_files <<< "${world_profile}"
fi
IFS=: read -r -a param_file_list <<< "${param_files}"
for param_file in "${param_file_list[@]}"; do
  if [[ ! -r "${param_file}" ]]; then
    printf 'SITL parameter file is not readable: %s\n' "${param_file}" >&2
    exit 1
  fi
done

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
for param_file in "${param_file_list[@]}"; do
  printf '\n# Loaded from %s\n' "${param_file}" >> "${run_dir}/sitl-overrides.parm"
  cat -- "${param_file}" >> "${run_dir}/sitl-overrides.parm"
done
printf '%s\n' "${run_id}" > "${run_root}/.active"

# Gazebo maps its GZ home to HOME; XDG_CACHE_HOME also keeps the Ogre2 cache run-local.
export HORUS_SIM_RUN_DIR="${run_dir}"
export HOME="${run_dir}"
export XDG_CACHE_HOME="${run_dir}/.cache"
export XDG_CONFIG_HOME="${run_dir}/.config"
export XDG_DATA_HOME="${run_dir}/.local/share"
export GZ_SIM_SYSTEM_PLUGIN_PATH="${plugin_root}/build"
export GZ_SIM_RESOURCE_PATH="${plugin_root}/models:${plugin_root}/worlds:/horus/sim/models:/horus/sim/worlds"
if [[ -d "${world_dir}/models" ]]; then
  GZ_SIM_RESOURCE_PATH="${world_dir}/models:${GZ_SIM_RESOURCE_PATH}"
fi

# Job control gives Gazebo and SITL separate process groups so stop_sim.sh can
# terminate each command and its children without touching unrelated processes.
set -m
rm -f "${log_dir}/gazebo.pid" "${log_dir}/sitl.pid" \
  "${log_dir}/tof_bridge.pid" "${log_dir}/civilian_life.pid" "${log_dir}/gui.pid"

civilian_life_pid_matches() {
  local pid="$1" command
  [[ "${pid}" =~ ^[1-9][0-9]*$ && -r "/proc/${pid}/cmdline" ]] || return 1
  command="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
  [[ "${command}" == *"/horus/sim/scripts/civilian_life.py"* ]]
}

terminate_pid_file() {
  local pid_file="$1" pid
  [[ -f "${pid_file}" ]] || return 0
  read -r pid < "${pid_file}" || true
  [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] || return 0
  if [[ "${pid_file}" == "${log_dir}/civilian_life.pid" ]]; then
    kill -0 "${pid}" 2>/dev/null || return 0
    if ! civilian_life_pid_matches "${pid}"; then
      printf 'Refusing to signal civilian PID %s: it is not civilian_life.py.\n' "${pid}" >&2
      return 0
    fi
    kill -TERM -- "${pid}" 2>/dev/null || true
  else
    kill -TERM -- "-${pid}" 2>/dev/null || true
  fi
}
bash /horus/scripts/start_grounder.sh

cleanup() {
  local pid_file pid alive attempt
  for pid_file in "${log_dir}/civilian_life.pid" "${log_dir}/tof_bridge.pid" \
    "${log_dir}/sitl.pid" "${log_dir}/gui.pid" "${log_dir}/gazebo.pid"; do
    [[ -f "${pid_file}" ]] || continue
    read -r pid < "${pid_file}" || true
    [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] || continue
    terminate_pid_file "${pid_file}"
  done
  for ((attempt = 0; attempt < 10; attempt++)); do
    alive=0
    for pid_file in "${log_dir}/civilian_life.pid" "${log_dir}/tof_bridge.pid" \
      "${log_dir}/sitl.pid" "${log_dir}/gui.pid" "${log_dir}/gazebo.pid"; do
      [[ -f "${pid_file}" ]] || continue
      read -r pid < "${pid_file}" || true
      [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] || continue
      if [[ "${pid_file}" == "${log_dir}/civilian_life.pid" ]]; then
        if civilian_life_pid_matches "${pid}" && kill -0 "${pid}" 2>/dev/null; then
          alive=1
        fi
      elif kill -0 -- "-${pid}" 2>/dev/null; then
        alive=1
      fi
    done
    (( alive == 0 )) && break
    sleep 1
  done
  for pid_file in "${log_dir}/civilian_life.pid" "${log_dir}/tof_bridge.pid" \
    "${log_dir}/sitl.pid" "${log_dir}/gui.pid" "${log_dir}/gazebo.pid"; do
    [[ -f "${pid_file}" ]] || continue
    read -r pid < "${pid_file}" || true
    [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] || continue
    if [[ "${pid_file}" == "${log_dir}/civilian_life.pid" ]]; then
      if civilian_life_pid_matches "${pid}"; then
        kill -KILL -- "${pid}" 2>/dev/null || true
      fi
    else
      kill -KILL -- "-${pid}" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
  rm -f "${log_dir}/gazebo.pid" "${log_dir}/sitl.pid" \
    "${log_dir}/tof_bridge.pid" "${log_dir}/civilian_life.pid" "${log_dir}/gui.pid"
}
trap 'exit 0' INT TERM
trap cleanup EXIT

wait_for_tcp_port() {
  local port="$1"
  local deadline=$((SECONDS + ${2:-60}))
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

wait_for_world_stats() {
  local world="$1"
  local topic="/world/${world}/stats"
  local deadline=$((SECONDS + 120))
  while (( SECONDS < deadline )); do
    if ! kill -0 "${gazebo_pid}" 2>/dev/null; then
      printf 'Gazebo exited while waiting for statistics on %s; inspect %s/gazebo.log.\n' \
        "${topic}" "${log_dir}" >&2
      return 1
    fi
    if timeout 2s gz topic -e -t "${topic}" -n 1 >/dev/null 2>&1; then
      printf 'Gazebo is publishing world statistics on %s.\n' "${topic}"
      return 0
    fi
    sleep 0.25
  done
  printf 'Timed out waiting for world statistics on %s; inspect %s/gazebo.log.\n' \
    "${topic}" "${log_dir}" >&2
  return 1
}



keeper_pid=""

stop_mavlink_keeper() {
  if [[ -n "${keeper_pid}" ]] && kill -0 "${keeper_pid}" 2>/dev/null; then
    kill -TERM "${keeper_pid}" 2>/dev/null || true
    wait "${keeper_pid}" 2>/dev/null || true
  fi
  keeper_pid=""
}

start_mavlink_keeper() {
  # SITL's lockstep world only iterates while a MAVLink client holds serial0, so the world
  # statistics the civilian driver and this script wait for would race the bootstrap client's
  # disconnect. Hold 5760 for the duration of that wait, then release it for the flight client.
  /opt/horus-venv/bin/python -c '
import time

from pymavlink import mavutil

link = None
for _attempt in range(60):
    try:
        link = mavutil.mavlink_connection("tcp:127.0.0.1:5760")
        break
    except OSError:
        time.sleep(0.5)
if link is None:
    raise SystemExit("mavlink keeper could not connect to SITL on 5760")
while True:
    link.recv_match(blocking=True, timeout=1)
' > "${log_dir}/mavlink_keeper.log" 2>&1 &
  keeper_pid=$!
  printf '%s\n' "${keeper_pid}" > "${log_dir}/mavlink_keeper.pid"
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
if [[ -n "${payload_chain}" ]]; then
  # sim_vehicle.py forwards this to the SITL binary via its instance arguments.
  sitl_args+=(--sitl-instance-args=--serial2=tcp:5763)
fi
sim_vehicle.py "${sitl_args[@]}" > "${log_dir}/sitl.log" 2>&1 &
sitl_pid=$!
printf '%s\n' "${sitl_pid}" > "${log_dir}/sitl.pid"
if [[ -n "${payload_chain}" ]]; then
  # A brief serial0 client makes ArduCopter open serial1/serial2 before the ToF bridge waits.
  if ! wait_for_tcp_port 5760; then
    exit 1
  fi
  printf 'SITL serial0 TCP port 5760 accepted a bootstrap client; serial2 can now start.\n'
fi
bridge_pid=""
if [[ -n "${payload_chain}" ]]; then
  (
    # serial2 opens only after Gazebo has loaded the world and SITL received its first physics
    # frames; generated 1.2 km worlds take 60-90 s to load, so this wait is longer than serial0's.
    if ! wait_for_tcp_port 5763 300; then
      printf 'Timed out waiting for SITL TCP port 5763; inspect %s/sitl.log.\n' "${log_dir}" >&2
      exit 1
    fi
    printf 'SITL serial2 TCP port 5763 is listening; starting the ToF bridge for %s.\n' "${world_stem}"
    export HORUS_PYTHON=/opt/horus-venv/bin/python
    export HORUS_MAVLINK_ENDPOINT=tcp:127.0.0.1:5763
    export HORUS_TOF_RATE_HZ=20
    export HORUS_TOF_LAYOUT="${payload_layout}"
    exec /horus/scripts/tof_bridges.sh --world "${world_stem}" --model-chain "${payload_chain}"
  ) > "${log_dir}/tof_bridge.log" 2>&1 &
  bridge_pid=$!
  printf '%s\n' "${bridge_pid}" > "${log_dir}/tof_bridge.pid"
fi
scenario_pid=""
if [[ -f "${scenario_file}" ]]; then
  civilian_driver=/horus/sim/scripts/civilian_life.py
  if [[ ! -r "${civilian_driver}" ]]; then
    printf 'Warning: civilian scenario %s exists but driver is missing; continuing without civilian motion.\n' \
      "${scenario_file}" >&2
  else
    start_mavlink_keeper
    if ! wait_for_world_stats "${world_stem}"; then
      stop_mavlink_keeper
      exit 1
    fi
    stop_mavlink_keeper
    (
      exec /opt/horus-venv/bin/python "${civilian_driver}" \
        --scenario "${scenario_file}" --group ambient \
        --trace "${log_dir}/civilians.jsonl"
    ) > "${log_dir}/civilian_life.log" 2>&1 &
    scenario_pid="$!"
    for ((attempt = 0; attempt < 50; attempt++)); do
      civilian_life_pid_matches "${scenario_pid}" && break
      kill -0 "${scenario_pid}" 2>/dev/null || break
      sleep 0.02
    done
    if ! civilian_life_pid_matches "${scenario_pid}"; then
      printf 'Civilian motion PID %s is not civilian_life.py; inspect %s/civilian_life.log.\n' \
        "${scenario_pid}" "${log_dir}" >&2
      kill -TERM "${scenario_pid}" 2>/dev/null || true
      exit 1
    fi
    printf '%s\n' "${scenario_pid}" > "${log_dir}/civilian_life.pid"
  fi
fi

gui_pid=""
if [[ "${gui_enabled}" == 1 ]]; then
  gui_config="${HORUS_SIM_GUI_CONFIG:-/horus/sim/gui/horus_demo.config}"
  if [[ ! -r "${gui_config}" ]]; then
    printf 'Gazebo GUI config is not readable: %s\n' "${gui_config}" >&2
    exit 1
  fi
  # The GUI layouts bind a few panels to indoor world topics; retarget them for any other world.
  if [[ "${world_stem}" != horus_indoor ]]; then
    layout_config="${gui_config}"
    gui_config="${run_dir}/$(basename -- "${layout_config}" .config)-${world_stem}.config"
    sed "s#/world/horus_indoor/#/world/${world_stem}/#g" "${layout_config}" > "${gui_config}"
    printf 'GUI panels bound to /world/%s/ topics.\n' "${world_stem}"
  fi
  gz sim -g --gui-config "${gui_config}" > "${log_dir}/gui.log" 2>&1 &
  gui_pid=$!
  printf '%s\n' "${gui_pid}" > "${log_dir}/gui.pid"
  # Generated stems (horus_<kind>_p<seed>) use the same horus_iris airframe and
  # camera topics as the canonical worlds, so they need the same camera-follow check.
  if [[ "${world_stem}" == horus_indoor || "${world_stem}" == horus_forest* || "${world_stem}" == horus_urban* ]] \
    && ! wait_for_gui_follow "${gui_pid}" "${log_dir}" horus_iris; then
    exit 1
  fi
fi
printf 'Gazebo server PID %s; ArduCopter SITL PID %s. Run data: %s\n' \
  "${gazebo_pid}" "${sitl_pid}" "${run_dir}"
# Setup is complete: the world publishes statistics and the bootstrap MAVLink keeper has released
# serial0, so the single flight client may connect now. run.sh start waits for this marker.
: > "${log_dir}/sim.ready"

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
    # The bridge waits for its first scan without a timeout, so any exit is a real failure
    # (bad arguments, topic resolution, MAVLink connect). Silently continuing would drop the
    # proximity data that PRX1_TYPE/AVOID_ENABLE depend on in these worlds.
    printf 'ToF bridge exited; inspect %s/tof_bridge.log.\n' "${log_dir}" >&2
    exit 1
  fi
  if [[ -n "${scenario_pid}" ]] && ! civilian_life_pid_matches "${scenario_pid}"; then
    printf 'Civilian motion driver exited; inspect %s/civilian_life.log.\n' "${log_dir}" >&2
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
