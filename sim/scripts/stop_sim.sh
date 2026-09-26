#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
CONTAINER="${HORUS_SIM_CONTAINER_NAME:-horus-sim}"

usage() {
  printf 'Usage: %s [--purge]\n' "${0##*/}" >&2
}

if (( $# > 1 )); then
  usage
  exit 2
fi
if (( $# == 1 )) && [[ "${1}" != --purge ]]; then
  usage
  exit 2
fi
purge=0
if (( $# == 1 )); then
  purge=1
fi

purge_active_run() {
  local run_root active_file run_id target canonical_root canonical_target
  run_root="${HORUS_SIM_RUN_ROOT:-${PROJECT_DIR}/sim/run}"
  active_file="${run_root}/.active"
  if [[ ! -e "${active_file}" ]]; then
    printf 'No active simulation run marker; nothing to purge.\n'
    return 0
  fi
  if [[ -L "${run_root}" || -L "${active_file}" || ! -f "${active_file}" ]]; then
    printf 'Refusing to purge through an invalid active run marker or directory.\n' >&2
    return 1
  fi
  IFS= read -r run_id < "${active_file}" || true
  if [[ ! "${run_id:-}" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]]; then
    printf 'Refusing to purge: invalid active run ID.\n' >&2
    return 1
  fi
  target="${run_root}/${run_id}"
  if [[ -L "${target}" ]]; then
    printf 'Refusing to purge symlinked run directory: %s\n' "${target}" >&2
    return 1
  fi
  if [[ ! -e "${target}" ]]; then
    printf 'Active simulation run directory is already absent: %s\n' "${target}"
    rm -f -- "${active_file}"
    return 0
  fi
  if [[ ! -d "${target}" ]]; then
    printf 'Refusing to purge non-directory run path: %s\n' "${target}" >&2
    return 1
  fi
  canonical_root="$(realpath -e -- "${run_root}")"
  canonical_target="$(realpath -e -- "${target}")"
  if [[ "$(dirname -- "${canonical_target}")" != "${canonical_root}" ]]; then
    printf 'Refusing to purge run directory outside %s: %s\n' \
      "${canonical_root}" "${canonical_target}" >&2
    return 1
  fi
  rm -rf -- "${canonical_target}"
  rm -f -- "${active_file}"
  printf 'Purged active simulation run directory %s.\n' "${canonical_target}"
}

if [[ "${HORUS_SIM_CONTAINER:-0}" != 1 ]]; then
  if docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
    container_status="$(docker inspect --format '{{.State.Status}}' "${CONTAINER}")"
    case "${container_status}" in
      running|restarting|paused)
        docker exec "${CONTAINER}" /horus/sim/scripts/stop_sim.sh || true
        docker stop --time 20 "${CONTAINER}" >/dev/null
        ;;
    esac
    docker rm "${CONTAINER}" >/dev/null
    printf 'Stopped and removed %s.\n' "${CONTAINER}"
  else
    docker info >/dev/null
    printf 'Simulation container %s is already absent.\n' "${CONTAINER}"
  fi
  if (( purge == 1 )); then
    purge_active_run
  else
    printf 'Runtime files are retained in %s/sim/run/.\n' "${PROJECT_DIR}"
  fi
  exit 0
fi

if (( purge == 1 )); then
  printf 'The --purge option is only valid on the host.\n' >&2
  usage
  exit 2
fi

if [[ -n "${HORUS_SIM_RUN_DIR:-}" ]]; then
  log_dir="${HORUS_SIM_RUN_DIR}/logs"
else
  log_dir=/horus/sim/logs
fi
pid_files=("${log_dir}/tof_bridge.pid" "${log_dir}/sitl.pid" \
  "${log_dir}/gui.pid" "${log_dir}/gazebo.pid")

for pid_file in "${pid_files[@]}"; do
  [[ -f "${pid_file}" ]] || continue
  pid=""
  read -r pid < "${pid_file}" || true
  [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || continue
  kill -TERM -- "-${pid}" 2>/dev/null || true
done
for ((attempt = 0; attempt < 10; attempt++)); do
  alive=0
  for pid_file in "${pid_files[@]}"; do
    [[ -f "${pid_file}" ]] || continue
    pid=""
    read -r pid < "${pid_file}" || true
    [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || continue
    if kill -0 -- "-${pid}" 2>/dev/null; then
      alive=1
    fi
  done
  (( alive == 0 )) && break
  sleep 1
done
for pid_file in "${pid_files[@]}"; do
  [[ -f "${pid_file}" ]] || continue
  pid=""
  read -r pid < "${pid_file}" || true
  [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || continue
  kill -KILL -- "-${pid}" 2>/dev/null || true
done
