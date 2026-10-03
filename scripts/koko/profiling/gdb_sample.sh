#!/usr/bin/env bash
# Throwaway: sample stacks of the busiest gz sim threads with gdb; print collapsed top frames.
pid=$(pgrep -f "gz sim -v4 -s -r --headless-rendering" | head -n1)
busy=$(top -b -H -n 1 -p "$pid" | awk 'NR>7 && $9>5 {print $1}' | head -n 5 | tr '\n' ' ')
echo "pid=$pid busy threads: $busy"
out=/tmp/gdb_samples.txt; : > "$out"
for n in $(seq 1 ${1:-25}); do
  timeout 60 gdb -p "$pid" -batch -nx \
    -ex "set sysroot /proc/$pid/root" -ex "set pagination off" -ex "set print frame-arguments none" \
    -ex "thread apply all bt 30" 2>/dev/null >> "$out"
  echo "=====" >> "$out"
  sleep 0.7
done
python3 - "$out" $busy <<'EOF'
import collections, re, sys
text = open(sys.argv[1]).read()
busy = set(sys.argv[2:])
stacks = collections.Counter()
for block in re.split(r"\nThread \d+ ", text):
    m = re.search(r"\(LWP (\d+)\)", block)
    if not m or m.group(1) not in busy:
        continue
    frames = []
    for line in block.splitlines():
        f = re.match(r"#\d+\s+(?:0x[0-9a-f]+ in )?(.+?) \(", line)
        if f:
            frames.append(re.sub(r"<.*>", "<>", f.group(1))[:110])
    if frames:
        stacks[(m.group(1),) + tuple(frames[:14])] += 1
for stack, n in stacks.most_common(12):
    print(f"--- {n}x LWP {stack[0]}")
    for frame in stack[1:]:
        print("     ", frame)
EOF
