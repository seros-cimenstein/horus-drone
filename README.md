# HORUS

HORUS is a safety-first supervisory-control core for ArduPilot: a remote commander chooses semantic targets and bounded skills, the onboard executor turns them into closed-loop motion, and ArduPilot retains stabilization, proximity avoidance, failsafes, and RC override. The core has no offline mission packages, specialist fine-tuning, thermal module, or DroneCAN integration.

## Architecture

- `horus/flight/` owns a threaded `pymavlink` connection, timestamped telemetry cache, heartbeat age, battery/proximity/status-text observations, and explicit Guided/arm/takeoff/LAND/RTL/yaw/velocity operations.
- `horus/executor/` serializes bounded `take_off`, `goto`, `approach`, `follow`, `orbit`, and raster `scan` tasks. Long skills return task IDs; tasks report requested and executed targets, any clamp reason, terminal status, and whether the action completed. TTL expiry, cancellation, and skill completion stop motion in Guided with zero velocity. `hold`, `rth`, and `land` preempt queued work.
- `horus/perception/` reads the UDP H.264 camera or a video source, detects proposals, tracks persistent IDs with appearance embeddings, keeps a timestamped frame/pose ring buffer, emits a numbered set-of-mark JPEG and candidate table, supports embedding matching and a tiny labeled-reference classifier, and provides a lazy-loaded grounding fallback for old frames.
- `horus/mcp_server/` exposes state, frame/candidate, grounding, recognition-profile, task, bounded skill, and emergency tools over the Python MCP SDK's streamable HTTP transport. Every HTTP request requires a bearer token; detector labels and ArduPilot status text are returned as explicitly quoted untrusted values.
- `horus/commander/` defaults to a headless Claude Code CLI agent that talks to HORUS only through authenticated MCP tools; the Anthropic and OpenAI API-backed clients remain optional.
- `horus/repl/` provides the terminal controls and runs replayable YAML skill sequences from `missions/`.
- `horus/tof_bridge/` forwards Gazebo LaserScan ranges as MAVLink `DISTANCE_SENSOR` messages. `config/ardupilot/horus_sitl.parm` enables MAVLink proximity and the proximity avoidance bit.

**Safety-critical control decision:** ArduCopter's simple proximity avoidance is intended for Guided **velocity** targets, not Guided position targets. All executor translations—including `goto` and scan waypoints—therefore use a local pose-feedback controller and velocity-only `SET_POSITION_TARGET_LOCAL_NED` messages at 10–20 Hz. Horizontal speed, vertical speed, distance, altitude, and geofence bounds are enforced in `config/horus.yaml` before motion. `hold` remains in Guided and continually commands zero velocity; after `executor.link_loss_rth_delay_s`, it escalates to RTH/RTL. It never relies on LOITER/ALT_HOLD without an RC pilot.

MCP and cloud links are supervisory only. The HTTP listener binds to loopback by default; do not expose it publicly. Use a strong `HORUS_MCP_TOKEN` and an RC override. Proximity avoidance parameters do not replace validation in SITL or a controlled test area.

## Install

Python 3.10 or newer is required. Create a virtual environment and install the project:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[commander,server,perception,model-tools,opencv-headless,test]'
bash scripts/fetch_models.sh
```

For the commander running on the host while MCP runs in the container, install the lean `commander` extra. It includes MCP, PyYAML, HTTPX, and Uvicorn for the authenticated host HTTP/SSE endpoint; it does not install NumPy, OpenCV, ONNX Runtime, or MAVLink:

```bash
uv venv .venv-host --python 3.14
source .venv-host/bin/activate
uv pip install -e '.[commander]'
horus --help
horus-mcp-call --help
```

With Linux host networking, point the client/REPL at `http://127.0.0.1:8000/mcp`.

The default commander backend is `claude-code`, which runs the authenticated Claude Code CLI and does not require `ANTHROPIC_API_KEY`. To use an optional API backend, set `commander.provider` to `anthropic` or `openai` in `config/horus.yaml` and install the matching extra:

```bash
python -m pip install -e '.[commander,commander-anthropic,test]'
# or
python -m pip install -e '.[commander,commander-openai,test]'
```

For OpenAI, also set `commander.api_key_env` to `OPENAI_API_KEY` and choose an OpenAI-compatible `commander.model`; configure `base_url` when using an API-compatible endpoint.

`perception` installs NumPy and ONNX Runtime for CPU-only YOLO and appearance embedding; it does not install PyTorch, Ultralytics, or OpenCV. `bash scripts/fetch_models.sh` checks pinned SHA256 hashes and prepares the OSNet checkpoint with a flexible batch axis. That one-time conversion uses the small `model-tools` ONNX extra, not Torch. `grounding` is an optional, lazy fallback that installs Torch/torchvision/Transformers only when you choose to use text grounding.

The optional `opencv-headless` extra is for local image/video sources that do not need GStreamer. It must not be installed in the simulation environment: the sim uses Ubuntu's `python3-opencv` build with GStreamer support. For sim, install `.[server,perception,model-tools,test]` and run in a venv that can import the system package (for example one created with `--system-site-packages`); the core NumPy `<2` constraint keeps the pip runtime compatible with Ubuntu's NumPy 1.x OpenCV bindings, and the UDP camera reader checks `cv2.getBuildInformation()` and reports a clear error if GStreamer is absent. OpenCV is needed by both image preprocessing and the camera, but the pip headless wheel has no GStreamer.

The fetched YOLOv8n ONNX checkpoint is published by [Kalray/yolov8](https://huggingface.co/Kalray/yolov8), whose model card declares AGPL-3.0; assess those terms before closed-product use or redistribution. The OSNet x0.25 ReID checkpoint is published by [anriha/osnet_x0_25_msmt17](https://huggingface.co/anriha/osnet_x0_25_msmt17) under MIT.

## Demo: MCP manager

HORUS exposes bounded flight/perception tools, the `horus_commander` MCP prompt, the `horus://mission/brief` resource, and set-of-mark JPEG frames over authenticated streamable HTTP. No provider API key is needed for the default Claude Code commander.

In the server terminal, set these values and start the authenticated MCP service with `horus-mcp`. Export the same values in separate client/REPL terminals so server tool calls and Claude's streamed events share one JSONL file:

```bash
export HORUS_MCP_URL=http://127.0.0.1:8000/mcp
export HORUS_MCP_TOKEN='replace-with-a-long-random-secret'
export HORUS_RUN_DIR="$PWD/runs/s6-demo"
export HORUS_EVENT_LOG="$HORUS_RUN_DIR/events.jsonl"
horus-mcp
```

In a terminal with Claude Code installed and authenticated, connect an interactive Claude Code session to HORUS (the current CLI flag syntax is documented in the [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) and [MCP guide](https://code.claude.com/docs/en/mcp)):

```bash
claude mcp add --transport http horus "$HORUS_MCP_URL" --header "Authorization: Bearer $HORUS_MCP_TOKEN"
```

For a terminal-only agent, use the real MCP client. `get_frame` saves its JPEG under `runs/manager/` and prints the path for opening:

```bash
horus-mcp-call list-tools
horus-mcp-call get-prompt horus_commander
horus-mcp-call get_state
horus-mcp-call get_frame
horus-mcp-call list_candidates
```

The `horus` REPL's free-text requests and mission `text:` steps use `claude -p` with a generated config containing only the HORUS MCP server, strict MCP config, an allowlist for `mcp__horus__*`, and no built-in shell/file/web tools. The REPL obtains the safety instructions from `horus_commander`; Claude's JSON stream and server-side tool calls append to `$HORUS_EVENT_LOG`.

```bash
horus --server-url "$HORUS_MCP_URL"
horus --server-url "$HORUS_MCP_URL" --mission missions/S6_find_red_shirt.yaml
```

Use `claude auth status` to check Claude Code login without printing credentials. The legacy API-backed commander remains available only when explicitly selected and requires its configured provider key.

## Connect to the simulation

The simulation toolchain is maintained separately under `sim/`. Its MAVLink endpoints are TCP `127.0.0.1:5760` for HORUS flight, TCP `127.0.0.1:5763` for the indoor range bridge, and TCP `127.0.0.1:5762` for a monitor/pose trace; the Gazebo H.264 RTP camera uses UDP `5600`. Run HORUS services in the same network namespace as these endpoints; the authenticated HTTP listener remains loopback-only.

For the end-to-end GUI commander demo, build the overlay described in
[`sim/README.md`](sim/README.md#visual-commander-demo), then run:

```bash
./scripts/demo.sh
```

The script starts the indoor GUI simulation, its automatic ToF bridge,
container MCP with perception, host `horus-commander-serve`, and
`horus-gz-bridge`; Ctrl-C stops the processes and retains run logs. Add
`--record out.mp4` to capture the Gazebo XWayland window with ffmpeg
`x11grab` (`xwininfo` selects the window region when available; otherwise the
full `:0` display is recorded). Native Wayland windows are not captured.

The demo uses the root install's `.venv` or `.venv-host` (both documented
commander installs include Uvicorn). Set `HORUS_HOST_VENV` to select another
environment that provides `horus-commander-serve` and Uvicorn.
The host commander listens on `HORUS_COMMANDER_URL` (default
`http://127.0.0.1:8765`); the demo refuses to start if another process already
holds that port, e.g. `HORUS_COMMANDER_URL=http://127.0.0.1:8766 ./scripts/demo.sh`.
Commander events are appended to `HORUS_EVENT_LOG` (default
`runs/demo/<UTC>-commander-events.jsonl`).

### Split laptop/Koko deployment

To run the headless Gazebo/SITL, ToF bridge, and perception MCP on Koko while
keeping the Gazebo GUI and commander bridge on the laptop, use:

```bash
./scripts/demo.sh --remote koko
```

This uses remote `horus-sim` and local `horus-gui` containers; it does not
stop or reuse the laptop's full-simulation container. `Ctrl-C` stops the
managed Koko stack and SSH tunnel, stops the local GUI client, and fetches
Koko's `sim/logs` and `runs` (colliding laptop files are retained with a
`.before-koko` suffix). To manage the server separately, run
`scripts/koko/remote_up.sh`, `scripts/koko/remote_down.sh`, and
`scripts/koko/remote_fetch_logs.sh`. `remote_down.sh` preserves Koko's
`sim/run`, logs, runs, and root `models/` cache.

The SSH alias must be reachable without an interactive password prompt; Koko's
Docker daemon must work for that user without `sudo`, and the user needs access
to the AMD render node. IP selection uses the SSH host configuration and
`getent`, then `ip -4 route get` on each host. Override the route-selected
addresses when needed:

- `HORUS_REMOTE` (default `koko`), `HORUS_REMOTE_DIR` (default `horus`),
  `HORUS_REMOTE_IP`, and `HORUS_LOCAL_IP`.
- `HORUS_REMOTE_SIM_IMAGE` (default `horus-sim:local-s1s2`) and
  `HORUS_REMOTE_SIM_GPU` (default `amd`; uses Mesa `radeonsi` and `/dev/dri`).
- `HORUS_REMOTE_GZ_IP` and `HORUS_LOCAL_GZ_IP` select Gazebo's advertised
  interfaces; `HORUS_GZ_PARTITION` (default `horus`) must match on both hosts.
- If multicast discovery is blocked, set `HORUS_GZ_RELAY_REMOTE` to the
  laptop's Gazebo IP and `HORUS_GZ_RELAY_LOCAL` to Koko's Gazebo IP.
- `HORUS_MCP_LOCAL_PORT` (default `8000`) selects the local loopback side of
  the SSH tunnel; choose another unprivileged port if local port 8000 is busy.

Gazebo Transport discovery needs UDP 10317/10318 multicast (`239.255.0.7`) and
peer-to-peer dynamic ZeroMQ TCP traffic between the two hosts; allow that traffic
in both directions on the interface pair, e.g. `sudo ufw allow from 10.0.0.0/24`
on each host (or configure the relay). Hosts running Cloudflare WARP drop that
multicast in WARP's own nftables output chain even on the Ethernet link; exclude
it with `warp-cli tunnel ip add-range 239.255.0.0/16` on each host. On a laptop
used as the compute host, select the `performance` power profile: in
power-saver, Koko's real-time factor was 0.40 instead of 0.65.
[`sim/logs/koko_bringup.md`](sim/logs/koko_bringup.md) records the measured setup. The camera's RTP UDP 5600
stream remains loopback-local on Koko. MCP is also loopback-only there and is
reachable solely through an SSH tunnel bound to `127.0.0.1` on
`HORUS_MCP_LOCAL_PORT` (default `8000`, remote port `8000`); do not open MCP to
the LAN.

1. Set `GZ_SIM_RESOURCE_PATH` to include `/horus/sim/worlds`, `/horus/sim/models`, and `/opt/ardupilot_gazebo/models`; `sim/worlds/horus_indoor.sdf` includes the local `horus_iris` wrapper, which nests upstream `iris_with_gimbal` with its original ArduPilotPlugin and camera.
2. Review and load the core proximity settings into SITL (or set the same parameters through MAVProxy):

   ```text
   PRX1_TYPE 2
   AVOID_ENABLE 3
   RNGFND1_TYPE 10
   RNGFND1_ORIENT 25
   RNGFND1_MIN_CM 5
   RNGFND1_MAX_CM 400
   ```

   Verify proximity sensor orientation and Guided velocity avoidance in simulation before moving beyond a prop-free test. Additional EKF, optical-flow, rangefinder, geofence, and failsafe settings depend on the world/vehicle configuration and must be checked against the selected ArduPilot version.
3. Edit `config/horus.yaml` for the camera source, local geofence/altitude/speed limits, model names, and server port. The defaults use MAVLink TCP `tcp:127.0.0.1:5760` and the OpenCV GStreamer source `udp://0.0.0.0:5600`.
4. Set a strong server bearer token and start the loopback-only authenticated MCP server:

   ```bash
   export HORUS_MCP_TOKEN='replace-with-a-long-random-secret'
   horus-mcp
   ```

The camera opens asynchronously with retry/backoff. `get_state` reports whether it is connecting, streaming, or stalled and includes the last-frame age. For UDP/RTP input, the GStreamer pipeline and camera watchdog enforce timeouts; OpenCV timeout properties are not passed to the GStreamer backend. Flight, emergency, and state tools remain available without video; set `perception.enabled: false` in `config/horus.yaml` or start with `horus-mcp --no-perception` for a flight-only run.

5. In the REPL network namespace, use the same token to connect:

   ```bash
   horus --server-url http://127.0.0.1:8000/mcp
   ```

   The terminal accepts `/state`, `/goto NORTH_M EAST_M ALTITUDE_M`, `/take_off [ALTITUDE_M]`, `/hold`, `/rth`, `/land`, `/approach ID [DISTANCE_M]`, `/follow ID [DISTANCE_M]`, and `/orbit ID [RADIUS_M]`. Other text is handled by the authenticated Claude Code MCP backend without an API key. Direct REPL commands and emergency tools do not require a cloud provider key.
6. Run the named mission through the REPL CLI:

   ```bash
   horus --server-url http://127.0.0.1:8000/mcp --mission missions/S1_square.yaml
   horus --server-url http://127.0.0.1:8000/mcp --mission missions/S2_goto_hold_rth.yaml
   horus --server-url http://127.0.0.1:8000/mcp --mission missions/S3_wall.yaml
   ```

   S5 is an onboard-only camera/perception/follow scenario and needs no provider API key. S6 is a commander-driven Claude Code mission and also needs no provider API key; Claude Code must be installed and authenticated.

   ```bash
   horus --server-url http://127.0.0.1:8000/mcp --mission missions/S5_follow_occlusion.yaml
   horus --server-url http://127.0.0.1:8000/mcp --mission missions/S6_find_red_shirt.yaml
   ```
   S5 takes off facing the room, selects the largest detected person with a blue-shirt attribute, starts `scripts/move_target.py` (a `start_driver` mission step, so the route is timed from the follow) and follows that tracker ID for 45 seconds. The target's route is paced by Gazebo simulation time and crosses behind `corridor_screen`, hidden from the follower for 2–3 s. The follow task records every loss/re-acquisition in autopilot time, with embedding validation and ID preservation/rematching; each run writes a JSON result under `runs/`. `sim/scripts/s5_follow_trials.sh` runs the scenario on the remote simulator and judges each run against Gazebo ground truth (`sim/scripts/s5_analyze.py`).

7. For `horus_indoor.sdf`, `start_sim.sh` automatically launches one ToF bridge after SITL opens TCP `127.0.0.1:5763`. To run or restart it manually on a running indoor world:

   ```bash
   bash scripts/tof_bridges_indoor.sh
   ```

   The bridge sends all seven LaserScan topics over one MAVLink connection from system 254/component 196. Sensor IDs 0–5 use horizontal orientations `0, 1, 2, 4, 6, 7`; ID 6 feeds the downward rangefinder at orientation 25 (`PITCH_270`). The Gazebo lidars render at 10 Hz (20 Hz costs ~40% real-time factor on an integrated GPU); the bridge forwards each sensor at 10–20 Hz (default 20). It waits up to `HORUS_TOF_STARTUP_TIMEOUT` (default 60 s) for first scans, because the lockstep world only runs once a MAVLink client holds SITL port 5760. `horus_sitl.parm` configures `RNGFND1_TYPE 10` and `RNGFND1_ORIENT 25`.

## GPS-free S4 and precision landing

Use `config/ardupilot/horus_sitl_nogps.parm` for the Copter-4.7.1 GPS-denied profile. It disables both simulated GPS1 and the GPS1 receiver backend, skips only the GPS-lock pre-arm check (`ARMING_SKIPCHK 8`), enables SITL optical flow, selects optical-flow horizontal velocity and barometer vertical position in EKF3, and configures the downward MAVLink rangefinder at orientation 25. All other ArduPilot pre-arm checks remain enabled. The profile retains the existing MAVLink proximity-avoidance settings; the indoor ToF bridge connects to SITL at TCP `127.0.0.1:5763`.

`executor.geofence_polygon_ned` optionally replaces the home-centered radius with an inset simple polygon whose vertices are `[north_m, east_m]`. The indoor profile maps Gazebo `(x east, y north)` to NED `[y, x]`, encloses the room plus north corridor along the wall centre lines, and uses a 1.2 m inset: 0.1 m half wall thickness, the 1.0 m `AVOID_MARGIN` that ArduPilot proximity avoidance holds from wall faces (set in `config/ardupilot/horus_sitl*.parm`; the ArduPilot default is 2 m), and 0.1 m slack. With a smaller inset an edge target lies inside the avoidance margin, so the executor and avoidance fight and the task never arrives. Targets outside the inset polygon project to the nearest in-fence point; task responses retain the requested target, executed target, and clamp reason.

Set `flight.gps_free_mode: true` in `config/horus.yaml` to send the configured WGS-84 `global_origin_latitude_deg`, `global_origin_longitude_deg`, and `global_origin_altitude_m` with `SET_GPS_GLOBAL_ORIGIN` on MAVLink connect. This sets the EKF global origin only; it does not fabricate local-position telemetry.

During `rth` or `land`, precision landing waits until the vehicle is within 3 m of local home, points the gimbal down, and requires a fresh tag36h11 id-0 detection before switching to GUIDED centering. Horizontal correction is proportional to the tag's image offset (full 0.35 m/s at a half-frame offset, zero inside a 5% deadband); descent at 0.15 m/s runs while the tag is within a 15% cone, so the vehicle centres and descends together. Below 0.6 m the tag overflows the camera and the centred vehicle is handed to vertical LAND. Before visual acquisition, RTL/LAND remains in control; after acquisition, tag loss commands a zero-velocity hold. The gimbal is levelled again after the landing and at every take-off. `get_state` reports `precision_landing` counters (detections, acquisitions, completed landings, last fallback). The mount range must match the model: `MNT1_PITCH_MIN -135` / `MNT1_PITCH_MAX 45` (upstream `gazebo-iris-gimbal.parm`), otherwise a -90° command points the camera 45° past nadir. The pad's tag is built from geometry in the world file (Gazebo renders the PNG texture override black). `python3 scripts/landing_error.py` streams planar ground-truth error between Gazebo models `horus_iris` and `home_apriltag_pad` from `/world/horus_indoor/pose/info`; it attaches to an existing world and does not start a simulator.

Measured results for S0–S7 are in [Simulation results](#simulation-results).

## Commander-link latency emulation

`tc netem` must run where commander↔MCP traffic passes—for example, in the dedicated commander container or on its veth—not on the MAVLink interface. The helper shapes egress and redirects ingress through an IFB so both directions receive the same delay/loss:

```bash
sudo bash scripts/netem.sh up veth-commander 500ms 2%
sudo bash scripts/netem.sh down veth-commander
```

Use a dedicated network interface/namespace: the helper shapes all traffic on the selected interface. The MAVLink path must remain unshaped so this experiment measures only the delayed supervisory link.

### S7 deterministic latency sweep

The sweep assumes the indoor S5 scene, blue target driver, and camera/perception stream are already running; this MCP client does not start or control Gazebo.

Run `python3 scripts/latency_sweep.py` from the MCP client environment. It uses no LLM, selects the largest blue-shirt candidate, then either calls `follow(id)` or `point(frame_id, box)` and follows the resolved current ID. The default sweep uses D=`0,0.5,1,2,3` seconds; ID mode and box compensation on/off; and N=20 repetitions per condition. In-process D waits for the SITL `SYSTEM_TIME.time_boot_ms` clock to advance by D simulation seconds. JSONL records include measured simulation and wall delays, measured RTF, sim-time-to-target, identity success, wrong-target, and raw evidence.

Without `--netem-interface`, D is measured and waited in SITL simulation time; `time_to_target_s` is also simulation time, with `wall_time_to_target_s` recorded separately. `--netem-interface <client-veth>` shapes all MCP traffic on the separate commander↔MCP client veth in both directions; its configured D is wall-clock time, never MAVLink time, and JSONL records per-trial measured RTF plus the equivalent simulation delay. Use `--compensation default` to let `config.perception.point_compensation_enabled` control box matching, or the default `--compensation both` to sweep explicit on/off overrides.

## Configuration and tests

`config/horus.yaml` is parsed strictly; unknown keys are rejected. Set `executor.target_width_m` only after calibrating the actual target width and camera geometry. Without a measured/calibrated range, `approach` and `orbit` fail safe rather than issuing unbounded forward motion; visual follow can keep the target centered without translating. Results never report a requested but unexecuted point as reached.
`perception.ignore_regions` configures normalized `[x1, y1, x2, y2]` image boxes; detector proposals with at least 50% of their area in any box are discarded before tracking.

Run the project test suite with:

```bash
pytest
```

The suite covers executor state/TTL/clamping and profile-match rejection, polygon geofence projection in the room/concave corner/corridor, synthetic tracker re-identification and set-of-mark rendering, optical-flow/rangefinder bridge behavior, MCP authentication and box-point configuration, GPS-free origin/gimbal/SITL-clock telemetry, rendered AprilTag 36h11 id-0 precision landing, Gazebo landing-error parsing, and S7 sim/wall delay, RTF, identity, and compensation behavior. Full flight safety and performance acceptance still requires SITL/Gazebo scenarios.

## Simulation model attributions

`person_standing` and the red, blue, and green shirt variants adapt OpenRobotics' [Standing person](https://fuel.gazebosim.org/1.0/OpenRobotics/models/Standing%20person) from Gazebo Fuel, licensed CC0 1.0. Each variant carries its own copy of `standing.dae` that points the T-shirt material at the variant's recolored texture (`tshirt_red.png`, `tshirt_green.png`, `tshirt_blue.png`) and the remaining textures at `person_standing`; the shirt texture files and material names are unique per variant because Gazebo's Ogre2 renderer caches textures by file name, so identically named textures render as whichever colour loads first. `horus_indoor.sdf` uses three point lights.

`person_laying` adapts OpenRobotics' [ElderMalePatient](https://fuel.gazebosim.org/1.0/OpenRobotics/models/ElderMalePatient), authored by Roselle Carmen and licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The Fuel description credits Autodesk Character Generator and Mixamo. The local copy retains the source mesh and texture; its SDF model name and mesh URI are localized and the model is marked static.
