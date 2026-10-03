#!/usr/bin/env bash
# Throwaway: start a live generated-world stack (civilians, MCP, camera RTP), then perf-sample the
# gz sim server and summarise hot functions per thread. Usage: koko_perf.sh STEM KIND
set -u
stem="$1"; kind="$2"
cd ~/horus
export HORUS_SIM_IMAGE=horus-sim:local-s1s2-gui HORUS_SIM_CONTAINER_NAME=horus-sim HORUS_SIM_GPU=amd
export HORUS_SITL_PARAM_FILE=/horus/config/ardupilot/horus_sitl_procedural.parm
export HORUS_SIM_MEMORY_LIMIT=8g HORUS_SIM_MEMORY_SWAP_LIMIT=9g GZ_IP=127.0.0.1 GZ_PARTITION=perfprof
docker rm -f horus-sim >/dev/null 2>&1; rm -f sim/run/.active
dir=$(dirname "$(ls sim/generated/*/"$stem".sdf | head -n1)")
./sim/run.sh start "/horus/$dir/$stem.sdf" >/dev/null 2>&1 || { echo "start failed"; exit 1; }
docker exec horus-sim bash /horus/scripts/start_grounder.sh >/dev/null
printf 'proftoken\n' | bash scripts/koko/start_mcp.sh horus-sim "/horus/config/horus_procedural_${kind}.yaml" INFO
for i in $(seq 1 150); do
  docker exec horus-sim python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(0.25); sys.exit(s.connect_ex(("127.0.0.1",8000)))' 2>/dev/null && break
  sleep 2
done
bash sim/scripts/enable_camera_stream.sh horus-sim >/dev/null 2>&1
sleep 45
pid=$(pgrep -f "gz sim -v4 -s -r --headless-rendering" | head -n1)
echo "gz pid $pid threads $(ls /proc/$pid/task | wc -l)"
top -b -H -n 1 -p "$pid" | sed -n '7,20p'
perf record -F 499 -g -p "$pid" -o /tmp/gz.perf -- sleep 20 >/dev/null 2>&1
echo "=== by thread (comm) ==="
perf report -i /tmp/gz.perf --stdio --no-children --sort comm 2>/dev/null | grep -E '^\s+[0-9.]+%' | head -n 12
echo "=== by dso ==="
perf report -i /tmp/gz.perf --stdio --no-children --sort dso 2>/dev/null | grep -E '^\s+[0-9.]+%' | head -n 15
echo "=== by symbol ==="
perf report -i /tmp/gz.perf --stdio --no-children --sort dso,sym 2>/dev/null | grep -E '^\s+[0-9.]+%' | head -n 45
