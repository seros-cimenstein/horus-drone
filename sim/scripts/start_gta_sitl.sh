#!/usr/bin/env bash
# GTA modes (gta/horus_mod/README.md), started by `sim/run.sh start --gta [--json-physics]` inside the
# horus-sim container; `sim/run.sh stop` ends them. No Gazebo.
# Pose mode (default): ArduCopter SITL with its built-in quad physics, plus horus-gta-bridge
# streaming the camera pose to HorusGta.dll in GTA V.
#   serial0 tcp:5760  HORUS (single flight client; free once sim.ready exists)
#   serial1 tcp:5762  horus-gta-bridge -> udp HORUS_GTA_TARGET (default 127.0.0.1:5611)
# Physics mode (HORUS_GTA_PHYSICS=json): SITL on its JSON backend; HorusGta.dll in GTA answers on
# udp HORUS_GTA_PHYSICS_ADDRESS:9002 with the quad's state, and SITL derives its sensors from it.
#   serial0 tcp:5760  HORUS
#   serial2 tcp:5763  horus.tof_bridge.gta <- udp 127.0.0.1:5612: HorusGta's emulated ToF payload
#                     (seven beams cast into the GTA world) as DISTANCE_SENSOR, as in the Gazebo sims
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

if [[ "${HORUS_SIM_CONTAINER:-0}" != 1 ]]; then
  exec "${PROJECT_DIR}/sim/run.sh" start --gta "$@"
fi
if (( $# != 0 )); then
  printf 'start_gta_sitl.sh takes no arguments (got %s).\n' "$*" >&2
  exit 2
fi
if ! command -v sim_vehicle.py >/dev/null; then
  printf 'sim_vehicle.py must be present in the image.\n' >&2
  exit 1
fi
gta_target="${HORUS_GTA_TARGET:-127.0.0.1:5611}"
physics="${HORUS_GTA_PHYSICS:-}"
physics_address="${HORUS_GTA_PHYSICS_ADDRESS:-127.0.0.1}"
if [[ -n "${physics}" && "${physics}" != json ]]; then
  printf 'HORUS_GTA_PHYSICS must be empty or json (got %s).\n' "${physics}" >&2
  exit 2
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
mkdir -p "${log_dir}"
printf '%s\n' "${run_id}" > "${run_root}/.active"

# The gimbal is the servo mount every HORUS profile uses (config/ardupilot/common.parm). The fence
# fragments describe Gazebo worlds and are left out; in physics mode the proximity and rangefinder
# lines are taken too, because HorusGta.dll emulates the ToF payload there.
param_file="${run_dir}/sitl-overrides.parm"
{
  printf 'LOG_DISARMED 0\n\n# Mount from /horus/config/ardupilot/common.parm\n'
  grep -E '^(MNT1_[A-Z_]+|SERVO(9|10|11)_FUNCTION)[[:space:]]' /horus/config/ardupilot/common.parm
  if [[ "${physics}" == json ]]; then
    # HorusGta's quad (gta/horus_mod/src/Core/QuadPhysics.cs) is an X frame in ArduPilot's motor
    # order; copter.parm's default is +. SITL waits for GTA's answer to every servo frame, and GTA
    # answers inside its ~30 Hz script ticks, so the frame rate is kept at 800 Hz instead of SITL's
    # 1200 Hz: arming needs a gyro rate of at least 1.8 x the 400 Hz loop ("PreArm: Gyro 0 rate
    # 400Hz < loop rate*1.8 720Hz" at SIM_RATE_HZ 400).
    printf '\n# GTA physics: quad X, 800 Hz lockstep\nFRAME_CLASS 1\nFRAME_TYPE 1\nSIM_RATE_HZ 800\n'
    printf '\n# Proximity avoidance and the down rangefinder from /horus/config/ardupilot/common.parm\n'
    grep -E '^(PRX1_TYPE|AVOID_ENABLE|AVOID_MARGIN|RNGFND1_[A-Z]+)[[:space:]]' /horus/config/ardupilot/common.parm
    # ArduPilot low-pass filters each proximity face (PRX_FILT, default 0.25 Hz: a 0.64 s time
    # constant), so a wall approached at the profile's 3 m/s reads about 2 m farther than it is.
    # On 2026-10-22 a goto into the sculpture face that way braked only from 3.0 to 2.36 m/s before
    # contact. HorusGta's beams are exact ray casts, with nothing to smooth: 0 turns the filter off.
    printf 'PRX_FILT 0\n'
  fi
} > "${param_file}"

export HORUS_SIM_RUN_DIR="${run_dir}"
export HOME="${run_dir}"

# Job control gives SITL, the keeper and the bridge separate process groups (stop_sim.sh signals
# each group through these pid files).
set -m
pid_files=("${log_dir}/gta_bridge.pid" "${log_dir}/range_bridge.pid" "${log_dir}/mavlink_keeper.pid"
  "${log_dir}/sitl.pid")
rm -f "${pid_files[@]}"

cleanup() {
  local pid_file pid alive attempt
  for pid_file in "${pid_files[@]}"; do
    [[ -f "${pid_file}" ]] || continue
    read -r pid < "${pid_file}" || true
    [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] && kill -TERM -- "-${pid}" 2>/dev/null || true
  done
  for ((attempt = 0; attempt < 10; attempt++)); do
    alive=0
    for pid_file in "${pid_files[@]}"; do
      [[ -f "${pid_file}" ]] || continue
      read -r pid < "${pid_file}" || true
      [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] && kill -0 -- "-${pid}" 2>/dev/null && alive=1
    done
    (( alive == 0 )) && break
    sleep 1
  done
  for pid_file in "${pid_files[@]}"; do
    [[ -f "${pid_file}" ]] || continue
    read -r pid < "${pid_file}" || true
    [[ "${pid:-}" =~ ^[1-9][0-9]*$ ]] && kill -KILL -- "-${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  rm -f "${pid_files[@]}"
}
trap 'exit 0' INT TERM
trap cleanup EXIT

port_open() {
  /opt/horus-venv/bin/python -c 'import socket, sys; s = socket.socket(); s.settimeout(0.2); sys.exit(s.connect_ex(("127.0.0.1", int(sys.argv[1]))))' "$1"
}

# SITL writes eeprom.bin and its logs into the cwd; /opt/ardupilot is not writable for this user
# ("PreArm: Param storage failed"), so it runs in the run directory.
cd -- "${run_dir}"
if [[ "${physics}" == json ]]; then
  sim_vehicle.py -v ArduCopter -f quad --model "JSON:${physics_address}" --no-rebuild --no-mavproxy \
    --sitl-instance-args=--serial2=tcp:5763 --add-param-file "${param_file}" > "${log_dir}/sitl.log" 2>&1 &
else
  sim_vehicle.py -v ArduCopter -f quad --no-rebuild --no-mavproxy \
    --add-param-file "${param_file}" > "${log_dir}/sitl.log" 2>&1 &
fi
sitl_pid=$!
printf '%s\n' "${sitl_pid}" > "${log_dir}/sitl.pid"

deadline=$((SECONDS + 120))
until port_open 5760; do
  if ! kill -0 "${sitl_pid}" 2>/dev/null || (( SECONDS >= deadline )); then
    printf 'SITL serial0 tcp:5760 did not open; inspect %s/sitl.log.\n' "${log_dir}" >&2
    exit 1
  fi
  sleep 0.25
done

if [[ "${physics}" == json ]]; then
  # No pose bridge: the camera rides on HorusGta's physics body. SITL steps only while GTA answers
  # its servo frames, so HORUS's first connection on 5760 waits for the game, not for this script.
  # SITL opens serial2 only once it has booted (a client on 5760 and GTA answering); the range
  # bridge keeps trying until then. It runs from the mounted checkout, like the pose bridge.
  (
    cd /horus
    exec /opt/horus-venv/bin/python -m horus.tof_bridge.gta --listen 127.0.0.1:5612 --mavlink tcp:127.0.0.1:5763
  ) > "${log_dir}/range_bridge.log" 2>&1 &
  range_bridge_pid=$!
  printf '%s\n' "${range_bridge_pid}" > "${log_dir}/range_bridge.pid"
  printf 'ArduCopter SITL PID %s on its JSON backend -> udp %s:9002 (HorusGta.dll in GTA); range bridge PID %s udp 5612 -> tcp:5763. Run data: %s\n' \
    "${sitl_pid}" "${physics_address}" "${range_bridge_pid}" "${run_dir}"
  : > "${log_dir}/sim.ready"
  while kill -0 "${sitl_pid}" 2>/dev/null; do
    if ! kill -0 "${range_bridge_pid}" 2>/dev/null; then
      printf 'the range bridge exited; inspect %s/range_bridge.log.\n' "${log_dir}" >&2
      exit 1
    fi
    sleep 1
  done
  printf 'SITL exited; inspect %s/sitl.log.\n' "${log_dir}" >&2
  exit 1
fi

# SITL boots only once a client holds serial0, and opens serial1 only then. The keeper holds 5760
# until the bridge is connected on 5762, then releases it for the flight client.
/opt/horus-venv/bin/python -c '
import time

from pymavlink import mavutil

link = mavutil.mavlink_connection("tcp:127.0.0.1:5760")
while True:
    link.recv_match(blocking=True, timeout=1)
' > "${log_dir}/mavlink_keeper.log" 2>&1 &
keeper_pid=$!
printf '%s\n' "${keeper_pid}" > "${log_dir}/mavlink_keeper.pid"

until port_open 5762; do
  if ! kill -0 "${sitl_pid}" 2>/dev/null || ! kill -0 "${keeper_pid}" 2>/dev/null || (( SECONDS >= deadline )); then
    printf 'SITL serial1 tcp:5762 did not open; inspect %s/sitl.log and mavlink_keeper.log.\n' "${log_dir}" >&2
    exit 1
  fi
  sleep 0.25
done

# The bridge runs from the mounted checkout (the image's installed horus package may be older).
(
  cd /horus
  exec /opt/horus-venv/bin/python -m horus.gta_bridge --mavlink tcp:127.0.0.1:5762 --target "${gta_target}"
) > "${log_dir}/gta_bridge.log" 2>&1 &
bridge_pid=$!
printf '%s\n' "${bridge_pid}" > "${log_dir}/gta_bridge.pid"

until grep -q 'pose packets to udp' "${log_dir}/gta_bridge.log" 2>/dev/null; do
  if ! kill -0 "${bridge_pid}" 2>/dev/null || (( SECONDS >= deadline )); then
    printf 'horus-gta-bridge did not connect to tcp:5762; inspect %s/gta_bridge.log.\n' "${log_dir}" >&2
    exit 1
  fi
  sleep 0.25
done

kill -TERM -- "-${keeper_pid}" 2>/dev/null || true
wait "${keeper_pid}" 2>/dev/null || true
rm -f "${log_dir}/mavlink_keeper.pid"

printf 'ArduCopter SITL PID %s (built-in quad, no Gazebo); horus-gta-bridge PID %s -> udp %s. Run data: %s\n' \
  "${sitl_pid}" "${bridge_pid}" "${gta_target}" "${run_dir}"
: > "${log_dir}/sim.ready"

while true; do
  if ! kill -0 "${sitl_pid}" 2>/dev/null; then
    printf 'SITL exited; inspect %s/sitl.log.\n' "${log_dir}" >&2
    exit 1
  fi
  if ! kill -0 "${bridge_pid}" 2>/dev/null; then
    printf 'horus-gta-bridge exited; inspect %s/gta_bridge.log.\n' "${log_dir}" >&2
    exit 1
  fi
  sleep 1
done
