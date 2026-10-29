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
source "${SIM_DIR}/scripts/procedural_world.sh"

usage() {
  cat <<'EOF'
Usage: ./sim/run.sh <build|start [--procedural [--seed N] [--kind forest|urban] [--stratum baseline|chaotic]] [--gui] [--gui-client] [world]|start --gta|gui-client|stop [--purge]|status|s0|frame|logs>

  build             Build the Ubuntu 22.04 + Gazebo Harmonic + ArduPilot image
  start [--gui] [--gui-client] [world]
  start --gta       GTA V renderer mode: ArduCopter SITL (built-in quad physics, no Gazebo) and
                    horus-gta-bridge (serial1 tcp:5762 -> udp HORUS_GTA_TARGET, default
                    127.0.0.1:5611); HORUS flies on tcp:5760 (gta/horus_mod/README.md)
  start --gta --json-physics
                    GTA V physics mode: ArduCopter SITL on its JSON backend; HorusGta.dll in GTA
                    (udp HORUS_GTA_PHYSICS_ADDRESS:9002, default 127.0.0.1) flies the quad and
                    feeds SITL its sensors; no bridge. HORUS flies on tcp:5760
  --procedural      Generate a randomized world (sim/generated) and start it; without
                    --seed a random 5-digit seed is chosen and printed for reproduction
  --seed N          Procedural seed (implies --procedural)
  --kind KIND       Procedural world kind: forest (default) or urban (implies --procedural)
  --stratum NAME    Force a dressing stratum: baseline or chaotic (implies --procedural;
                    default: sampled from the seed)
  --gui             Run Gazebo GUI beside the local server and SITL
                    HORUS_SIM_GUI_CONFIG=/horus/sim/gui/<layout>.config picks the panel layout
                    (default horus_demo.config; horus_compact.config: untitled corner insets)
                    HORUS_SIM_GUI_SCALE=<factor> scales the GUI chrome (Qt scale, default 1)
  gui-client        Run only the local Gazebo GUI and commander bridge for a remote server
  HORUS_SIM_GPU=auto|amd|mesa|on|off selects NVIDIA/Mesa rendering or CPU fallback
  stop [--purge]    Stop the container; optionally delete its active run directory
  status            Show whether the simulation container is running
  s0                Run the pymavlink 5 m takeoff-and-land proof
  frame             Enable the Gazebo camera stream and save a frame (Gazebo worlds only)
  logs              Follow the simulation container log (Gazebo or GTA SITL)
EOF
}

require_running_container() {
  local running
  running="$(docker inspect --format '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || true)"
  if [[ "${running}" != true ]]; then
    printf 'Simulation container %s is not running. Start it with ./sim/run.sh start [world] or ./sim/run.sh start --gta.\n' "${CONTAINER}" >&2
    exit 1
  fi
}

# One HORUS world per host, Gazebo or GTA V, never both: two worlds overload Koko. Refuses the
# other world's host processes and any running HORUS world container, whatever its name
# (labelled horus.world=gazebo|gta by `start`; older unlabelled ones by their start script).
refuse_second_world() {
  local world_kind="$1" name label container_command
  if [[ "${world_kind}" == gazebo ]] && pgrep -f '[G]TA5_Enhanced' >/dev/null; then
    printf 'GTA V is running on this host; stop it with scripts/koko/gta_game.sh stop before starting Gazebo.\n' >&2
    exit 1
  fi
  if [[ "${world_kind}" == gta ]] && pgrep -f '[g]z sim' >/dev/null; then
    printf 'Gazebo (gz sim) is running on this host; stop it with ./sim/run.sh stop before starting --gta.\n' >&2
    exit 1
  fi
  while IFS='|' read -r name label container_command; do
    [[ -n "${name}" ]] || continue
    if [[ -n "${label}" || "${container_command}" == *start_sim.sh* \
      || "${container_command}" == *start_gta_sitl.sh* ]]; then
      printf 'HORUS world container %s (%s) is already running; stop it first: HORUS_SIM_CONTAINER_NAME=%s ./sim/run.sh stop\n' \
        "${name}" "${label:-unlabelled}" "${name}" >&2
      exit 1
    fi
  done < <(docker ps --no-trunc --format '{{.Names}}|{{.Label "horus.world"}}|{{.Command}}')
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
    procedural=0
    seed=""
    kind=""
    gta=0
    json_physics=0
    stratum=""
    while (( $# > 0 )); do
      case "$1" in
        --procedural)
          procedural=1
          ;;
        --seed)
          if (( $# < 2 )) || [[ ! "$2" =~ ^[1-9][0-9]*$ ]]; then
            printf -- '--seed requires a positive integer (got %s).\n' "${2:-<missing>}" >&2
            usage >&2
            exit 2
          fi
          seed="$2"
          procedural=1
          shift
          ;;
        --kind)
          if (( $# < 2 )) || [[ ! "$2" =~ ^(forest|urban)$ ]]; then
            printf -- '--kind must be forest or urban (got %s).\n' "${2:-<missing>}" >&2
            usage >&2
            exit 2
          fi
          kind="$2"
          procedural=1
          shift
          ;;
        --stratum)
          if (( $# < 2 )) || [[ ! "$2" =~ ^(baseline|chaotic)$ ]]; then
            printf -- '--stratum must be baseline or chaotic (got %s).\n' "${2:-<missing>}" >&2
            usage >&2
            exit 2
          fi
          stratum="$2"
          procedural=1
          shift
          ;;
        --gta)
          gta=1
          ;;
        --json-physics)
          json_physics=1
          ;;
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
    if (( json_physics == 1 && gta == 0 )); then
      printf -- '--json-physics is a GTA mode option; use start --gta --json-physics.\n' >&2
      usage >&2
      exit 2
    fi
    if (( gta == 1 )) && (( procedural == 1 || gui_enabled == 1 || world_set == 1 )); then
      printf -- '--gta runs SITL without Gazebo; it takes no world, --procedural or --gui option.\n' >&2
      usage >&2
      exit 2
    fi
    if (( procedural == 1 )); then
      if (( gui_client_enabled == 1 )); then
        printf 'gui-client observes a remote world and cannot start a procedural world.\n' >&2
        exit 2
      fi
      if (( world_set == 1 )); then
        printf -- '--procedural generates its own world; drop the world argument (%s).\n' "${world}" >&2
        usage >&2
        exit 2
      fi
      kind="${kind:-forest}"
      if [[ -z "${seed}" ]]; then
        seed="$(( (RANDOM << 15 | RANDOM) % 90000 + 10000 ))"
        printf 'Random procedural seed for this run: %s\n' "${seed}"
      fi
      if ! world="$(generate_procedural_world "${kind}" "${seed}" "${stratum}")"; then
        exit 1
      fi
      # Generated worlds use the procedural flight profile: the SITL parameter files and the
      # executor config every later MCP/mission start should load. Explicit overrides win.
      world_profile="$(horus_world_profile "${world}")"
      read -r profile_config profile_params <<< "${world_profile}"
      export HORUS_SITL_PARAM_FILE="${HORUS_SITL_PARAM_FILE:-${profile_params}}"
      export HORUS_CONFIG="${HORUS_CONFIG:-${profile_config}}"
    fi
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
    elif (( gta == 1 )); then
      container_run_root=/horus/sim/run
      container_run_dir="${container_run_root}/${run_id}"
      container_workdir=/horus
      container_command=(/horus/sim/scripts/start_gta_sitl.sh)
      # The GTA profile becomes the container default, so the bridge sends its camera FOV and any
      # HORUS process started in the container loads the same profile.
      read -r profile_config _ <<< "$(horus_world_profile gta)"
      export HORUS_CONFIG="${HORUS_CONFIG:-${profile_config}}"
    else
      container_run_root=/horus/sim/run
      container_run_dir="${container_run_root}/${run_id}"
      container_workdir=/opt/ardupilot
      container_command=(/horus/sim/scripts/start_sim.sh "${world}")
    fi
    # A config carried over from the other world would wait forever for frames that never come.
    if (( gui_client_enabled == 0 )) && [[ -n "${HORUS_CONFIG:-}" ]]; then
      if (( gta == 1 )); then config_world=gta; else config_world="${world}"; fi
      horus_check_world_config "${config_world}" "${HORUS_CONFIG}" || exit 2
    fi
    if (( gui_enabled == 1 )); then
      IMAGE="${HORUS_SIM_GUI_IMAGE:-horus-sim:local-s1s2-gui}"
    fi
    if (( gui_enabled == 1 )); then
      if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
        printf 'GUI image %s is missing; build it with: HORUS_SIM_IMAGE=%s ./sim/run.sh build\n' \
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
    label_args=()
    if (( gui_client_enabled == 0 )); then
      if (( gta == 1 )); then world_label=gta; else world_label=gazebo; fi
      refuse_second_world "${world_label}"
      label_args=(--label "horus.world=${world_label}")
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
      gui_config="${HORUS_SIM_GUI_CONFIG:-/horus/sim/gui/horus_demo.config}"
      if [[ ! "${gui_config}" =~ ^/horus/[A-Za-z0-9_./-]+\.config$ ]]; then
        printf 'HORUS_SIM_GUI_CONFIG must be a .config path under /horus (got %s).\n' "${gui_config}" >&2
        exit 2
      fi
      gui_scale="${HORUS_SIM_GUI_SCALE:-1}"
      if [[ ! "${gui_scale}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || [[ "${gui_scale}" =~ ^0+(\.0*)?$ ]]; then
        printf 'HORUS_SIM_GUI_SCALE must be a positive number (got %s).\n' "${gui_scale}" >&2
        exit 2
      fi
      gui_args=(
        --user "$(id -u):$(id -g)"
        --env DISPLAY=:0
        --env "XAUTHORITY=${xauthority_file}"
        --env QT_QPA_PLATFORM=xcb
        --env "QT_SCALE_FACTOR=${gui_scale}"
        --env "HORUS_SIM_GUI_CONFIG=${gui_config}"
        --volume "${xauthority_file}:${xauthority_file}:ro"
        --volume /tmp/.X11-unix:/tmp/.X11-unix
      )
    else
      gui_args=()
    fi
    mkdir -p "${RUN_ROOT}" "${SIM_DIR}/logs"
    gpu_mode="${HORUS_SIM_GPU:-auto}"
    # SITL's built-in physics renders nothing; GTA draws the camera view on the host.
    (( gta == 1 )) && gpu_mode=off
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
        (( gta == 1 )) || printf 'GPU rendering disabled by HORUS_SIM_GPU=off; using CPU rendering.\n'
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
    # Gazebo transport and model paths only for Gazebo worlds and the GUI client; a GTA
    # container runs SITL alone and talks to no gz partition.
    gz_args=()
    if (( gta == 0 )); then
      gz_args=(
        --env "GZ_IP=${GZ_IP:-127.0.0.1}"
        --env "GZ_PARTITION=${GZ_PARTITION:-default}"
        --env "GZ_SIM_RESOURCE_PATH=/opt/ardupilot_gazebo/models:/opt/ardupilot_gazebo/worlds:/horus/sim/models:/horus/sim/worlds"
      )
      if [[ -n "${GZ_RELAY:-}" ]]; then
        gz_args+=(--env "GZ_RELAY=${GZ_RELAY}")
      fi
    fi
    gta_env_args=()
    if (( gta == 1 )); then
      gta_env_args=(--env "HORUS_GTA_TARGET=${HORUS_GTA_TARGET:-127.0.0.1:5611}")
      if (( json_physics == 1 )); then
        gta_env_args+=(--env HORUS_GTA_PHYSICS=json --env "HORUS_GTA_PHYSICS_ADDRESS=${HORUS_GTA_PHYSICS_ADDRESS:-127.0.0.1}")
      fi
    fi
    gui_client_env_args=()
    if (( gui_client_enabled == 1 )); then
      gui_client_env_args=(
        --env HORUS_MCP_TOKEN
        --env HORUS_COMMANDER_TOKEN
        --env "HORUS_MCP_URL=${HORUS_MCP_URL}"
        --env "HORUS_COMMANDER_URL=${HORUS_COMMANDER_URL}"
        --env "HORUS_GUI_WORLD=${HORUS_GUI_WORLD:-horus_indoor}"
      )
    fi
    resource_args=()
    if (( gui_client_enabled == 0 )); then
      if [[ -n "${HORUS_SIM_MEMORY_LIMIT:-}" ]]; then
        resource_args+=(--memory "${HORUS_SIM_MEMORY_LIMIT}")
      fi
      if [[ -n "${HORUS_SIM_MEMORY_SWAP_LIMIT:-}" ]]; then
        resource_args+=(--memory-swap "${HORUS_SIM_MEMORY_SWAP_LIMIT}")
      fi
    fi
    # An explicit profile (e.g. run.sh start --procedural) becomes the container default, so a
    # later `docker exec horus-sim /opt/horus-venv/bin/horus-mcp` loads it without extra flags.
    config_env_args=()
    if [[ -n "${HORUS_CONFIG:-}" ]]; then
      config_env_args=(--env "HORUS_CONFIG=${HORUS_CONFIG}")
    fi
    run_dir="${RUN_ROOT}/${run_id}"
    docker run --detach --init \
      --name "${CONTAINER}" \
      --network host \
      "${label_args[@]}" \
      "${gpu_args[@]}" \
      "${gui_args[@]}" \
      "${gz_args[@]}" \
      "${resource_args[@]}" \
      "${gui_client_env_args[@]}" \
      "${config_env_args[@]}" \
      "${gta_env_args[@]}" \
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
      # The flight client must not connect before start_sim.sh has released SITL's single serial0
      # slot (generated 1.2 km worlds take minutes to publish their first statistics).
      sim_ready=0
      ready_deadline=$((SECONDS + ${HORUS_SIM_READY_TIMEOUT_S:-900}))
      while (( SECONDS < ready_deadline )); do
        if [[ -f "${run_dir}/logs/sim.ready" ]]; then
          sim_ready=1
          break
        fi
        if [[ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || true)" != true ]]; then
          printf 'Simulation container stopped during startup; inspect %s/logs/ and docker logs.\n' \
            "${run_dir}" >&2
          break
        fi
        sleep 0.5
      done
      if (( sim_ready == 0 )); then
        printf 'Simulation did not finish starting; logs are retained in %s/logs/.\n' "${run_dir}" >&2
        HORUS_SIM_CONTAINER_NAME="${CONTAINER}" HORUS_SIM_RUN_ROOT="${RUN_ROOT}" \
          "${SIM_DIR}/scripts/stop_sim.sh" || true
        exit 1
      fi
      printf 'Started %s. Runtime files: %s/run/%s/ (SITL logs are in logs/).\n' \
        "${CONTAINER}" "${SIM_DIR}" "${run_id}"
      printf 'MAVLink flight endpoint (single TCP client): tcp:127.0.0.1:5760\n'
      if (( gta == 1 && json_physics == 1 )); then
        printf 'GTA physics: SITL JSON backend -> udp %s:9002 (HorusGta.dll in GTA answers with the quad state)\n' \
          "${HORUS_GTA_PHYSICS_ADDRESS:-127.0.0.1}"
      elif (( gta == 1 )); then
        printf 'GTA pose stream: horus-gta-bridge on tcp:127.0.0.1:5762 -> udp %s (logs/gta_bridge.log)\n' \
          "${HORUS_GTA_TARGET:-127.0.0.1:5611}"
      elif [[ "${world}" == "horus_indoor.sdf" ]]; then
        printf 'Indoor ToF bridge endpoint: tcp:127.0.0.1:5763\n'
      fi
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
    if [[ "$(docker inspect --format '{{index .Config.Labels "horus.world"}}' "${CONTAINER}" 2>/dev/null || true)" == gta ]]; then
      printf 'frame grabs the Gazebo camera; %s runs GTA, whose frames come from the game (get_frame on the HORUS MCP server).\n' "${CONTAINER}" >&2
      exit 2
    fi
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
