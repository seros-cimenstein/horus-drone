#!/usr/bin/env bash
# S7: latency sweep on the remote simulator (design headline experiment).
# Every trial starts from the same state: the drone at a fixed pose/heading and the blue-shirt target
# restarted on its route and already walking, so it moves during the injected delay. Each trial is one
# deterministic client run (scripts/latency_sweep.py, no LLM): select the target in a frame, wait D
# seconds of simulation time, then act by tracker ID ("id") or by the stale box ("box", fast-forward
# compensation on/off). Usage: sim/scripts/s7_latency_trials.sh [TRIALS_PER_CONDITION] [DELAYS]
# (defaults 5 and 0,0.5,1,2,3). Results: runs/s7/<stamp>/*.jsonl and sim/logs/s7/summary_<stamp>.json.
set -Eeuo pipefail
trials="${1:-5}"
delays="${2:-0,0.5,1,2,3}"
source "$(dirname -- "${BASH_SOURCE[0]}")/trial_lib.sh"
walk_delay_s="${HORUS_S7_WALK_DELAY:-13}"  # route holds 8 s of sim time before walking (~12 s wall at RTF 0.65)
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_dir="runs/s7/${stamp}"
log="sim/logs/s7/sweep_${stamp}.log"
mkdir -p "${out_dir}" sim/logs/s7
trial_remote_up sim/logs/s7/remote_up.log
call() { "${horus_bin}/horus-mcp-call" "$@"; }
wait_task() {
  local task_id="$1" status
  for _ in $(seq 1 60); do
    status="$(call get_task_status --args "{\"task_id\": \"${task_id}\"}" | "${horus_bin}/python" -c \
      'import json,sys; print(json.loads(sys.stdin.readline())["status"])')"
    case "${status}" in succeeded|failed|expired|cancelled) printf '%s' "${status}"; return 0 ;; esac
    sleep 1
  done
  printf 'timeout'
}
task_id_of() { "${horus_bin}/python" -c 'import json,sys; print(json.loads(sys.stdin.readline())["task_id"])'; }

sleep 10
wait_task "$(call take_off --args '{"altitude_m": 2.0, "heading_deg": 340.0, "ttl_s": 90}' | task_id_of)" >> "${log}"

IFS=, read -r -a delay_list <<< "${delays}"
for delay in "${delay_list[@]}"; do
  for condition in id box_on box_off; do
    for ((i = 1; i <= trials; i++)); do
      # Reset: fixed observation pose and heading, then restart the target's route.
      wait_task "$(call goto --args '{"pose": {"north_m": 0.3, "east_m": 0.0, "up_m": 2.0}, "heading_deg": 340.0, "ttl_s": 40}' \
        | task_id_of)" >> "${log}"
      ssh "${remote}" "docker exec ${container} pkill -f move_target.py; \
        docker exec ${container} gz service -s /world/horus_indoor/set_pose --reqtype gz.msgs.Pose \
          --reptype gz.msgs.Boolean --timeout 3000 \
          --req 'name: \"person_blue\", position: {x: -1.8, y: 3.0, z: 0}, orientation: {z: -0.7071068, w: 0.7071068}' >/dev/null; \
        docker exec --detach --workdir /horus ${container} /opt/horus-venv/bin/python scripts/move_target.py"
      sleep "${walk_delay_s}"
      case "${condition}" in
        id) mode_args=(--modes id) ;;
        box_on) mode_args=(--modes box --compensation on) ;;
        box_off) mode_args=(--modes box --compensation off) ;;
      esac
      "${horus_bin}/python" scripts/latency_sweep.py -n 1 --delays "${delay}" "${mode_args[@]}" \
        --output-dir "${out_dir}" 2>&1 | tee -a "${log}" | grep -E '^trial' | sed "s/^trial 1:/D=${delay} ${condition} #${i}:/"
    done
  done
done
ssh "${remote}" "docker exec ${container} pkill -f move_target.py" || true
call rth > /dev/null || true

"${horus_bin}/python" - "${out_dir}" <<'PY' | tee "sim/logs/s7/summary_${stamp}.json"
import json, pathlib, statistics, sys
rows = [json.loads(line) for path in sorted(pathlib.Path(sys.argv[1]).glob("*.jsonl")) for line in path.open() if line.strip()]
groups = {}
for row in rows:
    mode = row["mode"] if row["mode"] == "id" else f"box_{'on' if row['compensation'] else 'off'}"
    groups.setdefault((row["delay_s"], mode), []).append(row)
table = []
for (delay, mode), items in sorted(groups.items()):
    times = [r["time_to_target_s"] for r in items if r["success"] and r["time_to_target_s"] is not None]
    table.append({
        "delay_s": delay, "mode": mode, "trials": len(items),
        "success_rate": round(sum(r["success"] for r in items) / len(items), 2),
        "wrong_target_rate": round(sum(r["wrong_target"] for r in items) / len(items), 2),
        "median_time_to_target_sim_s": round(statistics.median(times), 2) if times else None,
        "statuses": sorted({r["result_status"] for r in items}),
        "measured_delay_sim_s": round(statistics.mean(r["sim_delay_s"] or 0 for r in items), 2),
    })
print(json.dumps({"trials": len(rows), "results": table}, indent=2))
PY
