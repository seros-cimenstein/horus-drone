# HORUS

HORUS lets an AI pilot fly an ArduPilot drone one base move at a time. You talk to the AI pilot through the terminal; it sees through HORUS's camera vision and commands the drone through HORUS's drone remote, and HORUS's flight moves turn each command into closed-loop motion over the autopilot link. HORUS has no limit layer of its own: ArduPilot holds the flight limits (its fence and altitude ceiling), proximity avoidance, failsafes and RC override. The test world is Gazebo or GTA V, one per run, never both. The core has no offline mission packages, specialist fine-tuning, thermal module, or DroneCAN integration.

## Architecture

- `horus/repl/` is the terminal: free text goes to the AI pilot in the background, and emergency commands skip the AI (see step 5 of [Connect to the simulation](#connect-to-the-simulation)).
- `horus/commander/` is the AI pilot. It maintains one Claude Code or Anthropic/OpenAI API conversation per mission, streams executor/perception events into it, and bypasses the model for authenticated emergency controls. An emergency interrupts the running model turn and drops operator requests and emergency notices that were queued before it and not yet sent to the model. An API-provider turn that is interrupted or fails (for example during a tool call) is removed from the conversation as a whole, so the next turn never carries an unanswered tool call; cancelling a turn still waiting for its predecessor leaves the predecessor's history intact.
- `horus/mcp_server/` is the drone remote: state, flight, frame/candidate, pointing, detection-mode, scene (motion and region-watch), map, task and event-wait tools over the Python MCP SDK's streamable HTTP transport. Every HTTP request requires a bearer token; detector labels, map names and kinds, and ArduPilot status text are returned as explicitly quoted untrusted values (`horus/untrusted.py`).
  - Flight: `take_off`, `goto`, `move`, `turn`, `look`, `hold`, `rth`, `land`, plus `get_state`, `get_map` (pose, proximity readings and tracked candidates) and `get_link_status`.
  - Camera vision: `get_frame`, `get_raw_frame`, `list_candidates`, `point`, `set_vocabulary`, `set_mode` (`active`/`paused` detection), `get_candidate_motion`, `watch_region`, `list_region_watches`, `remove_region_watch`.
  - Tasks and events: `get_task_status`, `wait_for_task`, `cancel`, `wait_events` and `wait_for_event(kinds, after_id=None, timeout_s=20)`. `wait_events` streams every executor and perception transition; the commander's event pump owns it, so the model is not offered it. `wait_for_event` blocks until an event of one of the listed kinds arrives, e.g. `["landed"]` after `rth`/`land`. Without `after_id` it counts only events published after the call starts, so a repeated wait must pass the previous result's `last_id` or it misses what arrived in between. It caps each call at 25 s and below 80 % of `link_loss_rth_delay_s`, so back-to-back waits keep a hold supervised. The alerts that wake the AI pilot are task, link, emergency, landing and pilot-override transitions, `object_detected` for `set_vocabulary` terms, and the region-watch events.
  - Maps (`horus/maps/`, `horus/mcp_server/maps.py`): `find_place(query, limit=5)` ranks named places and named areas (exact name, prefix, substring, all words), nearest first among equals, with NED position, distance and bearing from the vehicle (an area around the vehicle is at distance 0, `vehicle_inside`); `get_area_map(north_m, east_m, radius_m=100)` returns places, ways clipped to the circle, and the areas, walkable polygons and obstacles touching it (radius at most 300 m; polygons cut to the circle's bounding square and simplified to radius/150, at least 0.3 m, so 100 m around Legion Square is about 23 KiB). Both read the provider selected by the `map:` config section (`provider: none`, `gta` or `osm` with `data_path`, a `horus-map/1` JSON in local NED written by the map fetchers under the gitignored `data/maps/`). `config/horus_gta.yaml` uses `gta`: run `scripts/maps/fetch_gta_map.py` once per machine (`--out` writes the map elsewhere). It downloads DurtyFree's `gta-v-data-dumps` at a pinned commit (each file checked against its SHA-256), crops it to 600 m around `HorusGta.ini [origin]` and converts it to NED (`horus/maps/gta.py`). The result holds the road graph as polylines per street, junctions, pedestrian crossings, traffic-light nodes, bus stops, ATMs, seats and districts. `scripts/maps/fetch_gta_walkable.py` (run once in a throwaway venv with `msgpack lz4 cryptography pyyaml`) adds the walkable layer at `map.walkable_path`. It takes GTA's ped navmesh from DurtyFree's mega.nz dump (pinned SHA-256) and keeps the footpath and road polygons within 600 m, excluding underground, interior and water. `provider: osm` reads an OpenStreetMap Overpass extract that `scripts/maps/fetch_osm.py` stores under `data/maps/osm/` with its URL, time and SHA-256 (one cached request with an identifying User-Agent, by default a 2 km box around the simulators' home), converted at start-up about `map.origin_latitude_deg`/`map.origin_longitude_deg` or the flight's global origin (`horus/geo.py`, `horus/maps/osm.py`; © OpenStreetMap contributors, ODbL). In every world `get_area_map` also returns `own_map` with the `marks` set by `mark_place(name, north_m, east_m, note)`; marks persist as `marks.json` in the run directory, survive an MCP server restart in the same run, and are found by `find_place` (kind `mark`). The GTA dumps carry no licence and come from Rockstar's game, so they are for local research only and never committed.
- `horus/executor/` is the flight moves: it runs one base move at a time, `take_off`, `goto` (NED pose), `move`, `turn`, `look`, and the emergency `hold`, `rth` (return home and land) and `land`. No skill chases a target or sweeps an area by itself; the AI pilot chooses each next move from what the camera shows. A move returns a task ID with its requested and executed targets and terminal status. HORUS checks only its inputs, not flight limits: numbers must be finite, `take_off` needs `altitude_m > 0`, and a `goto` or `move` whose resolved altitude is at or below home is rejected. HORUS does not clamp a target to any fence; ArduPilot stops the vehicle at its fence (or lands or returns it, per `FENCE_ACTION`).
  - `move(forward_m, right_m, up_m)` flies relative to the nose. The target is fixed from the pose and heading when the task starts (not when it is queued) and flown by the same velocity controller as `goto`; the heading does not change. `requested_target` holds the offsets, `from_heading_deg` and the NED `pose` they give; `executed_target` holds `from_heading_deg` and that `pose`.
  - `goto` and `move` fly at the fixed controller speeds `executor.speed_m_s` and `executor.vertical_speed_m_s`; a profile may add an altitude-gated cruise (`executor.cruise_speed_m_s` while both the current and the target altitude are at or above `executor.cruise_min_altitude_m`). These are the controller's speeds, not safety limits: ArduPilot's waypoint speed parameters do not apply to Guided velocity setpoints.
  - `turn(heading_deg | by_deg, rate_deg_s)` turns in place: to an absolute heading the shorter way, or by a signed sweep (positive clockwise, at most ±360° so `by_deg=360` looks all around). It hovers at zero velocity while the yaw setpoint sweeps at `rate_deg_s` (default `executor.turn_rate_deg_s`, 30°/s; a given rate is flown as asked, and ArduPilot's yaw-rate limits bound the real turn), timed by autopilot time when `SYSTEM_TIME` is fresh so the rate holds at any sim RTF, and succeeds once the final heading is within 5°. Rotation stays with ArduPilot's yaw controller on velocity setpoints; HORUS never sends attitude, rate or position targets, which bypass the avoidance.
  - `look(pitch_deg)` tilts the gimbal camera (`GIMBAL_DEVICE_SET_ATTITUDE` to the gimbal, earth-frame pitch within `gimbal.pitch_min_deg`…`pitch_max_deg`, default -90…45°: 0 level, -90 straight down) at once. The pitch holds through later moves and take-off until changed (`get_state` reports `camera_pitch_deg`, and the measured attitude under `gimbal`; it starts at `executor.default_gimbal_pitch_deg`). RTH/LAND within 3 m of home point it straight down for the AprilTag and give the commanded pitch back when precision landing ends, and `look` is refused while they own it. `look` is refused without a configured gimbal or while it reports no attitude (a fixed camera), and under a pilot override. There is no pan: the gimbal stays aligned with the nose, because HORUS's image geometry assumes camera yaw = vehicle heading; `turn` the vehicle to look sideways.
  - Every finished task carries a short `reason`: `succeeded`; `clamped` (succeeded, but a `turn` sweep was limited to one revolution); `expired` (its TTL ran out); `failed`; `cancelled`.
  - TTL expiry, cancellation, and move completion stop motion in Guided with zero velocity; `hold`, `rth`, and `land` preempt queued and running work. An emergency takes effect even while a move step is mid-flight: the step's own completion, failure, TTL, or cancellation path never sends a later Guided, hold, or velocity command after the emergency, and the preempted task stays `cancelled`. An emergency waits at most for one guarded flight-command operation already in progress (for example a velocity setpoint, or a hold's Guided switch plus zero-velocity setpoint), never for perception or model work. Translations require pose telemetry, and turns a heading sample, no older than `executor.telemetry_max_age_s` (default 1.5 s wall). The flight link requests `ATTITUDE`, `LOCAL_POSITION_NED`, `GLOBAL_POSITION_INT` and `SYSTEM_TIME` at 20 Hz of autopilot time (the rest at 5 Hz) so that wall-clock gaps stay well under the guard down to a simulation real-time factor of ~0.1.

  HORUS leaves out the rest of ArduPilot's Guided commands on purpose: attitude/rate and position targets (no simple proximity avoidance), flight termination, parachute, fence enable and reboot.
- `horus/perception/` is the camera vision: it reads the UDP H.264 camera, GTA V frames (`gta://`) or a video source, tracks detector and CSRT-seeded (`open_vocab`/`point`) objects with appearance embeddings, keeps a timestamped frame/pose ring buffer, emits a numbered set-of-mark JPEG and candidate table, and uses the loopback grounder service for continuous vocabulary detection and point-by-description. The numbers are tracker IDs that stay with an object while it is in view; HORUS does not search for an object that has left the view. Detector tracks associate by box overlap after compensating camera yaw. When an image jump the yaw compensation does not cover (vehicle pitch or translation) leaves a detection without overlap, it keeps the ID of a nearby track of the same class that was not seen in that frame, under all of these conditions:
  - the detection's centre is within one box diagonal of the track's predicted box;
  - its height is within 2×;
  - its crop is usable;
  - its appearance matches the track's reference views at `revalidation_similarity_threshold`.

  Otherwise it starts a new ID. `horus/perception/scene.py` is a pure standard-library scene monitor fed by every processed frame: per-track image-motion history and fixed-camera region watches (see [Scene tools](#scene-tools-image-motion-and-region-watches)).
- `horus/flight/` is the autopilot link: a threaded `pymavlink` connection to ArduPilot, timestamped telemetry cache, heartbeat age, battery/proximity/status-text observations, vehicle roll/pitch (`ATTITUDE`) each stamped with its own monotonic receive time, and explicit Guided/arm/takeoff/LAND/RTL/yaw/velocity operations. ArduPilot does flight only; it has no camera or gimbal role.
- `horus/gimbal/` drives the camera gimbal directly over the MAVLink gimbal-device protocol (Gimbal Protocol v2 device messages) on the gimbal's own endpoint (`gimbal.connection`): HORUS is the gimbal manager, sends `GIMBAL_DEVICE_SET_ATTITUDE` (pitch relative to the horizon, yaw relative to the nose; re-sent every 0.5 s) and reads the measured `GIMBAL_DEVICE_ATTITUDE_STATUS` from the configured device system/component only. Without `gimbal.connection` the camera is fixed. All HORUS MAVLink links load pymavlink's MAVLink 2 dialect (`horus/mavlink.py`), which the gimbal-device messages need. In simulation the gimbal device is `horus-gz-gimbal` (Gazebo) or `horus-gta-bridge` (GTA pose mode); both send to `udpin:127.0.0.1:14600`.
- `horus/tof_bridge/` forwards the ToF payload's ranges as MAVLink `DISTANCE_SENSOR` messages, from Gazebo LaserScan topics (`cli.py`) or from HorusGta.dll's range packets (`gta.py`), both through `mavlink_out.py`. `config/ardupilot/common.parm` enables MAVLink proximity and the proximity avoidance bit.
- The test world is `sim/` (Gazebo) or `gta/` (GTA V), one per run: `sim/run.sh start` refuses a second world, whichever it is, and a config from the other world (see [`sim/README.md`](sim/README.md)).

**Safety-critical control decision:** ArduCopter's simple proximity avoidance is intended for Guided **velocity** targets, not Guided position targets. All executor translations, including `goto`, therefore use a local pose-feedback controller and velocity-only `SET_POSITION_TARGET_LOCAL_NED` messages at 10–20 Hz. The flight limits are ArduPilot's: each world's parameter files set its fence (`config/ardupilot/indoor.parm`: a 2.2 m altitude fence that lands; `outdoor.parm` with `canonical_fence.parm` or `procedural_fence.parm`: a home-centred circle and a 55 m ceiling that return home; `gta.parm`: a 40 m circle, a 48 m ceiling and a 6 m floor), and proximity avoidance (`common.parm`) holds the vehicle off obstacles. `hold` remains in Guided and continually commands zero velocity; if the AI goes quiet, the drone holds still, then flies home: once no MCP call (including the commander's event long-poll) has reached HORUS for `executor.link_loss_rth_delay_s`, it treats the supervisory link as lost and escalates to RTH/RTL. It never relies on LOITER/ALT_HOLD without an RC pilot.

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

HORUS exposes its flight moves, perception tools, the `horus_commander` MCP prompt, the `horus://mission/brief` resource, and camera JPEG frames over authenticated streamable HTTP: `get_frame`/`list_candidates` return a ≤640 px set-of-mark frame with its candidate table, and `get_raw_frame(frame_id=None)` returns the same camera frame at source resolution with nothing drawn on it (no boxes, IDs or labels; its pixels are the source-image geometry `point(frame_id, box)` takes). No provider API key is needed for the default Claude Code commander.

In the server terminal, set these values and start the authenticated MCP service with `horus-mcp`. Export the same values in separate client/REPL terminals so server tool calls and Claude's streamed events share one JSONL file:

```bash
export HORUS_MCP_URL=http://127.0.0.1:8000/mcp
export HORUS_MCP_TOKEN='replace-with-a-long-random-secret'
export HORUS_RUN_DIR="$PWD/runs/demo"
export HORUS_EVENT_LOG="$HORUS_RUN_DIR/events.jsonl"
horus-mcp
```

In a terminal with Claude Code installed and authenticated, connect an interactive Claude Code session to HORUS (the current CLI flag syntax is documented in the [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) and [MCP guide](https://code.claude.com/docs/en/mcp)):

```bash
claude mcp add --transport http horus "$HORUS_MCP_URL" --header "Authorization: Bearer $HORUS_MCP_TOKEN"
```

For a terminal-only agent, use the real MCP client. `get_frame`, `get_raw_frame` and `list_candidates` save their JPEG under `runs/manager/` and print the path for opening:

```bash
horus-mcp-call list-tools
horus-mcp-call get-prompt horus_commander
horus-mcp-call get_state
horus-mcp-call get_frame
horus-mcp-call list_candidates
horus-mcp-call get_raw_frame
```

The `horus` REPL's free-text requests use `claude -p` with a generated config containing only the HORUS MCP server, strict MCP config, an allowlist for `mcp__horus__*`, and no built-in shell/file/web tools. The REPL obtains the safety instructions from `horus_commander`; Claude's JSON stream and server-side tool calls append to `$HORUS_EVENT_LOG`. Its emergencies (`/hold`, `/rth`, `/home`, `/land` and short imperatives such as `stop` or `go home`) skip the model. Each one appends an `emergency` record with `source: operator` and `status: executed` or `error` to the same log.

```bash
horus --server-url "$HORUS_MCP_URL"
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

1. Set `GZ_SIM_RESOURCE_PATH` to include `/horus/sim/worlds`, `/horus/sim/models`, and `/opt/ardupilot_gazebo/models`; `sim/worlds/horus_indoor.sdf` includes the local `horus_iris` wrapper, which nests `horus_iris_with_gimbal` (upstream `iris_with_gimbal` without the ArduPilotPlugin gimbal channels) under the name `iris_with_gimbal`. `sim/scripts/start_sim.sh` runs the gimbal device `python -m horus.gimbal.gazebo` (`horus-gz-gimbal`) beside every HORUS world: it holds the camera level against the airframe's lean from HORUS's setpoints and reports the measured attitude (`sim/README.md`).
2. Review and load the core proximity settings into SITL (or set the same parameters through MAVProxy); `config/ardupilot/common.parm` carries them for every world:

   ```text
   PRX1_TYPE 2
   AVOID_ENABLE 3
   AVOID_MARGIN 1.0
   RNGFND1_TYPE 10
   RNGFND1_ORIENT 25
   RNGFND1_MIN 0.05
   RNGFND1_MAX 4.0
   ```

   Verify proximity sensor orientation and Guided velocity avoidance in simulation before moving beyond a prop-free test. ArduPilot's fence is the flight limit: each world profile loads its own `FENCE_*` fragment (`indoor.parm`, `outdoor.parm` with `canonical_fence.parm` or `procedural_fence.parm`, or `gta.parm`). Additional EKF, optical-flow, rangefinder, fence, and failsafe settings depend on the world/vehicle configuration and must be checked against the selected ArduPilot version.
3. Edit `config/horus.yaml` for the camera source, model names, controller speeds, and server port. The defaults use MAVLink TCP `tcp:127.0.0.1:5760` and the OpenCV GStreamer source `udp://0.0.0.0:5600`. `perception.source: gta://127.0.0.1:5610` instead reads frames copied out of GTA V Enhanced's renderer by `horus_capture.asi`; see [`gta/capture/README.md`](gta/capture/README.md).

   **GTA V renderer mode.** SITL keeps flying the vehicle and GTA V only renders the drone camera. `horus-gta-bridge` reads the vehicle pose from SITL serial1 `tcp:127.0.0.1:5762`, acts as HORUS's gimbal device (an ideal level-holding gimbal at HORUS's setpoint), and sends the camera pose (vehicle position plus that gimbal attitude) over UDP to `HorusGta.dll`, a ScriptHookVDotNet script that puts GTA's scripted camera at that pose every frame; `horus_capture.asi` then serves those frames on `gta://`. The scripts run on the HORUS Script Hook, our own Script Hook V implementation for GTA V Enhanced ([`gta/hook/README.md`](gta/hook/README.md)). See [`gta/horus_mod/README.md`](gta/horus_mod/README.md) for the build, install, origin placement and pose packet.

4. Set a strong server bearer token and start the loopback-only authenticated MCP server:

   ```bash
   export HORUS_MCP_TOKEN='replace-with-a-long-random-secret'
   horus-mcp
   ```

The camera opens asynchronously with retry/backoff. `get_state` reports whether it is connecting, streaming, or stalled and includes the last-frame age. For UDP/RTP input, the GStreamer pipeline and camera watchdog enforce timeouts; OpenCV timeout properties are not passed to the GStreamer backend. Flight, emergency, and state tools remain available without video; set `perception.enabled: false` in `config/horus.yaml` or start with `horus-mcp --no-perception` for a flight-only run. A flight-only server needs only the `server` extra: NumPy, OpenCV, and ONNX Runtime are imported only when perception starts, so `horus-mcp --help` and `horus-mcp --no-perception` run without them. A real flight link is still required to serve.

Tracks are embedded lazily, at most once per track per frame, and `point` revalidation shares that embedding. Validation uses the full `revalidation_similarity_threshold` against the best of a live, continuously associated track's stored reference views; there is no grace window. A track stores up to `perception.reference_views` confirmed views in total (its creation view plus extras), so a marginal dip against the creation view does not lose a correctly tracked target. A view is admitted only from a frame that passed the full threshold against the creation view, with the class gate, a crop of at least `perception.reference_view_min_width_px` x `reference_view_min_height_px` px (default 12x30; smaller crops are embedding noise), and a `reference_view_dedupe_cos` (0.98) distance from every stored view; the oldest extra view is evicted first and the creation view is never evicted. A track that leaves the view and expires is gone: HORUS does not re-identify it later; the AI pilot `point`s at it again when it sees it. `list_candidates` rows report the track's current `reference_views` count. `point` with latency compensation re-identifies the old-frame reference by the current-frame appearance of the latest candidates (same lazy cache) before falling back to box geometry, so a look-alike that crossed into the old box does not take the ID.

#### Scene tools: image motion and region watches

All scene coordinates are fractions of the processed frame (`x` right, `y` down, 0–1) and everything is image-relative. Frame timestamps are capture-time `time.monotonic()`; each frame carries the camera state sampled at capture: local position, heading, vehicle roll/pitch (from MAVLink `ATTITUDE`), and gimbal pitch/roll/yaw (the gimbal's own `GIMBAL_DEVICE_ATTITUDE_STATUS`), each with the age of its own latest message on the monotonic clock, so UTC/NTP steps never change freshness. Camera state older than 1.5 s is unavailable, never assumed stationary. Zoom/field of view is not reported by the current telemetry and is listed as unmeasured, so a zoom change can move the image undetected. The full schema is in the docstrings of `horus/perception/scene.py` and the MCP tools.

- `get_candidate_motion(id)` fits the tracker ID's last ≤2 s of processed boxes: centre and size rates in frame fractions per second, a log-scale rate, the fit residual and a fit confidence, plus `camera_motion`. `camera_motion.status` is `moving` (measured vehicle/gimbal drift beyond tolerance: the rates include ego-motion), `within_tolerance` (sub-tolerance and unmeasured components listed in `unmeasured` can still contribute), or `unknown` (no fresh camera state). It is never a metric or ground speed. Without an estimate, `available` is false with `reason` `no_frames`, `unknown_track`, `stale_frames`, `not_visible`, or `insufficient_samples` (fewer than 3 samples or 0.25 s); gaps over 1 s, clock resets, and frame-size changes restart the history (`stream_discontinuity`).
- `watch_region(name, box, class_name=None, dwell_s=None, absence_s=1.0, ttl_s=None)` watches a normalized box on the latest fresh frame and anchors it to that frame's camera state. A target is inside when its box centre is inside; `class_name` matches any detector or open-vocabulary label case-insensitively. Targets already inside are initial occupants without an enter event. `region_enter`, `region_exit` (after `absence_s` counted from the first frame that no longer saw the target inside; `reason` `left_region` when seen outside, `disappeared` when the track vanished), and `region_dwell` (once, after `dwell_s` of continuous inside observation) go to `wait_events` with quoted labels. Camera drift beyond 0.5 m horizontal, 0.3 m vertical, 3° heading, 3° vehicle roll/pitch, or 2° gimbal pitch/roll/yaw, a stale camera state, or a frame-size change emits `region_watch_paused`; no transitions happen while paused. After 1 s steady on the reference view, or after a stream gap, `region_watch_resumed` re-adopts current occupancy without inventing enter/exit events. At most 16 watches; reusing a name replaces a watch silently; each watch's events are budgeted (burst 10, 2/s) with suppressed counts reported. `ttl_s` ends a watch with `region_watch_expired`; the perception watchdog enforces it even while the camera delivers no frames.
- `list_region_watches()` reports stream freshness and, per watch, `active`/`stale`/`paused` status, the camera reference, unmeasured camera components, occupants and timers; stale or paused occupancy is last-confirmed state. `remove_region_watch(watch_id)` errors for unknown IDs; after it returns the watch generates no new events (earlier events stay in the `wait_events` history).

The commander wakes the active mission for region events; enter/exit/dwell repeats of the same watch within 10 s are counted rather than sent, while pause, resume, and expiry always wake it. Watches never command flight.

Label filters (`watch_region`) compare tracker labels exactly (case-insensitive), with no synonym mapping. The default `models/yolov8n.onnx` detector emits only the 80 COCO labels listed in `horus/perception/detector.py` — including the animals bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, and giraffe — so a label it cannot emit, such as `deer`, never matches. Open-vocabulary seeds carry the grounder's label; using them with these filters needs a running grounder and `set_vocabulary`, and that combination has not been exercised.

5. In the REPL network namespace, use the same token to connect:

   ```bash
   horus --server-url http://127.0.0.1:8000/mcp
   ```

   The terminal accepts `/state`, `/goto NORTH_M EAST_M ALTITUDE_M`, `/take_off [ALTITUDE_M]`, the emergencies `/hold`, `/rth`, `/home` (return home) and `/land`, and `/quit`. The short plain-text emergency words skip the AI as well: `stop`, `halt`, `abort`, `hold`, `freeze` (hold), `land`, and `rth`, `return home`, `home`, `go home` (return home), optionally followed by `now`/`immediately` and punctuation, the same grammar as the commander server's `/task`. Other text is handled by the authenticated Claude Code MCP backend without an API key; a sentence that merely starts with an emergency word, such as `land next to the hiker`, is free text for the commander. Free-text requests are sent one at a time in the background, so the prompt stays available while the model works. The emergencies call the MCP emergency tool directly (through the commander's own session once it has started). They interrupt the running model turn and discard free text queued behind it; the model then receives an `[operator emergency]` notice. End of input waits for the running request to finish, while `/quit` cancels it. Direct REPL commands and emergency tools do not require a cloud provider key.
6. For every world that carries the ToF payload (`horus_indoor.sdf`, `horus_forest*`, `horus_urban*`), `start_sim.sh` automatically launches one ToF bridge after SITL opens TCP `127.0.0.1:5763`. To run or restart it manually on a running world:

   ```bash
   bash scripts/tof_bridges.sh --world horus_indoor --model-chain horus_iris/horus_sensor_payload
   ```

   The bridge sends all seven LaserScan topics over one MAVLink connection from system 254/component 196. Sensor IDs 0–5 use horizontal orientations `0, 1, 2, 4, 6, 7`; ID 6 feeds the downward rangefinder at orientation 25 (`PITCH_270`). The Gazebo lidars render at 10 Hz (20 Hz costs ~40% real-time factor on an integrated GPU); the bridge forwards each sensor at 10–20 Hz (default 20). It waits up to `HORUS_TOF_STARTUP_TIMEOUT` (default 60 s) for first scans, because the lockstep world only runs once a MAVLink client holds SITL port 5760. `config/ardupilot/common.parm`, loaded by every Gazebo world profile, configures `RNGFND1_TYPE 10` and `RNGFND1_ORIENT 25`.

   In GTA's JSON-physics mode (`sim/run.sh start --gta --json-physics`) the same payload is emulated in the game: HorusGta.dll casts the seven beams into the GTA world at 20 Hz of simulation time and sends each sweep over UDP 5612. `python -m horus.tof_bridge.gta`, started by `sim/scripts/start_gta_sitl.sh`, forwards each sweep with the same sensor IDs, orientations, 5–400 cm limits and clear-beam rule to SITL serial2 (`tcp:5763`). The GTA SITL then loads the proximity, avoidance and rangefinder lines of `config/ardupilot/common.parm` and leaves obstacle avoidance to them; its ArduPilot fence (`config/ardupilot/gta.parm`, both GTA modes) is a 40 m radius operating area, not an obstacle-free box (gta/horus_mod/README.md "Range sensors").

## GPS-free flight and precision landing

Use `config/ardupilot/nogps.parm` for the Copter-4.7.1 GPS-denied profile. It disables both simulated GPS1 and the GPS1 receiver backend, skips only the GPS-lock pre-arm check (`ARMING_SKIPCHK 8`), enables SITL optical flow, selects optical-flow horizontal velocity and barometer vertical position in EKF3, and configures the downward MAVLink rangefinder at orientation 25. All other ArduPilot pre-arm checks remain enabled. The profile retains the existing MAVLink proximity-avoidance settings; the indoor ToF bridge connects to SITL at TCP `127.0.0.1:5763`.

The flight limits are ArduPilot's fence, per world profile (`sim/scripts/world_profiles.py`): the indoor room has an altitude-only fence (`config/ardupilot/indoor.parm`: `FENCE_TYPE 1`, `FENCE_ALT_MAX 2.2` below the 2.5 m walls, `FENCE_ACTION 2` lands on a breach; `FENCE_MARGIN 0.2`, because `AVOID_ENABLE`'s fence bit stops a climb that far below the ceiling), and ToF proximity avoidance holds the walls: `AVOID_MARGIN 1.0` (`common.parm`; the ArduPilot default is 2 m). The outdoor worlds add a home-centred circle (`outdoor.parm`: `FENCE_TYPE 3`, `FENCE_ALT_MAX 55`, `FENCE_ACTION 1` returns home, `FENCE_MARGIN 5`; `FENCE_RADIUS` 95 m in `canonical_fence.parm`, 870 m in `procedural_fence.parm`). GTA's `gta.parm` is a 40 m circle with a 48 m ceiling and a 6 m floor (`FENCE_TYPE 11`, `FENCE_ALT_MIN 6`) above the kiosk roof and low street furniture; Copter arms the floor only after the vehicle first climbs above it and drops it when LAND or RTL starts. HORUS sends targets unclamped; a target beyond the fence is ArduPilot's to stop.

Set `flight.gps_free_mode: true` in `config/horus.yaml` to send the configured WGS-84 `global_origin_latitude_deg`, `global_origin_longitude_deg`, and `global_origin_altitude_m` with `SET_GPS_GLOBAL_ORIGIN` on MAVLink connect. This sets the EKF global origin only; it does not fabricate local-position telemetry. `config/horus_nogps.yaml` is the indoor profile for this mode (GPS-free origin, perception on for AprilTag precision landing; the altitude fence is `indoor.parm`'s); pair it with `nogps.parm`.

During `rth` or `land`, precision landing waits until the vehicle is within 3 m of local home, points the gimbal down, and requires a fresh tag36h11 id-0 detection before switching to GUIDED centering. Horizontal correction is proportional to the tag's image offset (full 0.35 m/s at a half-frame offset, zero inside a 5% deadband); descent at 0.15 m/s runs while the tag is within a 15% cone, so the vehicle centres and descends together. Below 0.6 m the tag overflows the camera and the centred vehicle is handed to vertical LAND. Before visual acquisition, RTL/LAND remains in control; after acquisition, tag loss commands a zero-velocity hold. A heading sample older than `executor.telemetry_max_age_s` blocks acquisition and, once acquired, also commands the zero-velocity hold, because the tag offset is rotated into NED by the heading. Because GUIDED velocity setpoints mask the autopilot's own precision-landing failsafe, precision control hands the descent to LAND — the same hand-off as the 0.6 m hand-off — whenever it stops making progress or loses control: no descent progress (measured against `executor.precision_landing_min_descent_m_s` on the autopilot clock when it is fresh, over `executor.precision_landing_stall_s` of wall time, so a legitimate descent at low real-time factor is not misread) or a stale pose / detector / gimbal error after acquisition, both reported via `precision_landing_stalled` and `get_state`'s `last_fallback`. An armed GUIDED hover above the pad is never left as the terminal state. The tag detector only sees the pad on a fraction of frames at range, so the last validated centring/descent setpoint stays active for `executor.precision_landing_target_grace_s` (default 1 s) after the last detection instead of dropping to zero on every miss — that pulsing (0.15 m/s alternating with 0) is averaged by the autopilot into a near-hover. Past the grace, a lost tag returns to the zero-velocity hold. Each second of tag-controlled flight logs the tag pixel error, gimbal pitch, commanded NED velocity and altitude. The gimbal is levelled again after the landing and at every take-off. `get_state` reports `precision_landing` counters (detections, acquisitions, completed landings, last fallback). The Gazebo gimbal device maps pitch to the model's tilt joint (-135…45°) itself, so -90° is nadir. The pad's tag is built from geometry in the world file (Gazebo renders the PNG texture override black).

## Commander-link latency emulation

`tc netem` must run where commander↔MCP traffic passes—for example, in the dedicated commander container or on its veth—not on the MAVLink interface. The helper shapes egress and redirects ingress through an IFB so both directions receive the same delay/loss:

```bash
sudo bash scripts/netem.sh up veth-commander 500ms 2%
sudo bash scripts/netem.sh down veth-commander
```

Use a dedicated network interface/namespace: the helper shapes all traffic on the selected interface. The MAVLink path must remain unshaped so this experiment measures only the delayed supervisory link.

## Configuration and tests

`config/horus.yaml` is parsed strictly; unknown keys are rejected.
`perception.ignore_regions` configures normalized `[x1, y1, x2, y2]` image boxes; detector proposals with at least 50% of their area in any box are discarded before tracking.

Run the project test suite with:

```bash
pytest
```

The suite covers executor state/TTL and input checks, emergencies that arrive during a running step, stale pose/heading refusal, tracker reference views and set-of-mark rendering, optical-flow/rangefinder bridge behavior, MCP authentication and box-point configuration, REPL emergency routing during a running commander turn, the one-world guard and world/config checks of `sim/run.sh`, GPS-free origin/SITL-clock telemetry, the gimbal-device link (setpoint and status frames, device filtering), the simulated gimbal's stabilisation, and rendered AprilTag 36h11 id-0 precision landing. Full flight safety and performance acceptance still requires SITL/Gazebo scenarios.

## Simulation model attributions

`person_standing` and its red, blue, and green variants (`person_red`, `person_blue`, `person_green`) adapt OpenRobotics' [Standing person](https://fuel.gazebosim.org/1.0/OpenRobotics/models/Standing%20person) from Gazebo Fuel, licensed CC0 1.0. They share one `standing.dae` (in `person_blue/meshes`, pointing its textures at `person_standing`); each variant's `model.sdf` overrides only the top's material with its recolored texture. Those texture files and material names are unique per variant because Gazebo's Ogre2 renderer caches textures by file name, so identically named textures render as whichever colour loads first. `horus_indoor.sdf` uses three point lights.

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

## Outdoor worlds

Start only one world at a time; in one terminal run `./sim/run.sh start horus_forest.sdf` or `./sim/run.sh start horus_urban.sdf`. With the world running, start the MCP server in the simulation container using the matching flight config (the `HORUS_MCP_TOKEN` must already be set):

```bash
docker exec --interactive --workdir /horus \
  --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" \
  --env HORUS_CONFIG=/horus/config/horus_forest.yaml \
  horus-sim /opt/horus-venv/bin/horus-mcp
```

Connect the REPL from a second terminal with the same token, e.g. `docker exec --interactive --tty --workdir /horus --env "HORUS_MCP_TOKEN=${HORUS_MCP_TOKEN}" horus-sim /opt/horus-venv/bin/horus --server-url http://127.0.0.1:8000/mcp --config config/horus_forest.yaml`. `sim/scripts/start_sim.sh` already starts the scenario's `ambient` group of civilians and writes its ground-truth trace under the active run logs.

Both outdoor profiles run a low-rate person pass through the grounder (`perception.vocabulary`: `person`, `person lying on the ground`, `person sitting on the ground`, queried at `open_vocab_hz`): yolov8n at its fixed 640×640 input proposes no person at all for most overhead lying people, while Grounding DINO tiny at 0.30 box score recovers many of them. A hit above the grounder's box threshold is named after the matched vocabulary term (the raw span stays on the candidate row) and carries no detector person evidence; the pass deduplicates against detector tracks and ages out unconfirmed seeds, and with `grounder_url` unset or the service down the pipeline stays detector-only.

## Procedural worlds

`python3 -m sim.worldgen --procedural --seed N [--kind forest|urban] [--stratum baseline|chaotic]` samples a complete world into `sim/generated/horus_<kind>_pN/`; the same command without `--procedural` regenerates the canonical `sim/worlds/horus_{forest,urban}.sdf`, their scenarios and the generated person models. The generator varies the layout (forest: trail, creek, clearing, camp, hiker/ranger/family routes, tree field, rocks and logs, terrain grid; urban: road grid, buildings, plaza with the landing pad, park, crosswalks, sidewalk routes, circulating traffic and parked cars, lamps, wires and shelters), the sampled environment (sun, sky, ground and foliage tints), the dressing stratum (`baseline`, or `chaotic` with harsher light, more animals and clutter and a denser disturbance schedule), per-world civilian appearances (body, skin, hair, clothing textures and accessories), vehicles, fauna (dogs with walkers, deer and crows in the forest meadow, pigeons and cats in the urban plaza and park) and sim-time disturbance events (hesitate, speed change, breakdown, startle, reverse) that stay inside each agent's declared speed and acceleration limits.

A layout is accepted only after it passes the real-geometry report in `sim/worldgen/validation.py` — the same report the test suite runs on the canonical worlds: a clear home column and every actor's footprint clear of static geometry over its whole executed path (standing walkers ≥0.1 m, vehicles ≥0.3 m over their filleted lanes, animals ≥0.05 m). The sampler additionally keeps every route and wander area inside the 60 m geofence. Rejected candidates are re-sampled from the same seed; if a seed cannot be sampled the CLI exits non-zero naming the seed and the top rejection reasons.

`./sim/run.sh start --procedural [--seed N] [--kind forest|urban] [--stratum baseline|chaotic]` generates and starts one such world (default kind `forest`); without `--seed` a random 5-digit seed is chosen and printed for reproduction. See `sim/README.md` for the generator, layout and scenario details.

Generated worlds fly the procedural flight profile (`config/horus_procedural_forest.yaml`, `config/horus_procedural_urban.yaml`, and `config/ardupilot/common.parm`, `outdoor.parm`, `procedural_fence.parm`): a 1.2 km map bounded by ArduPilot's 870 m `FENCE_RADIUS` around home (clear of the map corners) and 55 m `FENCE_ALT_MAX`, and a 360 s command TTL sized for a map-crossing goto in wall-clock time (an 850 m goto at the 8 m/s cruise is 106 s of simulation time, ~304 s of wall time at RTF 0.35, plus acceleration, yaw and arrival margin). The controller's horizontal speed is altitude-gated: 8 m/s (`cruise_speed_m_s`) while both the current altitude above home and the commanded target altitude are at/above `cruise_min_altitude_m` (26 m forest, above the 23 m tree cap; 38 m urban, above the 34 m building cap), and the 3 m/s `speed_m_s` everywhere below. `run.sh start --procedural`, `demo.sh --procedural` (local and `--remote`) resolve these files from the generated world; the canonical worlds keep `config/horus_{forest,urban}.yaml` with `outdoor.parm` and `canonical_fence.parm`.
