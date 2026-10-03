#!/usr/bin/env bash
# Throwaway: live generated-world stack on Koko (civilians, grounder, MCP, camera RTP) and the
# mean step rate over two 30 s windows. Usage: koko_rtf.sh STEM KIND [WORLD_NAME]
set -u
stem="$1"; kind="$2"; world="${3:-$1}"
cd ~/horus
export HORUS_SIM_IMAGE=horus-sim:local-s1s2-gui HORUS_SIM_CONTAINER_NAME=horus-sim HORUS_SIM_GPU=amd
export HORUS_SITL_PARAM_FILE=/horus/config/ardupilot/horus_sitl_procedural.parm
export HORUS_SIM_MEMORY_LIMIT=8g HORUS_SIM_MEMORY_SWAP_LIMIT=9g GZ_IP=127.0.0.1 GZ_PARTITION=rtfprof
docker rm -f horus-sim >/dev/null 2>&1; rm -f sim/run/.active
dir=$(dirname "$(ls sim/generated/*/"$stem".sdf | head -n1)")
./sim/run.sh start "/horus/$dir/$stem.sdf" >/dev/null 2>&1 || { echo "$stem start failed"; exit 1; }
docker exec horus-sim bash /horus/scripts/start_grounder.sh >/dev/null
printf 'proftoken\n' | bash scripts/koko/start_mcp.sh horus-sim "/horus/config/horus_procedural_${kind}.yaml" INFO
for i in $(seq 1 150); do
  docker exec horus-sim python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(0.25); sys.exit(s.connect_ex(("127.0.0.1",8000)))' 2>/dev/null && break
  sleep 2
done
bash sim/scripts/enable_camera_stream.sh horus-sim >/dev/null 2>&1
sleep 30
civ=$(tail -n 1 "$(ls -td sim/run/2026*/ | head -n1)logs/civilian_life.log" 2>/dev/null)
iters() { docker exec horus-sim bash -c "timeout 8 gz topic -e -n 1 -t /world/$world/stats" | awk '/^iterations/ {print $2}'; }
rates=()
for k in 1 2; do
  a=$(iters); t0=$(date +%s.%N); sleep 30; t1=$(date +%s.%N); b=$(iters)
  rates+=("$(python3 -c "print(($b-$a)/(($t1-$t0)*1000))")")
done
python3 -c "import sys; r=[float(x) for x in sys.argv[1:]]; print(f'$stem rtf={sum(r)/len(r):.3f} samples={[round(x,3) for x in r]}')" "${rates[@]}"
echo "  civ: $civ"
docker rm -f horus-sim >/dev/null 2>&1; rm -f sim/run/.active
