# HORUS project memory

- Run resource-intensive work on Koko, not this laptop: live Gazebo/SITL episodes, full worldgen
  seed sweeps, grounder/model evaluation sweeps. Koko runs at RTF ~0.87 against ~0.2-0.35 here.
  Use `scripts/koko/remote_up.sh` / `remote_down.sh` / `remote_fetch_logs.sh`.
- Before claiming Koko is unreachable, check it: `ssh -o BatchMode=yes -o ConnectTimeout=10 koko true`.
- `rsync` to Koko excludes `/models`: copy `models/grounder-venv` and `models/hf` separately when needed.
- Koko has an HDMI dummy plug (since 2026-10-29), so a display output stays enabled with no monitor
  attached. `scripts/koko/gta_game.sh start` gates on `/sys/class/drm/card*-*/enabled`; if it still
  refuses, DPMS/blanking turned the output off. A screen lock still blocks it either way.
- Unit tests run fine locally in docker; `pytest-timeout` is not installed, so always wrap them in a
  shell `timeout` and run the suite file by file:
  `timeout 900 docker run --rm -v "$PWD":/horus -w /horus --entrypoint /opt/horus-venv/bin/python horus-sim:local-s1s2-gui -m pytest -q <files>`
