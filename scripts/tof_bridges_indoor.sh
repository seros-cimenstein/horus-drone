#!/usr/bin/env bash
set -euo pipefail

mavlink_endpoint="${HORUS_MAVLINK_ENDPOINT:-tcp:127.0.0.1:5763}"
python="${HORUS_PYTHON:-python3}"
rate_hz="${HORUS_TOF_RATE_HZ:-20}"
# Gazebo on slower render paths can take tens of seconds to publish the first scans.
startup_timeout="${HORUS_TOF_STARTUP_TIMEOUT:-60}"
topic_root="/world/horus_indoor/model/horus_iris/model/horus_sensor_payload/link/payload/sensor"

exec "$python" -m horus.tof_bridge \
    --mavlink "$mavlink_endpoint" \
    --rate-hz "$rate_hz" \
    --startup-timeout "$startup_timeout" \
    --sensor "$topic_root/proximity_front/scan:0:0" \
    --sensor "$topic_root/proximity_front_right/scan:1:1" \
    --sensor "$topic_root/proximity_right/scan:2:2" \
    --sensor "$topic_root/proximity_rear/scan:4:3" \
    --sensor "$topic_root/proximity_left/scan:6:4" \
    --sensor "$topic_root/proximity_front_left/scan:7:5" \
    --sensor "$topic_root/range_down/scan:25:6"
