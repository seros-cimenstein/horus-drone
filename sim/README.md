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

`run.sh build` monitors root free space and stops the Docker CLI below 1 GB.
On a failed or aborted build it removes only newly-created dangling images and
new stopped containers, then prunes the builder cache. `run.sh start` launches
`gz sim -v4 -s -r --headless-rendering` and
`sim_vehicle.py -v ArduCopter -f gazebo-iris --model JSON --no-rebuild --no-mavproxy`.
The default world is the plugin's bundled `worlds/iris_runway.sdf`, which
includes `iris_with_gimbal` and its `GstCameraPlugin` camera. An alternate
world can be selected with `./sim/run.sh start horus_indoor.sdf`.
The image resolves runtime, server, perception, and test dependencies from
`pyproject.toml` at build time. Every container mounts the checkout at `/horus`
and the image sets `PYTHONPATH=/horus`, so Python source edits are live without
running `pip` against the bind mount. Dependency or entry-point changes require
rebuilding the image. It provides `horus`, `horus-mcp`, `horus-mcp-call`,
`horus-commander-serve`, `horus-gz-bridge`, and `horus-tof-bridge` entry points.

Optional text-grounding weights are kept outside the image. Run `docker exec horus-sim bash /horus/scripts/setup_grounder.sh` inside the sim container to create `/horus/models/grounder-venv` and cache model weights in `/horus/models/hf`; no image rebuild is needed. `start_sim.sh` starts the service on loopback port 8100 before MCP when the venv is installed. Logs are in `/horus/sim/logs/horus-grounder.log`, and a missing venv is non-fatal. When huggingface.co is unreachable, copy an `HF_HOME` cache from another machine — the `hub/models--IDEA-Research--grounding-dino-tiny` and `hub/models--google--owlv2-base-patch16-ensemble` directories, whose `model.safetensors` sha256s are pinned in the root README — and set `HORUS_GROUNDER_HF_CACHE` to it, which the setup reads with `HF_HUB_OFFLINE=1`. `HF_ENDPOINT=<mirror-url>` works only for a mirror that serves the full Hub file metadata (`Content-Length` and `X-Repo-Commit`) required by `huggingface_hub` 0.36.2; a mirror that omits either header fails with a message naming both options.

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
`sim/worldgen/` changes; delete the bundle to force a rebuild. HORUS connects
with `config/horus_forest.yaml` or `config/horus_urban.yaml`
(`HORUS_CONFIG=/horus/config/horus_<kind>.yaml` inside the container), and the
outdoor SITL parameters are selected automatically. The two-step flow is still
available:

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

When a matching scenario and `sim/scripts/civilian_life.py` are present,
`start_sim.sh` starts its `ambient` group and records pose traces in that run's
`logs/civilians.jsonl`. Mission groups such as `follow_hiker` and
`follow_jogger` stay at their scripted start pose until selected with the
driver's `--group` option. The outdoor worlds keep GPS enabled and do not start
the indoor serial2 ToF bridge. The launcher loads
`config/ardupilot/horus_sitl_outdoor.parm` by default; set
`HORUS_SITL_PARAM_FILE` to override it. Use `config/horus_forest.yaml` or
`config/horus_urban.yaml` with HORUS for the corresponding altitude, speed,
geofence, and perception profile.

### Verification and limits

Both generated SDFs pass `gz sdf -k`, and headless outdoor SITL arms, takes off,
and lands. The sampled mean RTF was 0.41 in the forest and 0.43 in the city on
this host; neither reaches the original 0.9 target.

From a clear city-road point 15 m from home, `RTL_ALT_M=45` reached 45.24 m,
13.01 m above the world's highest rooftop, then returned home and landed
disarmed. The default `RTL_CONE_SLOPE=3` lowers the climb near home: a separate
4.5 m-range RTL peaked at 13.65 m, so verify obstacle clearance at close-in
start points rather than assuming every RTL reaches 45 m.

The outdoor camera is 640×480 with an 84° horizontal FOV. Outdoor SITL uses
a −45° neutral gimbal, and `take_off` applies the outdoor config's
`default_gimbal_pitch_deg: -45`; the indoor default remains 0°. A stage frame at
6.8 m AGL showed one green-shirt hiker (`check_detections.py`, confidence 0.283;
shirt-colour confidence 0.782). The executor now ranges targets from the ground
plane: the ray through the box's ground-contact row is intersected with the
ground plane using the measured height above ground (the downward rangefinder,
otherwise the pose altitude above home) and the camera pitch (fresh
`MOUNT_STATUS`, otherwise `default_gimbal_pitch_deg`), which also yields the true
ground bearing instead of the image-only one. On 52 logged campaign boxes this
tracks the true camera-to-torso slant to a 1% median error, against +24% median
and +65% p90 for the FOV-only person-height formula, so the previous
"uncalibrated at the default pitch" caveat no longer applies to the executor.
Rays shallower than `min_ground_range_depression_deg` (10°) are refused, and a
candidate's own `bearing` attribute stays image-relative.

The S9 mission rejects a 10 m raster across the western forest rectangle because
its tree-canopy collisions make every leg unsafe; it instead follows the open
trail and clearing. At the safe hiker standoff, the lying figure is visible but
the detector has not produced a target box; do not infer a tracker ID.

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
confidence, shirt-colour attribute, shaded ignore regions, and a footer with
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

### Local campaigns

The same campaign runs on this workstation with the local image and GPU:

```bash
HORUS_SIM_IMAGE=horus-sim:local-s1s2-gui \
  python3 sim/scripts/procedural_campaign.py --random 1 --kinds forest urban \
  --ttl-scale 6 --out runs/campaign/local
```

`sim/run.sh` auto-detects the NVIDIA GPU (`--gpus all`); expect a real-time
factor around 0.2-0.35, so a full 8-test episode is ~60-100 min wall. Set
`--ttl-scale` to roughly `1/RTF` (6 at RTF 0.2): it multiplies command TTLs and
the harness's per-task wall waits, and the per-episode wall timeout defaults to
`45 min x ttl-scale` so a slow host is not truncated mid-suite. `--tests
perception,scan` (or any subset) keeps an iteration short.

Two host-speed effects are not campaign bugs but they fail tests: the executor
holds when telemetry is older than `TELEMETRY_MAX_AGE_S` (1.5 s wall, in
`horus/executor/core.py`), which a pose stream delivered at RTF 0.2 can exceed
(`vehicle pose is stale (1.52s); entering hold` in `follow-mission.log`), and
task TTLs expire mid-flight without `--ttl-scale`. The optional text-grounding
service is absent locally (`Grounder not started: runtime venv is absent at
/horus/models/grounder-venv`). `scripts/setup_grounder.sh` creates the venv from
PyPI plus the PyTorch CPU index and then downloads the two HuggingFace
checkpoints into `/horus/models/hf`; this workstation cannot reach
huggingface.co. `hf-mirror.com` redirects non-mainland IPs back to
huggingface.co, and `hf-mirror.net` serves the checkpoint files but omits Hub
metadata `huggingface_hub` requires, so the working route here is the copied
cache: fetch the same files from ModelScope's mirror of the two repos (their
`X-Linked-Etag` sha256 matches the Hub's LFS hashes) into an `HF_HOME` layout,
then run the setup with `HORUS_GROUNDER_HF_CACHE` pointing at it (offline,
`HF_HUB_OFFLINE=1`).
`scripts/koko/remote_up.sh` only checks for an existing
venv and continues without text grounding when it is absent.

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
OOM records establish memory exhaustion, not the cause of every later reboot
or the earlier campaign's exit 137. The container limits protect this workload
but cannot prevent unrelated host applications from exhausting memory.

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

For S1/S2 indoor SITL, load the proximity parameters by starting with:

```bash
HORUS_SITL_PARAM_FILE=/horus/config/ardupilot/horus_sitl.parm \
  ./sim/run.sh start horus_indoor.sdf
```

The launcher appends that file to the run-local SITL overrides, preserving
`LOG_DISARMED 0`.

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

The outdoor profile `config/ardupilot/horus_sitl_outdoor.parm` loads the same
`PRX1_TYPE 2` / `AVOID_ENABLE 3` / `AVOID_MARGIN 1.0` proximity-avoidance
settings as the indoor profile, so ArduPilot's simple avoidance brakes before
trees, buildings and vehicles instead of flying through them.


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
