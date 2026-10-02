#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
: "${HORUS_SIM_IMAGE:=horus-sim:local-s1s2}"
: "${HORUS_SIM_GPU:=amd}"
export HORUS_SIM_IMAGE HORUS_SIM_GPU
source "${SCRIPT_DIR}/common.sh"

usage() {
  printf 'Usage: %s [--remote SSH_ALIAS] [campaign CLI args...]\n' "$0"
  printf '       %s --status CAMPAIGN_ID\n' "$0"
  printf '       %s --fetch CAMPAIGN_ID [LOCAL_OUTPUT_DIR]\n' "$0"
  printf 'Campaigns run detached on Koko; SSH loss does not stop the remote worker.\n'
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--remote" ]]; then
  [[ $# -ge 2 ]] || { usage >&2; exit 2; }
  export HORUS_REMOTE="$2"
  shift 2
fi
horus_koko_init

for required in ssh rsync docker python3; do
  command -v "$required" >/dev/null 2>&1 || {
    printf 'Required local command not found: %s\n' "$required" >&2
    exit 1
  }
done

remote_home="$(ssh -T "${HORUS_REMOTE}" 'printf "%s" "$HOME"')"
if [[ "${HORUS_REMOTE_DIR}" == /* ]]; then
  remote_project="${HORUS_REMOTE_DIR}"
else
  remote_project="${remote_home%/}/${HORUS_REMOTE_DIR}"
fi

fetch_campaign() {
  local campaign_id="$1" destination="${2:-}"
  if [[ ! "${campaign_id}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf 'Campaign ID contains unsupported characters.\n' >&2
    return 2
  fi
  local relative="runs/campaign/${campaign_id}"
  if [[ -z "${destination}" ]]; then
    destination="${PROJECT_DIR}/${relative}"
  elif [[ "${destination}" != /* ]]; then
    destination="${PROJECT_DIR}/${destination}"
  fi
  mkdir -p -- "${destination}"
  rsync --archive --compress "${HORUS_REMOTE}:${remote_project}/${relative}/" "${destination%/}/"
  printf 'Fetched %s/%s to %s\n' "${HORUS_REMOTE}" "${relative}" "${destination}"
}


if [[ "${1:-}" == --status ]]; then
  [[ $# -eq 2 && "$2" =~ ^[A-Za-z0-9._-]+$ ]] || { usage >&2; exit 2; }
  campaign_rel="runs/campaign/$2"
  ssh -T "${HORUS_REMOTE}" bash -s -- "${remote_project}" "${campaign_rel}" <<'REMOTE_STATUS'
set -Eeuo pipefail
folder="$1/$2"
status_file="$folder/.campaign-exit-code"
pid_file="$folder/.launcher-pid"
if [[ -f "${status_file}" ]]; then
  printf 'complete exit=%s\n' "$(<"${status_file}")"
elif [[ -f "${pid_file}" ]]; then
  worker_pid="$(<"${pid_file}")"
  if [[ "${worker_pid}" =~ ^[1-9][0-9]*$ ]] && kill -0 "${worker_pid}" 2>/dev/null; then
    printf 'running\n'
  else
    printf 'worker status unavailable\n'
  fi
else
  printf 'worker status unavailable\n'
fi
REMOTE_STATUS
  exit 0
fi

if [[ "${1:-}" == --fetch ]]; then
  [[ $# -ge 2 && $# -le 3 ]] || { usage >&2; exit 2; }
  fetch_campaign "$2" "${3:-}"
  exit 0
fi

campaign_args=()
requested_output=""
while (($#)); do
  case "$1" in
    --out)
      [[ $# -ge 2 ]] || { printf '%s\n' '--out requires a path' >&2; exit 2; }
      requested_output="$2"
      shift 2
      ;;
    --out=*)
      requested_output="${1#--out=}"
      shift
      ;;
    *)
      campaign_args+=("$1")
      shift
      ;;
  esac
done

local_image="${HORUS_SIM_IMAGE}"
remote_image="${HORUS_REMOTE_SIM_IMAGE}"
remote_gpu="${HORUS_REMOTE_SIM_GPU:-amd}"
container="${HORUS_REMOTE_SIM_CONTAINER_NAME:-horus-sim}"
for image in "${local_image}" "${remote_image}"; do
  [[ "${image}" =~ ^[A-Za-z0-9/:._-]+$ ]] || {
    printf 'Invalid Docker image reference: %s\n' "${image}" >&2
    exit 2
  }
done
[[ "${remote_gpu}" =~ ^(amd|mesa|off|on|auto)$ ]] || {
  printf 'Invalid remote GPU mode: %s\n' "${remote_gpu}" >&2
  exit 2
}

printf 'Syncing project to %s:%s (preserving remote runs, sim logs/run/generated, and models).\n' \
  "${HORUS_REMOTE}" "${remote_project}"
ssh -T "${HORUS_REMOTE}" 'command -v docker >/dev/null && command -v rsync >/dev/null && docker info >/dev/null'
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
  --exclude='/sim/generated/***' \
  --exclude='/models/***' \
  "${PROJECT_DIR}/" "${HORUS_REMOTE}:${remote_project}/"

local_arch="$(docker image inspect --format '{{.Architecture}}' "${local_image}")"
remote_arch="$(ssh -T "${HORUS_REMOTE}" uname -m)"
case "${remote_arch}" in
  x86_64|amd64) remote_arch=amd64 ;;
  aarch64|arm64) remote_arch=arm64 ;;
esac
if [[ "${local_arch}" != "${remote_arch}" ]]; then
  printf 'Local image architecture %s does not match %s architecture %s.\n' \
    "${local_arch}" "${HORUS_REMOTE}" "${remote_arch}" >&2
  exit 1
fi
local_image_id="$(docker image inspect --format '{{.Id}}' "${local_image}")"
remote_image_id="$(ssh -T "${HORUS_REMOTE}" docker image inspect --format '{{.Id}}' "${remote_image}" 2>/dev/null || true)"
if [[ "${remote_image_id}" != "${local_image_id}" ]]; then
  printf 'Transferring %s to %s.\n' "${local_image}" "${HORUS_REMOTE}"
  docker save "${local_image}" | ssh -T "${HORUS_REMOTE}" docker image load
  if [[ "${local_image}" != "${remote_image}" ]]; then
    ssh -T "${HORUS_REMOTE}" docker image tag "${local_image}" "${remote_image}"
  fi
fi

if remote_container_state="$(ssh -T "${HORUS_REMOTE}" docker inspect --format '{{.State.Status}}' "${container}" 2>/dev/null)"; then
  case "${remote_container_state}" in
    running|restarting|paused)
      printf 'Remote container %s is %s on %s; refusing to replace it.\n' \
        "${container}" "${remote_container_state}" "${HORUS_REMOTE}" >&2
      exit 1
      ;;
    created|exited|dead)
      printf 'Removing stale stopped container %s on %s; mounted run data is preserved.\n' \
        "${container}" "${HORUS_REMOTE}"
      ssh -T "${HORUS_REMOTE}" docker rm "${container}" >/dev/null
      ;;
    *)
      printf 'Remote container %s has unexpected state %s; refusing to replace it.\n' \
        "${container}" "${remote_container_state}" >&2
      exit 1
      ;;
  esac
fi

ssh -T "${HORUS_REMOTE}" bash -s -- "${remote_project}" <<'REMOTE_MARKER'
set -Eeuo pipefail
project="$1"
run_root="${project}/sim/run"
active="${run_root}/.active"
if [[ -L "${active}" || ( -e "${active}" && ! -f "${active}" ) ]]; then
  printf 'Refusing to alter invalid simulation run marker %s.\n' "${active}" >&2
  exit 1
fi
if [[ -f "${active}" ]]; then
  IFS= read -r run_id < "${active}" || true
  if [[ ! "${run_id:-}" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]]; then
    printf 'Refusing to alter invalid simulation run ID in %s.\n' "${active}" >&2
    exit 1
  fi
  target="${run_root}/${run_id}"
  if [[ -L "${target}" || ( -e "${target}" && ! -d "${target}" ) ]]; then
    printf 'Refusing to alter invalid stale run directory %s.\n' "${target}" >&2
    exit 1
  fi
  if [[ -d "${target}" ]]; then
    root_real="$(realpath -e -- "${run_root}")"
    target_real="$(realpath -e -- "${target}")"
    if [[ "$(dirname -- "${target_real}")" != "${root_real}" ]]; then
      printf 'Refusing to alter stale run outside %s.\n' "${run_root}" >&2
      exit 1
    fi
  fi
  printf 'Removing stale simulation marker %s; preserving its run directory.\n' "${active}"
  rm -f -- "${active}"
fi
REMOTE_MARKER

campaign_id="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
remote_output="runs/campaign/${campaign_id}"
local_output="${PROJECT_DIR}/${remote_output}"
if [[ -n "${requested_output}" ]]; then
  if [[ "${requested_output}" == /* ]]; then
    local_output="${requested_output}"
  else
    local_output="${PROJECT_DIR}/${requested_output}"
  fi
fi
mkdir -p -- "${local_output}"
payload_b64="$(python3 -c 'import base64,json,sys; print(base64.b64encode(json.dumps({"project":sys.argv[1],"output":sys.argv[2],"image":sys.argv[3],"gpu":sys.argv[4],"container":sys.argv[5],"arguments":json.loads(base64.b64decode(sys.argv[6]))}).encode()).decode())' \
  "${remote_project}" "${remote_output}" "${remote_image}" "${remote_gpu}" "${container}" \
  "$(python3 -c 'import base64,json,sys; print(base64.b64encode(json.dumps(sys.argv[1:]).encode()).decode())' "${campaign_args[@]}")")"

printf 'Launching detached campaign %s on %s (HORUS_SIM_GPU=%s).\n' \
  "${campaign_id}" "${HORUS_REMOTE}" "${remote_gpu}"
ssh -T "${HORUS_REMOTE}" bash -s -- "${remote_project}" "${remote_output}" "${payload_b64}" <<'REMOTE_LAUNCH'
set -Eeuo pipefail
project="$1"
output="$2"
payload="$3"
cd -- "${project}"
mkdir -p -- "${output}"
nohup setsid python3 scripts/koko/campaign_worker.py "${payload}" \
  </dev/null >"${output}/campaign-launcher.log" 2>&1 &
worker_pid=$!
printf '%s\n' "${worker_pid}" > "${output}/.launcher-pid"
printf 'pid=%s\n' "${worker_pid}"
REMOTE_LAUNCH

printf 'Campaign ID: %s\nRemote log: %s/%s/campaign-host.log\n' \
  "${campaign_id}" "${HORUS_REMOTE}" "${remote_project}/${remote_output}"
printf 'Detached status/fetch commands: %s --status %s ; %s --fetch %s\n' \
  "$0" "${campaign_id}" "$0" "${campaign_id}"

while true; do
  if ! state="$(ssh -T "${HORUS_REMOTE}" bash -s -- "${remote_project}" "${remote_output}" <<'REMOTE_POLL'
set -Eeuo pipefail
folder="$1/$2"
status_file="$folder/.campaign-exit-code"
pid_file="$folder/.launcher-pid"
if [[ -f "${status_file}" ]]; then
  printf 'exit:%s\n' "$(<"${status_file}")"
elif [[ -f "${pid_file}" ]]; then
  worker_pid="$(<"${pid_file}")"
  if [[ "${worker_pid}" =~ ^[1-9][0-9]*$ ]] && kill -0 "${worker_pid}" 2>/dev/null; then
    printf 'running\n'
  else
    printf 'lost\n'
  fi
else
  printf 'lost\n'
fi
REMOTE_POLL
  )"; then
    printf 'SSH polling failed; detached campaign %s continues if Koko remains reachable.\n' \
      "${campaign_id}" >&2
    exit 2
  fi
  case "${state}" in
    running) sleep 30 ;;
    exit:*)
      remote_exit="${state#exit:}"
      break
      ;;
    *)
      printf 'Remote campaign %s exited without a status file; fetching available artifacts.\n' \
        "${campaign_id}" >&2
      remote_exit=1
      break
      ;;
  esac
done

fetch_campaign "${campaign_id}" "${local_output}"
printf 'Remote campaign exit code: %s\n' "${remote_exit}"
exit "${remote_exit}"
