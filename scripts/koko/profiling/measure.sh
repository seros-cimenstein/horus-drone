#!/usr/bin/env bash
# Sync repo + generated worlds to Koko and measure live RTF (civilians, grounder, MCP, camera RTP
# on). Usage: scripts/koko/profiling/measure.sh STEM:KIND[:WORLD] ...  e.g. horus_forest_p21:forest
set -u
here=$(cd "$(dirname "$0")" && pwd)
cd "$here/../../.."
rsync -a --exclude /models --exclude /runs --exclude /sim/run --exclude /sim/logs --exclude .git \
  --exclude /sim/generated ./ koko:horus/ >/dev/null
scp -q "$here/koko_rtf.sh" koko:/tmp/koko_rtf.sh
for spec in "$@"; do
  IFS=: read -r stem kind world <<< "$spec"
  dir=$(dirname "$(ls sim/generated/*/"$stem".sdf | head -n1)")
  rsync -a --delete "$dir/" "koko:horus/$dir/" >/dev/null
  ssh -o BatchMode=yes -o ServerAliveInterval=60 koko "bash /tmp/koko_rtf.sh $stem $kind ${world:-$stem}" 2>&1 | grep -E 'rtf=|civ:|failed'
done
