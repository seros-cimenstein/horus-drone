# HORUS plan

The target architecture is the diagram in [`HORUS — Design Document v2.md`](<HORUS — Design Document v2.md#principles-and-architecture>),
approved on 2026-10-31 and updated with user confirmation on 2026-11-26 for standalone HORUS Studio and the built-in 3D test world. Its "Built and planned" table lists what is still missing. This file says in what order to build it and how each phase counts as done. Update the status lines when a phase moves.

## Blocks

The block codes are stable (`~/.omp/agent/easify/ezcode`), so a code pasted in chat always means the
same block.

| Code | Block | Path | One line |
| --- | --- | --- | --- |
| `#63bb` | Control app | `horus/commander/web/`, `ui/`, `horus/repl/` | HORUS Studio web app (laptop + phone PWA) or `horus tui` terminal; emergencies skip the AI |
| `#61d9` | AI pilot | `horus/commander/`, `horus/studio/` | One chat per mission with the AI model; loopback HTTP/SSE API; operator Studio session supervisor |
| `#5c06` | Crew | `horus/commander/crew.py` | Members the commander asks (planner first), each its own chat and drone board role token |
| `#a581` | AI model | outside | Reached through OpenAI/compatible APIs, Anthropic, Claude Code or omp; picks the next single move |
| `#03a1` | Drone board | `horus/mcp_server/` | The tool server on the drone's computer; the AI's only control panel |
| `#a3f1` | Flight moves | `horus/executor/` | take off, go to, move, turn, look, hold, land, home; one at a time |
| `#31b9` | Camera vision | `horus/perception/` | Detects, numbers and tracks things in the camera picture |
| `#9295` | Grounder | `horus/grounder/` | Finds things by description; called by camera vision |
| `#d27f` | Autopilot link | `horus/flight/` | MAVLink to ArduPilot; velocity setpoints only |
| `#12e6` | ArduPilot | outside | Stabilization, proximity avoidance, failsafes, RC override; no geofence |
| `#4e2c` | Test world (Built-in 3D) | `horus/world/`, `horus/bootstrap.py` | Cross-platform Python/MuJoCo 3D world + real ArduCopter SITL (`--model JSON`); default local sim |
| `#71dd` | Test world (Gazebo) | `sim/` | Gazebo + ArduCopter SITL; heavy runs on Koko |
| `#85c8` | Test world (GTA V) | `gta/` | GTA V, the quad always flown in the game with collisions (JSON physics) |

## Rules every phase keeps

- The AI chooses one base move at a time; no skill chases a target or sweeps an area by itself.
- HORUS checks only its inputs and sends velocity setpoints only. No world, and no real drone, has
  a geofence: no boundary, altitude ceiling or floor (removed 2026-11-02 at the user's request).
- Emergency commands (hold, land, home) never wait for the AI, in every control app, and the AI
  cannot undo them: its flight tools stay refused until the operator's next mission.
- If the AI goes quiet, the drone holds, then returns home (`executor.link_loss_rth_delay_s`).
- One test world (or one real drone link) per run: Built-in 3D, Gazebo, GTA V, or Real Hardware—never two at once.
- Operator Studio management endpoints (`/api/studio/*`) are authenticated operator-only controls and are never exposed on the Drone board (`#03a1`) to the AI model (`#a581`).
- Heavy Gazebo/GTA suites run on Koko (see `CLAUDE.md`); Built-in 3D unit/smoke tests run natively on Linux and Windows without Docker.
- Never touch `docs/course/`, `docs/paf/`, `annotated.jpg`, `omp-session-*.html`, `models/`, `data/`, `runs/`, `.venv-host/`.

## Phases

| # | Phase | Status |
| --- | --- | --- |
| 0 | Code matches the diagram | Done 2026-10-29 (`LOOP_DIAGRAM_ALIGN.md`, commit `10c0704`) |
| 1 | Docs use the diagram's names and list what is planned | Done 2026-10-31 |
| 2 | Control app `#63bb`: HORUS's own app | Done 2026-11-01 (`LOOP_CONTROL_APP.md`) |
| 3 | Codex connects to the drone board directly | Done 2026-10-31 |
| 4 | Drone board `#03a1` on the companion computer | Waits for hardware |
| 5 | Encryption on the drone board's radio link | Waits for phase 4 |
| 6 | Phone version of the control app `#63bb` | Built and installed 2026-11-01 (`LOOP_PHONE_APP.md`); camera decided and built 2026-11-02 (what the AI saw, optional Live); user's live GTA test pending |
| 7 | Crew with its planner, GTA gimbal, GTA height map and route planner | Decided 2026-11-03; ledger `LOOP_GTA_CREW_PLANNER.md` written, not run |
| 8 | Standalone cross-platform HORUS Studio (`horus`), built-in 3D test world `#4e2c`, and one-line installers | Built and unit/ASGI/build-verified 2026-11-26 (8A, 8B, 8C); live SITL release workflow ready (`assets-copter-4.7.1`) |
| 9 | Astro Starlight documentation site (`site/`) at `seros-cimenstein.github.io/horus-drone/` & modern `README.md` | Built and verified 2026-11-26 (`site/` build, `install.sh` / `install.ps1`, `.github/workflows/docs.yml`, `README.md`) |

### Phase 2: control app

What it built on:
- The AI pilot's loopback HTTP API, `horus-commander-serve` (`horus/commander/server.py`,
  default `127.0.0.1:8765`, bearer token `HORUS_COMMANDER_TOKEN`): `POST /task`, `POST /emergency`,
  `POST /reset`, `GET /events` (server-sent events).
- The drone board's camera frames (`get_frame`, `get_raw_frame`, `list_candidates`) and state
  (`get_state`, `get_map`) for any MCP client with the drone board token.

Steps:
1. **Design, with the user.** Done 2026-10-31: a laptop-only web page served by the AI pilot service,
   with mission box and news feed, live camera, drone state, map and emergency buttons. Details and
   safety constraints: design document, "Control app design".
2. **Write the ledger.** Done 2026-10-31: `LOOP_CONTROL_APP.md`, 15 items in five sections (drone board
   display reads, service, page, wiring and docs, end-to-end proof on Koko); E3 was added during
   the GTA run.
3. **Run it** with `/seref_manager run LOOP_CONTROL_APP.md`. Done 2026-11-01: the page
   (`horus/commander/web/`) at `http://127.0.0.1:8765/#token=<token>`, printed by the service, with
   mission box, news feed, camera, drone state, map and HOLD / LAND / HOME; passive display reads
   through the service's own drone board session (`/api/state`, `/api/frame`, `/api/map`);
   `scripts/demo.sh` starts the service (`--terminal` opens the `horus` terminal instead). Proven on
   Koko with AI missions in Gazebo (E1) and in GTA V with live game frames (E2). The GTA run found
   that the AI could undo an operator emergency; operator emergencies now lock out the AI's flight
   tools until the operator's next mission (E3).

Done when:
- A mission typed in the app reaches the AI pilot, and its events show in the app.
- Hold, land and home go straight to the AI pilot's `/emergency` and work while a model turn runs.
- The app works with both Gazebo and GTA V runs, proven by a smoke run on Koko.
- Its tests pass, and README and the design document describe it.

### Phase 3: Codex on the drone board

Done 2026-10-31: Codex CLI 0.151 called `get_state` on a Koko world; the `codex mcp add` command is in README "Demo: MCP manager", next to Claude Code.

### Phase 4: drone board on the companion computer

Target: an AM67A-class board (BeagleY-AI or T3 Gemstone O1) next to a separate ArduPilot flight
controller (design document, "Flight controller" and "Hardware and risks"). It starts after the
hardware go/no-go in the design document's timeline: stable hover with optical flow + ToF, and
perception at its target rate on the board. Plan the items then: camera input, perception on the
board's accelerator, gimbal, ToF bridge, start-up services, tethered tests.

### Phase 5: link encryption

Today the drone board serves plain HTTP on loopback with a bearer token; between machines it is
reached through an SSH tunnel (`scripts/koko/remote_up.sh`), which encrypts it. Once the drone talks
over a radio link, choose between TLS on the drone board itself and a VPN such as WireGuard. Either
way, the token stays, and `scripts/netem.sh` latency tests must still apply only to the AI link,
never to MAVLink.

### Phase 6: phone version of the control app

Designed with the user on 2026-11-01 (design document, "Phone app design"): the same page as a
home-screen web app on the user's phone, reached over the tailnet with `tailscale serve` (the
service stays on `127.0.0.1`), its own phone token, no camera, the AI's commands folded into a
"Show commands" block, and a map whose static layers are stored on the phone so only positions
travel. Ledger: `LOOP_PHONE_APP.md`. Before running it, enable HTTPS certificates for the tailnet
(Tailscale admin console, DNS).

Done when:
- The phone opens the app over the tailnet, logs in with its own token and can be installed to the
  home screen.
- A mission sent from the phone reaches the AI pilot; the feed shows the AI's words, with its
  commands folded; HOLD / LAND / HOME work from the phone, also while a model turn runs.
- The map's static layers are fetched once and drawn from the phone's storage; live updates are
  small (measured against today's `/api/map` poll).
- Its tests pass, and README and the design document describe it.

### Phase 7: crew, GTA gimbal and GTA route planner

Decided with the user on 2026-11-03 (design document, "Crew, GTA gimbal and GTA route planner"):
the crew machinery with the planner as its first member, a gimbal with pitch and pan for GTA, a
whole-map GTA height survey at 5 m, and a `plan_route` drone board tool with a fixed-altitude and a
climb-over mode. Ledger: `LOOP_GTA_CREW_PLANNER.md`. The pilot, spotter and safety officer, the
gimbal lease between members and the equipment manifest for specialists are later phases.

Done when:
- `look(pitch, yaw)` turns the GTA camera, `get_state` reports the measured angle, and the
  ArduPilot servo mount is gone.
- The whole GTA map is surveyed at 5 m and served as height tiles; `plan_route` returns routes in
  both modes whose every leg clears the height map plus margins.
- A mission in the control app makes the commander ask the planner, the planner's route shows in
  the feed, and the commander flies it in GTA without touching an obstacle; the drone board refuses
  the planner every tool outside its role.
- Its tests pass, and README, `sim/README.md`, the mod README and the design document describe it.

---

### Phase 8: standalone cross-platform HORUS Studio (`horus`), built-in 3D test world `#4e2c`, and one-line installers

**Goal:** Turn HORUS into a standalone, production-grade application that any user on Windows or
Linux (and macOS) installs with one command and launches by typing `horus`. Everything HORUS offers—
3D simulation with real ArduCopter SITL, live vision & tracking, AI mission supervision, API & model
configuration, link-loss/latency fault injection, and real-drone bench/field connection—is configured,
launched, and tested directly inside one minimal, high-signal web interface (**HORUS Studio**),
without Docker, Gazebo, GStreamer, YAML hand-editing, or multi-terminal token setup.

**Core preservation contract:**
- `horus/mcp_server/` (`#03a1`), `horus/executor/` (`#a3f1`), `horus/flight/` (`#d27f`),
  `horus/perception/` (`#31b9`), and `horus/maps/` keep their safety and control contracts unchanged
  (velocity-only setpoints, one base move at a time, no geofence, `operator_emergency` lock, passive
  display reads, `link_loss_rth_delay_s` -> `auto_rth`).
- Gazebo (`#71dd`, `scripts/demo.sh`) and GTA V (`#85c8`, `sim/run.sh start --gta`) remain supported
  as advanced simulation modes and are selectable/attachable from HORUS Studio.

Phase 8 breaks cleanly into three sub-phases (**8A**, **8B**, **8C**):

#### Phase 8A: packaging, asset/SITL bootstrapper, and built-in 3D test world `#4e2c`

1. **Cross-platform packaging (`pyproject.toml`) & unified CLI (`horus/cli.py`):**
   - Make `pip install horus-drone` / `uv tool install horus-drone` install the complete standalone
     stack out of the box on Python 3.10–3.13+ across Windows (`win_amd64`) and Linux
     (`manylinux` `x86_64` / `aarch64`):
     - Core dependencies: `PyYAML>=6.0,<7`, `mcp>=1.30.0,<2`, `httpx>=0.27,<1`, `uvicorn>=0.29,<1`,
       `pymavlink>=2.4.41,<3`, `numpy>=1.24,<3`, `onnxruntime>=1.17,<2`,
       `opencv-contrib-python-headless>=4.10,<5`, `mujoco>=3.2,<4`, `platformdirs>=4.2,<5`,
       `pyserial>=3.5,<4` (for real-drone COM/tty discovery & MAVLink serial), `anthropic>=0.34,<1`,
       `openai>=1.30,<3`.
     - Keep a lean `[board]` extra (no `mujoco`, no cloud LLM SDKs) for companion-computer installs
       (Phase 4), and update `sim/docker/Dockerfile` to pin `'numpy<2'` explicitly without
       `opencv-contrib-python-headless` so Ubuntu 22.04's GStreamer `python3-opencv` keeps working
       inside the Gazebo image.
   - Route the `horus` console script through `horus/cli.py`:
     - `horus` (no subcommand) or `horus studio [--port PORT] [--no-browser] [--phone]` -> starts
       HORUS Studio on `127.0.0.1` and opens the browser.
     - `horus tui [--server-url URL] [--config PATH]` -> starts the terminal REPL (`horus/repl/app.py`).
       *(Backwards compatibility: if `horus --server-url ...` is invoked directly without a
       subcommand, dispatch to `horus tui` so legacy scripts keep working.)*
     - `horus mcp [--config PATH] [--no-perception]` -> runs the Drone board (`horus/mcp_server/server.py`).
     - `horus setup [--with-grounder] [--check]` -> pre-downloads and SHA-256-verifies SITL + ONNX
       models (and optionally the local Grounding DINO / OWLv2 grounder environment).
     - `horus doctor [--json]` -> verifies Python runtime, MuJoCo offscreen GL context, SITL binary,
       ONNX models, serial port access, Tailscale availability, and configured AI credentials.

2. **Pinned SITL & model release pipeline (`.github/workflows/release-assets.yml`) + `horus/bootstrap.py`:**
   - GitHub Actions workflow builds/bundles and publishes an immutable release (`assets-copter-4.7.1`)
     on `seros-cimenstein/horus-drone` containing:
     - `arducopter-4.7.1-linux-x86_64.tar.gz` (official ArduCopter 4.7.1 SITL ELF + `copter-default.parm`),
     - `arducopter-4.7.1-linux-aarch64.tar.gz` (compiled from `ArduPilot/ardupilot` tag `Copter-4.7.1`),
     - `arducopter-4.7.1-windows-x86_64.zip` (`ArduCopter.exe` built from tag `Copter-4.7.1` + minimal
       required Cygwin runtime DLLs `cygwin1.dll`, `cyggcc_s-seh-1.dll`, `cygstdc++-6.dll` +
       `copter-default.parm`, smoke-tested on `windows-latest`),
     - `yolov8n.onnx` and pre-patched dynamic-batch `osnet_x0_25_msmt17.onnx` (so end users never
       need the `onnx` package at install/runtime),
     - `SHA256SUMS.txt`.
   - `horus/bootstrap.py` resolves assets in order: explicit env override -> repo-local `./models`
     (if SHA-256 matches) -> `platformdirs.user_cache_dir("horus")`. Downloads stream via `httpx` to
     atomic `.part` files, verify pinned SHA-256 digests before rename, set executable bits on POSIX,
     and emit structured progress callbacks `(asset_name, bytes_done, bytes_total, state)` for both
     CLI progress bars and live Studio SSE events.

3. **Direct cross-platform ArduCopter SITL runner (`horus/world/sitl.py`):**
   - Invokes `arducopter` / `ArduCopter.exe` directly without `sim_vehicle.py`, MAVProxy, or `pexpect`:
     ```text
     arducopter -S -I0 --home <lat>,<lon>,<alt>,<hdg> --model JSON:127.0.0.1
       --speedup 1 --slave 0 --defaults <copter-default.parm>,<sitl-overrides.parm>
       --serial0 tcp:127.0.0.1:<mavlink_port>:wait --serial2 tcp:127.0.0.1:<range_port>
     ```
   - Composes `sitl-overrides.parm` in Python from `config/ardupilot/common.parm` (packaged inside
     `horus` package data so pip installs outside a git checkout have all `.parm` fragments and
     default profiles!) plus `indoor.parm` or `outdoor.parm`, `FRAME_CLASS 1`, `FRAME_TYPE 1`,
     `SIM_RATE_HZ 800`, and `PRX_FILT 0`.
   - Runs inside an isolated writable directory (`runs/studio/<run_id>/sitl/`) so `eeprom.bin` and
     logs never collide across runs.

4. **Built-in Python/MuJoCo 3D test world (`horus/world/`):**
   - **Physics & JSON lockstep (`horus/world/physics.py`):** Implements ArduPilot's UDP `9002`
     binary servo-frame decoder and JSON state encoder (matching `gta/horus_mod/src/Core/SitlJson.cs`
     and the F450 motor/prop thrust, drag, and torque model in `QuadPhysics.cs`) stepped against
     MuJoCo's 3D rigid-body & contact engine (`mj_step`), so the quad physically rests on pads,
     collides with walls/ceilings/trees/furniture, and sustains RTF ~1.0. Calls
     `ctypes.windll.winmm.timeBeginPeriod(1)` while active on Windows so localhost UDP lockstep never
     stalls on the 15.6 ms Windows system tick.
   - **Sensors & gimbal (`horus/world/sensors.py`):**
     - 7 MuJoCo `<rangefinder>` sensors (6 horizontal sectors + downward `range_down`, 0.05–4.0 m)
       sent at 20 Hz via `horus.tof_bridge.mavlink_out.PymavlinkSender` to SITL `serial2`.
     - 2-axis stabilized gimbal (`pitch_deg` relative to horizon, `yaw_deg` relative to nose) wired
       to `horus.gimbal.device.GimbalDeviceEndpoint` (`udpout:127.0.0.1:<gimbal_port>`).
   - **Camera streaming (`sim://` alias in `horus/perception/camera.py` + `gta_stream.py`):**
     - Accept `sim://host:port` alongside `gta://host:port` in `OpenCVCameraReader` and
       `horus/mcp_server/server.py` (disabling `GazeboImagePublisher`), serving 640×480 BGR8 frames
       from `mujoco.Renderer` over the existing zero-backlog pull-TCP protocol—no GStreamer required.
     - Render a second offscreen **3D Spectator Camera** (`follow`, `overhead`, or `orbit` preset,
       with optional semi-transparent 7-beam ToF laser visualization and flight-path ribbon) exposed
       to HORUS Studio for live 3D situational awareness right inside the browser.
   - **Shipped 3D scenes & packaged assets (`horus/world/scenes/`):**
     - Bundle lightweight, redistributable low-poly textured human/prop meshes (`person_red`,
       `person_blue`, `person_standing`, `person_sitting`, `person_lying`), checkerboard/textured
       surfaces, and the `tag36h11` AprilTag landing pad texture so YOLOv8n (`yolov8n.onnx`), OSNet
       ReID (`osnet_x0_25_msmt17.onnx`), and HORUS's AprilTag precision landing work reliably:
       1. `indoor_sar` (default): main room, doorway, corridor, furniture, AprilTag pad at home,
          people in distinct shirt colors (including one lying down for SAR scenarios), optional
          kinematic walking person.
       2. `forest_sar`: outdoor terrain, trees with collision trunks/canopies, trails, hikers,
          AprilTag pad, and matching vector map data for the Tactical Map.
       3. `urban_plaza`: city blocks/plaza, buildings, street furniture, vehicles, pedestrians,
          AprilTag pad, and full tiled street/place map (`find_place` and `get_area_map` enabled).
       4. `procedural`: seeded outdoor world generated from `sim/worldgen` (packaged under
          `horus/world/worldgen/` or imported cleanly).

#### Phase 8B: operator Studio supervisor (`horus/studio/`) & zero-YAML runtime management

1. **Process & session lifecycle manager (`horus/studio/supervisor.py`):**
   - Manages at most **one** active drone session at a time (`idle -> bootstrapping -> starting -> running -> stopping -> idle | error`), broadcasting state transitions over `/events`:
     - **`builtin_sim` mode:** Picks free localhost ports for SITL (`5760`/`5763` base), camera
       (`5610` base), gimbal (`14600` base), and Drone board (`8000` base); generates a fresh
       `HORUS_MCP_TOKEN`; writes a run-scoped config; starts `horus.world`, `arducopter` SITL, and
       `horus.mcp_server`; waits for readiness; and hot-binds the host `HorusCommander` (`#61d9`)
       and `DisplayReader` sessions to the new Drone board!
     - **`real_drone` mode:** Two sub-modes selectable from the UI:
       1. *Remote Companion Board:* Connects `HorusCommander` + `DisplayReader` to a user-provided
          `mcp_url` (e.g. over SSH tunnel or Tailscale/WireGuard) + bearer token.
       2. *Direct Flight Controller (USB Serial / Telemetry Radio / UDP):* Spawns a local
          `horus-mcp` process configured with the chosen MAVLink endpoint (e.g. `COM4` @ `57600`,
          `/dev/ttyACM0` @ `115200`, or `udpin:0.0.0.0:14550`), optional USB/RTSP camera source (or
          `--no-perception`), and optional MAVLink gimbal port.
     - **`gazebo` & `gta` advanced modes:** Attaches to or launches Gazebo (`sim/run.sh`, local or
       remote SSH host) or GTA V (`sim/run.sh start --gta` / host Windows `HorusGta.dll` + local
       SITL!—note: on Windows, `horus` will be able to run GTA V's `ArduCopter.exe --model JSON` +
       `horus-gta-gimbal` + `horus.tof_bridge.gta` **natively on the same Windows PC as GTA V**
       without needing a Linux container!).
   - **OS-level orphan prevention (`horus/studio/proc.py`):**
     - **Windows:** Assigns every child process (`ArduCopter.exe`, `horus-mcp`, `horus-world`,
       `horus-grounder`) to a Win32 Job Object configured with
       `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` via `ctypes.windll.kernel32`.
     - **Linux / macOS:** Launches children in dedicated process groups (`start_new_session=True`),
       tears down in strict order (**AI pilot** active turn interrupted -> **Drone board** ->
       **Test world** & **ArduPilot SITL**) with `SIGTERM` grace followed by `SIGKILL`.

2. **Persistent operator settings & write-only secret store (`horus/studio/settings.py`):**
   - Stores non-secret preferences (`ai_provider`, `model`, `base_url`, `reasoning_effort`,
     `default_scene`, `prefer_3d_view`, `theme`, recent hardware endpoints) in
     `platformdirs.user_config_dir("horus") / "settings.json"`.
   - Stores API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
     custom gateway keys, remote drone tokens) in `secrets.json` with `0600` permissions on POSIX
     (and current-user ACL on Windows).
   - Smart provider auto-detection on first launch: checks saved Studio settings -> environment
     variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` mapped to Google's
     OpenAI-compatible endpoint, `OPENROUTER_API_KEY`) -> installed CLIs (`claude`, `omp`). If none
     is configured yet, the Studio still launches and runs simulations, manual moves, cameras, maps,
     and emergencies normally while prompting the user in the Mission box to pick an AI provider in
     1 click.

3. **Cross-platform AI-link fault shaper & hardware pre-flight engine:**
   - **Link shaper (`horus/studio/link_shaper.py`):** Wraps the **AI pilot**'s MCP HTTP transport
     (never `DisplayReader`'s passive UI reads and never MAVLink!) with runtime-adjustable
     `delay_ms` (`0..5000`), `jitter_ms`, and `link_cut` (`bool`). When `link_cut=True`, both the AI
     pilot's tool calls and its `wait_events` long-poll fail/pause as on a lost radio link, while
     passive display reads keep updating the UI so the operator watches the drone enter `HOLDING`
     and trigger `auto_rth` after `link_loss_rth_delay_s` on both Windows and Linux.
   - **Pre-flight checker (`horus/studio/preflight.py`):** Evaluates telemetry freshness, EKF local
     position, battery level, ToF sensor health, downward rangefinder, camera stream FPS, gimbal
     status, `PILOT` mode override status, and AI backend reachability into a structured checklist.

4. **Studio HTTP API routes added to `CommanderASGI` (`horus/commander/server.py`):**
   - Protected by the same constant-time `HORUS_COMMANDER_TOKEN` bearer gate (laptop token only;
     optional restriction so phone tokens can view status but cannot reconfigure host serial ports
     or secrets):
     - `GET /api/studio/status`
     - `POST /api/studio/bootstrap`
     - `POST /api/studio/session/start`, `POST /api/studio/session/stop`, `POST /api/studio/session/restart`
     - `GET /api/studio/spectator-frame`
     - `GET /api/studio/settings`, `PUT /api/studio/settings`, `POST /api/studio/ai/test`
     - `POST /api/studio/link-shaper`
     - `POST /api/studio/manual-move` (sends authenticated operator base moves `take_off`, `move`,
       `turn`, `look`, `goto` with `operator_release`, matching `horus tui`'s `/take_off` and `/goto`)
     - `GET /api/studio/preflight`, `GET /api/studio/hardware/ports`, `GET /api/studio/runs`

#### Phase 8C: modern, minimal HORUS Studio frontend (`ui/` -> `horus/commander/web/`)

1. **Frontend architecture (`ui/`):**
   - Built with **Preact + Signals + TypeScript + Vite** (tiny ~15 KB bundle, fast, strict types)
     with zero external runtime CDN calls (`content-security-policy: default-src 'self'; img-src 'self' data: blob:`).
   - Production build outputs directly into `horus/commander/web/` (committed and included in wheel
     `package-data` so Python users never need Node.js). Every untrusted string from drone/AI/maps
     renders strictly as text nodes (never `innerHTML`).
   - Aesthetic: clean, modern "Tactical Minimal" design system (crisp dark & sunlight-readable light
     themes, `tabular-nums` telemetry, subtle borders, smooth micro-states, keyboard-accessible).

2. **Desktop / Laptop Studio UX:**
   - **Pinned Top Command & Safety Bar:**
     - `HORUS` brand + **Session Pill** (shows active world or hardware target, RTF, one-click
       *Start / Switch / Restart World* popover).
     - Live Telemetry Strip: Altitude, Ground Rangefinder, Speed, Flight Mode (`GUIDED`, `LAND`,
       `RTL`, `PILOT`), Executor State (`IDLE`, `EXECUTING`, `HOLDING`, `RTH`), Battery, Data Age.
     - Quick triggers for **Fault Lab**, **Settings** (with green/amber AI status dot), **Phone QR**,
       and always-enabled **`HOLD` / `LAND` / `HOME` emergency buttons** (with clear `OPERATOR LOCK`
       banner and 1-click release when active).
   - **Zero-Friction Welcome / Launchpad State (when no session is running yet):**
     - Automatic first-run download card (if SITL/ONNX assets need fetching) with live MB/s bar.
     - Scene cards to launch in one click: **Indoor SAR Room**, **Outdoor Forest**, **Urban Plaza**,
       **Procedural World**, **Real Drone (USB / Radio / Companion)**, plus **Gazebo / GTA V**.
   - **Flight Deck Workspace (2-column responsive layout):**
     - **Visual Stage Card:** Tabs/split toggle between **Drone Camera** (live annotated YOLOv8n +
       numbered tracker pills + candidate list), **3D World View** (live MuJoCo follow/overhead
       camera with ToF beam overlay toggle), and **AI Views** (filmstrip of exact frames the AI
       received). Includes a compact Gimbal Pitch/Yaw pad.
     - **Tactical 2D Map Card:** Preserves all existing tile-cached pan/zoom & Mission Planner
       selection tools (`P1` points, `A1` polygons/rectangles, IndexedDB offline cache, *"Fly here"*,
       *"Search area"*, *"Use in mission"*), enhanced with a live 8-sector ToF proximity ring around
       the vehicle icon and home AprilTag marker.
     - **Mission Composer & Curated Demo Chips:** Free-text mission box (`Ctrl/Cmd+Enter` to send,
       *"New mission"* reset) + 1-click starter scenarios tailored to the active world (e.g.
       *"Find the person in the red shirt and hover 2 m away"*, *"Fly down the corridor and report
       what you see"*, *"Test wall avoidance: try flying 8 m north"*, *"Return home and land on the
       AprilTag pad"*). If no AI provider is configured yet, clicking a mission opens the inline
       10-second AI Provider Setup popover instead of failing cryptically.
     - **Mission Timeline Feed:** Clean chat + collapsible tool-call cards showing each base move's
       parameters, live execution badge, duration, inline AI-view thumbnails, and highlighted safety
       events.
   - **Test & Hardware Lab Drawer/Tab:**
     - **Link-Loss & Latency Simulator:** Interactive delay presets (`0 ms`, `500 ms`, `1.5 s`,
       `3 s`) + **"Cut AI Link"** toggle with live countdown showing `HOLDING -> auto_rth`.
     - **Manual Flight & Vision Pad:** Direct operator buttons for `Take Off`, `Move`
       (Nudge Forward/Back/Left/Right/Up/Down), `Turn`, `Look`, and open-vocabulary `set_vocabulary`
       / `point` testing—letting users fly and test hardware or sim even with zero AI keys!
     - **Real-Drone Pre-Flight Checklist:** Live pass/warn/fail items for MAVLink, EKF3, ToF ring,
       Rangefinder, Battery, Camera, and RC Override switch verification.
   - **Settings & Connections Modal:**
     - **AI Pilot & Crew:** Provider cards (OpenAI, Anthropic, Google Gemini, OpenRouter, Local
       Ollama / Custom URL, Claude Code CLI, OMP), model selector, masked API key input, *"Test
       Connection"* button.
     - **Hardware / Sim Setup:** Serial port dropdown + baud rate, UDP/TCP MAVLink input, remote
       `HORUS_MCP_URL`, camera source picker, SITL/Model cache status & optional Grounder installer.
     - **Run Logs:** Browse and download JSONL mission logs from `runs/`.

3. **Phone Companion View (`?view=phone` & Home-Screen PWA):**
   - Preserves all Phase 6 contracts: single-screen viewport-locked layout, thumb-reach `HOLD` /
     `LAND` / `HOME`, `Feed` / `Map` / `Camera` tabs, folded `"Show commands"` blocks, `"What the AI
     saw"` + optional `Live` toggle with byte-size readout, and IndexedDB offline map tiles.

**Phase 8 done when:**
- Fresh install in clean Linux and Windows environments (`uv tool install .` or `pip install .`)
  succeeds with prebuilt wheels only, and `horus setup` fetches and verifies `arducopter` SITL +
  ONNX models cleanly.
- Running `horus` starts HORUS Studio, opens the browser, launches the Built-in 3D `indoor_sar`
  world in 1 click, shows live annotated camera detections + 3D spectator view + map + telemetry,
  and completes:
  1. `take_off`, `look`, `turn`, `move`, `goto`, and `rth` AprilTag landing;
  2. Milestone S3 wall avoidance (`goto` into a wall stops ~1 m short via `DISTANCE_SENSOR` +
     ArduPilot `AVOID_ENABLE`);
  3. Operator `HOLD` / `LAND` / `HOME` interrupting an active AI turn and enforcing `operator_lock`;
  4. Link-Cut Simulator triggering `HOLDING` -> `auto_rth` while passive UI camera/map stay live;
  5. Configuring an AI provider from the Settings UI and completing *"Find the person in the red
     shirt"* end-to-end.
- Closing `horus` with `Ctrl+C` (on Linux and Windows) leaves zero orphan `arducopter` or Python
  processes and zero bound ports.
- Cross-platform GitHub Actions CI (`ubuntu-latest` + `windows-latest`) runs unit tests and an
  automated headless Studio + SITL + 3D world flight smoke test.

---

### Phase 9: Astro Starlight documentation site (`site/`), one-line installers, and modern `README.md`

**Goal:** Replace the dense 430-line `README.md` wall of text with a polished, searchable,
production-grade **Astro Starlight** documentation website deployed at
`https://seros-cimenstein.github.io/horus-drone/` (which also serves the one-line `install.sh` and
`install.ps1` installers), paired with a concise, modern root `README.md`.

**Directory safeguard:** Keep all website code in **`site/`** at the repository root. Never modify
`docs/course/` or `docs/paf/`.

Steps:
1. **Scaffold Astro Starlight in `site/`:**
   - Configure `site/astro.config.mjs` with:
     - `site: "https://seros-cimenstein.github.io"`
     - `base: "/horus-drone"`
     - Starlight integration: title `"HORUS"`, custom SVG logo/favicon matching the Studio brand,
       GitHub social link (`https://github.com/seros-cimenstein/horus-drone`), edit-on-GitHub links,
       Pagefind full-text search, client/build-time Mermaid diagram support, link validation, and a
       refined custom CSS theme (dark/light aerospace palette).
2. **Author complete, well-structured documentation (`site/src/content/docs/`):**
   - **Landing Page (`index.mdx`):** Hero splash page with value proposition, quick-install OS tabs,
     feature cards, architecture preview, and *"Get Started in 60 Seconds"* CTA.
   - **Getting Started:**
     - `getting-started/installation.mdx` — One-line installers (`install.sh`, `install.ps1`),
       `uv` / `pip` install, platform support matrix (Windows, Linux, macOS), `horus doctor`,
       updating and uninstalling.
     - `getting-started/quickstart.mdx` — Launching `horus`, touring the Flight Deck, running your
       first 3D mission, testing manual moves and emergency controls.
     - `getting-started/ai-providers.mdx` — Connecting OpenAI, Anthropic, Gemini, OpenRouter, local
       models (Ollama / vLLM), Claude Code CLI, and OMP; Crew configuration.
   - **Core Concepts & Safety:**
     - `concepts/architecture.mdx` — Full v2 system diagram, block-by-block responsibilities
       (`Control app` through `Test world`), and why semantics stay in the cloud while geometry
       runs onboard.
     - `concepts/safety-model.mdx` — Deterministic ArduPilot safety, Guided velocity-only control,
       7-beam ToF avoidance, `operator_emergency` lock, `PILOT` RC takeover, and `auto_rth` on AI
       silence.
     - `concepts/vision-and-latency.mdx` — Set-of-mark numbered tracker IDs, OSNet ReID, CSRT
       ring-buffer fast-forward compensation, open-vocabulary detection, and Grounding DINO / OWLv2.
   - **Simulation Worlds:**
     - `simulation/builtin-3d.mdx` — Indoor SAR, Forest, Urban, Procedural worlds, 3D Spectator
       camera, and Fault Lab link-cut testing.
     - `simulation/gazebo.mdx` — Advanced Gazebo Harmonic setup, Docker, procedural worldgen, and
       split remote compute (`Koko` workflow).
     - `simulation/gta-v.mdx` — Advanced GTA V integration (`HorusGta.dll` JSON physics, gimbal,
       raycast ToF, whole-map tiles, native Windows Studio workflow).
   - **Real Hardware & Field Operations:**
     - `hardware/companion-setup.mdx` — ArduPilot flight controller + companion board (`horus mcp`),
       serial/UDP wiring, `.parm` fragments, ToF ring, and MAVLink gimbal.
     - `hardware/preflight-and-testing.mdx` — Props-off bench tests, tethered hover, GPS-free
       optical flow (`nogps.parm`), AprilTag pad setup, and the Studio Pre-Flight Checklist.
     - `hardware/phone-app.mdx` — Tailscale HTTPS setup, pairing QR/token, PWA installation, and
       low-bandwidth offline map tiles.
   - **Reference:**
     - `reference/mcp-tools.mdx` — Complete Drone board MCP tool schemas, parameters, return
       payloads, passive reads, and direct Claude Code / Codex MCP client setup.
     - `reference/cli-and-config.mdx` — `horus` subcommands, Studio settings, YAML profile fields,
       and environment variables.
     - `reference/experiments.mdx` — Reproducing latency sweeps, safety trials, and JSONL event logs.
3. **One-line cross-platform installers (`site/public/install.sh` & `site/public/install.ps1`):**
   - Served directly at `https://seros-cimenstein.github.io/horus-drone/install.sh` and
     `https://seros-cimenstein.github.io/horus-drone/install.ps1`.
   - Idempotent, shellcheck-clean (`install.sh`) and PowerShell StrictMode-clean (`install.ps1`):
     bootstraps `uv` if missing, installs `horus-drone` in an isolated Python 3.12 tool environment,
     optionally runs `horus setup` (skippable via `HORUS_SKIP_SETUP=1`), verifies `horus --help`,
     and prints next-step instructions.
4. **GitHub Pages CI/CD workflow (`.github/workflows/docs.yml`):**
   - Builds `site/` on Node 22, checks that zero broken internal links exist (respecting the
     `/horus-drone/` base path!), verifies `dist/install.sh` and `dist/install.ps1` are present,
     and deploys automatically to GitHub Pages (`https://seros-cimenstein.github.io/horus-drone/`)
     on pushes to `main` (and runs the build/link check as a PR gate without deploying).
5. **Rewrite root `README.md`:**
   - Transform `README.md` into a clean, modern ~140-line project front door:
     - Centered header, crisp tagline, badges (Documentation, CI, License, Python, Windows/Linux),
       and hero screenshot of HORUS Studio.
     - **Quickstart** with copy-paste `curl` (Linux/macOS), `irm` (Windows), and `uv` commands +
       `horus`.
     - **Highlights** (4 concise bullets), **Architecture** Mermaid diagram (using the exact diagram
       block names), **Safety Principles** summary, and a clean **Documentation** table linking out
       to `https://seros-cimenstein.github.io/horus-drone/` for every deep guide.

**Phase 9 done when:**
- `npm run build` in `site/` succeeds with zero warnings or broken `/horus-drone/...` base-path
  links, and `.github/workflows/docs.yml` is configured to publish cleanly to
  `https://seros-cimenstein.github.io/horus-drone/`.
- `install.sh` and `install.ps1` pass automated smoke tests on `ubuntu-latest` and `windows-latest`.
- Root `README.md` is concise, visually clean, uses the diagram's exact block names, and links every
  topic to `https://seros-cimenstein.github.io/horus-drone/`.
- `docs/course/` and `docs/paf/` remain completely untouched (`git status docs/` clean).

---

## Suggested Execution Order for the Next Session

To deliver visible value cleanly and verify each layer before building on top of it:
1. **Phase 8A** (Packaging, `horus/bootstrap.py`, SITL release assets/runner, and Built-in MuJoCo 3D
   Test world `#4e2c` + headless flight/vision/avoidance tests).
2. **Phase 8B** (`horus/studio/` supervisor, cross-platform process Job Objects/groups, settings &
   secret store, link-cut shaper, pre-flight engine, and unified `horus` CLI).
3. **Phase 8C** (Modern `ui/` -> `horus/commander/web/` HORUS Studio Flight Deck, 3D spectator view,
   Fault Lab, Settings wizard, and Phone PWA parity + end-to-end UI/smoke tests).
4. **Phase 9** (Astro Starlight site in `site/` configured for
   `https://seros-cimenstein.github.io/horus-drone/`, `install.sh` & `install.ps1`,
   `.github/workflows/docs.yml`, and the redesigned modern `README.md` featuring real Studio
   screenshots).

## How to run a phase as a loop

Use a loop once a sub-phase (8A, 8B, 8C, or 9) is split into atomic, testable items:

1. `/seref_loop <goal>` checks the claims and writes `LOOP_<SLUG>.md` from the seref_loop template.
2. `/seref_manager run LOOP_<SLUG>.md --max N` works through it; the manager reviews each iteration.
3. `seref-loop-gate LOOP_<SLUG>.md` exits 0 only when every item is ticked with evidence.

`LOOP_DIAGRAM_ALIGN.md` is the worked example of the ledger format and the Koko test protocol.
