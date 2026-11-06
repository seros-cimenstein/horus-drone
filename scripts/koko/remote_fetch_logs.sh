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

# remote_up.sh records Koko's clock at session start; remote_down.sh keeps this marker so the
# fetch still works after the stack is stopped. Only files under Koko's sim/logs/ and runs/
# written since then are copied: the whole runs/ tree is several GB.
session_start_file="${HORUS_REMOTE_STATE_DIR}/session-start"
if [[ ! -f "${session_start_file}" ]]; then
  printf 'No managed %s session start marker at %s; fetching nothing from sim/logs or runs.\n' \
    "${HORUS_REMOTE}" "${session_start_file}"
  exit 0
fi
read -r session_start < "${session_start_file}" || session_start=""
if [[ ! "${session_start}" =~ ^[0-9]+$ ]]; then
  printf 'Session start marker %s is not a Unix timestamp.\n' "${session_start_file}" >&2
  exit 1
fi

file_list="$(mktemp "${TMPDIR:-/tmp}/horus-fetch-list.XXXXXX")"
trap 'rm -f -- "${file_list}"' EXIT
horus_koko_remote_script "${session_start}" > "${file_list}" <<'REMOTE_LIST'
set -Eeuo pipefail
cd -- "${project_dir}"
for dir in sim/logs runs; do
  if [[ -d "${dir}" ]]; then
    find "${dir}" -type f -newermt "@$1" -print0
  fi
done
REMOTE_LIST

file_count="$(tr -cd '\0' < "${file_list}" | wc -c)"
if (( file_count == 0 )); then
  printf 'No sim/logs or runs files on %s changed since the session start (%s).\n' \
    "${HORUS_REMOTE}" "$(date -d "@${session_start}" '+%F %T')"
  exit 0
fi

rsync --archive --compress --backup --suffix=".before-${HORUS_REMOTE}" \
  --from0 --files-from="${file_list}" \
  "${HORUS_REMOTE}:${HORUS_REMOTE_DIR}/" "${PROJECT_DIR}/"
printf 'Fetched %s sim/logs and runs files written on %s since %s.\n' \
  "${file_count}" "${HORUS_REMOTE}" "$(date -d "@${session_start}" '+%F %T')"
