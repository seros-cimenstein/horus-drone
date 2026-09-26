#!/usr/bin/env bash
# S5: follow the blue-shirt person through the corridor-screen occlusion on the remote simulator.
# One simulator session; each run takes off, selects the target by shirt colour, follows for the
# mission TTL, records re-acquisition, then returns home and lands before the next run.
# Usage: sim/scripts/s5_follow_trials.sh [RUNS] (default 5). Results: runs/S5_*.json, sim/logs/s5/.
set -Eeuo pipefail
runs="${1:-5}"
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "${project_dir}"
remote="${HORUS_REMOTE:-koko}"
remote_dir="${HORUS_REMOTE_DIR:-horus}"
container="${HORUS_REMOTE_SIM_CONTAINER_NAME:-horus-sim}"
horus_bin="${HORUS_HOST_VENV:-${project_dir}/.venv-host}/bin"
token_file="${XDG_RUNTIME_DIR:-/tmp}/horus-$(id -u)/${remote}/mcp-token"
out_dir="sim/logs/s5"
mkdir -p "${out_dir}" runs
export HORUS_MCP_URL="http://127.0.0.1:${HORUS_MCP_LOCAL_PORT:-8000}/mcp"

stop_remote() { bash scripts/koko/remote_down.sh >/dev/null 2>&1 || true; }
trap stop_remote EXIT
bash scripts/koko/remote_up.sh > "${out_dir}/remote_up.log" 2>&1
HORUS_MCP_TOKEN="$(cat "${token_file}")"
export HORUS_MCP_TOKEN

wait_disarmed() {
  local armed_seen=0 deadline=$((SECONDS + 240)) state
  while (( SECONDS < deadline )); do
    state="$("${horus_bin}/horus-mcp-call" get_state 2>/dev/null)" || { sleep 2; continue; }
    grep -q '"armed": *true' <<< "${state}" && armed_seen=1
    if (( armed_seen )) && grep -q '"armed": *false' <<< "${state}"; then return 0; fi
    sleep 2
  done
  return 1
}

for ((i = 1; i <= runs; i++)); do
  log="${out_dir}/run${i}.log"
  run_marker="$(ssh "${remote}" 'mktemp')"
  ssh "${remote}" docker exec --detach "${container}" /opt/horus-venv/bin/python \
    /horus/sim/scripts/gt_trace.py "/horus/sim/logs/s5_run${i}_gt.jsonl" --rate-hz 5
  printf '%s\n' "${HORUS_MCP_TOKEN}" | ssh "${remote}" \
    "IFS= read -r t; docker exec --env HORUS_MCP_TOKEN=\"\$t\" --workdir /horus ${container} \
     /opt/horus-venv/bin/horus --server-url http://127.0.0.1:8000/mcp \
     --mission missions/S5_follow_occlusion.yaml" > "${log}" 2>&1 || printf 'run %d mission exited non-zero\n' "${i}" >> "${log}"
  "${horus_bin}/horus-mcp-call" rth >> "${log}" 2>&1 || true
  wait_disarmed || printf 'run %d did not disarm within 240 s\n' "${i}" >> "${log}"
  # The mover stops mid-route; put the target back at its start pose (facing the room) for the next run.
  ssh "${remote}" docker exec "${container}" gz service -s /world/horus_indoor/set_pose \
    --reqtype gz.msgs.Pose --reptype gz.msgs.Boolean --timeout 3000 \
    --req "'name: \"person_blue\", position: {x: -1.8, y: 3.0, z: 0}, orientation: {z: -0.7071068, w: 0.7071068}'" \
    >> "${log}" 2>&1 || true
  ssh "${remote}" "docker exec ${container} pkill -INT -f gt_trace.py; sleep 1"
  scp -q "${remote}:${remote_dir}/sim/logs/s5_run${i}_gt.jsonl" "${out_dir}/run${i}_gt.jsonl" || true
  result="$(ssh "${remote}" "find ${remote_dir}/runs -name 'S5_*.json' -newer ${run_marker} | sort | tail -1")"
  ssh "${remote}" "rm -f ${run_marker}"
  if [[ -n "${result}" ]]; then
    scp -q "${remote}:${result}" runs/
    "${horus_bin}/python" sim/scripts/s5_analyze.py "${out_dir}/run${i}_gt.jsonl" "runs/${result##*/}" \
      > "${out_dir}/run${i}_analysis.json"
  else
    printf '{"run": %d, "passed": false, "error": "no S5 result JSON (see run%d.log)"}\n' "${i}" "${i}" \
      > "${out_dir}/run${i}_analysis.json"
  fi
  printf 'run %d: %s\n' "${i}" "$(tr -d '\n ' < "${out_dir}/run${i}_analysis.json" | grep -o '"passed":[a-z]*' | tail -1)"
done
"${horus_bin}/python" - "${out_dir}" <<'PY' | tee "${out_dir}/summary.json"
import json, pathlib, sys
runs = [json.loads(p.read_text()) for p in sorted(pathlib.Path(sys.argv[1]).glob("run*_analysis.json"))]
passed = sum(1 for r in runs if r.get("passed"))
print(json.dumps({"runs": len(runs), "passed": passed, "success_rate": passed / len(runs) if runs else 0.0,
                  "criterion": "occluded >= 1.5 s sim and drone still pointing at person_blue (median error <= 25 deg, "
                               "closer than to person_red) for 3 s after reappearance",
                  "per_run": runs}, indent=2))
PY
