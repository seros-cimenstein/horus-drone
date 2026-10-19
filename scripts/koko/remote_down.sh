#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/common.sh"
horus_koko_init

state_dir="${HORUS_REMOTE_STATE_DIR}"
control_socket="${state_dir}/ssh-control"
token_file="${state_dir}/mcp-token"
started_file="${state_dir}/started"
status=0

# The split GUI has its own container and run directory; never stop the local
# full-simulation container used by other experiments.
if ! HORUS_SIM_CONTAINER_NAME="${HORUS_GUI_CLIENT_CONTAINER_NAME}" \
  HORUS_SIM_RUN_ROOT="${PROJECT_DIR}/sim/run/gui-client" \
  "${PROJECT_DIR}/sim/run.sh" stop --purge; then
  status=1
fi

if [[ -f "${started_file}" ]]; then
  read -r recorded_container < "${started_file}" || recorded_container=""
  if [[ "${recorded_container}" != "${HORUS_REMOTE_SIM_CONTAINER_NAME}" ]]; then
    printf 'Remote state names unexpected container %s; refusing to stop it.\n' \
      "${recorded_container:-<empty>}" >&2
    status=1
  elif horus_koko_remote_script "${recorded_container}" <<'REMOTE_STOP'
set -Eeuo pipefail
container="$1"
if docker container inspect "${container}" >/dev/null 2>&1; then
  cd -- "${project_dir}"
  HORUS_SIM_CONTAINER_NAME="${container}" ./sim/run.sh stop
else
  printf 'Remote simulation container %s is already absent.\n' "${container}"
fi
REMOTE_STOP
  then
    rm -f -- "${started_file}" "${token_file}"
  else
    printf 'Could not stop the managed remote container; state remains for retry.\n' >&2
    status=1
  fi
else
  printf 'No managed remote simulation marker for %s.\n' "${HORUS_REMOTE}"
fi

if [[ -S "${control_socket}" ]]; then
  if ssh -S "${control_socket}" -O exit "${HORUS_REMOTE}" >/dev/null 2>&1; then
    rm -f -- "${control_socket}"
  else
    printf 'Could not close MCP SSH tunnel via %s.\n' "${control_socket}" >&2
    status=1
  fi
fi
if (( status == 0 )); then
  rm -f -- "${token_file}" "${state_dir}/tunnel.log"
  rmdir -- "${state_dir}" 2>/dev/null || true
fi
exit "${status}"
