#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
SIM_DIR="${PROJECT_DIR}/sim"
source "${SIM_DIR}/scripts/procedural_world.sh"
CONTAINER="${HORUS_SIM_CONTAINER_NAME:-horus-sim}"
MCP_URL="${HORUS_MCP_URL:-http://127.0.0.1:8000/mcp}"
COMMANDER_URL="${HORUS_COMMANDER_URL:-http://127.0.0.1:8765}"
remote_mode=1
remote_host="${HORUS_REMOTE:-koko}"
sim_cleanup_needed=0
remote_cleanup_needed=0
record_path=""
procedural=0
terminal_mode=0
phone_mode=0
phone_serve_started=0
procedural_seed=""
procedural_kind=""
procedural_stratum=""

usage() {
  printf 'Usage: %s [--remote [host] | --local] [--terminal | --phone] [--procedural [--seed N] [--kind forest|urban] [--stratum baseline|chaotic]] [--record out.mp4]\n' "${0##*/}"
  printf '%s\n' 'Default: visible laptop GUI with simulation and perception on Koko.'
  printf '%s\n' 'Missions go through the HORUS control app, a page served by horus-commander-serve at HORUS_COMMANDER_URL (default http://127.0.0.1:8765; set e.g. HORUS_COMMANDER_URL=http://127.0.0.1:8766 when that port is taken).'
  printf '%s\n' '--terminal opens the horus terminal instead of the control app; never both, each has its own AI pilot.'
  printf '%s\n' '--phone also publishes the control app on your tailnet (scripts/phone_serve.sh) with its own phone token and prints the phone address; needs HTTPS certificates enabled for the tailnet.'
  printf '%s\n' '--procedural starts a generated outdoor world instead of the indoor demo; in remote mode it is generated here and synced to the remote.'
}

while (( $# > 0 )); do
  case "$1" in
    --remote)
      remote_mode=1
      if (( $# >= 2 )) && [[ "$2" != -* ]]; then
        remote_host="$2"
        shift 2
      else
        shift
      fi
      ;;
    --local)
      remote_mode=0
      shift
      ;;
    --terminal)
      terminal_mode=1
      shift
      ;;
    --phone)
      phone_mode=1
      shift
      ;;
    --record)
      if (( $# < 2 )) || [[ -z "$2" ]]; then
        printf '%s\n' '--record requires an output path.' >&2
        usage >&2
        exit 2
      fi
      record_path="$2"
      shift 2
      ;;
    --procedural)
      procedural=1
      shift
      ;;
    --seed)
      if (( $# < 2 )) || [[ ! "$2" =~ ^[1-9][0-9]*$ ]]; then
        printf -- '--seed requires a positive integer (got %s).\n' "${2:-<missing>}" >&2
        usage >&2
        exit 2
      fi
      procedural_seed="$2"
      procedural=1
      shift 2
      ;;
    --kind)
      if (( $# < 2 )) || [[ ! "$2" =~ ^(forest|urban)$ ]]; then
        printf -- '--kind must be forest or urban (got %s).\n' "${2:-<missing>}" >&2
        usage >&2
        exit 2
      fi
      procedural_kind="$2"
      procedural=1
      shift 2
      ;;
    --stratum)
      if (( $# < 2 )) || [[ ! "$2" =~ ^(baseline|chaotic)$ ]]; then
        printf -- '--stratum must be baseline or chaotic (got %s).\n' "${2:-<missing>}" >&2
        usage >&2
        exit 2
      fi
      procedural_stratum="$2"
      procedural=1
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -n "${record_path}" ]]; then
  if ! command -v ffmpeg >/dev/null 2>&1; then
    printf '%s\n' 'ffmpeg is required for --record.' >&2
    exit 1
  fi
  record_path="$(realpath -m -- "${record_path}")"
  if [[ -e "${record_path}" ]]; then
    printf 'Recording output already exists: %s\n' "${record_path}" >&2
    exit 1
  fi
fi
if (( terminal_mode == 1 && phone_mode == 1 )); then
  printf '%s\n' '--phone needs the control app; it cannot be combined with --terminal.' >&2
  exit 2
fi
if (( phone_mode == 1 )); then
  # Before any world starts: HTTPS certificates on the tailnet, no foreign serve configuration.
  HORUS_COMMANDER_URL="${COMMANDER_URL}" "${PROJECT_DIR}/scripts/phone_serve.sh" check
fi

if (( terminal_mode == 0 )); then
  if [[ ! "${COMMANDER_URL}" =~ ^http://127\.0\.0\.1:([1-9][0-9]{0,4})/?$ ]] \
    || (( BASH_REMATCH[1] > 65535 )); then
    printf 'HORUS_COMMANDER_URL must be http://127.0.0.1:<port> (got %s).\n' "${COMMANDER_URL}" >&2
    exit 2
  fi
  COMMANDER_PORT="${BASH_REMATCH[1]}"
  # Another listener there would receive the page's token; never share or stop it.
  if (exec 3<>"/dev/tcp/127.0.0.1/${COMMANDER_PORT}") 2>/dev/null; then
    printf 'Control app port 127.0.0.1:%s is already in use by another process; set HORUS_COMMANDER_URL=http://127.0.0.1:<free port>, for example http://127.0.0.1:8766.\n' \
      "${COMMANDER_PORT}" >&2
    exit 1
  fi
fi

cd -- "${PROJECT_DIR}"
if (( remote_mode == 1 )); then
  HORUS_REMOTE="${remote_host}"
  export HORUS_REMOTE
  source "${PROJECT_DIR}/scripts/koko/common.sh"
  horus_koko_init
  horus_koko_resolve_ips
  horus_koko_set_local_gz_env
  CONTAINER="${HORUS_GUI_CLIENT_CONTAINER_NAME}"
  MCP_URL="http://127.0.0.1:${HORUS_MCP_LOCAL_PORT}/mcp"
fi
if [[ -n "${HORUS_HOST_VENV:-}" ]]; then
  host_venv_candidates=("${HORUS_HOST_VENV}")
else
  host_venv_candidates=("${PROJECT_DIR}/.venv" "${PROJECT_DIR}/.venv-host")
fi
host_venv=""
# Run HORUS from this checkout (cwd is PROJECT_DIR), not the venv's console scripts: a venv
# created at another path keeps a stale launcher shebang.
if (( terminal_mode == 1 )); then
  host_import='import horus.repl.app'
  host_need='the horus terminal'
else
  host_import='import horus.commander.server, uvicorn'
  host_need='horus-commander-serve and uvicorn'
fi
for candidate in "${host_venv_candidates[@]}"; do
  if [[ -x "${candidate}/bin/python" ]] \
    && "${candidate}/bin/python" -c "${host_import}" >/dev/null 2>&1; then
    host_venv="${candidate}"
    break
  fi
done
if [[ -z "${host_venv}" ]]; then
  printf 'Host venv needs %s. Use the root README .venv or .venv-host install, or set HORUS_HOST_VENV to a venv with the commander extra.\n' \
    "${host_need}" >&2
  exit 1
fi
host_python="${host_venv}/bin/python"
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'docker is required to run the GUI demo.' >&2
  exit 1
fi

if [[ -z "${HORUS_MCP_TOKEN:-}" ]]; then
  HORUS_MCP_TOKEN="$("${host_python}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi
export HORUS_MCP_TOKEN
if (( terminal_mode == 0 )); then
  if [[ -z "${HORUS_COMMANDER_TOKEN:-}" ]]; then
    HORUS_COMMANDER_TOKEN="$("${host_python}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  fi
  export HORUS_COMMANDER_TOKEN
  if (( phone_mode == 1 )) && [[ -z "${HORUS_COMMANDER_PHONE_TOKEN:-}" ]]; then
    HORUS_COMMANDER_PHONE_TOKEN="$("${host_python}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  fi
  if (( phone_mode == 1 )); then
    export HORUS_COMMANDER_PHONE_TOKEN
  fi
fi
export HORUS_MCP_URL="${MCP_URL}"
export HORUS_SIM_CONTAINER_NAME="${CONTAINER}"
if (( procedural == 1 )); then
  procedural_kind="${procedural_kind:-forest}"
  if [[ -z "${procedural_seed}" ]]; then
    procedural_seed="$(( (RANDOM << 15 | RANDOM) % 90000 + 10000 ))"
    printf 'Random procedural seed for this run: %s\n' "${procedural_seed}"
  fi
  procedural_stem="$(procedural_world_stem "${procedural_kind}" "${procedural_seed}")"
  # The SITL parameters and the remote MCP config follow the world (run.sh, start_sim.sh and
  # remote_up.sh resolve them); the MCP started here and the host terminal need the config.
  world_profile="$(horus_world_profile "${procedural_stem}")"
  read -r profile_config _ <<< "${world_profile}"
  # Container path for the MCP; the host terminal gets the same profile at its host path.
  mcp_config="${HORUS_CONFIG:-${profile_config}}"
  host_config="${PROJECT_DIR}/${profile_config#/horus/}"
fi

if (( procedural == 1 )) && (( remote_mode == 1 )); then
  # Generate on the laptop; remote_up.sh rsyncs sim/generated and starts this SDF remotely.
  if ! procedural_sdf="$(generate_procedural_world "${procedural_kind}" "${procedural_seed}" "${procedural_stratum}")"; then
    exit 1
  fi
  export HORUS_REMOTE_WORLD="${procedural_sdf}"
  export HORUS_GUI_WORLD="${procedural_stem}"
fi
# The MCP run pointer holds a container path (/horus/...); the host AI pilot needs a host path.
export HORUS_EVENT_LOG="${HORUS_EVENT_LOG:-${PROJECT_DIR}/runs/demo/$(date -u +%Y%m%dT%H%M%SZ)-commander-events.jsonl}"

mcp_exec_pid=""
commander_pid=""
gui_wait_pid=""
record_pid=""

stop_background_pid() {
  local pid="$1" attempt
  [[ -n "${pid}" ]] || return 0
  if kill -0 "${pid}" 2>/dev/null; then
    kill -TERM "${pid}" 2>/dev/null || true
    for ((attempt = 0; attempt < 5; attempt++)); do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "${pid}" 2>/dev/null || true
  fi
  wait "${pid}" 2>/dev/null || true
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  stop_background_pid "${record_pid}"
  if (( phone_serve_started == 1 )); then
    HORUS_COMMANDER_URL="${COMMANDER_URL}" "${PROJECT_DIR}/scripts/phone_serve.sh" stop || true
  fi
  # The AI pilot stops first (it ends its Claude Code process), before its drone board goes away.
  stop_background_pid "${commander_pid}"
  stop_background_pid "${gui_wait_pid}"
  if (( remote_mode == 1 )); then
    if (( remote_cleanup_needed == 1 )); then
      if ! "${PROJECT_DIR}/scripts/koko/remote_down.sh" && (( status == 0 )); then
        status=1
      fi
      if ! "${PROJECT_DIR}/scripts/koko/remote_fetch_logs.sh" && (( status == 0 )); then
        status=1
      fi
    fi
  else
    if (( sim_cleanup_needed == 1 )); then
      "${SIM_DIR}/run.sh" stop || true
    fi
    stop_background_pid "${mcp_exec_pid}"
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_port() {
  local port="$1" label="$2" pid="$3" deadline=$((SECONDS + 120))
  while (( SECONDS < deadline )); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      printf '%s exited before port %s became available.\n' "${label}" "${port}" >&2
      return 1
    fi
    if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  printf 'Timed out waiting for %s on 127.0.0.1:%s.\n' "${label}" "${port}" >&2
  return 1
}


find_gazebo_window() {
  local tree line
  tree="$(xwininfo -display :0 -root -tree 2>/dev/null || true)"
  while IFS= read -r line; do
    if [[ "${line,,}" != *gazebo* && "${line,,}" != *"gz sim"* ]]; then
      continue
    fi
    if [[ "${line}" =~ ^[[:space:]]*(0x[[:xdigit:]]+) ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}"
      return 0
    fi
  done <<< "${tree}"
  return 1
}

start_recording() {
  local window_id="" window_info="" x="" y="" width="" height="" line
  local deadline=$((SECONDS + 60)) input_source=:0
  local video_size=""

  if command -v xwininfo >/dev/null 2>&1; then
    while (( SECONDS < deadline )); do
      window_id="$(find_gazebo_window || true)"
      [[ -n "${window_id}" ]] && break
      sleep 0.5
    done
    if [[ -n "${window_id}" ]]; then
      window_info="$(xwininfo -display :0 -id "${window_id}" 2>/dev/null || true)"
      while IFS= read -r line; do
        case "${line}" in
          *'Absolute upper-left X:'*) x="${line##*:}" ;;
          *'Absolute upper-left Y:'*) y="${line##*:}" ;;
          *'Width:'*) width="${line##*:}" ;;
          *'Height:'*) height="${line##*:}" ;;
        esac
      done <<< "${window_info}"
      x="${x//[[:space:]]/}"
      y="${y//[[:space:]]/}"
      width="${width//[[:space:]]/}"
      height="${height//[[:space:]]/}"
      if [[ "${x}" =~ ^[0-9]+$ && "${y}" =~ ^[0-9]+$ \
        && "${width}" =~ ^[1-9][0-9]*$ && "${height}" =~ ^[1-9][0-9]*$ ]]; then
        input_source=":0.0+${x},${y}"
        video_size="${width}x${height}"
      fi
    fi
  fi

  mkdir -p -- "$(dirname -- "${record_path}")"
  if [[ -n "${video_size}" ]]; then
    printf 'Recording Gazebo XWayland window %s (%s) to %s.\n' \
      "${window_id}" "${video_size}+${x}+${y}" "${record_path}"
    ffmpeg -nostdin -n -f x11grab -framerate 30 -video_size "${video_size}" \
      -i "${input_source}" -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
      "${record_path}" &
  else
    printf 'Recording full XWayland display :0 to %s. Native Wayland windows are not captured.\n' \
      "${record_path}"
    ffmpeg -nostdin -n -f x11grab -framerate 30 -i :0 \
      -c:v libx264 -preset ultrafast -pix_fmt yuv420p "${record_path}" &
  fi
  record_pid=$!
}

if docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
  printf 'Simulation container %s already exists; stop it before starting the demo.\n' \
    "${CONTAINER}" >&2
  exit 1
fi
if (( remote_mode == 1 )); then
  printf 'Starting the headless simulation and MCP on %s (%s)...\n' \
    "${HORUS_REMOTE}" "${HORUS_REMOTE_WORLD:-horus_indoor.sdf}"
  "${PROJECT_DIR}/scripts/koko/remote_up.sh" "${HORUS_REMOTE_WORLD:-horus_indoor.sdf}"
  remote_cleanup_needed=1
else
  sim_cleanup_needed=1
  if (( procedural == 1 )); then
    printf 'Starting the GUI procedural %s simulation in %s...\n' \
      "${procedural_kind}" "${CONTAINER}"
    run_args=(start --gui --procedural --seed "${procedural_seed}" --kind "${procedural_kind}")
    if [[ -n "${procedural_stratum}" ]]; then
      run_args+=(--stratum "${procedural_stratum}")
    fi
  else
    printf 'Starting the GUI indoor simulation in %s...\n' "${CONTAINER}"
    run_args=(start --gui horus_indoor.sdf)
  fi
  "${SIM_DIR}/run.sh" "${run_args[@]}"
  mcp_env_args=(--env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}")
  if [[ -n "${mcp_config:-}" ]]; then
    mcp_env_args+=(--env "HORUS_CONFIG=${mcp_config}")
  fi
  docker exec --workdir /horus "${mcp_env_args[@]}" \
    "${CONTAINER}" /opt/horus-venv/bin/horus-mcp &
  mcp_exec_pid=$!
  wait_for_port 8000 'HORUS MCP with perception' "${mcp_exec_pid}"
  # The world only runs once MCP holds SITL's MAVLink link, so enable the camera afterwards.
  bash "${SIM_DIR}/scripts/enable_camera_stream.sh" "${CONTAINER}"
fi

if (( remote_mode == 1 )); then
  printf 'Starting local GUI client...\n'
  "${SIM_DIR}/run.sh" gui-client
fi
if [[ -n "${record_path}" ]]; then
  start_recording
fi
host_args=()
if [[ -n "${host_config:-}" ]]; then
  host_args+=(--config "${host_config}")
fi
if (( terminal_mode == 1 )); then
  printf '\nDemo ready. Type a mission in the HORUS terminal below, for example:\n'
  printf '  Take off and hover near the center of the room.\n'
  printf '/hold, /land and /home skip the AI. /quit or Ctrl-D stops all demo processes.\n'
  "${host_python}" -c 'from horus.repl.app import main; main()' \
    --server-url "${MCP_URL}" "${host_args[@]}"
  exit 0
fi

# It prints the page address, with the token in the URL fragment, once it listens.
"${host_python}" -c 'from horus.commander.server import main; main()' \
  --port "${COMMANDER_PORT}" "${host_args[@]}" &
commander_pid=$!
wait_for_port "${COMMANDER_PORT}" 'horus-commander-serve' "${commander_pid}"
if (( remote_mode == 1 )); then
  # Closing the Gazebo window ends the demo.
  docker wait "${CONTAINER}" >/dev/null &
  gui_wait_pid=$!
fi
if (( phone_mode == 1 )); then
  # Prints the phone address with the phone token in its fragment (and a QR code).
  HORUS_COMMANDER_URL="${COMMANDER_URL}" "${PROJECT_DIR}/scripts/phone_serve.sh" start
  phone_serve_started=1
fi
printf '\nDemo ready. Open the HORUS control app address printed above in a browser on this laptop\n'
if (( phone_mode == 1 )); then
  printf 'or the phone address on your phone,\n'
fi
printf 'and type a mission, for example:\n'
printf '  Take off and hover near the center of the room.\n'
printf 'HOLD, LAND and HOME on the page skip the AI. Press Ctrl-C to stop all demo processes.\n'

children=()
for pid in "${mcp_exec_pid}" "${commander_pid}" "${gui_wait_pid}" "${record_pid}"; do
  [[ -n "${pid}" ]] && children+=("${pid}")
done
finished_pid=""
if wait -n -p finished_pid "${children[@]}"; then
  child_status=0
else
  child_status=$?
fi
# Do not signal an exited child during cleanup: its PID may already be reused.
case "${finished_pid:-}" in
  "${mcp_exec_pid}") mcp_exec_pid="" ;;
  "${commander_pid}") commander_pid="" ;;
  "${gui_wait_pid}") gui_wait_pid="" ;;
  "${record_pid}") record_pid="" ;;
esac
exit "${child_status}"
