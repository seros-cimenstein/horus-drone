#!/usr/bin/env bash
set -euo pipefail

# Bridge the HORUS ToF payload (sim/models/horus_sensor_payload) to MAVLink DISTANCE_SENSOR
# messages for any world that carries it. gz-sim scopes LaserScan topics under the world and
# every enclosing model, so the caller names that chain.
#
# Usage: tof_bridges.sh --world WORLD --model-chain a/b/c
#   indoor:  --world horus_indoor --model-chain horus_iris/horus_sensor_payload
#   outdoor: --world horus_forest_p12648 --model-chain horus_iris/iris_with_gimbal/horus_sensor_payload

world=""
model_chain=""
while (( $# )); do
  case "$1" in
    --world) world="${2:-}"; shift 2 ;;
    --model-chain) model_chain="${2:-}"; shift 2 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ -z "${world}" || -z "${model_chain}" ]]; then
  printf 'Usage: tof_bridges.sh --world WORLD --model-chain a/b/c\n' >&2
  exit 2
fi

mavlink_endpoint="${HORUS_MAVLINK_ENDPOINT:-tcp:127.0.0.1:5763}"
python="${HORUS_PYTHON:-python3}"
rate_hz="${HORUS_TOF_RATE_HZ:-20}"
# Gazebo on slower render paths can take tens of seconds to publish the first scans.
startup_timeout="${HORUS_TOF_STARTUP_TIMEOUT:-60}"

exec "$python" -m horus.tof_bridge \
    --mavlink "$mavlink_endpoint" \
    --rate-hz "$rate_hz" \
    --startup-timeout "$startup_timeout" \
    --world "$world" \
    --model-chain "$model_chain"
