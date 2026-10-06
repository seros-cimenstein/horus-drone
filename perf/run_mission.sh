#!/usr/bin/env bash
# Run one commander mission against a fresh headless indoor simulation and collect timing evidence.
# Usage: perf/run_mission.sh LABEL [MISSION_YAML]   (default: missions/S6_find_red_shirt.yaml)
# Output: perf/runs/<LABEL>_<UTC>/ with raw logs, summary.json and summary.md.
set -Eeuo pipefail

label="${1:?usage: perf/run_mission.sh LABEL [MISSION_YAML]}"
mission="${2:-missions/S6_find_red_shirt.yaml}"
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "${project_dir}"
container="${HORUS_SIM_CONTAINER_NAME:-horus-sim}"
venv="${HORUS_HOST_VENV:-${project_dir}/.venv}"
out="perf/runs/${label}_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${out}"

command -v claude >/dev/null || { printf 'claude CLI not found on PATH\n' >&2; exit 1; }
if docker container inspect "${container}" >/dev/null 2>&1; then
  printf 'Container %s already exists; stop it first (./sim/run.sh stop).\n' "${container}" >&2
  exit 1
fi

export HORUS_MCP_URL="http://127.0.0.1:8000/mcp"
HORUS_MCP_TOKEN="$("${venv}/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
export HORUS_MCP_TOKEN
export HORUS_SITL_PARAM_FILE="${HORUS_SITL_PARAM_FILE:-/horus/config/ardupilot/horus_sitl.parm}"

mcp_pid=""
cleanup() {
  local status=$?
  trap - EXIT
  ./sim/run.sh stop >/dev/null 2>&1 || true
  [[ -n "${mcp_pid}" ]] && wait "${mcp_pid}" 2>/dev/null || true
  exit "${status}"
}
trap cleanup EXIT

git rev-parse HEAD > "${out}/commit.txt"
git status --porcelain > "${out}/dirty.txt"
cp config/horus.yaml "${out}/horus.yaml"

./sim/run.sh start horus_indoor.sdf > "${out}/sim_start.log" 2>&1
docker exec --workdir /horus --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" \
  "${container}" /opt/horus-venv/bin/horus-mcp > "${out}/mcp.log" 2>&1 &
mcp_pid=$!
deadline=$((SECONDS + 120))
until (exec 3<>/dev/tcp/127.0.0.1/8000) 2>/dev/null; do
  kill -0 "${mcp_pid}" 2>/dev/null || { printf 'horus-mcp exited during startup; see %s/mcp.log\n' "${out}" >&2; exit 1; }
  (( SECONDS < deadline )) || { printf 'Timed out waiting for horus-mcp on port 8000.\n' >&2; exit 1; }
  sleep 0.5
done
# The lockstep world only runs once MCP holds SITL's MAVLink link, so enable the camera afterwards.
bash sim/scripts/enable_camera_stream.sh "${container}" > "${out}/camera.log" 2>&1

docker exec --detach "${container}" /opt/horus-venv/bin/python \
  /horus/sim/scripts/gt_trace.py "/horus/${out}/groundtruth.jsonl"
docker exec --detach "${container}" /opt/horus-venv/bin/python \
  /horus/sim/scripts/s5_rtf_sampler.py "/horus/${out}/rtf.jsonl" "/horus/${out}/rtf.stop"

# Same starting condition for every run: the EKF has a position source and perception has frames.
deadline=$((SECONDS + 240))
until "${venv}/bin/horus-mcp-call" get_state 2>/dev/null | grep -q 'is using GPS'; do
  (( SECONDS < deadline )) || { printf 'Timed out waiting for EKF position readiness.\n' >&2; exit 1; }
  sleep 2
done
sleep 10

mission_started="$(date +%s.%N)"
mission_rc=0
HORUS_EVENT_LOG="${project_dir}/${out}/events.jsonl" "${venv}/bin/horus" \
  --server-url "${HORUS_MCP_URL}" --mission "${mission}" > "${out}/mission.log" 2>&1 || mission_rc=$?
mission_ended="$(date +%s.%N)"
printf '{"mission": "%s", "started": %s, "ended": %s, "return_code": %s}\n' \
  "${mission}" "${mission_started}" "${mission_ended}" "${mission_rc}" > "${out}/window.json"

# Let an in-progress return/landing settle before reading the final state (bounded).
deadline=$((SECONDS + 90))
until "${venv}/bin/horus-mcp-call" get_state > "${out}/final_state.json" 2>/dev/null \
  && grep -q '"armed": *false' "${out}/final_state.json"; do
  (( SECONDS < deadline )) || break
  sleep 3
done
# The MCP event log cuts tool results at 500 characters, so save each scan task's full status.
mcp_run_dir="$(sed 's#^/horus/##' runs/horus/current-run 2>/dev/null || true)"
if [[ -n "${mcp_run_dir}" && -f "${mcp_run_dir}/events.jsonl" ]]; then
  cp "${mcp_run_dir}/events.jsonl" "${out}/mcp_events.jsonl"
  while IFS= read -r task_id; do
    "${venv}/bin/horus-mcp-call" get_task_status --args "{\"task_id\": \"${task_id}\"}" \
      > "${out}/scan_task_${task_id}.json" 2>/dev/null || true
  done < <(grep '"tool":"scan"' "${out}/mcp_events.jsonl" | grep -o 'task_id\\":\\"[0-9a-f]*' | grep -o '[0-9a-f]*$' | sort -u)
fi
: > "${out}/rtf.stop"
docker exec "${container}" pkill -INT -f gt_trace.py || true
sleep 1
"${venv}/bin/python" perf/summarize.py "${out}"
printf 'Run directory: %s\n' "${out}"
