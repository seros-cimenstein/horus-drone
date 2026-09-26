#!/usr/bin/env bash
set -Eeuo pipefail

SIM_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SIM_DIR}/.." && pwd)"
IMAGE="${HORUS_SIM_IMAGE:-horus-sim:jammy-harmonic-copter-4.7.1-slim}"
SIM_CONTAINER="${HORUS_SIM_CONTAINER_NAME:-horus-sim}"
GUI_CLIENT_CONTAINER="${HORUS_GUI_CLIENT_CONTAINER_NAME:-horus-gui}"
CONTAINER="${SIM_CONTAINER}"
RUN_ROOT="${SIM_DIR}/run"
command="${1:-help}"
if [[ "${command}" == "gui-client" ]]; then
  if (( $# != 1 )); then
    printf '%s\n' 'gui-client accepts no extra arguments; use --help for syntax.' >&2
    exit 2
  fi
  command="start"
  set -- start --gui-client
  CONTAINER="${GUI_CLIENT_CONTAINER}"
  RUN_ROOT="${SIM_DIR}/run/gui-client"
fi
DOCKERFILE="${SIM_DIR}/docker/Dockerfile"

usage() {
  cat <<'EOF'
Usage: ./sim/run.sh <build|start [--gui] [--gui-client] [world]|gui-client|stop [--purge]|status|s0|frame|logs>

  build             Build the Ubuntu 22.04 + Gazebo Harmonic + ArduPilot image
  start [--gui] [--gui-client] [world]
  --gui             Run Gazebo GUI beside the local server and SITL
  gui-client        Run only the local Gazebo GUI and commander bridge for a remote server
  HORUS_SIM_GPU=auto|amd|mesa|on|off selects NVIDIA/Mesa rendering or CPU fallback
  stop [--purge]    Stop the container; optionally delete its active run directory
  status            Show whether the simulation container is running
  s0                Run the pymavlink 5 m takeoff-and-land proof
  frame             Enable the camera stream and save a frame
  logs              Follow the Gazebo/SITL container log
EOF
}

require_running_container() {
  local running
  running="$(docker inspect --format '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || true)"
  if [[ "${running}" != true ]]; then
    printf 'Simulation container %s is not running. Start it with ./sim/scripts/start_sim.sh.\n' "${CONTAINER}" >&2
    exit 1
  fi
}

case "${command}" in
  build)
    exec bash "${SIM_DIR}/scripts/build_image.sh"
    ;;
  start)
    shift
    world="iris_runway.sdf"
    world_set=0
    gui_enabled=0
    gui_client_enabled=0
    while (( $# > 0 )); do
      case "$1" in
        --gui)
          gui_enabled=1
          ;;
        --gui-client)
          gui_client_enabled=1
          gui_enabled=1
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        -*)
          printf 'Unknown start option: %s\n' "$1" >&2
          usage >&2
          exit 2
          ;;
        *)
          if (( world_set == 1 )); then
            printf 'Only one world may be specified (got %s).\n' "$1" >&2
            usage >&2
            exit 2
          fi
          world="$1"
          world_set=1
          ;;
      esac
      shift
    done
    run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    if (( gui_client_enabled == 1 )); then
      if (( world_set == 1 )); then
        printf 'gui-client observes the remote world and does not accept a local world file.\n' >&2
        exit 2
      fi
      source "${PROJECT_DIR}/scripts/koko/common.sh"
      horus_koko_init
      horus_koko_resolve_ips
      horus_koko_set_local_gz_env
      if [[ ! "${GZ_IP}" =~ ^[0-9]+(\.[0-9]+){3}$ || "${GZ_IP}" == 127.* ]]; then
        printf 'gui-client requires a local Ethernet GZ_IP; set HORUS_LOCAL_IP or HORUS_LOCAL_GZ_IP.\n' >&2
        exit 2
      fi
      if [[ -z "${HORUS_MCP_TOKEN:-}" && -r "${HORUS_REMOTE_STATE_DIR}/mcp-token" ]]; then
        IFS= read -r HORUS_MCP_TOKEN < "${HORUS_REMOTE_STATE_DIR}/mcp-token"
      fi
      if [[ -z "${HORUS_MCP_TOKEN:-}" || -z "${HORUS_COMMANDER_TOKEN:-}" ]]; then
        printf 'gui-client requires HORUS_MCP_TOKEN and HORUS_COMMANDER_TOKEN.\n' >&2
        exit 2
      fi
      export HORUS_MCP_TOKEN HORUS_COMMANDER_TOKEN
      HORUS_MCP_URL="${HORUS_MCP_URL:-http://127.0.0.1:${HORUS_MCP_LOCAL_PORT}/mcp}"
      HORUS_COMMANDER_URL="${HORUS_COMMANDER_URL:-http://127.0.0.1:8765}"
      export HORUS_MCP_URL HORUS_COMMANDER_URL
      CONTAINER="${GUI_CLIENT_CONTAINER}"
      RUN_ROOT="${SIM_DIR}/run/gui-client"
      container_run_root=/horus/sim/run/gui-client
      container_run_dir="${container_run_root}/${run_id}"
      container_workdir=/horus
      container_command=(/horus/sim/scripts/start_gui_client.sh)
    else
      container_run_root=/horus/sim/run
      container_run_dir="${container_run_root}/${run_id}"
      container_workdir=/opt/ardupilot
      container_command=(/horus/sim/scripts/start_sim.sh "${world}")
    fi
    if (( gui_enabled == 1 )); then
      IMAGE="${HORUS_SIM_GUI_IMAGE:-horus-sim:local-s1s2-gui}"
    fi
    if (( gui_enabled == 1 )); then
      if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
        printf 'GUI image %s is missing; build it with: docker build -f sim/docker/Dockerfile.gui-overlay -t %s .\n' \
          "${IMAGE}" "${IMAGE}" >&2
        exit 1
      fi
    elif ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
      printf 'Image %s is missing; run ./sim/run.sh build first.\n' "${IMAGE}" >&2
      exit 1
    fi
    if docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
      printf 'Container %s already exists; stop it with ./sim/scripts/stop_sim.sh first.\n' "${CONTAINER}" >&2
      exit 1
    fi
    if (( gui_enabled == 1 )); then
      if [[ ! -d /tmp/.X11-unix ]]; then
        printf 'X11 socket directory /tmp/.X11-unix is missing; cannot start the GUI.\n' >&2
        exit 1
      fi
      if [[ -n "${XAUTHORITY:-}" ]]; then
        xauthority_source="${XAUTHORITY}"
      elif [[ -n "${HOME:-}" ]]; then
        xauthority_source="${HOME}/.Xauthority"
      else
        printf 'Set XAUTHORITY or HOME to locate the XWayland authorization file.\n' >&2
        exit 1
      fi
      if [[ ! -f "${xauthority_source}" || ! -r "${xauthority_source}" ]]; then
        printf 'XWayland authorization file is not a readable file: %s\n' \
          "${xauthority_source}" >&2
        exit 1
      fi
      xauthority_file="$(realpath -e -- "${xauthority_source}")"
      gui_args=(
        --user "$(id -u):$(id -g)"
        --env DISPLAY=:0
        --env "XAUTHORITY=${xauthority_file}"
        --env QT_QPA_PLATFORM=xcb
        --volume "${xauthority_file}:${xauthority_file}:ro"
        --volume /tmp/.X11-unix:/tmp/.X11-unix
      )
    else
      gui_args=()
    fi
    mkdir -p "${RUN_ROOT}" "${SIM_DIR}/logs"
    gpu_mode="${HORUS_SIM_GPU:-auto}"
    gpu_args=()
    nvidia_enabled=false
    mesa_enabled=false
    render_node="${HORUS_SIM_RENDER_NODE:-/dev/dri/renderD128}"
    render_gid=""
    if [[ -e "${render_node}" ]]; then
      render_gid="$(stat -c '%g' "${render_node}" 2>/dev/null || true)"
    fi
    mesa_available=false
    if [[ -n "${render_gid}" && -r "${render_node}" && -w "${render_node}" ]]; then
      mesa_available=true
    fi
    case "${gpu_mode}" in
      auto)
        if command -v nvidia-smi >/dev/null 2>&1 \
          && nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
          gpu_args=(--gpus all --env NVIDIA_DRIVER_CAPABILITIES=all)
          nvidia_enabled=true
          printf 'NVIDIA GPU detected; enabling GPU rendering.\n'
          if (( gui_enabled == 1 )) && [[ "${mesa_available}" == true ]]; then
            # Hybrid laptops: the X server's GLX vendor may be Mesa (iGPU), which needs the DRI
            # render node; without it the GUI silently falls back to llvmpipe.
            gpu_args+=(--device /dev/dri:/dev/dri --group-add "${render_gid}")
            printf 'Also exposing %s for the GUI X server'\''s GLX vendor.\n' "${render_node}"
          fi
        elif [[ "${mesa_available}" == true ]]; then
          gpu_args=(--device /dev/dri:/dev/dri --group-add "${render_gid}" \
            --env LIBGL_ALWAYS_SOFTWARE=0)
          mesa_enabled=true
          printf 'Mesa render node %s is accessible; enabling hardware rendering.\n' "${render_node}"
        else
          printf 'No accessible NVIDIA or Mesa render device; using CPU rendering.\n'
        fi
        ;;
      amd)
        if [[ "${mesa_available}" != true ]]; then
          printf 'HORUS_SIM_GPU=amd requires readable/writable %s; add the user to render and re-login.\n' \
            "${render_node}" >&2
          exit 1
        fi
        gpu_args=(--device /dev/dri:/dev/dri --group-add "${render_gid}" \
          --env LIBGL_ALWAYS_SOFTWARE=0 \
          --env MESA_LOADER_DRIVER_OVERRIDE=radeonsi)
        mesa_enabled=true
        printf 'Using AMD radeonsi through Mesa render node %s (group %s).\n' \
          "${render_node}" "${render_gid}"
        ;;
      mesa)
        if [[ "${mesa_available}" != true ]]; then
          printf 'HORUS_SIM_GPU=mesa requires readable/writable %s.\n' "${render_node}" >&2
          exit 1
        fi
        gpu_args=(--device /dev/dri:/dev/dri --group-add "${render_gid}" \
          --env LIBGL_ALWAYS_SOFTWARE=0)
        mesa_enabled=true
        printf 'Using Mesa hardware rendering through %s (group %s).\n' \
          "${render_node}" "${render_gid}"
        ;;
      on)
        if ! command -v nvidia-smi >/dev/null 2>&1 \
          || ! nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
          printf 'HORUS_SIM_GPU=on requires a working nvidia-smi and available NVIDIA GPU.\n' >&2
          exit 1
        fi
        gpu_args=(--gpus all --env NVIDIA_DRIVER_CAPABILITIES=all)
        nvidia_enabled=true
        printf 'NVIDIA GPU enabled by HORUS_SIM_GPU=on.\n'
        ;;
      off)
        printf 'GPU rendering disabled by HORUS_SIM_GPU=off; using CPU rendering.\n'
        ;;
      *)
        printf 'HORUS_SIM_GPU must be auto, amd, mesa, on, or off (got %s).\n' "${gpu_mode}" >&2
        exit 2
        ;;
    esac
    if [[ "${mesa_enabled}" == true && "${gui_enabled}" == 0 ]]; then
      gpu_args+=(--env EGL_PLATFORM=surfaceless)
    fi
    if [[ "${nvidia_enabled}" == true ]]; then
      nvidia_egl_manifest=/usr/share/glvnd/egl_vendor.d/10_nvidia.json
      if [[ -r "${nvidia_egl_manifest}" ]]; then
        gpu_args+=(
          --volume "${nvidia_egl_manifest}:/usr/share/glvnd/egl_vendor.d/10_nvidia.json:ro"
          --env __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json
        )
        printf 'Mounting NVIDIA EGL vendor manifest for Gazebo rendering.\n'
      else
        printf 'Host NVIDIA EGL vendor manifest is absent; relying on container runtime injection.\n' >&2
      fi
    fi
    gz_args=(
      --env "GZ_IP=${GZ_IP:-127.0.0.1}"
      --env "GZ_PARTITION=${GZ_PARTITION:-default}"
    )
    if [[ -n "${GZ_RELAY:-}" ]]; then
      gz_args+=(--env "GZ_RELAY=${GZ_RELAY}")
    fi
    gui_client_env_args=()
    if (( gui_client_enabled == 1 )); then
      gui_client_env_args=(
        --env HORUS_MCP_TOKEN
        --env HORUS_COMMANDER_TOKEN
        --env "HORUS_MCP_URL=${HORUS_MCP_URL}"
        --env "HORUS_COMMANDER_URL=${HORUS_COMMANDER_URL}"
      )
    fi
    run_dir="${RUN_ROOT}/${run_id}"
    docker run --detach --init \
      --name "${CONTAINER}" \
      --network host \
      "${gpu_args[@]}" \
      "${gui_args[@]}" \
      "${gz_args[@]}" \
      "${gui_client_env_args[@]}" \
      --env "HORUS_SITL_PARAM_FILE=${HORUS_SITL_PARAM_FILE:-}" \
      --env HORUS_SIM_CONTAINER=1 \
      --env "HORUS_SIM_GUI=${gui_enabled}" \
      --env "HORUS_SIM_GUI_CLIENT=${gui_client_enabled}" \
      --env "HORUS_SIM_RUN_ID=${run_id}" \
      --env "HORUS_SIM_RUN_ROOT=${container_run_root}" \
      --env "HORUS_SIM_RUN_DIR=${container_run_dir}" \
      --env "HOME=${container_run_dir}" \
      --env "XDG_CACHE_HOME=${container_run_dir}/.cache" \
      --env "XDG_CONFIG_HOME=${container_run_dir}/.config" \
      --env "XDG_DATA_HOME=${container_run_dir}/.local/share" \
      --env "GZ_SIM_RESOURCE_PATH=/opt/ardupilot_gazebo/models:/opt/ardupilot_gazebo/worlds:/horus/sim/models:/horus/sim/worlds" \
      --volume "${PROJECT_DIR}:/horus" \
      --workdir "${container_workdir}" \
      "${IMAGE}" \
      "${container_command[@]}"
    printf '%s\n' "${run_id}" > "${RUN_ROOT}/.active"
    if (( gui_client_enabled == 1 )); then
      gui_ready=0
      ready_deadline=$((SECONDS + 120))
      while (( SECONDS < ready_deadline )); do
        if [[ -f "${run_dir}/logs/gui.ready" ]]; then
          gui_ready=1
          break
        fi
        if [[ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || true)" != true ]]; then
          printf 'GUI client stopped during startup; inspect %s/logs/gui.log and gz_bridge.log.\n' \
            "${run_dir}" >&2
          break
        fi
        sleep 0.25
      done
      if (( gui_ready == 0 )); then
        printf 'Timed out waiting for remote Gazebo GUI discovery; logs are retained in %s/logs/.\n' \
          "${run_dir}" >&2
        HORUS_SIM_CONTAINER_NAME="${CONTAINER}" HORUS_SIM_RUN_ROOT="${RUN_ROOT}" \
          "${SIM_DIR}/scripts/stop_sim.sh" || true
        exit 1
      fi
      printf 'Started remote GUI client %s. Runtime files: %s/logs/.\n' \
        "${CONTAINER}" "${run_dir}"
    else
      printf 'Started %s. Runtime files: %s/run/%s/ (Gazebo and SITL logs are in logs/).\n' \
        "${CONTAINER}" "${SIM_DIR}" "${run_id}"
      printf 'MAVLink flight endpoint (single TCP client): tcp:127.0.0.1:5760\n'
      printf 'Indoor ToF bridge endpoint: tcp:127.0.0.1:5763\n'
    fi
    ;;
  stop)
    exec "${SIM_DIR}/scripts/stop_sim.sh" "${@:2}"
    ;;
  status)
    if docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
      docker inspect --format 'container={{.Name}} state={{.State.Status}}' "${CONTAINER}"
    else
      printf 'container=%s state=absent\n' "${CONTAINER}"
    fi
    ;;
  s0)
    require_running_container
    docker exec --env "HORUS_MAVLINK_CONNECTION=${HORUS_MAVLINK_CONNECTION:-tcp:127.0.0.1:5760}" \
      --workdir /opt/ardupilot "${CONTAINER}" \
      python3 /horus/sim/scripts/s0_takeoff.py "${@:2}"
    ;;
  frame)
    require_running_container
    docker exec --workdir /opt/ardupilot "${CONTAINER}" \
      python3 /horus/sim/scripts/grab_frame.py "${@:2}"
    ;;
  logs)
    require_running_container
    exec docker logs --follow "${CONTAINER}"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
