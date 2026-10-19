#!/usr/bin/env bash
# S3 safety proof on the remote simulator: an executor-issued goto toward the east wall (target beyond
# the wall, inside the 8 m radius geofence of config/horus_s3.yaml) must be stopped by ArduPilot
# proximity avoidance fed by the ToF ring. Judged on Gazebo ground truth: the vehicle's centre must stay
# west of the wall's inner face (x = 4.9 m) minus its 0.3 m body radius.
# Usage: sim/scripts/s3_wall_trials.sh [TRIALS] (default 3). Results: sim/logs/s3/.
set -Eeuo pipefail
trials="${1:-3}"
source "$(dirname -- "${BASH_SOURCE[0]}")/trial_lib.sh"
out_dir="sim/logs/s3"
mkdir -p "${out_dir}"

for ((i = 1; i <= trials; i++)); do
  HORUS_REMOTE_CONFIG=/horus/config/horus_s3.yaml trial_remote_up "${out_dir}/trial${i}_remote_up.log"
  trial_gt_start "s3_trial${i}_gt.jsonl" --rate-hz 10
  sleep 10
  "${horus_bin}/horus-mcp-call" take_off --args '{"altitude_m": 1.5, "heading_deg": 90.0, "ttl_s": 90}' > "${out_dir}/trial${i}.log"
  sleep 35
  "${horus_bin}/horus-mcp-call" goto --args '{"pose": {"north_m": 0.0, "east_m": 6.5, "up_m": 1.5}, "ttl_s": 30}' >> "${out_dir}/trial${i}.log"
  sleep 45
  "${horus_bin}/horus-mcp-call" get_state > "${out_dir}/trial${i}_state.json"
  "${horus_bin}/horus-mcp-call" rth >> "${out_dir}/trial${i}.log" || true
  sleep 30
  trial_gt_stop "s3_trial${i}_gt.jsonl" "${out_dir}/trial${i}_gt.jsonl"
  bash scripts/koko/remote_down.sh > /dev/null 2>&1
done

"${horus_bin}/python" - "${out_dir}" "${trials}" <<'PY' | tee "${out_dir}/summary.json"
import json, pathlib, sys
out, trials = pathlib.Path(sys.argv[1]), int(sys.argv[2])
wall_face_x, body_radius = 4.9, 0.3
results = []
for i in range(1, trials + 1):
    samples = [json.loads(l) for l in (out / f"trial{i}_gt.jsonl").read_text().splitlines() if l.strip()]
    xs = [s["models"]["horus_iris"][0] for s in samples if "horus_iris" in s["models"]]
    max_x = max(xs) if xs else None
    results.append({
        "trial": i,
        "max_east_m": round(max_x, 3) if max_x is not None else None,
        "min_clearance_to_wall_m": round(wall_face_x - body_radius - max_x, 3) if max_x is not None else None,
        "contact": max_x is None or max_x > wall_face_x - body_radius,
    })
print(json.dumps({"requested_east_m": 6.5, "wall_inner_face_x_m": wall_face_x, "body_radius_m": body_radius,
                  "trials": results, "no_contact_all": all(not r["contact"] for r in results)}, indent=2))
PY
