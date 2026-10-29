# HORUS simulation toolchain

This toolchain runs Gazebo Harmonic and ArduCopter SITL in an Ubuntu 22.04
container on the Arch host. Ubuntu 22.04 is selected because Gazebo Harmonic
and the official `ardupilot_gazebo` plugin support Jammy; the full upstream
`gz-harmonic` metapackage is installed from the OSRF binary repository.

The image is single-stage. It downloads the official Copter 4.7.1 SITL
executable from
`firmware.ardupilot.org/Copter/stable-4.7.1/SITL_x86_64_linux_gnu/arducopter`
and verifies SHA256
`011627d41dd95640c3aca02d45283474be48721c8b1bb7a4b234d38b69063482`.
ArduPilot is not compiled: a blobless sparse checkout of `Copter-4.7.1`
retains `ArduCopter/`, `Tools/autotest`, and `Tools/scripts`, including
`sim_vehicle.py` and its default parameter files. The container uses
`sim_vehicle.py --no-rebuild` with the downloaded binary. The image builds
pinned `ardupilot_gazebo` commit
`082a0fe231f6e63bc8d1598f1cba461d9e2ea7f5` in one Docker layer, copies its
runtime plugin and assets, then removes the source/build trees; compiler and
Gazebo development packages remain installed with the full `gz-harmonic`
runtime.

Gazebo runtime libraries, OpenCV/GStreamer camera support, ONNX Runtime, and a
Python venv containing the project dependencies and `pymavlink` are included.
The venv uses system site packages for the Gazebo Python bindings. Project
models and worlds are available at `/horus/sim/models` and
`/horus/sim/worlds`, alongside the plugin's `/opt/ardupilot_gazebo/models` and
`/opt/ardupilot_gazebo/worlds`.

The container uses host networking and mounts this project at `/horus`.
`GZ_IP` defaults to `127.0.0.1` and `GZ_PARTITION` to `default` for a
single-host run; split deployments advertise each host's Ethernet address on a
shared partition. SITL TCP 5760 accepts one MAVLink client for HORUS flight.
On `horus_indoor.sdf`, `start_sim.sh` adds `--serial2=tcp:5763` and starts one
multi-sensor ToF bridge as its sole client; TCP 5762 remains available for a
pose/DISTANCE_SENSOR monitor. Gazebo JSON/FDM traffic uses UDP 9002, and camera
video uses UDP 5600. `HORUS_SIM_GPU=auto` prefers available NVIDIA or Mesa
hardware; `amd` explicitly selects Mesa `radeonsi` through `/dev/dri`, `mesa`
uses the default Mesa driver, `on` requires NVIDIA, and `off` forces software
rendering.

## Build and run

From the project root:

```bash
./sim/run.sh build
./sim/run.sh start
./sim/run.sh status
```

One HORUS world runs per host, Gazebo or GTA V, never both: two worlds overload
the host. `run.sh start` refuses Gazebo while the GTA V game runs
(`pgrep -f '[G]TA5_Enhanced'`; stop it with `scripts/koko/gta_game.sh stop`),
refuses `--gta` while `gz sim` runs, and refuses any second running HORUS world
container whatever its `HORUS_SIM_CONTAINER_NAME` (world containers carry the
label `horus.world=gazebo|gta`). It also refuses a `HORUS_CONFIG` from the other
world: a `gta://` frame source or the `gta` map provider in a Gazebo run, or any
other source in a GTA run (`sim/scripts/world_profiles.py check-config`). The
`gui-client` only observes a remote world and is not checked. The flight limits
of every world are ArduPilot's: each world profile's parameter fragments carry
its `FENCE_*` settings, and HORUS clamps nothing.

`run.sh build` monitors root free space and stops the Docker CLI below 1 GB.
On a failed or aborted build it removes only newly-created dangling images and
new stopped containers, then prunes the builder cache. `run.sh start` launches
`gz sim -v4 -s -r --headless-rendering` and
`sim_vehicle.py -v ArduCopter -f gazebo-iris --model JSON --no-rebuild --no-mavproxy`.
The default world is the plugin's bundled `worlds/iris_runway.sdf`, which
includes the upstream `iris_with_gimbal` and its `GstCameraPlugin` camera (no
gimbal device runs there). An alternate world can be selected with
`./sim/run.sh start horus_indoor.sdf`.
The image resolves runtime, server, perception, and test dependencies from
`pyproject.toml` at build time. Every container mounts the checkout at `/horus`
and the image sets `PYTHONPATH=/horus`, so Python source edits are live without
running `pip` against the bind mount. Dependency or entry-point changes require
rebuilding the image. It provides `horus`, `horus-mcp`, `horus-mcp-call`,
`horus-commander-serve`, `horus-gz-bridge`, `horus-gz-gimbal`, and
`horus-tof-bridge` entry points.

Optional text-grounding weights are kept outside the image. Run `docker exec horus-sim bash /horus/scripts/setup_grounder.sh` inside the sim container to create `/horus/models/grounder-venv` and cache model weights in `/horus/models/hf`; no image rebuild is needed. `start_sim.sh` starts the service on loopback port 8100 before MCP when the venv is installed. Logs are in `/horus/sim/logs/horus-grounder.log`, and a missing venv is non-fatal. When huggingface.co is unreachable, copy an `HF_HOME` cache from another machine — the `hub/models--IDEA-Research--grounding-dino-tiny` and `hub/models--google--owlv2-base-patch16-ensemble` directories, whose `model.safetensors` sha256s are pinned in the root README — and set `HORUS_GROUNDER_HF_CACHE` to it, which the setup reads with `HF_HUB_OFFLINE=1`. `HF_ENDPOINT=<mirror-url>` works only for a mirror that serves the full Hub file metadata (`Content-Length` and `X-Repo-Commit`) required by `huggingface_hub` 0.36.2; a mirror that omits either header fails with a message naming both options. ModelScope's mirrors of the two repos serve the same files (their `X-Linked-Etag` sha256 matches the Hub's LFS hashes), so they can fill that cache.

## Outdoor forest and urban worlds

Generate both reproducible outdoor worlds, their layout manifests, local
models, and matching civilian scenarios from the project root:

```bash
python3 -m sim.worldgen
./sim/run.sh start horus_forest.sdf
./sim/run.sh start horus_urban.sdf
```

Canonical generation uses only the Python standard library; `--seed N` selects
a repeatable base seed (default `2026`, producing forest seed `2027` and urban
seed `2028`).

`run.sh start --procedural` generates a randomized world and starts it in one
step. Without `--seed` it draws a 5-digit seed and prints it, so any failure is
reproducible:

```bash
./sim/run.sh start --procedural                      # random seed, forest
./sim/run.sh start --procedural --kind urban --seed 17
./sim/run.sh start --procedural --stratum chaotic --gui
```

Generation is deterministic per seed and takes about 10-40 s. An existing
`sim/generated/horus_<kind>_p<seed>/` bundle is reused until a file under
`sim/worldgen/` changes; delete the bundle to force a rebuild. Generated worlds
fly the procedural profile: HORUS connects with
`config/horus_procedural_forest.yaml` or `config/horus_procedural_urban.yaml`
(`HORUS_CONFIG=/horus/config/horus_procedural_<kind>.yaml` inside the
container), and `start_sim.sh` loads `config/ardupilot/common.parm`,
`outdoor.parm` and `procedural_fence.parm` for the `horus_<kind>_p<seed>` stem
(`sim/scripts/world_profiles.py`). Canonical `horus_forest.sdf`/`horus_urban.sdf`
keep `config/horus_{forest,urban}.yaml` with `common.parm`, `outdoor.parm` and
`canonical_fence.parm`.
`run.sh start --procedural` also exports the procedural `HORUS_CONFIG` and
`HORUS_SITL_PARAM_FILE` (into the container too), so a later
`docker exec horus-sim /opt/horus-venv/bin/horus-mcp` loads the matching
profile; an explicit `HORUS_SITL_PARAM_FILE` always wins. The two-step flow is
still available:

```bash
python3 -m sim.worldgen --procedural --seed 17
./sim/run.sh start /horus/sim/generated/horus_forest_p17/horus_forest_p17.sdf
```

Use `--kind forest` or `--kind urban` to generate only one kind, and `--out`
to select a different output root:

```bash
python3 -m sim.worldgen --procedural --seed 17 --kind forest --out sim/generated
```

Procedural worlds are written under `sim/generated/horus_<kind>_p<seed>/`
with their SDF, scenario YAML, layout manifest, and per-world terrain model
assets. Forest terrain, tree scatter, and the matching ground NPY are generated
together. Canonical scenarios are `sim/scenarios/horus_forest.yaml` and
`sim/scenarios/horus_urban.yaml`; forest routes follow a dirt trail past the
clearing, creek, and campsite, while urban routes use sidewalks, marked
crosswalks, and vehicle lanes.

Generated 1.2 km forests are sampled by `sim/worldgen/forest_procedural.py`:
a trail network (a main trail within 15 m of home plus one loop per quadrant),
one or two creeks, 4-8 clearings, 1-3 campsites and up to ~9000 instanced
`tree_pine`/`tree_oak` assets (saplings included) on gentle terrain, all emitted
as per-tile scenery with a `<stem>.scenery.json` collision sidecar. The
manifest carries `areas.trails/creeks/clearings/camps` and `actor_geofence_m`.

When a matching scenario and `sim/scripts/civilian_life.py` are present,
`start_sim.sh` starts its `ambient` group and records pose traces in that run's
`logs/civilians.jsonl`. The outdoor worlds keep GPS enabled and do not start
the indoor serial2 ToF bridge. The launcher loads `common.parm`, `outdoor.parm`
and `canonical_fence.parm` for canonical worlds and `procedural_fence.parm` in
place of `canonical_fence.parm` for generated ones; set
`HORUS_SITL_PARAM_FILE` to override it. Use `config/horus_forest.yaml` /
`config/horus_urban.yaml` with HORUS for the canonical worlds, and
`config/horus_procedural_forest.yaml` / `config/horus_procedural_urban.yaml`
for generated ones; each profile carries its own controller speeds and
perception settings, and the parameter fragments its ArduPilot fence.

### Procedural flight profile

The procedural worlds are a 1.2 km map bounded by ArduPilot: `outdoor.parm`
sets a home-centred circular fence (`FENCE_TYPE 3`, `FENCE_ALT_MAX 55`,
`FENCE_ACTION 1` returns home, `FENCE_MARGIN 5`) and `procedural_fence.parm`
its `FENCE_RADIUS 870`, which covers the map corners (600 * sqrt(2) = 848.5 m)
with margin. HORUS sends every target unclamped. `command_ttl_s` is 360 s,
sized for a map-crossing goto in wall-clock time: an 850 m goto at the 8 m/s
cruise is 850 / 8 = 106 s of simulation time, i.e. ~304 s of wall time at
RTF 0.35, plus acceleration (~3 s), yaw alignment and the arrival slowdown; a
longer leg passes a larger `ttl_s`.

The controller's horizontal speed is altitude-gated. `cruise_speed_m_s: 8.0`
applies only while the vehicle's current altitude above home and the commanded
target altitude are both at/above `cruise_min_altitude_m` - 26 m for forest
(above the 23 m tree cap) and 38 m for urban (above the 34 m
building/roof-equipment cap). Below that, or on a leg whose commanded target
altitude is lower, the controller flies at `speed_m_s: 3.0`. These are the
controller's speeds, not limits: a GUIDED velocity target is not clipped by
ArduPilot's waypoint-speed parameters (Copter 4.7's velocity/acceleration
submode feeds the target straight into the position controller), so the
fragments change no speed parameter: 8 m/s is well inside the default 30 deg
`ATC_ANGLE_MAX`, and the default 2.5 m/s^2 `WP_ACC` ramp reaches it in ~3.2 s.

### Verification and limits

Both generated SDFs pass `gz sdf -k`, and headless outdoor SITL arms, takes off,
and lands. The sampled mean RTF was 0.41 in the forest and 0.43 in the city on
this host; neither reaches the original 0.9 target.

From a clear city-road point 15 m from home, `RTL_ALT_M=45` reached 45.24 m,
13.01 m above the world's highest rooftop, then returned home and landed
disarmed. The default `RTL_CONE_SLOPE=3` lowers the climb near home: a separate
4.5 m-range RTL peaked at 13.65 m, so verify obstacle clearance at close-in
start points rather than assuming every RTL reaches 45 m.

The outdoor camera is 640×480 with an 84° horizontal FOV. The Gazebo gimbal
holds −45° until HORUS sends a setpoint, and `take_off` applies the outdoor config's
`default_gimbal_pitch_deg: -45`; the indoor default remains 0°.

## GTA V renderer mode (no Gazebo)

GTA V Enhanced can render the drone camera instead of Gazebo (`gta/horus_mod/README.md`).
`run.sh start --gta` starts only ArduCopter SITL, using its built-in quad physics, and
`horus-gta-bridge`. Gazebo, the GUI, world files and the GPU are not used:

```bash
./sim/run.sh start --gta     # SITL + bridge in the horus-sim container (HORUS_SIM_IMAGE as usual)
./sim/run.sh s0              # 5 m takeoff and land on tcp:5760, as with Gazebo
./sim/run.sh stop            # stops SITL and the bridge and removes the container
```

With real drone physics in GTA instead (`gta/horus_mod/README.md` "Physics"):

```bash
./sim/run.sh start --gta --json-physics   # SITL on its JSON backend, no bridge
scripts/koko/gta_game.sh start             # GTA with HorusGta.dll answers SITL on udp 9002
```

- SITL runs `--model JSON:${HORUS_GTA_PHYSICS_ADDRESS:-127.0.0.1}`. `sitl-overrides.parm` adds
  `FRAME_CLASS 1`, `FRAME_TYPE 1` (quad X, the motor order HorusGta's quad uses) and
  `SIM_RATE_HZ 800`.
- SITL steps only when HorusGta answers its servo frames, so its clock is GTA's. 800 Hz is the
  lowest rate that arms: at 400 Hz, `PreArm: Gyro 0 rate 400Hz < loop rate*1.8 720Hz`.
- SITL boots only once a MAVLink client holds tcp:5760, and sends no servo frames before then.
  HorusGta logs `physics: no SITL frame for 1.9 s` in that gap, and HORUS (or any client) on 5760
  starts it. No keeper is used, because no bridge needs serial1.
- Without GTA, `gta/horus_mod/build/physics_sim/horus-gta-physics-sim` answers instead, over flat
  ground.
- HORUS's first `GLOBAL_POSITION_INT` arrives before the EKF has a position (lat = lon = 0),
  because HORUS connects before SITL boots. `horus/flight/link.py` ignores such messages; taking
  one as the origin had put HORUS's pose 4000 km off.
- No gimbal device answers HORUS in physics mode. Until HorusGta.dll takes gimbal setpoints, ArduPilot's
  servo mount (`MNT1_TYPE 1`, SERVO9–11, set by `start_gta_sitl.sh` for this mode only) is a
  temporary stand-in that HORUS never commands: it rests at −45° pitch, yaw 0. The mount already
  subtracts the vehicle's lean from SERVO9–11, so HorusGta treats those outputs as angles relative
  to the body.
- ArduPilot's clock is the sum of HorusGta's timestamp steps. Its `time_boot_ms` is physics
  time, which runs slower than wall time when the real-time factor is below 1 (0.46–1.00 per 5 s
  in a checked session). Compare telemetry with GTA traces by physics time, not by receive time.

Checked on Koko on 2026-10-20:

- **Start:** `start --gta --json-physics`, then GTA. A pymavlink client on 5760 armed and hovered
  at 5 m for 120 s, with roll/pitch RMS 0.10°/0.10°, EKF flags 0x33f and no failsafe.
- **HORUS:** `horus-mcp` then took off to 15 m, flew to the NW corner and landed. The camera stayed
  `streaming` (`last_frame_age_s` 0.10–0.23), and `list_candidates` found a car.
- **Frame timing:** from a state answered to SITL to its frame reaching HORUS took 53.0 ms p50 and
  63.0 ms p95 of wall time. The telemetry HORUS pairs with a frame was 10.7 ms (p50), at most
  42 ms, of physics time newer than the rendered state.
- **Details:** in `gta/horus_mod/README.md` "Checked in game".

`sim/scripts/start_gta_sitl.sh` runs inside the container:

- SITL runs from the run directory (`sim/run/<id>/`), because it writes `eeprom.bin` and its logs
  to its cwd. From `/opt/ardupilot` ArduCopter fails with "PreArm: Param storage failed".
- In pose mode `sitl-overrides.parm` holds `LOG_DISARMED 0`, no mount parameters
  (`horus-gta-bridge` is HORUS's gimbal device) and GTA's ArduPilot fence from
  `config/ardupilot/gta.parm` (in both modes). The Gazebo proximity and rangefinder fragments
  depend on Gazebo sensors and are left out; physics mode takes those lines from `common.parm`.
- SITL boots, and opens serial1 (5762), only once a client holds serial0 (5760). A keeper client
  holds 5760 until the bridge is connected on 5762, then releases it. `sim.ready` follows, and HORUS
  (or `s0`) can connect to 5760.
- The bridge runs from the mounted checkout (`python -m horus.gta_bridge`) and sends to
  `HORUS_GTA_TARGET` (default `127.0.0.1:5611`, HorusGta.dll in GTA on the same host). Its output
  is in `logs/gta_bridge.log`. If SITL or the bridge exits, the container stops.
- Unlike `start_sim.sh`, it does not start the grounder (`scripts/start_grounder.sh`), which keeps
  memory free for GTA on the same host.
- `HORUS_CONFIG` defaults to `/horus/config/horus_gta.yaml` in the container (the `gta` row of
  `sim/scripts/world_profiles.py`), as `--procedural` does with its profile. The bridge takes its
  FOV (84°) and default pitch (-45°) from it, and HORUS processes started in the container load
  it; it reads frames from `gta://127.0.0.1:5610`. GTA's flight limits are ArduPilot's
  `gta.parm`: a 40 m circle around home (the Legion Square origin in `HorusGta.ini`), a 48 m
  ceiling and a 6 m floor above the kiosk roof and low street furniture (`FENCE_TYPE 11`,
  `FENCE_ALT_MIN 6`; Copter arms the floor only after the first climb above it and drops it when
  LAND or RTL starts), with `FENCE_ACTION 1`. Obstacles are left to ArduPilot's proximity
  avoidance on the emulated ToF beams in physics mode.

Checked on Koko on 2026-10-20 (image `horus-sim:local-s1s2`, GTA not running). `start --gta` was
ready in 2.3 s. The bridge sends nothing until the EKF has a position; it started 45 s after
start and then logged `250 packets in 5 s`. `s0` took off to 4.86 m and landed, and the bridge
followed with down = -4.62 m. `stop` removed the container, and no SITL or bridge process was
left. That run predates the bridge's gimbal device, so it checked no gimbal setpoints.

HORUS itself runs in the same container, against SITL on 5760 and GTA frames on 5610. A GTA run
is smoked like this, with Gazebo stopped (`run.sh` refuses otherwise). Run it on Koko in `~/horus`,
except the two `gta_game.sh` lines, which run from the laptop checkout because they reach Koko over
SSH:

```bash
HORUS_SIM_IMAGE=horus-sim:local-s1s2 ./sim/run.sh start --gta
export HORUS_MCP_TOKEN=$(openssl rand -hex 24)
config=""   # "": the container's own horus_gta.yaml, which needs data/maps/gta/map.json
if [[ ! -f data/maps/gta/map.json ]]; then
  # No GTA map in this checkout: build a scratch map and config under sim/run/, not data/ or config/.
  scratch=sim/run/gta-scratch-map
  mkdir -p "$scratch"
  docker exec -w /horus horus-sim /opt/horus-venv/bin/python scripts/maps/fetch_gta_map.py \
    --out "/horus/$scratch/map.json"
  sed -e "s#data/maps/gta/map.json#/horus/$scratch/map.json#" -e '/walkable_path:/d' \
    config/horus_gta.yaml > "$scratch/horus_gta_smoke.yaml"
  config="/horus/$scratch/horus_gta_smoke.yaml"
fi
printf '%s\n' "$HORUS_MCP_TOKEN" | bash scripts/koko/start_mcp.sh horus-sim "$config"
docker exec -e HORUS_MCP_TOKEN -e HORUS_MCP_URL=http://127.0.0.1:8000/mcp -w /horus horus-sim \
  /opt/horus-venv/bin/python -m horus.mcp_server.client get_state            # perception.source gta://...
scripts/koko/gta_game.sh start      # laptop; for frames: Steam signed in, a display, no screen lock
docker exec -e HORUS_MCP_TOKEN -e HORUS_MCP_URL=http://127.0.0.1:8000/mcp -w /horus horus-sim \
  /opt/horus-venv/bin/python -m horus.mcp_server.client list_candidates     # JPEG under runs/manager/
scripts/koko/gta_game.sh stop       # laptop
./sim/run.sh stop
rm -rf sim/run/gta-scratch-map
```

`start_mcp.sh` with an empty config argument keeps the container's `HORUS_CONFIG`
(`horus_gta.yaml`), and with a `/horus/….yaml` path it uses that config instead; its log
(`sim/logs/horus-mcp.log`) names the loaded config and frame source. `config/horus_gta.yaml` needs
the GTA map at `data/maps/gta/map.json`, or the server refuses to start. `rsync` to Koko leaves out
`data/`, so there the block builds a scratch map instead: `fetch_gta_map.py --out` keeps its
download cache beside the output, and the scratch config drops `map.walkable_path` (GTA's ped
navmesh, which `fetch_gta_walkable.py --out` builds from a 466 MB archive and a 659 MB extracted
file). With a `gta://` source the server builds no Gazebo image publisher. Pose mode works without
the game for SITL, bridge, fence and flight checks. `run.sh frame` grabs the Gazebo camera and
refuses a GTA container; use `get_frame`.

The checkout needs `models/yolov8n.onnx` and `models/osnet_x0_25_msmt17.onnx` (not synced by the
usual rsync, which excludes `/models`) and a `runs/` directory. Without a grounder, perception runs
detector-only and `get_state` reports the grounder `unavailable`. With no MCP call for
`link_loss_rth_delay_s` (30 s) the hovering vehicle returns home. Results from the checked run are
in `gta/horus_mod/README.md` "Checked in game".

## Visual commander demo

The GUI plugin is built into the canonical image as a final layer. For quick
local iteration on an existing `horus-sim:local-s1s2` image, build only that
layer instead of rebuilding Gazebo and SITL:

```bash
docker build -f sim/docker/Dockerfile.gui-overlay \
  -t horus-sim:local-s1s2-gui .
```

The GUI launcher selects this overlay automatically; set `HORUS_SIM_GUI_IMAGE`
to override its tag (`HORUS_SIM_IMAGE` remains the headless image setting).

The image installs the `HorusCommander` gz-gui8 plugin under
`/opt/horus-gui/lib`, exports `GZ_GUI_PLUGIN_PATH`, and installs the layout at
`$HORUS_GUI_CONFIG` (`/opt/horus-gui/horus_demo.config`). The layout contains
the scene, WorldStats (sim time and real-time factor), CameraTracking, three
ImageDisplay panels (the raw indoor camera, the live perception view on
`/horus/perception/live`, and Claude's last set-of-mark view on
`/horus/som_image`), and the commander task/event/status panel with HOLD, LAND, RTH, and NEW MISSION controls.
WorldControl is omitted on purpose: its Space shortcut pauses the world whenever the 3D view has
focus, and a paused world freezes lockstep SITL, the MAVLink heartbeat and the
camera. Resume an accidentally paused world with
`gz service -s /world/horus_indoor/control --reqtype gz.msgs.WorldControl --reptype gz.msgs.Boolean --timeout 3000 --req 'pause: false'`.

`./sim/run.sh start --gui horus_indoor.sdf` selects the overlay and launches
`gz sim -g --gui-config /horus/sim/gui/horus_demo.config` beside the server in
the same container. For `horus_indoor.sdf`, the launcher waits for the
`horus_iris` model and `/gui/follow` service, follows the model, and applies
`/gui/follow/offset` when available.

GUI mode mounts `${XAUTHORITY:-$HOME/.Xauthority}` read-only and
`/tmp/.X11-unix`, runs as the host UID/GID with `DISPLAY=:0` and
`QT_QPA_PLATFORM=xcb`, and adds NVIDIA GPU access when available. It does not
use `xhost`.

For the host commander, use the root README's standard `.venv` or lean
`.venv-host` installation; the `commander` extra includes Uvicorn for the host
HTTP/SSE endpoint. Set `HORUS_HOST_VENV` to select another suitable venv.

Then run the demo:

```bash
./scripts/demo.sh
./scripts/demo.sh --record out.mp4
```

The default deployment keeps Gazebo/SITL, the automatic ToF bridge, and
perception MCP on Koko, while the laptop displays Gazebo and runs
`horus-commander-serve` and `horus-gz-bridge`. Use `--remote HOST` to select
another server, or `--local` to explicitly run the entire stack locally.
The camera stream is enabled automatically. Press Ctrl-C to stop the managed
processes. GUI runtime logs are under `sim/run/gui-client/` while it runs;
shutdown purges the active GUI-client run directory. Server logs remain on
Koko and are fetched into `sim/logs/` and `runs/`.

`--record` uses ffmpeg `x11grab`. When `xwininfo` is available it captures the
Gazebo window region; otherwise it captures the full XWayland display `:0`.
Native Wayland windows are not included in the recording.

Start `horus-commander-serve` on the host with `HORUS_MCP_URL` and
`HORUS_MCP_TOKEN` set for the container's authenticated MCP endpoint, and set
a separate `HORUS_COMMANDER_TOKEN`. The server invokes the host's authenticated
Claude Code CLI; it does not require an LLM API key. In the host-networked
simulation container, run `horus-gz-bridge` with the same
`HORUS_COMMANDER_TOKEN` and the MCP URL/token. The bridge forwards `/horus/task`
to the host, renders commander SSE events on `/horus/events` as readable lines
(task, Claude text, `→` tool calls, `←`/`✗` results, final answer; per-token
deltas are omitted), and publishes `get_state` results to `/horus/state` at
2 Hz. The MCP server publishes the set-of-mark image returned by `get_frame` /
`list_candidates` on `/horus/som_image`, and every processed perception frame on
`/horus/perception/live`: tracked boxes with tracker ID, detector class and
confidence, shaded ignore regions, and a footer with
median detector and tracker/ReID milliseconds, processed fps, and detection
counts. The same throughput figures appear under `perception.processing` in
`get_state` and as a `perception … fps` INFO line in the MCP log every 10 s.

HOLD, LAND, and RTH publish their action strings to `/horus/emergency`; NEW MISSION publishes on `/horus/reset`. `horus-gz-bridge` forwards these topics to the authenticated host `POST /emergency` and `POST /reset` endpoints. Text tasks still use `POST /task`; short imperatives such as `stop`, `land now`, and `return home` are recognized as direct emergencies, while longer sentences go to the model. The host keeps one Claude Code stream-json process (or API conversation history) per mission, sends executor/perception events from MCP `wait_events` into the active mission, and exposes readable emergency/session events over its SSE stream. Use `commander.max_turns` (default 40) for the per-response tool-call budget and `commander.timeout_s` for the per-response timeout.

For example, with the container MCP server already running, start the host
commander:

```bash
export HORUS_MCP_URL=http://127.0.0.1:8000/mcp
export HORUS_MCP_TOKEN='replace-with-the-container-MCP-token'
export HORUS_COMMANDER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
horus-commander-serve
```

In another host terminal, forward the same credentials to the bridge process:

```bash
docker exec --interactive --tty \
  --env HORUS_COMMANDER_URL=http://127.0.0.1:8765 \
  --env "HORUS_COMMANDER_TOKEN=${HORUS_COMMANDER_TOKEN}" \
  --env "HORUS_MCP_URL=${HORUS_MCP_URL}" \
  --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" \
  "${HORUS_SIM_CONTAINER_NAME:-horus-sim}" horus-gz-bridge
```

### Local runs

`sim/run.sh` auto-detects an NVIDIA GPU (`--gpus all`); on this workstation
expect a real-time factor around 0.2-0.35. At RTF 0.2 the pose stream can arrive
older than the executor's telemetry age guard (`executor.telemetry_max_age_s`,
1.5 s wall), which then holds (`vehicle pose is stale (1.52s); entering hold`),
and command TTLs run out sooner in simulation time; raise both for slow hosts
rather than relaxing the guard elsewhere. `scripts/koko/remote_up.sh` only
checks for an existing grounder venv and continues without text grounding when
it is absent.

### Split laptop/Koko deployment

`./scripts/demo.sh` defaults to this split; `--remote koko` selects it explicitly.
It runs the headless Gazebo/SITL world, ToF
bridge, and perception MCP on Koko, while the laptop runs only the `horus-gui`
container with Gazebo GUI and `horus-gz-bridge`. The local
`./sim/run.sh gui-client` mode does not start a server, SITL, or ToF bridge;
its run data is isolated under `sim/run/gui-client/`. The Koko server keeps its
normal `horus-sim` name and run/log data. `remote_up.sh` syncs source while
preserving `runs/`, `sim/logs/`, `sim/run/`, and root `models/`; it transfers a
same-architecture local image with `docker save | ssh docker load` when
available, otherwise it builds natively on Koko. Missing model-cache files are
fetched on Koko.

`./scripts/demo.sh --remote --procedural --kind forest` (or `urban`, plus
`--seed N`/`--stratum baseline|chaotic`) generates the world on the laptop,
ships it with the same rsync, starts that SDF on Koko with the outdoor SITL
parameters and `config/horus_<kind>.yaml`, and binds the GUI panels to the
generated world's topics. `remote_up.sh` takes an optional world argument (or
`HORUS_REMOTE_WORLD`) and defaults to `horus_indoor.sdf`.

The remote container defaults to an 8 GiB RAM limit and a 9 GiB combined
RAM-plus-swap limit. Override `HORUS_REMOTE_SIM_MEMORY_LIMIT` and
`HORUS_REMOTE_SIM_MEMORY_SWAP_LIMIT` together when the server's capacity
differs. Docker reports the combined limit as `HostConfig.MemorySwap`; with
the defaults, cgroup `memory.max` is 8 GiB and `memory.swap.max` is 1 GiB.
The local GUI client does not inherit these limits. For a server launched
directly with `sim/run.sh`, the corresponding optional settings are
`HORUS_SIM_MEMORY_LIMIT` and `HORUS_SIM_MEMORY_SWAP_LIMIT`.

Koko's previous boot (`b22ac9307ec448f69c173b9e2cf2a1dd`) records global OOM
kills on 30 September 2026 at 18:00:50, 18:01:26, and 18:01:57 (+03:00).
The three Python victims each held about 6.3 GiB anonymous RSS, with the
2 GiB host swap exhausted. Their cgroup was a GNOME Terminal scope under
`user@1000.service`, not the HORUS Docker container; the log does not identify
their Python command lines. The host journal continued afterward, so these
OOM records establish memory exhaustion, not the cause of every later reboot.
The container limits protect this workload but cannot prevent unrelated host
applications from exhausting memory.

The visible split smoke run on 1 October exercised takeoff and the GUI's
emergency controls, with live camera/perception at 5 fps. Remote cgroup
accounting showed about 2.8 GiB peak RAM and zero OOM events during that run.
The Claude Code account returned a weekly-limit message; direct MCP skills
and emergency controls remained available without model inference.

Use the helpers independently when needed:

```bash
scripts/koko/remote_up.sh
scripts/koko/remote_down.sh
scripts/koko/remote_fetch_logs.sh
```

`remote_down.sh` stops only the managed Koko server and laptop `horus-gui`
container, closes the SSH tunnel, and preserves remote run data. Log retrieval
copies only Koko's `sim/logs/` and `runs/`; existing laptop files with the same
names are retained as `.before-koko` backups.

The deployment uses SSH configuration plus `getent` to find Koko's address,
`ip -4 route get` on the laptop to select its interface, and the same route
lookup on Koko to select the Gazebo server address. Overrides are
`HORUS_REMOTE_IP`, `HORUS_LOCAL_IP`, `HORUS_REMOTE_GZ_IP`, and
`HORUS_LOCAL_GZ_IP`. `HORUS_REMOTE` selects the SSH alias, `HORUS_REMOTE_DIR`
selects the checkout (default `horus`), `HORUS_REMOTE_SIM_IMAGE` selects the
server image (default `horus-sim:local-s1s2`), and `HORUS_REMOTE_SIM_GPU`
defaults to `amd`. `HORUS_GZ_PARTITION` defaults to `horus` for the split
deployment; optional `HORUS_GZ_RELAY_REMOTE` and `HORUS_GZ_RELAY_LOCAL` set
the relay peer addresses if multicast discovery is unavailable.

Gazebo discovery requires UDP 10317/10318 and bidirectional dynamic ZeroMQ
TCP traffic between laptop and Koko. Permit peer-to-peer traffic on the two
Ethernet interfaces, or configure the relay. Koko's camera RTP UDP 5600 stays
loopback-local. MCP listens only on Koko's loopback and the laptop reaches it
through an SSH tunnel bound to `127.0.0.1:${HORUS_MCP_LOCAL_PORT}` (default
`8000`, remote port `8000`); choose another unprivileged local port if needed.
Do not expose MCP on the LAN.

Indoor SITL loads its proximity and fence parameters automatically:
`./sim/run.sh start horus_indoor.sdf` resolves `common.parm` and `indoor.parm`
through `sim/scripts/world_profiles.py`. `indoor.parm` is the room's ArduPilot
fence, altitude only (`FENCE_ALT_MAX 2.2` below the 2.5 m walls,
`FENCE_ACTION 2` lands, `FENCE_MARGIN 0.2`), and proximity avoidance
(`AVOID_MARGIN 1.0`) holds the walls. The launcher appends the fragments to the
run-local SITL overrides, preserving `LOG_DISARMED 0`; `HORUS_SITL_PARAM_FILE`
overrides the list. `common.parm` also sets `PLND_ENABLED 0`: the `gazebo-iris`
SITL defaults enable ArduPilot precision landing for an IR-lock beacon nothing
feeds, which made every LAND hover 7 s ("PrecLand: Failsafe Measures") before
descending. HORUS's own AprilTag landing is unaffected.

Gazebo's world frame is ENU (x east, y north), so the room's NED `(n, e)` is
world `(e, n)`. Off the pad, test `land` over clear floor such as NED `(2, -3)`
(world `(-3, 2)`). NED `(-2, 2)` is above the static
`person_laying` patient mesh (world `(2.4, -2.2)`, up to 1.09 m tall): LAND
strikes it and settles on the floor about 1 m from its position target. It then
stays armed, because the position controller's lean toward that target exceeds
ArduPilot's 15° landing-detector limit.

For every world that carries the ToF payload (`horus_indoor.sdf`, the
`horus_forest*` and `horus_urban*` worlds), the launcher automatically starts
one bridge after SITL opens serial2 on TCP 5763. It subscribes to all six
horizontal proximity scans and `range_down`, using a unique sensor ID and
MAVLink orientation for each topic at 10–20 Hz. gz-sim scopes sensor topics
under the world and every enclosing model, so the launcher passes the world and
its model chain (`horus_iris/iris_with_gimbal/horus_sensor_payload` outdoors,
`horus_iris/horus_sensor_payload` indoors) to `scripts/tof_bridges.sh`, which
derives the seven topics. Bridge startup fails if any configured topic produces
no valid scan; diagnostics are written to `logs/tof_bridge.log`.

For every world with the HORUS airframe (`horus_indoor.sdf`, the `horus_forest*`
and `horus_urban*` worlds), the launcher also starts the Gazebo gimbal device,
`python -m horus.gimbal.gazebo --world <world>` (`horus-gz-gimbal`). ArduPilot
has no mount: its plugin drives only the rotors. Like a brushless gimbal with its
own IMU, the device reads the vehicle's lean from the airframe IMU
(`.../iris_with_standoffs/link/imu_link/sensor/imu_sensor/imu`), holds HORUS's
`GIMBAL_DEVICE_SET_ATTITUDE` setpoint (horizon pitch, nose-relative yaw, level
roll; −45° pitch until the first one) against it on the gimbal's yaw/roll/pitch
joints (`/gimbal/cmd_yaw|roll|pitch`, within ±160°, ±30° and −135..45°), and
reports the attitude its joints (`/gimbal/joint_state`) and IMU measure as
`GIMBAL_DEVICE_ATTITUDE_STATUS` to HORUS's `gimbal.connection`
(`udpout:127.0.0.1:14600`), at 50 Hz. It reports nothing until both topics have
data. The indoor airframe is a vendored copy of `iris_with_gimbal`
(`sim/models/horus_iris_with_gimbal`) without ArduPilot's gimbal channels,
included under the same name, so camera topics are unchanged. Diagnostics are in
`logs/gimbal.log`; if the device exits, the simulation stops.

The outdoor fragment sets (`common.parm` with `outdoor.parm` and
`canonical_fence.parm` or `procedural_fence.parm`) load the same
`PRX1_TYPE 2` / `AVOID_ENABLE 3` / `AVOID_MARGIN 1.0` proximity-avoidance
settings as the indoor profile (all from `common.parm`), so ArduPilot's simple
avoidance brakes before trees, buildings and vehicles instead of flying through them.


Each start creates a disposable `sim/run/<run-id>/` working directory for
SITL `eeprom.bin`, `terrain/`, logs, and Gazebo's HOME/XDG cache. Gazebo and
SITL logs are in `sim/run/<run-id>/logs/`. The SITL parameter override sets
`LOG_DISARMED 0` after frame defaults. While running, a disk watchdog stops
both processes if root free space falls below 1 GB.

Run S0 and then capture one camera frame:

```bash
./sim/run.sh s0
./sim/run.sh frame
./sim/run.sh frame /horus/sim/logs/indoor_frame.jpg
```

The S0 proof connects directly to `tcp:127.0.0.1:5760`. SITL serves one client
on this endpoint, so `--no-mavproxy` leaves it available for the S0 pymavlink
client. The indoor range bridge uses the dedicated serial2 TCP port 5763, and
serial1 TCP 5762 remains available for a concurrent telemetry monitor. The
proof waits for its direct TCP connection, requests GPS/EKF status and 5 Hz
global-position streams, waits for navigation readiness, sets GUIDED, and waits
for ArduPilot pre-arm checks before arming. It then commands a 5 m takeoff and
confirms three `GLOBAL_POSITION_INT.relative_alt` samples in the 4.5–5.5 m band,
then enters LAND and waits for disarm. `s0_takeoff.py` prints sampled relative
altitudes. `grab_frame.py` discovers and enables a camera `/enable_streaming`
topic, decodes H.264 with GStreamer/OpenCV, and writes
`sim/logs/s0_frame.jpg` by default; an optional output path can save another
camera frame.

Stop and remove the container while retaining its active run directory:

```bash
./sim/run.sh stop
```

Use `./sim/run.sh stop --purge` to stop the container and delete only the run
directory named by `sim/run/.active`. `./sim/run.sh frame` still writes the S0
camera image to `sim/logs/s0_frame.jpg`.

The container name defaults to `horus-sim` and the image name to
`horus-sim:jammy-harmonic-copter-4.7.1-slim`; set `HORUS_SIM_CONTAINER_NAME` or
`HORUS_SIM_IMAGE` to override them consistently for the start/stop commands.

## Requirements and limitations

The image downloads Ubuntu and OSRF packages, the official firmware binary,
the sparse ArduPilot checkout (including `ArduCopter/`), the pinned plugin
source, and Python dependencies; it needs IPv4 access to Ubuntu, OSRF, GitHub,
and PyPI. An unforced Ubuntu-container `apt-get update` timed out after 240
seconds, so the Dockerfile forces APT downloads to IPv4.

The plugin source/build trees are removed after copying installed runtime
files; compiler and development packages remain. Build assertions cover Gazebo
version, SITL tool availability, and headless rendering. The build and runtime
watchdogs stop below 1 GB free.
