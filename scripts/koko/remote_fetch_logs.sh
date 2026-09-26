#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
source "${SCRIPT_DIR}/common.sh"
horus_koko_init

for required in ssh rsync; do
  if ! command -v "${required}" >/dev/null 2>&1; then
    printf '%s is required to fetch remote logs.\n' "${required}" >&2
    exit 1
  fi
done

ssh -T "${HORUS_REMOTE}" bash -s -- "${HORUS_REMOTE_DIR}" <<'REMOTE_DIRS'
set -Eeuo pipefail
remote_dir="$1"
if [[ "${remote_dir}" == /* ]]; then
  project_dir="${remote_dir}"
else
  project_dir="${HOME}/${remote_dir}"
fi
mkdir -p -- "${project_dir}/sim/logs" "${project_dir}/runs"
REMOTE_DIRS
mkdir -p -- "${PROJECT_DIR}/sim/logs" "${PROJECT_DIR}/runs"

rsync --archive --compress --backup --suffix=".before-${HORUS_REMOTE}" \
  "${HORUS_REMOTE}:${HORUS_REMOTE_DIR}/sim/logs/" \
  "${PROJECT_DIR}/sim/logs/"
rsync --archive --compress --backup --suffix=".before-${HORUS_REMOTE}" \
  "${HORUS_REMOTE}:${HORUS_REMOTE_DIR}/runs/" \
  "${PROJECT_DIR}/runs/"
printf 'Fetched remote sim/logs and runs from %s.\n' "${HORUS_REMOTE}"
