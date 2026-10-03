#!/usr/bin/env bash
set -Eeuo pipefail

venv=/horus/models/grounder-venv
python="${venv}/bin/python"
if [[ ! -x "${python}" ]]; then
  printf 'Grounder not started: runtime venv is absent at %s; run scripts/setup_grounder.sh inside horus-sim.\n' \
    "${venv}" >&2
  exit 0
fi

log_dir=/horus/sim/logs
log_file="${log_dir}/horus-grounder.log"
pid_file="${log_dir}/horus-grounder.pid"
mkdir -p -- "${log_dir}"
export HF_HOME="${HORUS_GROUNDER_HF_CACHE:-/horus/models/hf}"
if /opt/horus-venv/bin/python -c \
  'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8100/health", timeout=0.4)' \
  >/dev/null 2>&1; then
  printf '%s\n' 'Grounder already responds on 127.0.0.1:8100.'
  exit 0
fi
if [[ -f "${pid_file}" ]]; then
  read -r pid < "${pid_file}" || pid=""
  if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
    command_line="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
    if [[ "${command_line}" == *horus.grounder.server* ]]; then
      printf 'Grounder is loading under PID %s; see %s.\n' "${pid}" "${log_file}"
      exit 0
    fi
  fi
  rm -f -- "${pid_file}"
fi

model="${HORUS_GROUNDER_MODEL:-grounding-dino-tiny}"
threads="${HORUS_GROUNDER_THREADS:-6}"
nohup env HF_HOME="${HF_HOME}" PYTHONPATH=/horus \
  "${python}" -m horus.grounder.server --host 127.0.0.1 --port 8100 \
  --model "${model}" --threads "${threads}" \
  >>"${log_file}" 2>&1 </dev/null &
pid=$!
printf '%s\n' "${pid}" > "${pid_file}"
printf 'Started grounder PID %s on 127.0.0.1:8100; log: %s\n' "${pid}" "${log_file}"
