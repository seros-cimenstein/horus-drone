#!/usr/bin/env bash
set -euo pipefail

# Bridge the HORUS ToF payload to MAVLink DISTANCE_SENSOR messages for any world that carries
# it: sim/models/horus_sensor_payload (seven single-beam ToFs) for indoor and canonical
# outdoor worlds, sim/models/horus_sensor_payload_procedural (one 8-beam proximity_ring, fanned
# out to the same MAVLink sectors) for generated worlds. gz-sim scopes LaserScan topics under
# the world and every enclosing model, so the caller names that chain.
#
# Usage: tof_bridges.sh --world WORLD --model-chain a/b/c [--layout seven|ring]
#   indoor:    --world horus_indoor --model-chain horus_iris/horus_sensor_payload
#   canonical: --world horus_forest --model-chain horus_iris/iris_with_gimbal/horus_sensor_payload
#   generated: --world horus_forest_p12648 \
#                --model-chain horus_iris/iris_with_gimbal/horus_sensor_payload --layout ring

world=""
model_chain=""
layout="${HORUS_TOF_LAYOUT:-seven}"
while (( $# )); do
  case "$1" in
    --world) world="${2:-}"; shift 2 ;;
    --model-chain) model_chain="${2:-}"; shift 2 ;;
    --layout) layout="${2:-}"; shift 2 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ -z "${world}" || -z "${model_chain}" ]]; then
  printf 'Usage: tof_bridges.sh --world WORLD --model-chain a/b/c [--layout seven|ring]\n' >&2
  exit 2
fi
if [[ "${layout}" != seven && "${layout}" != ring ]]; then
  printf 'Layout must be seven or ring (got %s).\n' "${layout}" >&2
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
    --layout "$layout" \
    --world "$world" \
    --model-chain "$model_chain"
