#!/usr/bin/env bash
# Throwaway: start a live generated-world stack (civilians, grounder, MCP, camera RTP), then
# perf-sample the gz sim server and summarise hot functions per thread. Usage: koko_perf.sh STEM KIND
set -u
stem="$1"; kind="$2"
cd ~/horus
source scripts/koko/profiling/stack.sh
profiling_stack_up "$stem" "$kind" perfprof || exit 1
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
