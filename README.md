# HORUS

HORUS is a safety-first supervisory-control core for ArduPilot: a remote commander chooses semantic targets and bounded skills, the onboard executor turns them into closed-loop motion, and ArduPilot retains stabilization, proximity avoidance, failsafes, and RC override. The core has no offline mission packages, specialist fine-tuning, thermal module, or DroneCAN integration.

## Architecture

- `horus/flight/` owns a threaded `pymavlink` connection, timestamped telemetry cache, heartbeat age, battery/proximity/status-text observations, vehicle roll/pitch (`ATTITUDE`) and gimbal attitude (`MOUNT_STATUS`) each stamped with its own monotonic receive time, and explicit Guided/arm/takeoff/LAND/RTL/yaw/velocity operations.
- `horus/executor/` serializes bounded `take_off`, `goto`, `approach`, `follow`, `orbit`, raster `scan`, and camera-driven `search` tasks. Long skills return task IDs; target tasks report range and range source as well as requested/executed targets, clamps, and terminal status. `approach`, `follow`, and `orbit` all hold at zero velocity and re-identify the target by appearance when a frame drops or fails to validate it, bounded by the task TTL, and all three publish `target_lost`/`target_reacquired` events with the loss cause (candidate gone/stale versus reference appearance mismatch); all three keep the target centred in yaw while they move, so a circling orbit does not translate out of its own camera's field of view. `target_size_m` supplies a known object height, and the configured `target_height_m` ranges person targets only — the current label, a classless candidate, or track-level person evidence (a shirt-colour attribute, or a person label remembered for the track) — so a frame that mislabels a person (for example as `kite`) does not remove the range, and any disagreement used is reported in `tracking.range_source_note`. A non-person-labeled target without that evidence refuses (the error names the label) until `target_size_m` or a measured forward ToF range is available. TTL expiry, cancellation, and skill completion stop motion in Guided with zero velocity; `hold`, `rth`, and `land` preempt queued and running work. An emergency takes effect even while a skill step is mid-flight (revalidating its target or sending a command): the step's own completion, failure, TTL, or cancellation path never sends a later Guided, hold, or velocity command after the emergency, and the preempted task stays `cancelled`. An emergency waits at most for one guarded flight-command operation already in progress (for example a velocity setpoint, or a hold's Guided switch plus zero-velocity setpoint), never for perception or model work. Translations require pose telemetry, and target skills also a heading sample, no older than `executor.telemetry_max_age_s` (default 1.5 s wall); forward ToF ranges and search frames older than that are ignored. The flight link requests `ATTITUDE`, `LOCAL_POSITION_NED`, `GLOBAL_POSITION_INT` and `SYSTEM_TIME` at 20 Hz of autopilot time (the rest at 5 Hz) so that wall-clock gaps stay well under the guard down to a simulation real-time factor of ~0.1; a low-RTF run raises the limit explicitly instead of relaxing the guard silently.
- `horus/perception/` reads the UDP H.264 camera or a video source, tracks detector and CSRT-seeded (`open_vocab`/`point`) targets with appearance embeddings, keeps a timestamped frame/pose ring buffer, emits a numbered set-of-mark JPEG and candidate table, and uses the loopback grounder service for continuous vocabulary detection and point-by-description. `horus/perception/scene.py` is a pure standard-library scene monitor fed by every processed frame: per-track image-motion history and fixed-camera region watches (see [Scene tools](#scene-tools-image-motion-and-region-watches)).
- `horus/mcp_server/` exposes state, frame/candidate, grounding, recognition-profile, scene (motion and region-watch), task, bounded skill, emergency, and event-wait tools over the Python MCP SDK's streamable HTTP transport. `wait_events` returns executor and perception transitions without polling task status. Every HTTP request requires a bearer token; detector labels and ArduPilot status text are returned as explicitly quoted untrusted values.
- `horus/commander/` maintains one Claude Code or Anthropic/OpenAI API conversation per mission, streams executor/perception events into it, and bypasses the model for authenticated emergency controls. An emergency interrupts the running model turn and drops operator requests and emergency notices that were queued before it and not yet sent to the model. An API-provider turn that is interrupted or fails (for example during a tool call) is removed from the conversation as a whole, so the next turn never carries an unanswered tool call; cancelling a turn still waiting for its predecessor leaves the predecessor's history intact.
- `horus/repl/` provides the terminal controls and runs replayable YAML skill sequences from `missions/`. Free text runs in the background, so emergency commands can still be typed during a model turn.
- `horus/tof_bridge/` forwards Gazebo LaserScan ranges as MAVLink `DISTANCE_SENSOR` messages. `config/ardupilot/horus_sitl.parm` enables MAVLink proximity and the proximity avoidance bit.

**Safety-critical control decision:** ArduCopter's simple proximity avoidance is intended for Guided **velocity** targets, not Guided position targets. All executor translations—including `goto` and scan waypoints—therefore use a local pose-feedback controller and velocity-only `SET_POSITION_TARGET_LOCAL_NED` messages at 10–20 Hz. Horizontal speed, vertical speed, distance, altitude, and geofence bounds are enforced in `config/horus.yaml` before motion. `hold` remains in Guided and continually commands zero velocity; once no MCP call (including the commander's event long-poll) has reached HORUS for `executor.link_loss_rth_delay_s`, it treats the supervisory link as lost and escalates to RTH/RTL. It never relies on LOITER/ALT_HOLD without an RC pilot.

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

`perception` installs NumPy and ONNX Runtime for CPU-only YOLO and appearance embedding; it does not install PyTorch, Ultralytics, or OpenCV. `bash scripts/fetch_models.sh` checks pinned SHA256 hashes and prepares the OSNet checkpoint with a flexible batch axis. That one-time conversion uses the small `model-tools` ONNX extra, not Torch. The optional `grounder` extra pins Python 3.10-compatible PyTorch 2.5.1, torchvision 0.20.1, Transformers 4.46.3, SciPy 1.10.1, and Pillow 10.4.0; `horus-grounder` serves `/detect` and `/health` on `127.0.0.1:8100` using Grounding DINO tiny by default, with a 320 px shortest-edge processor setting for CPU inference and source-pixel boxes.

For a standalone Python install without system OpenCV, use `python -m pip install -e '.[grounder,opencv-headless]'`, then run `horus-grounder`. Select `owlv2-base-patch16-ensemble` with `horus-grounder --model owlv2-base-patch16-ensemble`; Grounding DINO tiny is the default.

The `opencv-headless` extra installs `opencv-contrib-python-headless`, the pip build that provides the CSRT tracker used by `point` seeding, open-vocabulary seeds, and point latency compensation. Do not install it alongside `opencv-python-headless` or any other OpenCV wheel, because they all provide the same `cv2` module.

The optional `opencv-headless` extra is for local image/video sources that do not need GStreamer. It must not be installed in the simulation environment: the sim uses Ubuntu's `python3-opencv` build with GStreamer support. For sim, install `.[server,perception,model-tools,test]` and run in a venv that can import the system package (for example one created with `--system-site-packages`); the core NumPy `<2` constraint keeps the pip runtime compatible with Ubuntu's NumPy 1.x OpenCV bindings, and the UDP camera reader checks `cv2.getBuildInformation()` and reports a clear error if GStreamer is absent. OpenCV is needed by both image preprocessing and the camera, but the pip headless wheels (including the contrib build) have no GStreamer.

For the simulation container, install the optional runtime without rebuilding the image by running `docker exec horus-sim bash /horus/scripts/setup_grounder.sh` once. It creates `/horus/models/grounder-venv`, caches model weights under `/horus/models/hf`, and subsequent simulation starts launch the service automatically. Logs are written to `sim/logs/horus-grounder.log`; if the venv is absent, simulation startup continues without the service. When huggingface.co is unreachable, copy an `HF_HOME` cache (the directory containing `hub/`) from a machine that downloaded the checkpoints — specifically `hub/models--IDEA-Research--grounding-dino-tiny` (`model.safetensors` sha256 `1a2412ef99bd74bcd3c2a246fa1e48581f8889a1300c9051974741314fc042f3`, 689359096 bytes) and `hub/models--google--owlv2-base-patch16-ensemble` (`model.safetensors` sha256 `e1e130b9e404cf91a75ad45644c1da9d7fa5284085eeec864266a6923efb99e7`, 619918824 bytes) — and set `HORUS_GROUNDER_HF_CACHE` to it for an offline load (`HF_HUB_OFFLINE=1`); `start_grounder.sh` reads the same variable. `HF_ENDPOINT` works only when it points at a mirror that serves the full Hub file metadata (`Content-Length` and `X-Repo-Commit`) required by `huggingface_hub` 0.36.2 (`docker exec -e HF_ENDPOINT=<mirror-url> horus-sim bash /horus/scripts/setup_grounder.sh`); a mirror that omits either header fails with a message naming both options. The checkpoint licences (Apache-2.0 for both Grounding DINO tiny and OWLv2 base) are unchanged by the download path.

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

By default, the script starts Gazebo/SITL, the automatic ToF bridge, and
perception MCP on Koko, with a visible Gazebo GUI, `horus-gz-bridge`, and
`horus-commander-serve` on the laptop. Use `--remote HOST` for another SSH
host, or explicitly select `--local` to run the entire stack on the laptop.
Ctrl-C stops the managed processes and retains server run logs. Add
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

The host commander keeps one Claude Code `stream-json` process for the mission (API providers retain the same per-mission message history). Each `/task` message is tagged `[operator]`; the service event pump forwards relevant MCP events to an active mission without starting a session on its own. `commander.max_turns` defaults to 40 agentic tool-call turns per response, while `commander.timeout_s` bounds each response.

The authenticated loopback service exposes `POST /task` with `{"text":"..."}`, `POST /emergency` with `{"action":"hold"|"land"|"rth"}`, and `POST /reset`. Short exact imperatives such as `STOP!`, `land now`, or `return home` on `/task` use the direct emergency path; longer requests are left to the commander. The GUI's HOLD/LAND/RTH and NEW MISSION buttons publish on `/horus/emergency` and `/horus/reset`.

### Split laptop/Koko deployment

This is the default demo deployment. To select Koko explicitly while keeping
the Gazebo GUI and commander bridge visible on the laptop, use:

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
- `HORUS_REMOTE_SIM_MEMORY_LIMIT` (default `8g`) caps the remote container's
  RAM; `HORUS_REMOTE_SIM_MEMORY_SWAP_LIMIT` (default `9g`) caps **RAM plus
  swap**, allowing at most 1 GiB swap with the defaults. These limits apply
  to Gazebo, SITL, perception, and the optional grounder together, not the
  laptop GUI. They do not constrain unrelated Koko host processes.
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

The camera opens asynchronously with retry/backoff. `get_state` reports whether it is connecting, streaming, or stalled and includes the last-frame age. For UDP/RTP input, the GStreamer pipeline and camera watchdog enforce timeouts; OpenCV timeout properties are not passed to the GStreamer backend. Flight, emergency, and state tools remain available without video; set `perception.enabled: false` in `config/horus.yaml` or start with `horus-mcp --no-perception` for a flight-only run. A flight-only server needs only the `server` extra: NumPy, OpenCV, and ONNX Runtime are imported only when perception starts, so `horus-mcp --help` and `horus-mcp --no-perception` run without them. A real flight link is still required to serve.

`set_target_profile` validates every reference and trains the optional labeled head before it replaces the active profile. A rejected profile returns an error and leaves the previous references, head, and recognition mode in effect. `target_match_confidence` in `get_map` candidates and the perception API's `list_candidates`/`get_candidate` records reflects the candidate's appearance in the current frame on every frame of a track, not only the first. Tracks are embedded lazily, at most once per track per frame, and share that embedding with executor revalidation. The revalidation threshold itself is never relaxed: a live, continuously associated track survives a score that dips no more than `perception.revalidation_grace_margin` (default 0.05) below the threshold for up to `perception.revalidation_grace_s` (default 1 s) after its last accepted validation — a correctly tracked target's score wobbles across the threshold (0.7917 then 0.7196 on the next frame), while a total appearance change (an identity swap) still fails immediately and a persistent dip still loses the target once the window expires. Re-acquisition after a loss gets no grace and keeps the class/shirt-colour gates and the full threshold. Multi-view references (R1) apply to *live, continuously associated* tracks only: a track stores up to `perception.reference_views` confirmed views in total (its creation view plus extras) and validates against the best of them, so a marginal dip against the creation view no longer loses a correctly tracked target. A view is admitted only from a frame that passed the full threshold against the creation view (never a grace-forgiven frame), with the class and shirt-colour gates, a crop of at least `perception.reference_view_min_width_px` x `reference_view_min_height_px` px (default 12x30; smaller crops are embedding noise — every wrong candidate in the smoke6 sweeps was a 10-25 px tall sliver or had an unconfirmed colour, and the smallest campaign-validated true re-acquisition crop was 30x39 px), and a `reference_view_dedupe_cos` (0.98) distance from every stored view; the oldest extra view is evicted first and the creation view is never evicted. Re-acquisition deliberately does NOT consult the gallery: it stays anchored on the creation view at the full threshold with all gates, because widening it raised same-colour look-alike false accepts from 5-21% to 76-92% in the offline replay, and it additionally requires the same candidate to pass on two distinct processed frames within `perception.reacquisition_confirm_s` (default 1 s; 0 disables), which filters one-frame artifacts. `list_candidates`/`get_candidate` rows report the track's current `reference_views` count. Appearance comparisons are logged per frame at DEBUG and aggregated into one `perception_reid_summary` INFO line per second (comparison count, best score and candidate, matched view index, whether the reference's own track was among them), so a long hold cannot flood the log. When a profile needs appearance evidence that is unavailable, the score is 0.0; a class-only profile scores by class. `point` with latency compensation re-identifies the old-frame reference by the current-frame appearance of the latest candidates (same lazy cache) before falling back to box geometry, so a look-alike that crossed into the old box does not take the ID.

#### Scene tools: image motion and region watches

All scene coordinates are fractions of the processed frame (`x` right, `y` down, 0–1) and everything is image-relative. Frame timestamps are capture-time `time.monotonic()`; each frame carries the camera state sampled at capture: local position, heading, vehicle roll/pitch (from MAVLink `ATTITUDE`), and gimbal pitch/roll/yaw (from `MOUNT_STATUS`), each with the age of its own latest message on the monotonic clock, so UTC/NTP steps never change freshness. Camera state older than 1.5 s is unavailable, never assumed stationary. Zoom/field of view is not reported by the current telemetry and is listed as unmeasured, so a zoom change can move the image undetected. The full schema is in the docstrings of `horus/perception/scene.py` and the MCP tools.

- `get_candidate_motion(id)` fits the tracker ID's last ≤2 s of processed boxes: centre and size rates in frame fractions per second, a log-scale rate, the fit residual and a fit confidence, plus `camera_motion`. `camera_motion.status` is `moving` (measured vehicle/gimbal drift beyond tolerance: the rates include ego-motion), `within_tolerance` (sub-tolerance and unmeasured components listed in `unmeasured` can still contribute), or `unknown` (no fresh camera state). It is never a metric or ground speed. Without an estimate, `available` is false with `reason` `no_frames`, `unknown_track`, `stale_frames`, `not_visible`, or `insufficient_samples` (fewer than 3 samples or 0.25 s); gaps over 1 s, clock resets, and frame-size changes restart the history (`stream_discontinuity`).
- `watch_region(name, box, class_name=None, dwell_s=None, absence_s=1.0, ttl_s=None)` watches a normalized box on the latest fresh frame and anchors it to that frame's camera state. A target is inside when its box centre is inside; `class_name` matches any detector or open-vocabulary label case-insensitively. Targets already inside are initial occupants without an enter event. `region_enter`, `region_exit` (after `absence_s` counted from the first frame that no longer saw the target inside; `reason` `left_region` when seen outside, `disappeared` when the track vanished), and `region_dwell` (once, after `dwell_s` of continuous inside observation) go to `wait_events` with quoted labels. Camera drift beyond 0.5 m horizontal, 0.3 m vertical, 3° heading, 3° vehicle roll/pitch, or 2° gimbal pitch/roll/yaw, a stale camera state, or a frame-size change emits `region_watch_paused`; no transitions happen while paused. After 1 s steady on the reference view, or after a stream gap, `region_watch_resumed` re-adopts current occupancy without inventing enter/exit events. At most 16 watches; reusing a name replaces a watch silently; each watch's events are budgeted (burst 10, 2/s) with suppressed counts reported. `ttl_s` ends a watch with `region_watch_expired`; the perception watchdog enforces it even while the camera delivers no frames.
- `list_region_watches()` reports stream freshness and, per watch, `active`/`stale`/`paused` status, the camera reference, unmeasured camera components, occupants and timers; stale or paused occupancy is last-confirmed state. `remove_region_watch(watch_id)` errors for unknown IDs; after it returns the watch generates no new events (earlier events stay in the `wait_events` history).

The commander wakes the active mission for region events; enter/exit/dwell repeats of the same watch within 10 s are counted rather than sent, while pause, resume, and expiry always wake it. Watches never command flight.

Label filters (`watch_region`, `search`, `resolve_candidate`) compare tracker labels exactly (case-insensitive), with no synonym mapping. The default `models/yolov8n.onnx` detector emits only the 80 COCO labels listed in `horus/perception/detector.py` — including the animals bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, and giraffe — so a label it cannot emit, such as `deer`, never matches. Open-vocabulary seeds carry the grounder's label; using them with these filters needs a running grounder and `set_vocabulary`, and that combination has not been exercised.

`search(area, class_name=None, reference_id=None, ttl_s=None)` is the bounded camera-driven search/re-acquisition skill. It flies exactly the clamped raster of `scan` and checks each newly processed frame once. With `class_name`, the largest candidate with that detector/open-vocabulary label that also passes fresh appearance validation is a match; with `reference_id`, a lost tracker ID must be re-identified above the similarity threshold from its stored appearance. On a match the executor commands zero velocity and holds, the task succeeds with `tracking.found_id`, and a `target_found` event (label quoted) wakes the commander; covering the area without a match fails the task. TTL, cancellation, clamps, and `hold`/`rth`/`land` preemption behave as for every skill. Mission YAML may use `search` as a step.

5. In the REPL network namespace, use the same token to connect:

   ```bash
   horus --server-url http://127.0.0.1:8000/mcp
   ```

   The terminal accepts `/state`, `/goto NORTH_M EAST_M ALTITUDE_M`, `/take_off [ALTITUDE_M]`, `/hold`, `/rth`, `/land` (also the bare single words `hold`, `rth`, `land`), `/approach ID [DISTANCE_M]`, `/follow ID [DISTANCE_M]`, `/orbit ID [RADIUS_M]`, and `/quit`. Other text is handled by the authenticated Claude Code MCP backend without an API key; a sentence that merely starts with an emergency word, such as `land next to the hiker`, is free text for the commander. Free-text requests are sent one at a time in the background, so the prompt stays available while the model works. `/hold`, `/rth`, and `/land` call the MCP emergency tool directly (through the commander's own session once it has started). They interrupt the running model turn and discard free text queued behind it; the model then receives an `[operator emergency]` notice. End of input waits for the running request to finish, while `/quit` cancels it. Direct REPL commands and emergency tools do not require a cloud provider key.
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

   Mission YAML may specify a top-level `driver`, `driver_args` (a list of
   literal strings, requiring `driver`), and `driver_min_duration_s`.
   The `driver` path itself resolves relative to the mission YAML.
   `driver_args` are passed unchanged: mission `$` variables are not expanded,
   and paths inside the arguments are not rebased. The driver resolves relative
   argument paths against the runner's current working directory. Run missions
   from the repository root; both trial scripts `cd` to the project root, and
   the remote S5 runner uses `/horus`. For example:

   ```yaml
   driver: ../sim/scripts/civilian_life.py
   driver_args:
     - --scenario
     - sim/scenarios/horus_urban.yaml
     - --group
     - follow_jogger
   ```

   A `select_candidate` step requires `attribute`, `equals`, `choose: largest`,
   and `store_as`. Its optional `timeout_s` must be a finite positive number;
   when supplied, the runner polls `list_candidates` every 0.5 seconds until a
   match is found or the timeout expires. On timeout it raises an error with
   the timeout and a summary of the last candidate table. Without `timeout_s`,
   it queries once and fails immediately if no candidate matches. The optional
   `min_confidence` (finite, in [0, 1], default 0.5) rejects a match whose
   detector confidence is below it, so the largest exact-attribute hit is never
   a low-confidence detector artifact; when matches exist but all fail the
   floor, the error names the best rejected match. `shirt_colour` itself is only
   reported for a torso whose saturated hue matches a supported family (red,
   green, blue, with orange and hi-vis orange reading as red), and only once a
   track's recent classifications agree (a bounded majority vote), so a track
   carries no colour for its first frames and a single misread frame cannot
   label it; achromatic and off-hue garments stay unlabelled, so such a
   selection reports `no_match` instead of attaching a wrong-coloured attribute
   to a track. For example:

   ```yaml
   - select_candidate:
       attribute: shirt_colour
       equals: green
       choose: largest
       store_as: hiker
       timeout_s: 30.0
       min_confidence: 0.5
   ```

   A `resolve_candidate` step resolves a public cue to exactly one current
   tracker ID or abstains; it never picks among look-alikes. It requires
   `class_name` and `store_as`; `attribute`/`equals` (together), `timeout_s`
   (default 10 s, positive) and `settle_s` (default 1 s) are optional. The runner
   polls `list_candidates` until a candidate matches or `timeout_s` expires, then
   keeps observing for `settle_s`. If two or more matching IDs were visible
   together during that window, it prints an `abstain` outcome with reason
   `ambiguous` and the candidate IDs; without a match it abstains with
   `no_match`. An abstaining run skips its remaining steps and still exits
   successfully; a resolved run stores the candidate (so `$target.id` works):

   ```yaml
   - resolve_candidate:
       class_name: person
       attribute: shirt_colour
       equals: red
       store_as: target
       timeout_s: 15.0
   ```

7. For every world that carries the ToF payload (`horus_indoor.sdf`, `horus_forest*`, `horus_urban*`), `start_sim.sh` automatically launches one ToF bridge after SITL opens TCP `127.0.0.1:5763`. To run or restart it manually on a running world:

   ```bash
   bash scripts/tof_bridges.sh --world horus_indoor --model-chain horus_iris/horus_sensor_payload
   ```

   The bridge sends all seven LaserScan topics over one MAVLink connection from system 254/component 196. Sensor IDs 0–5 use horizontal orientations `0, 1, 2, 4, 6, 7`; ID 6 feeds the downward rangefinder at orientation 25 (`PITCH_270`). The Gazebo lidars render at 10 Hz (20 Hz costs ~40% real-time factor on an integrated GPU); the bridge forwards each sensor at 10–20 Hz (default 20). It waits up to `HORUS_TOF_STARTUP_TIMEOUT` (default 60 s) for first scans, because the lockstep world only runs once a MAVLink client holds SITL port 5760. `horus_sitl.parm`, `horus_sitl_outdoor.parm` and `horus_sitl_procedural.parm` all configure `RNGFND1_TYPE 10` and `RNGFND1_ORIENT 25`.

## GPS-free S4 and precision landing

Use `config/ardupilot/horus_sitl_nogps.parm` for the Copter-4.7.1 GPS-denied profile. It disables both simulated GPS1 and the GPS1 receiver backend, skips only the GPS-lock pre-arm check (`ARMING_SKIPCHK 8`), enables SITL optical flow, selects optical-flow horizontal velocity and barometer vertical position in EKF3, and configures the downward MAVLink rangefinder at orientation 25. All other ArduPilot pre-arm checks remain enabled. The profile retains the existing MAVLink proximity-avoidance settings; the indoor ToF bridge connects to SITL at TCP `127.0.0.1:5763`.

`executor.geofence_polygon_ned` optionally replaces the home-centered radius with an inset simple polygon whose vertices are `[north_m, east_m]`. The indoor profile maps Gazebo `(x east, y north)` to NED `[y, x]`, encloses the room plus north corridor along the wall centre lines, and uses a 1.2 m inset: 0.1 m half wall thickness, the 1.0 m `AVOID_MARGIN` that ArduPilot proximity avoidance holds from wall faces (set in `config/ardupilot/horus_sitl*.parm`; the ArduPilot default is 2 m), and 0.1 m slack. With a smaller inset an edge target lies inside the avoidance margin, so the executor and avoidance fight and the task never arrives. Targets outside the inset polygon project to the nearest in-fence point; task responses retain the requested target, executed target, and clamp reason.

Set `flight.gps_free_mode: true` in `config/horus.yaml` to send the configured WGS-84 `global_origin_latitude_deg`, `global_origin_longitude_deg`, and `global_origin_altitude_m` with `SET_GPS_GLOBAL_ORIGIN` on MAVLink connect. This sets the EKF global origin only; it does not fabricate local-position telemetry.

During `rth` or `land`, precision landing waits until the vehicle is within 3 m of local home, points the gimbal down, and requires a fresh tag36h11 id-0 detection before switching to GUIDED centering. Horizontal correction is proportional to the tag's image offset (full 0.35 m/s at a half-frame offset, zero inside a 5% deadband); descent at 0.15 m/s runs while the tag is within a 15% cone, so the vehicle centres and descends together. Below 0.6 m the tag overflows the camera and the centred vehicle is handed to vertical LAND. Before visual acquisition, RTL/LAND remains in control; after acquisition, tag loss commands a zero-velocity hold. A heading sample older than `executor.telemetry_max_age_s` blocks acquisition and, once acquired, also commands the zero-velocity hold, because the tag offset is rotated into NED by the heading. Because GUIDED velocity setpoints mask the autopilot's own precision-landing failsafe, precision control hands the descent to LAND — the same hand-off as the 0.6 m hand-off — whenever it stops making progress or loses control: no descent progress (measured against `executor.precision_landing_min_descent_m_s` on the autopilot clock when it is fresh, over `executor.precision_landing_stall_s` of wall time, so a legitimate descent at low real-time factor is not misread) or a stale pose / detector / gimbal error after acquisition, both reported via `precision_landing_stalled` and `get_state`'s `last_fallback`. An armed GUIDED hover above the pad is never left as the terminal state. The tag detector only sees the pad on a fraction of frames at range, so the last validated centring/descent setpoint stays active for `executor.precision_landing_target_grace_s` (default 1 s) after the last detection instead of dropping to zero on every miss — that pulsing (0.15 m/s alternating with 0) is averaged by the autopilot into a near-hover. Past the grace, a lost tag returns to the zero-velocity hold. Each second of tag-controlled flight logs the tag pixel error, gimbal pitch, commanded NED velocity and altitude. The gimbal is levelled again after the landing and at every take-off. `get_state` reports `precision_landing` counters (detections, acquisitions, completed landings, last fallback). The mount range must match the model: `MNT1_PITCH_MIN -135` / `MNT1_PITCH_MAX 45` (upstream `gazebo-iris-gimbal.parm`), otherwise a -90° command points the camera 45° past nadir. The pad's tag is built from geometry in the world file (Gazebo renders the PNG texture override black). `python3 scripts/landing_error.py` streams planar ground-truth error between Gazebo models `horus_iris` and `home_apriltag_pad` from `/world/horus_indoor/pose/info`; it attaches to an existing world and does not start a simulator.

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

`config/horus.yaml` is parsed strictly; unknown keys are rejected. Set `executor.target_width_m` only after calibrating the actual target width and camera geometry. Range sources are preferred measured/geometric first: a fresh forward ToF range within the 8° bearing gate *and* within the beam's vertical coverage (the payload's forward lidars are single-ray beams on the flight plane, so `executor.forward_tof_vertical_half_fov_deg`, default 5°, skips a target that sits below the beam - outdoors at the -45° gimbal the beam flies over the person and would measure whatever is behind it), then the ground-plane estimate (the ray through the box's ground-contact row intersected with the ground plane from the height above ground - the fresh downward rangefinder, otherwise the pose altitude above home on the flat-ground assumption - and the camera pitch: fresh `MOUNT_STATUS`, otherwise `executor.default_gimbal_pitch_deg`; refused below `executor.min_ground_range_depression_deg`, default 10°, where a small pitch error dominates), then `target_size_m` (a known object height for this call), then the configured `executor.target_height_m` for person targets (the detector label, a classless candidate, or track-level person evidence: a shirt-colour attribute or a detector person label remembered for the track), then `target_width_m`. The ground-plane estimate reports the camera-to-target slant (the metric the campaign's standoff is measured with) and its note carries the horizontal distance; it is posture-agnostic, so it also ranges prone and sitting targets that the height formula mis-sizes by 2x or more, and it supplies the true ground bearing for translation and the ToF gate. An open-vocabulary seed's `person` label names it for selection but is not person evidence: a grounder-only target is refused rather than ranged from a person height (the error names the label so the caller can pass `target_size_m`, the prone-target path). A non-person-labeled target without person evidence is refused the same way. Without any measured/calibrated range, `approach` and `orbit` fail safe rather than issuing unbounded forward motion; visual follow can keep the target centered without translating. Results never report a requested but unexecuted point as reached.
`perception.ignore_regions` configures normalized `[x1, y1, x2, y2]` image boxes; detector proposals with at least 50% of their area in any box are discarded before tracking.

Run the project test suite with:

```bash
pytest
```

The suite covers executor state/TTL/clamping and profile-match rejection, emergencies that arrive during a running step, stale pose/heading/forward-ToF refusal, polygon geofence projection in the room/concave corner/corridor, synthetic tracker re-identification and set-of-mark rendering, optical-flow/rangefinder bridge behavior, MCP authentication and box-point configuration, REPL emergency routing during a running commander turn, GPS-free origin/gimbal/SITL-clock telemetry, rendered AprilTag 36h11 id-0 precision landing, Gazebo landing-error parsing, and S7 sim/wall delay, RTF, identity, and compensation behavior. Full flight safety and performance acceptance still requires SITL/Gazebo scenarios.

## Simulation model attributions

`person_standing` and the red, blue, and green shirt variants adapt OpenRobotics' [Standing person](https://fuel.gazebosim.org/1.0/OpenRobotics/models/Standing%20person) from Gazebo Fuel, licensed CC0 1.0. Each variant carries its own copy of `standing.dae` that points the T-shirt material at the variant's recolored texture (`tshirt_red.png`, `tshirt_green.png`, `tshirt_blue.png`) and the remaining textures at `person_standing`; the shirt texture files and material names are unique per variant because Gazebo's Ogre2 renderer caches textures by file name, so identically named textures render as whichever colour loads first. `horus_indoor.sdf` uses three point lights.

`person_laying` adapts OpenRobotics' [ElderMalePatient](https://fuel.gazebosim.org/1.0/OpenRobotics/models/ElderMalePatient), authored by Roselle Carmen and licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The Fuel description credits Autodesk Character Generator and Mixamo. The local copy retains the source mesh and texture; its SDF model name and mesh URI are localized and the model is marked static.

The procedural-scenery assets (`sim/models/horus_tree_pine`, `horus_tree_oak`, `horus_rock_a`, `horus_lamp_post`, `horus_vehicle_bus`, `horus_vehicle_hatchback`, `horus_vehicle_hatchback_red`, `horus_vehicle_hatchback_blue`, `horus_vehicle_sedan`, `horus_vehicle_suv`, `horus_vehicle_pickup`, `horus_vehicle_box_truck`, `horus_vehicle_delivery_truck`, `horus_person_sitting_a`, `horus_bench_a`) are imported from Gazebo Fuel by `sim/scripts/import_licensed_assets.py`; each directory pins the Fuel owner/name/version and archive SHA-256 in its `PROVENANCE.txt`, keeps the upstream author line and lists the HORUS changes (mesh/texture subset, textures downscaled to 1024 px, COLLADA/OBJ texture paths rewritten to `meshes/` + `materials/textures/`):

| Local model | Fuel model | Author | Licence | Source |
| --- | --- | --- | --- | --- |
| `horus_tree_pine` | OpenRobotics/Pine Tree | Louise Poubel | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Pine%20Tree/6> |
| `horus_tree_oak` | OpenRobotics/Oak tree | Louise Poubel | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Oak%20tree/7> |
| `horus_rock_a` | OpenRobotics/Falling Rock 1 | Addisu Taddese, Cole Biesemeyer | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Falling%20Rock%201/6> |
| `horus_lamp_post` | OpenRobotics/Lamp Post | Cole Biesemeyer, Nate Koenig | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Lamp%20Post/3> |
| `horus_vehicle_bus` | OpenRobotics/Bus | Nate Koenig | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Bus/3> |
| `horus_vehicle_hatchback` | OpenRobotics/Hatchback | Nate Koenig | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Hatchback/3> |
| `horus_vehicle_hatchback_red` | OpenRobotics/Hatchback red | Nate Koenig | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Hatchback%20red/4> |
| `horus_vehicle_hatchback_blue` | OpenRobotics/Hatchback blue | Nate Koenig | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Hatchback%20blue/5> |
| `horus_vehicle_sedan` | OpenRobotics/Prius Hybrid | Ian Chen | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Prius%20Hybrid/3> |
| `horus_vehicle_suv` | OpenRobotics/SUV | Nate Koenig | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/SUV/4> |
| `horus_vehicle_pickup` | OpenRobotics/Pickup | Nate Koenig | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Pickup/4> |
| `horus_vehicle_box_truck` | OpenRobotics/TruckBox | Roselle Carmen | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/TruckBox/2> |
| `horus_vehicle_delivery_truck` | OpenRobotics/TruckDelivery | Roselle Carmen | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/TruckDelivery/2> |
| `horus_person_sitting_a` | OpenRobotics/Rescue Randy Sitting | Ian Chen, Cole Biesemeyer | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/Rescue%20Randy%20Sitting/8> |
| `horus_bench_a` | OpenRobotics/FoodCourtBenchShort | Wan Yi Seow | CC0 1.0 | <https://fuel.gazebosim.org/1.0/OpenRobotics/models/FoodCourtBenchShort/2> |

`horus_rock_a` ("Falling Rock 1" by Addisu Taddese and Cole Biesemeyer, Open Robotics) is licensed CC BY 4.0: the local copy keeps the albedo and normal-map textures (the roughness map is not used) and the upstream decimated collider hull, and the standalone `model.sdf` adds a box proxy. `horus_vehicle_bus` reuses the SUV's wheel texture from `horus_vehicle_suv` (same author and licence), because the Bus material library names it.

## Outdoor missions

The hand-written outdoor mission set is S9–S13 on the canonical worlds; procedurally generated worlds get generated missions from the campaign harness (see [Procedural worlds and campaigns](#procedural-worlds-and-campaigns)). Start only one world at a time; in one terminal run `./sim/run.sh start horus_forest.sdf` or `./sim/run.sh start horus_urban.sdf`. With the world running, start the MCP server in the simulation container using the matching flight config (the `HORUS_MCP_TOKEN` must already be set):

```bash
docker exec --interactive --workdir /horus \
  --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" \
  --env HORUS_CONFIG=/horus/config/horus_forest.yaml \
  horus-sim /opt/horus-venv/bin/horus-mcp
```

For each run command below, use a second terminal with the same token. The runner executes inside the container so `civilian_life.py` has Gazebo bindings; `/horus` is its working directory, and scenario arguments remain repository-root-relative.

`sim/scripts/start_sim.sh` already starts the scenario's `ambient` group and writes its ground-truth trace under the active run logs. Do not start another ambient driver in a mission; S10 and S12 launch only their dedicated `follow_hiker` and `follow_jogger` groups.

| Mission | World / config | Run |
| --- | --- | --- |
| S9 — find the missing hiker | `horus_forest.sdf` / `config/horus_forest.yaml` | `docker exec --workdir /horus --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" horus-sim /opt/horus-venv/bin/horus --server-url http://127.0.0.1:8000/mcp --config config/horus_forest.yaml --mission missions/S9_forest_find_missing_hiker.yaml` |
| S10 — follow hiker through tree occlusion | `horus_forest.sdf` / `config/horus_forest.yaml` | `docker exec --workdir /horus --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" horus-sim /opt/horus-venv/bin/horus --server-url http://127.0.0.1:8000/mcp --config config/horus_forest.yaml --mission missions/S10_forest_follow_hiker.yaml` |
| S11 — find the red shirt in the plaza | `horus_urban.sdf` / `config/horus_urban.yaml` | `docker exec --workdir /horus --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" horus-sim /opt/horus-venv/bin/horus --server-url http://127.0.0.1:8000/mcp --config config/horus_urban.yaml --mission missions/S11_urban_find_red_shirt.yaml` |
| S12 — follow jogger through street occlusion | `horus_urban.sdf` / `config/horus_urban.yaml` | `docker exec --workdir /horus --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" horus-sim /opt/horus-venv/bin/horus --server-url http://127.0.0.1:8000/mcp --config config/horus_urban.yaml --mission missions/S12_urban_follow_jogger.yaml` |
| S13 — urban medical emergency | `horus_urban.sdf` / `config/horus_urban.yaml` | `docker exec --workdir /horus --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" horus-sim /opt/horus-venv/bin/horus --server-url http://127.0.0.1:8000/mcp --config config/horus_urban.yaml --mission missions/S13_urban_medical_emergency.yaml` |

S9 flies a listed trail and clearing route under the canopy (28 m transits, 5–8 m search legs) to find the missing hiker. S10 stages at 8 m, follows the green-shirt hiker at an 8.5 m slant range (a trail-edge bush occludes the camera for 2.6 s) and asserts a 1–8 s re-acquisition. S11 handles ambiguity in the plaza crowd at 10 m; S12 follows the red-shirt jogger at 10 m and asserts a 3–15 s re-acquisition under the bus-stop canopy; S13 finds the collapsed person, orbits at 8 m after a 24 m transit, holds for 20 s, then performs the authorized return.

Both outdoor profiles run a low-rate person pass through the grounder (`perception.vocabulary`: `person`, `person lying on the ground`, `person sitting on the ground`, queried at `open_vocab_hz`): yolov8n at its fixed 640×640 input proposes no person at all for most overhead lying people, while Grounding DINO tiny at 0.30 box score recovers 6/11 lying and 17/23 sitting people on the campaign frames. A seed is named after the matched vocabulary term (the raw span stays on the candidate row) and carries no detector person evidence, so a grounder-only target still refuses a person-height range and needs `target_size_m`; the pass deduplicates against detector tracks and ages out unconfirmed seeds, and with `grounder_url` unset or the service down the pipeline stays detector-only.

`gt_trace.py` keeps its indoor defaults and accepts `--world horus_forest` or `--world horus_urban`; use `--models` to list the outdoor model names to record, or `--topic` to override `/world/<world>/pose/info`. `s6_analyze.py` likewise keeps the S6 defaults and accepts `--target-model`, `--distractor-models`, and `--success-distance-m`. For example, a trace named `runs/urban_groundtruth.jsonl` can be analyzed with prefix `runs/urban` and `--target-model civ_red_shirt_01`.

## Procedural worlds and campaigns

`python3 -m sim.worldgen --procedural --seed N [--kind forest|urban] [--stratum baseline|chaotic]` samples a complete world into `sim/generated/horus_<kind>_pN/`; the same command without `--procedural` regenerates the canonical `sim/worlds/horus_{forest,urban}.sdf`, their scenarios and the generated person models. The generator varies the layout (forest: trail, creek, clearing, camp, hiker/ranger/family routes, follow target and occluder bush, tree field, rocks and logs, terrain grid; urban: road grid, buildings, plaza with the landing pad, park, crosswalks, sidewalk routes, circulating traffic and parked cars, lamps, wires and shelters), the sampled environment (sun, sky, ground and foliage tints), the dressing stratum (`baseline`, or `chaotic` with harsher light, more animals and clutter and a denser disturbance schedule), per-world civilian appearances (body, skin, hair, clothing textures and accessories, each with its classifier labels), vehicles, fauna (dogs with walkers, deer and crows in the forest meadow, pigeons and cats in the urban plaza and park) and sim-time disturbance events (hesitate, speed change, breakdown, startle, reverse) that stay inside each agent's declared speed and acceleration limits.

A layout is accepted only after it passes the real-geometry report in `sim/worldgen/validation.py` — the same report the test suite runs on the canonical worlds: a clear home column; the clearing scan raster at 10 m clear by 2 m (forest); every actor's footprint clear of static geometry over its whole executed path (standing walkers ≥0.1 m, vehicles ≥0.3 m over their filleted lanes, animals ≥0.05 m); a follow plan with 2.5–4.0 s occlusion, ≥2 m lane and ≥1.5 m column clearance, the target framed from the stage and uniquely identifiable by the shirt-colour class the detector reports; a safe line-of-sight vantage with a clear descent for each person of interest. The sampler additionally keeps every route and wander area inside the 60 m geofence. Rejected candidates are re-sampled from the same seed; if a seed cannot be sampled the CLI exits non-zero naming the seed and the top rejection reasons.

`./sim/run.sh start --procedural [--seed N] [--kind forest|urban] [--stratum baseline|chaotic]` generates and starts one such world (default kind `forest`); without `--seed` a random 5-digit seed is chosen and printed for reproduction. `python3 sim/scripts/procedural_campaign.py` runs the generated missions end to end — by default seeds 1–3 for both kinds — with `--random N` for N distinct random seeds (printed), `--seeds ...` for explicit ones, and `--kinds`, `--tests` and `--vantages` to select the sweep; results land in `runs/campaign/<UTC timestamp>/` (`summary.json`, `summary.md`, and one directory per world holding `episode_result.json`, logs and traces). `scripts/koko/procedural_campaign.sh` runs the same campaign detached on the Koko host, with `--status` and `--fetch` to check and retrieve results. See `sim/README.md` for the generator, layout and scenario details.

Generated worlds fly the procedural flight profile (`config/horus_procedural_forest.yaml`, `config/horus_procedural_urban.yaml`, `config/ardupilot/horus_sitl_procedural.parm`): a 1.2 km map with a ±585 m executor geofence polygon, `max_distance_m: 850`, and a 360 s command TTL sized for a map-crossing goto in wall-clock time (an 850 m goto at the 8 m/s cruise is 106 s of simulation time, ~304 s of wall time at the RTF 0.35 Koko sustains on the 1.2 km worlds, plus acceleration, yaw and arrival margin). The horizontal speed limit is altitude-gated: 8 m/s (`cruise_speed_m_s`) while both the current altitude above home and the commanded target altitude are at/above `cruise_min_altitude_m` (26 m forest, above the 23 m tree cap; 38 m urban, above the 34 m building cap), and the proven 3 m/s `max_speed_m_s` everywhere below. `run.sh start --procedural`, `demo.sh --procedural` (local and `--remote`) and the campaign resolve these files from the generated world; canonical S9–S13 worlds keep `config/horus_{forest,urban}.yaml` and `horus_sitl_outdoor.parm`.
