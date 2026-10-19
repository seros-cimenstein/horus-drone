#!/usr/bin/env bash
# S6: "find the person in the red shirt" end to end with the Claude Code commander (MCP only, no API key).
# Each run: fresh remote simulator, Gazebo ground-truth trace, commander mission from this machine
# through the MCP tunnel, full commander/MCP event log, then an analysis of which person was approached.
# Usage: sim/scripts/s6_commander_trials.sh [RUNS] (default 1). Results: sim/logs/s6/.
set -Eeuo pipefail
runs="${1:-1}"
source "$(dirname -- "${BASH_SOURCE[0]}")/trial_lib.sh"
out_dir="sim/logs/s6"
mkdir -p "${out_dir}"
command -v claude >/dev/null || { printf 'claude CLI not found on PATH\n' >&2; exit 1; }

for ((i = 1; i <= runs; i++)); do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  prefix="${out_dir}/run${i}_${stamp}"
  trial_remote_up "${prefix}_remote_up.log"
  trial_gt_start "s6_gt_${stamp}.jsonl"
  sleep 10
  started="$(date +%s)"
  HORUS_EVENT_LOG="${project_dir}/${prefix}_events.jsonl" "${horus_bin}/horus" \
    --server-url "${HORUS_MCP_URL}" --mission missions/S6_find_red_shirt.yaml \
    > "${prefix}_mission.log" 2>&1 || printf 'mission exited non-zero\n' >> "${prefix}_mission.log"
  printf '%s\n' "$(( $(date +%s) - started ))" > "${prefix}_wall_seconds.txt"
  "${horus_bin}/horus-mcp-call" get_state > "${prefix}_final_state.json" 2>&1 || true
  trial_gt_stop "s6_gt_${stamp}.jsonl" "${prefix}_groundtruth.jsonl"
  bash scripts/koko/remote_down.sh > /dev/null 2>&1
  "${horus_bin}/python" sim/scripts/s6_analyze.py "${prefix}" | tee "${prefix}_analysis.json"
done
