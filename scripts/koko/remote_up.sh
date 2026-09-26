#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/common.sh"
horus_koko_init

for required in ssh rsync docker python3 ip getent; do
  if ! command -v "${required}" >/dev/null 2>&1; then
    printf '%s is required for remote deployment.\n' "${required}" >&2
    exit 1
  fi
done
horus_koko_resolve_ips

remote_container="${HORUS_REMOTE_SIM_CONTAINER_NAME}"
if [[ ! "${remote_container}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  printf 'HORUS_REMOTE_SIM_CONTAINER_NAME contains unsupported characters.\n' >&2
  exit 2
fi
if [[ ! "${HORUS_REMOTE_SIM_IMAGE}" =~ ^[A-Za-z0-9/:._-]+$ ]]; then
  printf 'HORUS_REMOTE_SIM_IMAGE contains unsupported characters.\n' >&2
  exit 2
fi
if [[ ! "${HORUS_REMOTE_SIM_GPU}" =~ ^(auto|amd|mesa|off|on)$ ]]; then
  printf 'HORUS_REMOTE_SIM_GPU must be auto, amd, mesa, off, or on.\n' >&2
  exit 2
fi
sitl_param_file="${HORUS_SITL_PARAM_FILE:-/horus/config/ardupilot/horus_sitl.parm}"
if [[ ! "${sitl_param_file}" =~ ^[A-Za-z0-9_./-]+$ ]]; then
  printf 'HORUS_SITL_PARAM_FILE must be a simple container path.\n' >&2
  exit 2
fi

state_dir="${HORUS_REMOTE_STATE_DIR}"
local_mcp_port="${HORUS_MCP_LOCAL_PORT}"
control_socket="${state_dir}/ssh-control"
token_file="${state_dir}/mcp-token"
started_file="${state_dir}/started"
tunnel_log="${state_dir}/tunnel.log"
if [[ -e "${started_file}" || -S "${control_socket}" ]]; then
  printf 'A managed %s session already exists; run scripts/koko/remote_down.sh first.\n' \
    "${HORUS_REMOTE}" >&2
  exit 1
fi
mkdir -p -- "${state_dir}"
chmod 700 "${state_dir}"

if python3 -c 'import socket, sys; s=socket.socket(); s.settimeout(0.2); r=s.connect_ex(("127.0.0.1", int(sys.argv[1]))); s.close(); sys.exit(r != 0)' "${local_mcp_port}"; then
  printf 'Local 127.0.0.1:%s is already in use; choose another HORUS_MCP_LOCAL_PORT before opening the Koko tunnel.\n' \
    "${local_mcp_port}" >&2
  exit 1
fi

if [[ -z "${HORUS_MCP_TOKEN:-}" ]]; then
  HORUS_MCP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi
if [[ "${HORUS_MCP_TOKEN}" == *$'\n'* || -z "${HORUS_MCP_TOKEN}" ]]; then
  printf 'HORUS_MCP_TOKEN must be a single non-empty line.\n' >&2
  exit 2
fi
export HORUS_MCP_TOKEN
umask 077
printf '%s\n' "${HORUS_MCP_TOKEN}" > "${token_file}"
chmod 600 "${token_file}"

remote_started=0
tunnel_started=0
complete=0
close_tunnel() {
  if (( tunnel_started == 1 )); then
    ssh -S "${control_socket}" -O exit "${HORUS_REMOTE}" >/dev/null 2>&1 || true
    tunnel_started=0
  fi
}
stop_remote_stack() {
  local status=0
  ssh -T "${HORUS_REMOTE}" bash -s -- \
    "${HORUS_REMOTE_DIR}" "${remote_container}" <<'REMOTE_STOP' || status=$?
set -Eeuo pipefail
remote_dir="$1"
container="$2"
if [[ "${remote_dir}" == /* ]]; then
  project_dir="${remote_dir}"
else
  project_dir="${HOME}/${remote_dir}"
fi
if docker container inspect "${container}" >/dev/null 2>&1; then
  cd -- "${project_dir}"
  HORUS_SIM_CONTAINER_NAME="${container}" ./sim/run.sh stop
fi
REMOTE_STOP
  if (( status == 0 )); then
    rm -f -- "${started_file}" "${token_file}"
  fi
  return "${status}"
}
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if (( complete == 0 )); then
    close_tunnel
    if (( remote_started == 1 )); then
      if stop_remote_stack; then
        remote_started=0
      else
        printf 'Remote stack cleanup failed; state remains in %s for retry.\n' \
          "${state_dir}" >&2
      fi
    else
      rm -f -- "${token_file}"
    fi
    if (( tunnel_started == 0 )); then
      rm -f -- "${tunnel_log}"
    fi
  fi
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if ! ssh -T "${HORUS_REMOTE}" \
  'command -v docker >/dev/null && command -v rsync >/dev/null && docker info >/dev/null'; then
  printf 'Remote Docker/rsync preflight failed on %s (Docker must work without sudo).\n' \
    "${HORUS_REMOTE}" >&2
  exit 1
fi
if ssh -T "${HORUS_REMOTE}" docker container inspect "${remote_container}" >/dev/null 2>&1; then
  printf 'Remote container %s already exists on %s; refusing to replace it.\n' \
    "${remote_container}" "${HORUS_REMOTE}" >&2
  exit 1
fi

rsync --archive --compress --delete \
  --exclude='/.git/***' \
  --exclude='/.venv/***' \
  --exclude='/.venv-host/***' \
  --exclude='/.pytest_cache/***' \
  --exclude='/**/__pycache__/***' \
  --exclude='/**/.cache/***' \
  --exclude='/runs/***' \
  --exclude='/sim/logs/***' \
  --exclude='/sim/run/***' \
  --exclude='/models/***' \
  "${PROJECT_DIR}/" "${HORUS_REMOTE}:${HORUS_REMOTE_DIR}/"

remote_arch="$(ssh -T "${HORUS_REMOTE}" uname -m)"
case "${remote_arch}" in
  x86_64|amd64) remote_arch=amd64 ;;
  aarch64|arm64) remote_arch=arm64 ;;
esac
remote_image_arch="$(ssh -T "${HORUS_REMOTE}" docker image inspect \
  --format '{{.Architecture}}' "${HORUS_REMOTE_SIM_IMAGE}" 2>/dev/null || true)"
remote_image_id="$(ssh -T "${HORUS_REMOTE}" docker image inspect \
  --format '{{.Id}}' "${HORUS_REMOTE_SIM_IMAGE}" 2>/dev/null || true)"
local_image_arch="$(docker image inspect --format '{{.Architecture}}' \
  "${HORUS_REMOTE_SIM_IMAGE}" 2>/dev/null || true)"
local_image_id="$(docker image inspect --format '{{.Id}}' \
  "${HORUS_REMOTE_SIM_IMAGE}" 2>/dev/null || true)"
if [[ "${remote_image_arch}" != "${remote_arch}" \
  || -z "${remote_image_id}" \
  || ( -n "${local_image_id}" && "${remote_image_id}" != "${local_image_id}" ) ]]; then
  if [[ -n "${local_image_id}" && "${local_image_arch}" == "${remote_arch}" ]]; then
    printf 'Transferring %s to %s over SSH.\n' \
      "${HORUS_REMOTE_SIM_IMAGE}" "${HORUS_REMOTE}"
    docker save "${HORUS_REMOTE_SIM_IMAGE}" | \
      ssh -T "${HORUS_REMOTE}" docker load
  else
    printf 'Building %s natively on %s; no matching local image is available.\n' \
      "${HORUS_REMOTE_SIM_IMAGE}" "${HORUS_REMOTE}"
    ssh -T "${HORUS_REMOTE}" bash -s -- \
      "${HORUS_REMOTE_DIR}" "${HORUS_REMOTE_SIM_IMAGE}" <<'REMOTE_BUILD'
set -Eeuo pipefail
remote_dir="$1"
image="$2"
if [[ "${remote_dir}" == /* ]]; then
  project_dir="${remote_dir}"
else
  project_dir="${HOME}/${remote_dir}"
fi
cd -- "${project_dir}"
HORUS_SIM_IMAGE="${image}" ./sim/run.sh build
REMOTE_BUILD
  fi
fi

printf 'Ensuring perception model cache on %s.\n' "${HORUS_REMOTE}"
ssh -T "${HORUS_REMOTE}" bash -s -- "${HORUS_REMOTE_DIR}" <<'REMOTE_MODELS'
set -Eeuo pipefail
remote_dir="$1"
if [[ "${remote_dir}" == /* ]]; then
  project_dir="${remote_dir}"
else
  project_dir="${HOME}/${remote_dir}"
fi
cd -- "${project_dir}"
if [[ ! -f models/yolov8n.onnx || ! -f models/osnet_x0_25_msmt17.onnx ]]; then
  model_tools_dir="$(mktemp -d "${TMPDIR:-/tmp}/horus-model-tools.XXXXXX")"
  trap 'rm -rf -- "${model_tools_dir}"' EXIT
  python3 -m venv "${model_tools_dir}/venv"
  "${model_tools_dir}/venv/bin/pip" install --disable-pip-version-check \
    --no-cache-dir 'onnx>=1.16,<2'
  HORUS_PYTHON="${model_tools_dir}/venv/bin/python" bash scripts/fetch_models.sh
fi
REMOTE_MODELS

printf 'Starting headless Gazebo/SITL on %s.\n' "${HORUS_REMOTE}"
remote_gz_override="${HORUS_REMOTE_GZ_IP:--}"
remote_relay="${HORUS_GZ_RELAY_REMOTE:--}"
ssh -T "${HORUS_REMOTE}" bash -s -- \
  "${HORUS_REMOTE_DIR}" "${HORUS_LOCAL_IP}" "${remote_gz_override}" \
  "${HORUS_GZ_PARTITION}" "${remote_relay}" "${HORUS_REMOTE_SIM_IMAGE}" \
  "${remote_container}" "${HORUS_REMOTE_SIM_GPU}" "${sitl_param_file}" <<'REMOTE_START'
set -Eeuo pipefail
remote_dir="$1"
local_ip="$2"
remote_gz_ip="$3"
partition="$4"
relay="$5"
image="$6"
container="$7"
gpu_mode="$8"
param_file="$9"
if [[ "${remote_dir}" == /* ]]; then
  project_dir="${remote_dir}"
else
  project_dir="${HOME}/${remote_dir}"
fi
if [[ "${remote_gz_ip}" == - ]]; then
  route="$(ip -4 -o route get "${local_ip}")"
  read -r -a route_fields <<< "${route}"
  remote_gz_ip=""
  for ((index = 0; index + 1 < ${#route_fields[@]}; index++)); do
    if [[ "${route_fields[index]}" == src ]]; then
      remote_gz_ip="${route_fields[index + 1]}"
      break
    fi
  done
fi
if [[ -z "${remote_gz_ip}" || "${remote_gz_ip}" == 127.* ]]; then
  printf 'Could not determine Koko Ethernet GZ_IP; set HORUS_REMOTE_GZ_IP.\n' >&2
  exit 1
fi
cd -- "${project_dir}"
export GZ_IP="${remote_gz_ip}"
export GZ_PARTITION="${partition}"
if [[ "${relay}" == - ]]; then
  unset GZ_RELAY
else
  export GZ_RELAY="${relay}"
fi
export HORUS_SIM_IMAGE="${image}"
export HORUS_SIM_CONTAINER_NAME="${container}"
export HORUS_SIM_GPU="${gpu_mode}"
export HORUS_SITL_PARAM_FILE="${param_file}"
./sim/run.sh start horus_indoor.sdf
REMOTE_START
remote_started=1
printf '%s\n' "${remote_container}" > "${started_file}"

remote_project_dir="${HORUS_REMOTE_DIR}"
if [[ "${remote_project_dir}" != /* ]]; then
  remote_project_dir="\$HOME/${remote_project_dir}"
fi
printf 'Starting authenticated MCP on %s.\n' "${HORUS_REMOTE}"
printf '%s\n' "${HORUS_MCP_TOKEN}" | ssh -T "${HORUS_REMOTE}" \
  bash "${remote_project_dir}/scripts/koko/start_mcp.sh" "${remote_container}" \
  "${HORUS_REMOTE_CONFIG:-/horus/config/horus.yaml}" "${HORUS_LOG_LEVEL:-INFO}"

ssh -T "${HORUS_REMOTE}" bash -s -- "${remote_container}" <<'REMOTE_READY'
set -Eeuo pipefail
container="$1"
for ((attempt = 0; attempt < 120; attempt++)); do
  if [[ "$(docker inspect --format '{{.State.Running}}' "${container}" 2>/dev/null || true)" != true ]]; then
    printf 'Remote simulation container stopped while MCP was starting.\n' >&2
    exit 1
  fi
  if docker exec "${container}" python3 -c \
    'import socket,sys; s=socket.socket(); s.settimeout(0.25); r=s.connect_ex(("127.0.0.1",8000)); s.close(); sys.exit(r)'; then
    exit 0
  fi
  sleep 0.5
done
printf 'Timed out waiting for remote loopback MCP on 127.0.0.1:8000.\n' >&2
exit 1
REMOTE_READY
# SITL (and so Gazebo's lockstep world) only runs once MCP has connected on 5760, so the camera
# renders frames only after this point.
printf 'Enabling Koko camera RTP.\n'
ssh -T "${HORUS_REMOTE}" bash -s -- \
  "${HORUS_REMOTE_DIR}" "${remote_container}" <<'REMOTE_CAMERA'
set -Eeuo pipefail
remote_dir="$1"
container="$2"
if [[ "${remote_dir}" == /* ]]; then
  project_dir="${remote_dir}"
else
  project_dir="${HOME}/${remote_dir}"
fi
"${project_dir}/sim/scripts/enable_camera_stream.sh" "${container}"
REMOTE_CAMERA

nohup ssh -M -S "${control_socket}" -N -T \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L "127.0.0.1:${local_mcp_port}:127.0.0.1:8000" "${HORUS_REMOTE}" \
  </dev/null >"${tunnel_log}" 2>&1 &
tunnel_pid=$!
tunnel_started=1
for ((attempt = 0; attempt < 60; attempt++)); do
  if python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(0.25); r=s.connect_ex(("127.0.0.1",int(sys.argv[1]))); s.close(); sys.exit(r)' "${local_mcp_port}" \
    >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "${tunnel_pid}" 2>/dev/null; then
    printf 'SSH MCP tunnel exited; inspect %s.\n' "${tunnel_log}" >&2
    exit 1
  fi
  sleep 0.25
done
if ! python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(0.25); r=s.connect_ex(("127.0.0.1",int(sys.argv[1]))); s.close(); sys.exit(r)' "${local_mcp_port}" \
  >/dev/null 2>&1; then
  printf 'Timed out waiting for local MCP tunnel at 127.0.0.1:%s.\n' "${local_mcp_port}" >&2
  exit 1
fi
complete=1
printf 'Remote stack is ready. MCP is loopback-only at http://127.0.0.1:%s/mcp.\n' "${local_mcp_port}"
printf 'GZ partition %s; local %s -> remote %s.\n' \
  "${HORUS_GZ_PARTITION}" "${HORUS_LOCAL_GZ_IP}" \
  "${HORUS_REMOTE_GZ_IP:-route-selected}"
printf 'MCP token stored with mode 0600 at %s.\n' "${token_file}"
