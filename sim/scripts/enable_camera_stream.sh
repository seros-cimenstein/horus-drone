#!/usr/bin/env bash
set -Eeuo pipefail

# Enable the simulation camera's RTP stream (sim/campaign/camera_stream.py). Call this after MCP
# has connected to SITL: the lockstep world, and so the camera, only runs once a MAVLink client
# is attached. HORUS_CAMERA_STREAM_TIMEOUT_S bounds the re-enable budget (default 180 s).
container="${1:-${HORUS_SIM_CONTAINER_NAME:-horus-sim}}"
if [[ "$(docker inspect --format '{{.State.Running}}' "${container}" 2>/dev/null || true)" != true ]]; then
  printf 'Simulation container %s stopped before its camera stream was available.\n' \
    "${container}" >&2
  exit 1
fi
exec docker exec --workdir /horus "${container}" python3 -m sim.campaign.camera_stream \
  --budget-s "${HORUS_CAMERA_STREAM_TIMEOUT_S:-180}"
