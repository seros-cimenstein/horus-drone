#!/usr/bin/env bash
# S4: GPS-free RTH landing accuracy on the remote simulator, with vs without AprilTag correction.
# Each trial uses a fresh simulator so ArduPilot's home (reset at arming) is always the pad.
# Usage: sim/scripts/s4_landing_trials.sh [TRIALS_PER_CONDITION] (default 3).
# HORUS_S4_PAD_OFFSET="x,y" (Gazebo metres) moves the AprilTag pad away from the take-off point before
# each flight, injecting that home-position error. Results: sim/logs/s4/<label>/ (label from HORUS_S4_LABEL,
# default ideal_home or pad_offset_<x>_<y>).
set -Eeuo pipefail
trials="${1:-3}"
source "$(dirname -- "${BASH_SOURCE[0]}")/trial_lib.sh"
pad_offset="${HORUS_S4_PAD_OFFSET:-}"
if [[ -n "${pad_offset}" && ! "${pad_offset}" =~ ^-?[0-9.]+,-?[0-9.]+$ ]]; then
  printf 'HORUS_S4_PAD_OFFSET must be "x,y" in metres.\n' >&2
  exit 2
fi
default_label="ideal_home"
[[ -n "${pad_offset}" ]] && default_label="pad_offset_${pad_offset/,/_}"
out_dir="sim/logs/s4/${HORUS_S4_LABEL:-${default_label}}"
mkdir -p "${out_dir}"
results="${out_dir}/landing_trials.jsonl"
params=/horus/config/ardupilot
export HORUS_SITL_PARAM_FILE="${params}/common.parm:${params}/indoor.parm:${params}/nogps.parm"

run_trial() {
  local condition="$1" config="$2" index="$3" log="${out_dir}/${1}_trial${3}.log"
  HORUS_REMOTE_CONFIG="${config}" trial_remote_up "${log}"
  if [[ -n "${pad_offset}" ]]; then
    ssh "${remote}" docker exec "${container}" gz service -s /world/horus_indoor/set_pose \
      --reqtype gz.msgs.Pose --reptype gz.msgs.Boolean --timeout 3000 \
      --req "'name: \"home_apriltag_pad\", position: {x: ${pad_offset%,*}, y: ${pad_offset#*,}, z: 0}'" >> "${log}" 2>&1
  fi
  sleep 10  # EKF settles on optical flow before arming
  local mission_ok=1
  "${horus_bin}/horus" --server-url "${HORUS_MCP_URL}" --mission missions/S4_rth_offset.yaml >> "${log}" 2>&1 \
    || mission_ok=0
  "${horus_bin}/horus-mcp-call" rth >> "${log}" 2>&1 || true
  local landed=1
  trial_wait_disarmed 420 || landed=0
  sleep 2
  local err
  err="$(ssh "${remote}" docker exec "${container}" timeout -s INT 4 \
    /opt/horus-venv/bin/python /horus/scripts/landing_error.py 2>/dev/null | tail -1 || true)"
  printf '%s\n' "${trial_state}" > "${out_dir}/${condition}_trial${index}_final_state.json"
  python3 - "${condition}" "${index}" "${err}" "${trial_armed_seen}" "${landed}" "${mission_ok}" \
    >> "${results}" <<'PY'
import json, re, sys
condition, index, line, armed_seen, landed, mission_ok = sys.argv[1:7]
m = re.search(r"landing_error_m=([\d.]+) dx_m=([-+\d.]+) dy_m=([-+\d.]+)", line)
print(json.dumps({"condition": condition, "trial": int(index), "mission_ok": mission_ok == "1", "landed_disarmed": landed == "1" and armed_seen == "1",
                  "landing_error_m": float(m.group(1)) if m else None,
                  "dx_m": float(m.group(2)) if m else None, "dy_m": float(m.group(3)) if m else None, "raw": line}))
PY
  bash scripts/koko/remote_down.sh >> "${log}" 2>&1
  tail -1 "${results}"
}

for ((i = 1; i <= trials; i++)); do
  run_trial apriltag /horus/config/horus_s4_nogps.yaml "${i}"
  run_trial plain_rtl /horus/config/horus_s4_nogps_plain_rtl.yaml "${i}"
done
