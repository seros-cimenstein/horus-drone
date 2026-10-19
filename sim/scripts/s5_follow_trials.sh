#!/usr/bin/env bash
# S5: follow the blue-shirt person through the corridor-screen occlusion on the remote simulator.
# One simulator session; each run takes off, selects the target by shirt colour, follows for the
# mission TTL, records re-acquisition, then returns home and lands before the next run.
# Usage: sim/scripts/s5_follow_trials.sh [RUNS] (default 5). Results: runs/S5_*.json, sim/logs/s5/.
set -Eeuo pipefail
runs="${1:-5}"
source "$(dirname -- "${BASH_SOURCE[0]}")/trial_lib.sh"
out_dir="sim/logs/s5"
mkdir -p "${out_dir}" runs
trial_remote_up "${out_dir}/remote_up.log"

for ((i = 1; i <= runs; i++)); do
  log="${out_dir}/run${i}.log"
  run_marker="$(ssh "${remote}" 'mktemp')"
  trial_gt_start "s5_run${i}_gt.jsonl" --rate-hz 5
  printf '%s\n' "${HORUS_MCP_TOKEN}" | ssh "${remote}" \
    "IFS= read -r t; docker exec --env HORUS_MCP_TOKEN=\"\$t\" --workdir /horus ${container} \
     /opt/horus-venv/bin/horus --server-url http://127.0.0.1:8000/mcp \
     --mission missions/S5_follow_occlusion.yaml" > "${log}" 2>&1 || printf 'run %d mission exited non-zero\n' "${i}" >> "${log}"
  "${horus_bin}/horus-mcp-call" rth >> "${log}" 2>&1 || true
  trial_wait_disarmed 240 || printf 'run %d did not disarm within 240 s\n' "${i}" >> "${log}"
  # The mover stops mid-route; put the target back at its start pose (facing the room) for the next run.
  ssh "${remote}" docker exec "${container}" gz service -s /world/horus_indoor/set_pose \
    --reqtype gz.msgs.Pose --reptype gz.msgs.Boolean --timeout 3000 \
    --req "'name: \"person_blue\", position: {x: -1.8, y: 3.0, z: 0}, orientation: {z: -0.7071068, w: 0.7071068}'" \
    >> "${log}" 2>&1 || true
  trial_gt_stop "s5_run${i}_gt.jsonl" "${out_dir}/run${i}_gt.jsonl" || true
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
