#!/usr/bin/env bash
# Throwaway: live generated-world stack on Koko (civilians, grounder, MCP, camera RTP) and the
# mean step rate over two 30 s windows. Usage: koko_rtf.sh STEM KIND [WORLD_NAME]
set -u
stem="$1"; kind="$2"; world="${3:-$1}"
cd ~/horus
source scripts/koko/profiling/stack.sh
profiling_stack_up "$stem" "$kind" rtfprof || exit 1
sleep 30
civ=$(tail -n 1 "sim/run/$(cat sim/run/.active)/logs/civilian_life.log" 2>/dev/null)
iters() { docker exec horus-sim bash -c "timeout 8 gz topic -e -n 1 -t /world/$world/stats" | awk '/^iterations/ {print $2}'; }
rates=()
for k in 1 2; do
  a=$(iters); t0=$(date +%s.%N); sleep 30; t1=$(date +%s.%N); b=$(iters)
  rates+=("$(python3 -c "print(($b-$a)/(($t1-$t0)*1000))")")
done
python3 -c "import sys; r=[float(x) for x in sys.argv[1:]]; print(f'$stem rtf={sum(r)/len(r):.3f} samples={[round(x,3) for x in r]}')" "${rates[@]}"
echo "  civ: $civ"
docker rm -f horus-sim >/dev/null 2>&1; rm -f sim/run/.active
