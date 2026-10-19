#!/usr/bin/env bash
# Shared live-stack bring-up for koko_rtf.sh and koko_perf.sh, run on Koko from ~/horus.
# Sourced, not executed: callers get profiling_stack_up().
source sim/scripts/procedural_world.sh

profiling_stack_up() {
  # Start generated world STEM headless (civilians and grounder come up with the world), its MCP
  # with the world's executor profile and the camera RTP stream.
  # Usage: profiling_stack_up STEM KIND GZ_PARTITION
  local stem="$1" kind="$2" partition="$3" dir config
  if [[ "${stem}" != "$(procedural_world_stem "${kind}" "${stem##*_p}")" ]]; then
    echo "$stem start failed: not a generated $kind world"
    return 1
  fi
  export HORUS_SIM_IMAGE=horus-sim:local-s1s2-gui HORUS_SIM_CONTAINER_NAME=horus-sim HORUS_SIM_GPU=amd
  export HORUS_SIM_MEMORY_LIMIT=8g HORUS_SIM_MEMORY_SWAP_LIMIT=9g GZ_IP=127.0.0.1 GZ_PARTITION="$partition"
  read -r config _ <<< "$(horus_world_profile "$stem")"
  docker rm -f horus-sim >/dev/null 2>&1; rm -f sim/run/.active
  dir=$(dirname "$(ls sim/generated/*/"$stem".sdf | head -n1)")
  ./sim/run.sh start "/horus/$dir/$stem.sdf" >/dev/null 2>&1 || { echo "$stem start failed"; return 1; }
  printf 'proftoken\n' | bash scripts/koko/start_mcp.sh horus-sim "$config" INFO
  for _ in $(seq 1 150); do
    docker exec horus-sim python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(0.25); sys.exit(s.connect_ex(("127.0.0.1",8000)))' 2>/dev/null && break
    sleep 2
  done
  bash sim/scripts/enable_camera_stream.sh horus-sim >/dev/null 2>&1
}
