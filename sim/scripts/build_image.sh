#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SIM_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PROJECT_DIR="$(cd -- "${SIM_DIR}/.." && pwd)"
IMAGE="${HORUS_SIM_IMAGE:-horus-sim:jammy-harmonic-copter-4.7.1-slim}"
DOCKERFILE="${SIM_DIR}/docker/Dockerfile"
STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/horus-sim-build.XXXXXX")"
ABORT_FLAG="${STATE_DIR}/disk-abort"
BUILD_PID=""
WATCHDOG_PID=""
BUILD_STARTED=0

cleanup_new_resources() {
  local container_id status image_id

  docker ps -aq | sort -u > "${STATE_DIR}/containers-after" || true
  comm -13 "${STATE_DIR}/containers-before" "${STATE_DIR}/containers-after" \
    > "${STATE_DIR}/containers-new" || true
  while IFS= read -r container_id; do
    [[ -n "${container_id}" ]] || continue
    status="$(docker inspect --format '{{.State.Status}}' "${container_id}" 2>/dev/null || true)"
    case "${status}" in
      created|exited|dead)
        docker rm "${container_id}" >/dev/null 2>&1 || true
        ;;
    esac
  done < "${STATE_DIR}/containers-new"

  docker images -q --filter dangling=true | sort -u > "${STATE_DIR}/images-after" || true
  comm -13 "${STATE_DIR}/images-before" "${STATE_DIR}/images-after" \
    > "${STATE_DIR}/images-new" || true
  while IFS= read -r image_id; do
    [[ -n "${image_id}" ]] || continue
    docker image rm "${image_id}" >/dev/null 2>&1 || true
  done < "${STATE_DIR}/images-new"

  docker builder prune -f || true
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM

  if [[ -n "${WATCHDOG_PID}" ]] && kill -0 "${WATCHDOG_PID}" 2>/dev/null; then
    kill -TERM "${WATCHDOG_PID}" 2>/dev/null || true
    wait "${WATCHDOG_PID}" 2>/dev/null || true
  fi
  if [[ -n "${BUILD_PID}" ]] && kill -0 "${BUILD_PID}" 2>/dev/null; then
    printf 'Stopping the Docker build process after failure or interruption.\n' >&2
    kill -TERM "${BUILD_PID}" 2>/dev/null || true
    wait "${BUILD_PID}" 2>/dev/null || true
  fi

  if (( BUILD_STARTED == 1 && status != 0 )); then
    cleanup_new_resources
  fi
  rm -rf -- "${STATE_DIR}"
  exit "${status}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker images -q --filter dangling=true | sort -u > "${STATE_DIR}/images-before"
docker ps -aq | sort -u > "${STATE_DIR}/containers-before"

watch_disk() {
  local available_kb
  while kill -0 "${BUILD_PID}" 2>/dev/null; do
    sleep 5
    kill -0 "${BUILD_PID}" 2>/dev/null || return 0
    available_kb="$(df --output=avail -k /)"
    available_kb="${available_kb#*$'\n'}"
    available_kb="${available_kb//[[:space:]]/}"
    if [[ ! "${available_kb}" =~ ^[0-9]+$ ]] || (( available_kb < 1000000 )); then
      printf 'Root free space is %s KiB; aborting build below the 1000000 KiB limit.\n' \
        "${available_kb:-unknown}" >&2
      : > "${ABORT_FLAG}"
      kill -TERM "${BUILD_PID}" 2>/dev/null || true
      return 1
    fi
  done
}

BUILD_STARTED=1
docker build -f "${DOCKERFILE}" -t "${IMAGE}" "${PROJECT_DIR}" &
BUILD_PID=$!
watch_disk &
WATCHDOG_PID=$!
if wait "${BUILD_PID}"; then
  build_status=0
else
  build_status=$?
fi
BUILD_PID=""
kill -TERM "${WATCHDOG_PID}" 2>/dev/null || true
wait "${WATCHDOG_PID}" 2>/dev/null || true
WATCHDOG_PID=""

if [[ -f "${ABORT_FLAG}" ]]; then
  build_status=1
fi
exit "${build_status}"
