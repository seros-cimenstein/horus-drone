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
the scene, WorldControl, CameraTracking, two ImageDisplay panels (the raw
indoor camera and `/horus/som_image`), and the commander task/event/status
panel.

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

The script starts the host `horus-commander-serve`, GUI indoor SITL, automatic
ToF bridge, `horus-mcp` with the default perception config, and
`horus-gz-bridge`, and enables the camera stream. Press Ctrl-C to stop the
processes; runtime logs are retained under `sim/run/`.

`--record` uses ffmpeg `x11grab`. When `xwininfo` is available it captures the
Gazebo window region; otherwise it captures the full XWayland display `:0`.
Native Wayland windows are not included in the recording.

Start `horus-commander-serve` on the host with `HORUS_MCP_URL` and
`HORUS_MCP_TOKEN` set for the container's authenticated MCP endpoint, and set
a separate `HORUS_COMMANDER_TOKEN`. The server invokes the host's authenticated
Claude Code CLI; it does not require an LLM API key. In the host-networked
simulation container, run `horus-gz-bridge` with the same
`HORUS_COMMANDER_TOKEN` and the MCP URL/token. The bridge forwards `/horus/task`
to the host, mirrors commander/MCP SSE events to `/horus/events`, and publishes
`get_state` results to `/horus/state` at 2 Hz. The MCP server publishes the
set-of-mark image returned by `get_frame` / `list_candidates` on
`/horus/som_image`.

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

### Split laptop/Koko deployment

`./scripts/demo.sh --remote koko` runs the headless Gazebo/SITL world, ToF
bridge, and perception MCP on Koko, while the laptop runs only the `horus-gui`
container with Gazebo GUI and `horus-gz-bridge`. The local
`./sim/run.sh gui-client` mode does not start a server, SITL, or ToF bridge;
its run data is isolated under `sim/run/gui-client/`. The Koko server keeps its
normal `horus-sim` name and run/log data. `remote_up.sh` syncs source while
preserving `runs/`, `sim/logs/`, `sim/run/`, and root `models/`; it transfers a
same-architecture local image with `docker save | ssh docker load` when
available, otherwise it builds natively on Koko. Missing model-cache files are
fetched on Koko.

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

For `horus_indoor.sdf`, the launcher automatically starts one bridge after
SITL opens serial2 on TCP 5763. It subscribes to all six horizontal proximity
scans and `range_down`, using a unique sensor ID and MAVLink orientation for
each topic at 10–20 Hz. Bridge startup fails if any configured topic produces
no valid scan; diagnostics are written to `logs/tof_bridge.log`.


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
