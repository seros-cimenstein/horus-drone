#!/usr/bin/env bash
set -Eeuo pipefail

container="${1:-${HORUS_SIM_CONTAINER_NAME:-horus-sim}}"
deadline=$((SECONDS + 60))
camera_topic=""
while (( SECONDS < deadline )); do
  if [[ "$(docker inspect --format '{{.State.Running}}' "${container}" 2>/dev/null || true)" != true ]]; then
    printf 'Simulation container %s stopped before its camera stream was available.\n' \
      "${container}" >&2
    exit 1
  fi
  topics="$(docker exec --workdir /horus "${container}" gz topic -l 2>/dev/null || true)"
  while IFS= read -r candidate; do
    if [[ "${candidate}" == *"/camera/image/"* \
      && "${candidate}" == *"/enable_streaming" ]]; then
      camera_topic="${candidate}"
      break
    fi
  done <<< "${topics}"
  [[ -n "${camera_topic}" ]] && break
  sleep 0.5
done
if [[ -z "${camera_topic}" ]]; then
  printf 'Timed out waiting for the indoor camera enable_streaming topic.\n' >&2
  exit 1
fi
# A single publish can be lost before discovery connects it to the camera plugin (slower over an
# Ethernet GZ_IP), so re-publish until RTP packets arrive on UDP 5600. Call this after MCP has
# connected to SITL: the lockstep world, and so the camera, only runs once a MAVLink client is
# attached. The 1.5 s probe shares 5600 with the MCP camera reader (both use SO_REUSEADDR).
for ((attempt = 1; attempt <= 20; attempt++)); do
  docker exec --workdir /horus "${container}" gz topic \
    -t "${camera_topic}" -m gz.msgs.Boolean -p 'data: 1'
  if docker exec "${container}" python3 -c '
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", 5600))
s.settimeout(1.5)
try:
    s.recv(65535)
except socket.timeout:
    sys.exit(1)
'; then
    printf 'Enabled indoor camera stream: %s (RTP flowing after %d attempt(s))\n' "${camera_topic}" "${attempt}"
    exit 0
  fi
done
printf 'Camera stream did not start after 20 enable attempts: %s\n' "${camera_topic}" >&2
exit 1
