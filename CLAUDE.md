# HORUS project memory

- Run resource-intensive work on Koko, not this laptop: live Gazebo/SITL episodes, full worldgen
  seed sweeps, grounder/model evaluation sweeps. Koko runs at RTF ~0.87 against ~0.2-0.35 here.
  Use `scripts/koko/remote_up.sh` / `remote_down.sh` / `remote_fetch_logs.sh`. The fetch copies only
  the files in Koko's `sim/logs/` and `runs/` written since the last `remote_up.sh`.
- Before claiming Koko is unreachable, check it: `ssh -o BatchMode=yes -o ConnectTimeout=10 koko true`.
- `rsync` to Koko excludes `/models`: copy `models/grounder-venv` and `models/hf` separately when needed.
- The whole-map GTA tiles (`scripts/maps/build_gta_tiles.py`, built on the laptop) are copied to
  Koko's `~/horus-loop/data/maps/gta/tiles` (rsync leaves out `/data`, so they survive syncs);
  `config/horus_gta.yaml` reads them there.
- Koko runs two X screens since 2026-11-02 (`scripts/koko/display/`, installed with the user's sudo
  once): `:0.0` is the laptop panel (eDP) with the Cinnamon desktop, `:0.1` the HDMI dummy plug,
  where simulations run (no window manager there). `gta_game.sh` and `sim/run.sh --gui` pick `:0.1`
  when it exists, else `:0`. Steam must run on `:0.1` for GTA to open there: `gta_game.sh start`
  launches it when none runs, and refuses one on the panel. Before launching it signs Steam in as
  the account that owns GTA (`31hastalavista31`) and turns off Steam's "Who's playing?" picker
  (`config.vdf` `AlwaysShowUserChooser`). LightDM logs `sero` in by itself.
- The panel is off by default (an autostart entry switches it off at login); Ctrl+Alt+] runs
  `~/.local/bin/screen-backlight-toggle`, which with two screens switches only the panel
  (`xrandr -d :0.0 --output eDP --off|--auto`) and never the dummy. Never use `xset dpms force off`:
  DPMS is per X server and switches the dummy off too (GTA then crawls at 1 frame/s).
- `sudo -n /usr/local/sbin/horus-display two-screens|one-screen|restart|status` needs no password
  (one-screen is the old single desktop; switching restarts the desktop and closes GTA/Gazebo).
- The drone's first GTA frames at the start pose are white (camera at ground level); take off to
  see the plaza. `gta_game.sh start` wants 9728 MiB available (`GTA_MIN_AVAILABLE_MIB`); after a
  desktop restart with Steam and the drone board running Koko has only 8.3-9.2 GB.
- GTA always flies with collisions (user decision 2026-11-02): `sim/run.sh start --gta` runs
  SITL's JSON backend, the quad is a physical object in GTA, its seven ToF beams feed ArduPilot's
  avoidance, and it stops about 1 m from walls (checked 2026-11-02: a `goto` into the Legion
  Square sculpture stopped at east 11-12, face at 13.1, no contact). The camera-only pose mode and
  `--json-physics` are gone; never bring back a GTA start without collisions. The camera rides on
  HorusGta.dll's gimbal, pointed by HORUS through `horus-gta-gimbal` (started by
  `start_gta_sitl.sh`), resting at -45° until HORUS points it. Collision works anywhere on the map: the
  2026-11-02 fall through a Vinewood hillside was the mod's contact response, not unloaded
  collision (fixed that day, `gta/horus_mod/README.md` "Checked in game"). After landing on a
  steep slope ArduPilot refuses to arm (`PreArm: Proximity … want > 0.6m`: the level beams see the
  slope); a world restart puts the quad back at home. ArduPilot moves home to where it arms, so
  after a take-off away from home `rth` lands there; fly home with `goto` instead.
- Unit tests run fine locally in docker; `pytest-timeout` is not installed, so always wrap them in a
  shell `timeout` and run the suite file by file:
  `timeout 900 docker run --rm -v "$PWD":/horus -w /horus --entrypoint /opt/horus-venv/bin/python horus-sim:local-s1s2-gui -m pytest -q <files>`

## Architecture and plan

- The target architecture is the diagram in `HORUS — Design Document v2.md` ("Principles and
  architecture"); its "Built and planned" table lists what is missing. `PLAN.md` holds the phases,
  their status, the rules every phase keeps and the stable block codes (`#63bb` etc.). Read
  `PLAN.md` before feature work; a code pasted in chat means that block.
- New work must fit the diagram. Adding or removing a block or an arrow needs the user's approval
  and a diagram update first.
- Use the diagram's names in docs and replies: Control app, AI pilot, AI model, Drone board,
  Flight moves, Camera vision, Autopilot link, ArduPilot, Test world.
- A change that alters behaviour updates `README.md`, the design document's "Built and planned"
  table and `PLAN.md`'s status in the same step.

## Working rules

- Never touch the lecture deliverables `docs/course/` and `docs/paf/`, or `annotated.jpg`,
  `omp-session-*.html`, `models/`, `data/`, `runs/`, `.venv-host/`.
- Never `git push` without asking.
- Work with many atomic items runs as a seref_loop ledger `LOOP_<SLUG>.md` (see `PLAN.md`,
  "How to run a phase as a loop"); `LOOP_DIAGRAM_ALIGN.md` is the worked example.
- Leave pick-up notes for the next session in `NEXT_SESSION.md`.
