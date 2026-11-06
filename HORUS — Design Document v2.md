# HORUS — Design Document v2

Sep 24, 2026 · @hasan\_piker

## Summary

HORUS is a drone you direct instead of pilot: a cloud AI decides *what* to do, an onboard executor decides *how*, and ArduPilot decides *whether it is safe*. The primary use case is indoor search and rescue; the platform is civilian, non-weaponized, privacy-aware and always human-overridable.

**Research question:** how can a slow, remote AI model reliably direct a fast physical system over a delayed or intermittent link, while staying task-general?

**Method in one line:** the cloud grounds tasks by picking targets on camera frames, a class-agnostic onboard layer executes them in real time with latency compensation, and specialist recognition is added on demand without redesigning the system.

### What changed from v1

- **Scope is tiered.** A must-have core answers the research question; offline autonomy and modules are stretch goals; distillation-at-scale, local LLM and VIO are future work.
- **Grounding uses IDs, not pixels.** Onboard proposals are marked with tracker IDs and the cloud picks an ID (set-of-mark). The grounding model becomes a fallback and an auto-labeler.
- **Latency compensation has a concrete mechanism:** frame buffer with fast-forward tracking, embedding re-match, slow-down while waiting, check on arrival.
- **Specialist training is a ladder** the manager agent climbs: embeddings → tiny classifier head → agent-triggered YOLOv8n fine-tune with a frozen, pre-validated architecture.
- **Optional ground-station edge tier** (laptop GPU on the LAN) for low-latency grounding.
- **Simulation plan** with ArduPilot SITL + Gazebo is now concrete, with milestones.
- **Headline experiments** are the latency sweep and cloud-only vs. HORUS.

## Principles and architecture

The priority order is Safety > Failsafe/RTH > Local executor > Cloud brain, and the core must fly safely with zero modules and zero cloud.

- **Semantics in the cloud, geometry onboard.** The drone never needs to understand what things are; it handles space, motion, identity-by-comparison and "that tracked thing."
- **Safety is deterministic.** ArduPilot holds collision avoidance and failsafes; none of them depends on a model, and HORUS has no limit layer of its own. There is no geofence: no boundary, altitude ceiling or floor.
- **The AI never sends raw motor or velocity commands.** It picks one base move at a time (take off, go to, move, turn, look, hold, land, return home); the executor flies it in closed loop with velocity setpoints that ArduPilot's avoidance bounds.
- **Simulation first.** Every feature runs in SITL + Gazebo before real flight.

```mermaid
flowchart TD
  Y["You"] --> R["Control app<br/>web page (horus/commander/web) or horus terminal"]
  R --> C["AI pilot<br/>horus/commander, the commander"]
  C -->|ask_planner| K["Crew<br/>planner (pilot, spotter, safety officer planned), one chat each"]
  R -.hold / land / home skip the AI.-> M
  C <-->|asks / next move| LLM["AI model<br/>Claude Code, Anthropic or OpenAI"]
  K <-->|asks| LLM
  C -->|MCP over tunnel| M["Drone board<br/>horus/mcp_server, computer on the drone"]
  K -->|MCP, own role token| M
  M --> E["Flight moves<br/>horus/executor, one move at a time"]
  M --> P["Camera vision<br/>horus/perception, numbered objects"]
  P -.fallback.-> G["Grounding model<br/>loopback grounder"]
  E --> L["Autopilot link<br/>horus/flight"]
  L --> A["ArduPilot<br/>limits, avoidance, failsafes"]
  A --> W["Test world<br/>Gazebo (sim/) OR GTA V (gta/)"]
  W -.camera frames.-> P
```

The AI pilot plans and reports; emergency words skip it; the grounding model is called only when onboard proposals miss the target. Alerts (task, link, emergency, landing and pilot-override transitions, vocabulary detections and region-watch events) wake the AI pilot; everything else waits for it to look.

| Tier | Runs | Rate | Examples |
| --- | --- | --- | --- |
| Cloud commander | Frontier model (Claude / GPT-class / Qwen) | ≤ 1 Hz, event-driven | Plan, pick target ID, ask operator, report |
| Ground-station edge (optional) | Laptop GPU on the same LAN | 3–10 Hz | Grounding DINO / Florence-2 fallback pointing |
| Onboard executor | AI board (BeagleY-AI / AM67A class) A53 cores + accelerator | 10–30 Hz | One base move at a time, tracking, `point` re-identification, precision landing |
| Flight control | ArduPilot on a dedicated flight controller | 400 Hz | Stabilization, avoidance, failsafes |

Executor states (`ExecutorState` in `horus/executor/core.py`): `IDLE → EXECUTING → HOLDING → RTH`, plus `PILOT` whenever a pilot or autopilot failsafe takes over. `OFFLINE_MISSION` and `RETURNING_TO_LINK` are planned states for disconnected autonomy (stretch) and do not exist yet. Every command has a time-to-live: a queued command that expires is dropped, and a running one that expires is stopped and the executor holds in GUIDED.

### Built and planned

The diagram above is the target architecture. For three of its parts the table shows what is built today and what is still planned:

| Part | Built | Planned |
| --- | --- | --- |
| Control app (`horus/commander/web/`, `horus/repl/`) | The HORUS control app, built 2026-10-31: a web page on the laptop, served by the AI pilot service (see "Control app design" below), with mission box, news feed, live camera, drone state, map and HOLD / LAND / HOME buttons that skip the AI. Since 2026-11-01 the same page is also the phone app, installed from the user's tailnet (see "Phone app design" below). Since 2026-11-02 both show the pictures the AI received in the feed, and the phone has a Camera tab with them and an optional live view. The `horus` terminal remains a second control app: free text goes to the AI pilot, emergency words skip the AI. Any MCP client, such as Claude Code or Codex, can also connect to the drone board directly. | Decide the laptop's live camera cost on a real drone link (open question in "Phone app design"). |
| Drone board (`horus/mcp_server/`) | Runs in the simulation container, on the laptop or on Koko. | Runs on the AM67A-class companion computer on the drone (see Flight controller and Hardware). |
| Drone board link | Bearer token on every request, loopback-only listener; between machines an SSH tunnel carries and encrypts it. | Encryption built into the drone board's link, for the radio link to a real drone. |

#### Control app design (decided and built 2026-10-31)

Built by the loop `LOOP_CONTROL_APP.md`; the root README, "The control app", is the user guide.

- **Form:** a web page served on the laptop at `127.0.0.1` and opened in a browser. Laptop only: no phone or network access in the first version (the phone came on 2026-11-01, see "Phone app design"). Built: plain HTML, CSS and JavaScript in `horus/commander/web/`, no build step and no outside downloads, so it works offline.
- **Back end:** the AI pilot service `horus-commander-serve` serves the page and is the only thing the browser talks to. The browser never holds the drone board token; the service reads the drone board for the page. Built: the service prints `http://127.0.0.1:<port>/#token=<token>` to its own terminal once it listens; the token travels in the URL fragment, which never reaches a server, and the page keeps it in `sessionStorage` and sends it as a bearer token. Only the three static files are served without the token. The page's reads go through `GET /api/state`, `/api/frame` and `/api/map` on a second drone board session of the service (`horus/commander/display.py`), never the AI pilot's. `scripts/demo.sh` starts the service and prints the address; `--terminal` opens the `horus` terminal instead, never both.
- **First version screens (all built):**
  - mission box and news feed: missions go to `POST /task`, the AI's words and moves arrive on `GET /events`, read with a streaming `fetch` that reconnects with `Last-Event-ID`; "New mission" posts `POST /reset`;
  - live camera with the numbered objects, refreshed about once a second; "No camera" without frames;
  - drone state: height, rangefinder, battery, flight mode, executor state, link status and data age, stale values marked;
  - map: drone with heading and trail, home, marks and candidates, plus roads and named places when the world has a street map (GTA V). Since 2026-11-01 (user request) it moves like a street-map app (drag, pinch, wheel, +/-, a centre button that follows the drone again) and has Mission Planner-style selections: points P1.. (Point tool, long-press, right-click) and areas A1.. (polygon by corners, rectangle by drag), movable by their handles. Decided with the user: selections act only through the AI, never as a direct operator flight command. A selected point offers "Fly here", an area "Search area": a ready-made mission sent on a second tap (a mission also ends an operator lock); "Use in mission" puts the name in the mission box, and any mission naming a selection carries its coordinates in metres from home. Since 2026-11-02 (user decision) the street map covers the whole GTA map: both views fetch only the tiles on screen at the zoom's level of detail and keep them in IndexedDB (browsed tiles up to a 2 MB cap, least recently used first; an "Offline map" download of every tile is kept until removed), plus the small live poll; `/api/map` and `/api/map/static` are gone;
  - emergency buttons hold, land and home, always visible, sent to `POST /emergency` and never through the AI, never disabled by a running mission or a lost feed.
- **Safety constraints:**
  - The page's reads are display-only. Like `get_state(passive=True)`, they must not count as AI contact; otherwise a page left open would keep the "AI goes quiet: hold, then home" failsafe from firing. Built: `list_candidates`, `get_map_index`, `get_map_tile` and `get_live_map` have the same `passive` parameter, and the service passes `passive=True` on every display read; a passive call is neither logged nor counted as contact. (`get_map` and `get_area_map` had it until 2026-11-01, when the page stopped calling them.)
  - Camera reads for the page must not replace the AI's last set-of-mark view. Built: `list_candidates(passive=True)` does not publish the set-of-mark image.
  - The AI must not undo an operator emergency (decided 2026-11-01, after the AI cancelled the operator's HOME with its own `hold` in a GTA run). Built: the control apps send emergencies as `operator_emergency`, which locks out the AI's `take_off`, `goto`, `move`, `turn`, `hold`, `rth` and `land` in the executor until `operator_release`, sent with the operator's next mission or New mission; the model is never offered these two tools, and the page shows the lock. The link-loss return home still fires during the lock.
- **Worlds:** works the same in Gazebo and GTA V runs; it needs no Gazebo window.
- **Terminal:** the `horus` terminal stays as a second control app, with its own AI pilot.

#### Phone app design (decided and built 2026-11-01)

Decided with the user on 2026-11-01 and built the same day (ledger `LOOP_PHONE_APP.md`, `PLAN.md` phase 6); the root README, "The phone app", is the user guide.

- **Form:** the same page made phone-friendly and installable to the home screen (a web app manifest and a service worker), opened in a phone view. No app store and no second code base; still plain HTML, CSS and JavaScript with no build step and no outside downloads. Built: `?view=phone` or a home-screen start switches the page to the phone view; installed on the user's Galaxy XCover Pro through Brave ("Install app"), where it runs standalone.
- **Access:** over the user's Tailscale tailnet: `tailscale serve` on the laptop gives the tailnet HTTPS address and forwards to the service, which keeps listening on `127.0.0.1` only. Only the user's own tailnet devices can connect, from anywhere. Prerequisite: HTTPS certificates enabled for the tailnet (Tailscale admin console, DNS; enabled 2026-11-01; the machine names then appear in public certificate transparency logs). Built: `scripts/phone_serve.sh start|stop|status|check` and `scripts/demo.sh --phone`; the script never uses `tailscale funnel` and refuses a serve configuration it did not add.
- **Login:** a separate phone token (`HORUS_COMMANDER_PHONE_TOKEN`), accepted next to the laptop's; restarting the service without it revokes the phone. Same rules as the laptop token: in the URL fragment of the printed address, never in logs or the event stream. Built: the phone view keeps it in the app's own storage so the home-screen app starts logged in, and drops it on a 401.
- **What it can do:** everything the laptop page can: missions, New mission, HOLD / LAND / HOME (with the operator lock), the news feed and drone state.
- **What it shows:**
  - the camera, decided 2026-11-02: what the AI saw. The AI pilot service keeps the pictures its HORUS tools returned to the AI model (the newest 30, at most 8 MB, in memory); the feed shows each as a small thumbnail outside the folded commands, and a Camera tab shows the newest large with its age, the older ones a swipe away. The page downloads a picture only when it shows it, so these cost no extra drone link. An optional Live toggle, off by default, shows the drone camera now, one picture a second, only while the Camera tab is open, with its measured size per picture. Built: `GET /api/ai-view/<id>` and `GET /api/ai-view/latest`, `ai_view` feed events without picture bytes;
  - the AI's work as a feed: the operator's requests and the AI's words in full, and each AI turn's commands and results folded into one collapsed "Show commands" block, opened on demand, like a thinking block in a chat app; emergencies and task results always visible;
  - a compact state line (height, mode, battery, link, what the drone is doing, the operator lock);
  - large HOLD / LAND / HOME within thumb reach, never disabled.
- **Map, optimised for a phone link:** decided 2026-11-02: the map covers the whole GTA map (Los Santos and Blaine County), not a 600 m circle. It comes as square tiles at three levels (250 m with every feature, 1000 m simplified, 4000 m overview). The phone downloads tiles on demand, like Google Maps, and keeps recently used ones up to a cap; an option downloads every tile at once for offline use. Only small changing data travels while it runs (pose, heading, marks, positioned candidates). Built: `scripts/maps/build_gta_tiles.py` builds the tiles once (2249, 157 and 18 tiles; 5.9 MB gzipped in all); hidden passive drone board reads `get_map_index(known_version)`, `get_map_tile(level, x, y, known_version)` and `get_live_map()`; the service answers `GET /api/map/index` and `GET /api/map/tile` with ETag/304 and gzip, and `GET /api/map/live` (about 200 bytes every 2 s). The drone board reads tiles from disk, so the whole map costs it under 40 MB. Built in both control apps: the level is picked by metres per screen pixel (250 m tiles up to 1 m/px, 1 km tiles up to 4 m/px, 4 km beyond), the tiles on screen plus a one-tile margin are fetched four at a time, newest view first, with a coarser tile drawn while a finer one loads; IndexedDB keeps browsed tiles up to 2 MB (least recently used evicted) and the "Offline map" download (every tile, pinned until removed, with `navigator.storage.persist`); a new map version drops every stored tile.
- **Open: camera bandwidth on a real drone (2026-11-01, still to discuss).** The laptop page's live camera reads a JPEG about once a second from the drone board, which on a real drone crosses the drone's radio or internet link on top of what the AI itself pulls. Options to weigh: keep it in simulation only, lower its rate or size, show it only on demand, or show only the frames the AI requested. The phone already does the last two (2026-11-02: what the AI saw, and Live only on demand).

#### Crew, GTA gimbal and GTA route planner (decided 2026-11-03, phase 7)

Decided with the user on 2026-11-03; ledger `LOOP_GTA_CREW_PLANNER.md`, `PLAN.md` phase 7. Not built yet.

- **Crew.** The commander stays the only agent that talks to the operator. It hands jobs to crew members, each its own chat with the AI model, its own system prompt and its own drone board role token, so the drone board itself refuses every tool outside the member's job (a prompt never enforces scope). Members go from general to special and are added only where they take load off, never for their own sake. The base crew is planner, pilot, spotter and safety officer; specialists (radio operator, audio spotter and so on) join only when the drone carries their equipment. Every sensing job has at least one member, under the chain that uses it (the audio spotter under the spotter). Phase 7 builds the machinery and the **planner**; the commander keeps its flight tools until the pilot exists.
- **One writer per actuator.** Sensor data is shared: any member may read the same camera frames. Pointing the camera, like flying, has exactly one owner at a time, enforced by the drone board: a second member's `look` is refused with the holder's name, and the commander hands the camera over per leg (pilot in transit, spotter while searching). Precision landing already takes the camera from the AI this way. The gimbal lease is built when a second member gets `look`; in phase 7 only the commander has it.
- **GTA gimbal.** A gimbal device for GTA, `horus/gimbal/gta.py`, on the same MAVLink gimbal-device protocol as Gazebo's (`horus/gimbal/device.py`), talks to `HorusGta.dll` over two small UDP datagrams (setpoint in, measured camera attitude out). The mod points the camera with HORUS's setpoint instead of ArduPilot's placeholder servo mount, which goes. `look` gains pan: `look(pitch_deg, yaw_deg)` with yaw relative to the nose, in every world with a gimbal.
- **GTA height map.** No GTA data on disk has obstacle heights (the navmesh has holes where buildings stand). The mod surveys the whole map once at 5 m cells by casting rays straight down after loading collision at each spot; the result is the top surface (roofs, trees, terrain) per 250 m tile. Rays from above miss what is under an overhang, so routes only ever go over obstacles, never under them.
- **Route tool and planner.** A drone board tool `plan_route` (planner role only) runs A* on the height map and returns waypoints that the commander flies one `goto` at a time, so the AI still chooses each move. Two modes, both decided: one altitude for the whole route (around everything taller), or climbing over obstacles leg by leg. Every returned leg is checked along its whole length against the height map plus the margins before it is returned.

## Scope tiers

The must-have core alone answers the research question; everything else is added only once the core flies reliably.

| Tier | Includes | Demo scenarios |
| --- | --- | --- |
| **Must-have core** | Dual-system loop, set-of-mark grounding, latency compensation, ArduPilot safety layer, base-move MCP tools, REPL | Find the person in the red shirt · Keep that person in view through an occlusion with base moves · Link cut with no package → hold → RTH → AprilTag landing |
| **Stretch** | Agent-triggered YOLOv8n fine-tune (ladder step 3), offline mission packages + store-and-forward, one thermal module, ground-station edge tier | Search room for someone lying down · Link cut mid-mission → continue → return to link zone → sync · Ambiguity: drone asks before acting |
| **Future work** | Full module manager with DroneCAN auto-discovery, local LLM for offline decisions, VIO, multi-drone | — |

Scripted, replayable REPL missions are part of the core because every experiment depends on them.

## Real-time grounding and latency

The cloud picks a tracker ID, not a pixel box, so a late answer is still correct and the frontier model never outputs coordinates.

### Primary: set-of-mark ID pointing

1. Onboard, a class-agnostic detector proposes candidates and the tracker assigns persistent IDs.
2. The drone sends a timestamped, downscaled frame (e.g. 640 px JPEG) with numbered marks drawn on each candidate, plus a small table of IDs, boxes and attributes.
3. The commander answers with an ID and a base move ("ID 3: turn toward it, then move 2 m forward").
4. The tracker has kept ID 3 alive the whole time it stayed in view, so the commander's next move uses its current position. An object that leaves the view is not searched for onboard: the commander re-points at it (`point`) when it sees it again.

### Fallback: grounding model

Used only when the target is not among the proposals (unusual object, small, partly hidden). The commander calls the pointer with the frame ID and a description, and the executor brings the returned box up to date:

- **Fast-forward tracking:** a 1–3 s ring buffer of frames onboard; initialize the tracker on the old frame and replay the buffer to the present.
- **Embedding re-match:** crop the returned box from the old frame, embed it, and match against current detections. Robust when the target moved a lot.
- **Check on arrival:** if the updated box matches nothing above a confidence threshold, discard it and re-query with a fresh frame.

### Reducing latency itself

- **Hold while waiting:** with a query pending, the commander holds the drone (`hold`) or waits before the next move, so there is less to compensate.
- **Small payloads:** compressed low-res frames or region crops only.
- **Ground-station edge tier:** run the grounding model on a laptop GPU on the same LAN; the commander stays in the cloud.
- **Event-driven queries:** call the cloud when tracking confidence drops, a skill finishes, something unexpected appears, or the executor is unsure; never on a timer.

Every frame carries frame ID + timestamp + drone pose, and every observation sent to the cloud states its age.

## Specialized recognition (future work)

Not built: the target profiles and embedding/tiny-head recognition modes (steps 1–2) were removed on 2026-10-29 (see History), so tracker identity comes only from the tracker's own appearance gallery and `point`. The ladder below stays the plan for specialist recognition.

The manager agent climbs a three-step ladder and stops at the first step that works; fine-tuning is the last resort, not the default.

| Step | Method | Time to ready | Where it runs | Best for |
| --- | --- | --- | --- | --- |
| 1 | Class-agnostic detector + embedding match against reference crops | Seconds | Onboard, no training | A specific person (ReID / face), a specific object |
| 2 | Tiny classifier head on frozen-backbone embeddings, 20–100 crops | Seconds to 1 min | Onboard CPU or cloud | A small set of look-alike objects |
| 3 | Fine-tuned YOLOv8n, auto-labeled | \~15–45 min | Cloud GPU + TIDL compile | A *type* of object or shape (valve model, crack, debris), long offline missions |

For a specific person, stay on step 1: fine-tuning YOLO on one person from a few photos teaches "person", not "this person". Identity is an embedding problem.

### Step 3 pipeline (planned specialist training)

```mermaid
flowchart LR
  A["Collect<br/>refs + drone keyframes"] --> B["Auto-label<br/>Grounding DINO / SAM"]
  B --> C["Augment<br/>scale, blur, light"]
  C --> D["Fine-tune<br/>YOLOv8n"]
  D --> E["Validate<br/>held-out frames"]
  E --> F["ONNX → TIDL<br/>calibrate on real frames"]
  F --> G["Sign + load onboard<br/>shadow mode"]
```

Planned, not built: the commander would ask for a specialist from a description and reference images and get a task back; the job runs in the cloud while the drone keeps working with its generic detector meanwhile. The signed result would then be loaded onboard in shadow mode. No MCP tool exists for either step yet.

- **Frozen architecture:** one YOLOv8n configuration is proven once to compile on TIDL with no CPU fallback layers; afterwards only weights change, so compilation is predictable.
- **Data from the drone's own camera:** recent keyframes are included so the training data matches the real viewpoint and sensor.
- **Quality gate:** below a set mAP on held-out frames, the model is rejected and the agent falls back to embeddings.
- **Shadow mode:** the specialist runs next to the generic detector for a short check before its candidates are trusted.
- **Safe loading:** mode switches only while hovering; a specialist only proposes candidates and never touches flight or avoidance.

In simulation, step 3 is testable end to end except the TIDL compile, which is validated once on the real board.

## Disconnected autonomy (stretch)

While connected the brain prepares the drone; while disconnected the drone executes and records; on reconnect the brain and operator confirm. Built so far: only the AI-link-loss failsafe (hold, then return home). Mission packages, store-and-forward and link-aware return below are planned.

**Mission package** (signed, sent before link loss):

| Field | Example |
| --- | --- |
| Goal | Find this person |
| Target profile | Embeddings, attributes, reference crops, optional specialist model |
| Search plan | Area, pattern, altitude |
| Thresholds | Match vs. "maybe" confidence |
| Allowed actions | Observe, record, hover, mark; nothing irreversible |
| Budgets | Max time, battery reserve, max distance |
| Return conditions | Found, time up, battery low, uncertain |

The executor runs the package as a rule-based behavior tree; it never invents new goals.

- **Store-and-forward:** candidates (crops, scores, pose, time), keyframes, coverage, events and link quality per position; uploads best matches first on reconnect.
- **Link-aware return:** fly to the nearest breadcrumb point with good link, retracing the path to limit drift; battery reserve always guarantees reaching home.
- **Link loss is emulated deliberately** with `tc netem` (delay, loss, full cut) on the link interface, in simulation and on hardware. An indoor room has no natural dead zone, so the thesis states this openly; it also makes runs repeatable.
- **Privacy:** person profiles only with a lawful SAR basis (KVKK / GDPR), mission-scoped, deleted after the mission, logs encrypted, matches are candidates for confirmation.

## Safety and MCP interface

ArduPilot's proximity avoidance and failsafes act on every move HORUS sends, and emergency tools bypass the task queue. There is no geofence, so nothing bounds how far or how high a move goes: a bad AI decision can fly the drone out of the area or into a ceiling. HORUS itself only checks its inputs (finite numbers, take-off above 0 m, no target at or below home) and clamps nothing.

- **Collision avoidance:** ToF ring (4–6× VL53L1X) feeding ArduPilot proximity (`PRX_*`, `AVOID_ENABLE`). Monocular depth may add awareness but is never the only safeguard.
- **Verify avoidance in Guided mode first.** The executor drives the drone in Guided mode, and avoidance behaves differently per flight mode. Milestone S3 in the simulation plan proves the proximity sensors stop executor-issued motion before anything else is built on it.
- **Odometry and RTH:** GPS in the default profile; without GPS, optical flow + downward rangefinder into EKF3 (Section: Navigation source). Home set at takeoff, AprilTag pad for precise landing, breadcrumb retracing as backup (planned).
- **Failsafes:** cloud link lost → the executor holds, and after `link_loss_rth_delay_s` (default 30 s) of hold with no MCP call it returns home (`auto_rth`): if the AI goes quiet, the drone holds still, then flies home. Planned (stretch): run a mission package if any, return to the link zone, then RTH. Low battery → RTH with enforced reserve. Hardware RC override always available.
- **Pilot override is final.** Any armed flight-mode change HORUS did not request (RC mode switch, GCS, ArduPilot battery/EKF failsafe) cancels all HORUS work, enters `PILOT` state, publishes `pilot_override`, and refuses every flight tool, emergencies included, until the vehicle is switched back to GUIDED (`pilot_released`) or disarmed. HORUS only re-sends GUIDED when telemetry does not already report it, so a stale GUIDED report never undoes a pilot's switch.

| Category | Tools |
| --- | --- |
| State | `get_state`, `get_frame`, `get_raw_frame`, `list_candidates`, `get_map`, `get_link_status` |
| Base moves | `take_off(altitude_m)`, `goto(pose)`, `move(forward_m, right_m, up_m)`, `turn(heading_deg|by_deg, rate_deg_s)`, `look(pitch_deg, yaw_deg)` (yaw from the nose); one at a time |
| Grounding | `set_vocabulary(terms)` (continuous open-vocabulary detection), `point(frame_id, description|box)` (may seed a new tracker ID), `set_mode(name)` (`active`/`paused` detection) |
| Scene | `get_candidate_motion(id)`, `watch_region(name, box, class_name)`, `list_region_watches`, `remove_region_watch(watch_id)` |
| Maps | `find_place(query)`, `get_area_map(north_m, east_m, radius_m)`, `mark_place(name, north_m, east_m, note)` |
| Tasks | `get_task_status(task_id)`, `wait_for_task(task_id, timeout_s)`, `wait_events(after_id, timeout_s)`, `wait_for_event(kinds, after_id, timeout_s)`, `cancel(task_id)` |
| Emergency | `hold`, `rth`, `land` |
| Planned (stretch; no tool yet) | Specialist training and loading a trained model onboard; uploading, starting, reading the log of and clearing an offline mission package |

Moves address positions, headings and tracker IDs the commander read from the latest frame, never pixel regions. Long actions return a `task_id` immediately. `wait_events` blocks asynchronously until an executor or perception transition arrives (or its timeout expires), so the commander can observe task completion, link state, emergency actions, and perception changes without polling. Event kinds are registered in `horus/events.py`: executor (`task_finished`, `link_lost`, `link_restored`, `auto_rth`, `emergency`, `landed`, `precision_landing_stalled`, `pilot_override`, `pilot_released`) and scene (`object_detected` and the `watch_region` kinds `region_enter`, `region_exit`, `region_dwell`, `region_watch_paused`, `region_watch_resumed`, `region_watch_expired`). Every registered kind may wake the commander model (the alerts); `vocabulary_changed` stays on the `wait_events` stream only. The server also offers its operating guidance as the `horus_commander` prompt and the `horus://mission/brief` resource. Every state and frame response is timestamped. The server is authenticated (bearer token), reached only through an encrypted tunnel, and built on the Python MCP SDK over streamable HTTP, with pymavlink to ArduPilot.

HORUS is a provider-agnostic MCP server: any MCP client and model can command it, one base move at a time. The onboard plan runner that let a provider send a whole mission as one program was removed (see History).

### Navigation source

GPS flight is the default on every shipped world: the indoor room (`horus_indoor`), the canonical outdoor worlds (`horus_forest`, `horus_urban`), generated procedural worlds (`horus_<kind>_p<seed>`) and the GTA V world (`gta`). The SITL parameters are composed from fragments in `config/ardupilot/`: `common.parm` (proximity avoidance, downward rangefinder) followed by `indoor.parm` or `outdoor.parm`; no fragment sets a geofence. The GTA SITL (`sim/scripts/start_gta_sitl.sh`) writes its own overrides. `sim/scripts/world_profiles.py` maps each world to its executor config and fragment list; `HORUS_SITL_PARAM_FILE` overrides it. The GPS-free profile (optical flow into EKF3, barometer altitude, downward rangefinder) is `nogps.parm` loaded after `common.parm` and `indoor.parm` (`sim/scripts/s4_landing_trials.sh`); it stays maintained so the same stack can fly without GPS when needed.

### Flight controller

ArduPilot runs on a dedicated flight controller; the AI board (BeagleY-AI / AM67A class with CNN accelerator) is the companion computer for the executor, perception and MCP server. ArduPilot does flight only: the AI board drives the camera gimbal itself over the MAVLink gimbal-device protocol on the gimbal's own link (`horus/gimbal/`), so no camera function runs on the flight controller.

## Simulation plan (ArduPilot SITL + Gazebo)

Start with Gazebo Harmonic on Ubuntu 22.04 and the official [ardupilot\_gazebo](https://github.com/ArduPilot/ardupilot_gazebo) plugin; the plugin repo recommends Harmonic and also supports Garden, Ionic and Jetty. The same ArduPilot firmware runs in SITL and on the real board, so everything above the flight controller carries over unchanged.

### Setup

1. Install Gazebo Harmonic (binary install) and check it runs: `gz sim -v4 -r shapes.sdf`.
2. Set up the ArduPilot development environment and confirm plain SITL runs with MAVProxy.
3. Install plugin dependencies, then build the plugin:

```bash
sudo apt install libgz-sim8-dev rapidjson-dev libopencv-dev \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl
git clone https://github.com/ArduPilot/ardupilot_gazebo
cd ardupilot_gazebo && mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=RelWithDebInfo && make -j4
echo 'export GZ_SIM_SYSTEM_PLUGIN_PATH=$HOME/ardupilot_gazebo/build:${GZ_SIM_SYSTEM_PLUGIN_PATH}' >> ~/.bashrc
echo 'export GZ_SIM_RESOURCE_PATH=$HOME/ardupilot_gazebo/models:$HOME/ardupilot_gazebo/worlds:${GZ_SIM_RESOURCE_PATH}' >> ~/.bashrc
```

4. First flight, in two terminals:

```bash
gz sim -v4 -r iris_runway.sdf
sim_vehicle.py -v ArduCopter -f gazebo-iris --model JSON --map --console
# in MAVProxy: mode guided → arm throttle → takeoff 5
```

5. Camera: attach the plugin's `GstCameraPlugin` to the drone camera; it streams H.264 over UDP (port 5600 by default), which the executor reads with OpenCV/GStreamer exactly as it will read the real CSI camera stream.

### Simulation stack

```mermaid
flowchart LR
  GZ["Gazebo Harmonic<br/>indoor world + actors"] <-->|JSON| SITL["ArduCopter SITL"]
  GZ -->|H.264 UDP 5600| EX["Executor + MCP server<br/>dev PC"]
  GZ -->|lidar rays| TOF["ToF bridge<br/>DISTANCE_SENSOR"]
  TOF --> SITL
  EX <-->|MAVLink| SITL
  EX <-->|MCP via netem link| AG["Agent + REPL<br/>container"]
```

The agent runs in its own container so `tc netem` delay and loss apply only to the brain link, never to MAVLink.

**Gazebo or GTA, one per run.** The test world is either Gazebo (`sim/`, the stack above) or GTA V (`gta/`): in GTA the quad always flies inside the game as a physical object (ArduCopter SITL on its JSON backend, `HorusGta.dll` flying the quad and casting the emulated ToF beams; `horus_capture.asi` serves the frames on `gta://`), so it collides with GTA's world. `sim/run.sh start --gta` starts GTA's SITL with no Gazebo. The two never run together: `sim/run.sh start` refuses a second HORUS world container under any name and the other world's host process (`gz sim` or the GTA V game), a config whose `perception.source` belongs to the other world, and `scripts/koko/gta_game.sh start` refuses to start GTA while Gazebo runs. Running both would overload the host.

### World and sensors

- **Indoor world (`horus_indoor`):** a room and corridor with furniture, a textured floor and an AprilTag pad; Gazebo actors (animated people) in different shirt colors, one lying on the floor.
- **GTA V world (`gta`):** Legion Square in GTA V Enhanced around `HorusGta.ini [origin]`, flown in the game with collisions; the camera rides on HorusGta.dll's gimbal, which HORUS points through `horus-gta-gimbal` (`horus/gimbal/gta.py`) and which rests at -45° until it does. Its map tools read the GTA road graph, places and walkable navmesh (`config/horus_gta.yaml`).
- **Outdoor worlds:** the canonical `horus_forest` and `horus_urban`, and procedurally generated forest/urban worlds (`horus_<kind>_p<seed>`), all flown on GPS with no geofence.
- **GPS-free flight (S4):** `nogps.parm` disables the SITL GPS and flies on SITL's simulated optical flow into EKF3, with the downward rangefinder from the ToF bridge. Every other profile flies on GPS (Section: Navigation source).
- **ToF ring:** six short-range Gazebo lidar sensors plus a downward range sensor indoors (one 8-beam ring lidar on the procedural payload), read by a small bridge that sends MAVLink `DISTANCE_SENSOR` messages, with ArduPilot proximity set to the MAVLink type (`PRX1_TYPE 2`).
- **Perception in sim** runs on the dev PC (PyTorch / ONNX). Cap its frame rate to what the AI board's accelerator is expected to sustain, so behavior does not depend on desktop-class compute.

### Milestones

| # | Milestone | Done when |
| --- | --- | --- |
| S0 | Toolchain | Iris flies `takeoff 5` in `iris_runway.sdf` |
| S1 | MAVLink control | Python script (pymavlink or MAVSDK) takes off, flies a square, lands in Guided mode |
| S2 | MCP server + REPL | `/state`, `/goto`, `/hold`, `/rth` work from the REPL; skills return `task_id`s |
| S3 | Safety proof | Indoor world + ToF bridge: executor-issued `goto` toward a wall is stopped by proximity avoidance in Guided mode |
| S4 | GPS-free | Same missions on optical flow + rangefinder; RTH lands within a set error on the AprilTag pad |
| S5 | Perception + tracking | Camera stream → detector → tracker with persistent IDs while an object stays in view; after a 2 s occlusion the commander re-seeds the ID with `point` |
| S6 | Cloud loop | Set-of-mark frames to the commander; "find the person in the red shirt" succeeds end to end |
| S7 | Latency | Frame buffer, fast-forward tracking, embedding re-match; latency sweep 0–3 s with `netem` |
| S8 | Stretch | Mission package + link cut; planned specialist training produces a model the executor loads in shadow mode |

Use `SIM_SPEEDUP` and headless Gazebo runs for batches of repeated trials once scripted REPL missions work.

## Experiments

Two headline experiments answer the research question directly; the rest support them and are run only as time allows.

| Priority | Experiment | Measure |
| --- | --- | --- |
| Headline | Latency sweep: 0, 0.5, 1, 2, 3 s added delay, with vs. without compensation | Task success rate, time to target, wrong-target rate |
| Headline | Cloud-only (LLM sends goto/velocity directly) vs. HORUS dual-system | Success rate, collisions, blocked unsafe commands |
| Secondary | Set-of-mark ID pointing vs. grounding-model boxes | Success rate, latency, pointer errors |
| Secondary | Re-pointing after occlusion: the commander re-seeds a lost ID with `point` | Re-pointing rate, time to re-point |
| Future work | Recognition ladder: embeddings vs. tiny head vs. fine-tuned YOLOv8n | Precision/recall, time to ready |
| Secondary | Generalization to target objects and descriptions not used during development | Success rate |
| Secondary | Commander model comparison on identical scripted missions | Success rate, API cost, latency |
| Secondary | RTH accuracy with vs. without AprilTag | Landing error (cm) |
| Stretch | Offline mission: detection and false-match rate offline vs. online; data recovered on sync | Rates, time to reconnect |

Every experiment runs as a scripted REPL mission, N ≥ 20 trials per condition in simulation and a smaller confirming set on hardware.

## Hardware and risks

The companion is an AM67A-class AI board (BeagleY-AI or T3 Gemstone O1: 4× A53 for Linux, 4 TOPS accelerator) next to a separate ArduPilot flight controller, on a 5–7" frame with prop guards; the default-core BOM from v1 (\~$785–1,190, or \~$510–915 with a free board) plus the flight controller.

| Risk | Mitigation |
| --- | --- |
| AI-board ecosystem is new (TIDL toolchain, drivers) | Flight control is on the separate controller, so the board only risks perception; compile models early |
| TIDL operator support | Prove the frozen YOLOv8n and tracker/ReID models compile early, before any training |
| Indoor optical flow drift on plain floors | Textured floor mats, AprilTag landing, breadcrumb retracing |
| ReID trained on eye-level footage fails from above | Early test on drone-view footage; fall back to attributes + fine-tuning if needed |
| Avoidance does not act on Guided-mode motion | Milestone S3 proves it in simulation before hardware |
| Loud, large drone in a small room | Netted test area, props-off and tethered tests first, low controller speeds |
| Wi-Fi instability | Command TTL, hold-by-default, failsafes |
| API cost and latency | Event-driven queries, low-res frames, ground-station edge tier |
| False matches offline | Conservative thresholds, confirmation after sync |
| Privacy | Mission-scoped profiles, deletion after mission, encrypted logs |

## Timeline and success criteria

**Minimum viable result:** in simulation and on real hardware, "find the person in the red shirt" and "keep that person in view" succeed reliably at up to 2 s added latency, with compensation clearly beating no compensation, and zero collisions caused by AI commands.

The timeline is in weeks from the start of simulation work; real dates depend on the submission deadline (open question).

| Weeks | Phase | Milestones |
| --- | --- | --- |
| 1–2 | Toolchain and control | S0, S1 |
| 3–4 | MCP server, REPL, safety proof | S2, S3 |
| 5–6 | GPS-free flight, perception, tracking | S4, S5 |
| 7–9 | Cloud loop and latency compensation | S6, S7 |
| 8 | **Hardware go/no-go:** the flight controller flies stable hover with flow + ToF and the AI board runs perception at target rate, or switch to fallback | Props-off, tethered, low-altitude tests |
| 10–12 | Indoor flights of the core scenarios | Core demos on hardware |
| 12–14 | Stretch goals if the core is solid | S8, thermal module |
| 14–16 | Experiments and write-up | Headline experiments, thesis, demo video |

Hardware bring-up runs in parallel from week 5, so the go/no-go in week 8 is a real decision, not a surprise.

| Scenario | Success criterion |
| --- | --- |
| Find the person in the red shirt | ≥ 80% correct target over 20 sim trials; report names the right location |
| Keep that person in view | Through a 2 s occlusion the commander keeps the person framed with `turn`/`move`/`look` and re-seeds the ID with `point`, in ≥ 80% of trials |
| Link cut, no package | Holds, returns home and lands within a set distance of the AprilTag center in every trial |
| Unsafe command injected | Stopped by ArduPilot's proximity avoidance in 100% of trials |

- [ ] Confirm the submission deadline and team size, then turn weeks into dates

## Related work

HORUS differs from prior LLM-drone work by combining ID-based grounding, onboard latency compensation, a deterministic ArduPilot safety layer and disconnected autonomy on real hardware.

| Project | Summary | Gap HORUS addresses |
| --- | --- | --- |
| [TypeFly](https://arxiv.org/abs/2312.14950v2) (Yale, 2023–24) | A cloud LLM writes programs in a small custom language (MiniSpec, since replaced by restricted Python) that a drone executes, with edge-based vision; the revised version targets LLM response latency | HORUS keeps the model in the loop one base move at a time over a provider-agnostic MCP interface, with tracker-ID grounding and an ArduPilot safety layer with pilot override (its TypeFly-style plan runner was removed; see History) |
| ChatGPT for Robotics (Microsoft, 2023) | ChatGPT generates Python code to fly a simulated drone toward an object, given skills and the object's precise location | Simulation only; object location given; no real-time layer |
| CognitiveDrone / -R1 (Skoltech, 2025) | 7B VLA trained on simulated trajectories; R1 adds a Qwen2.5-VL reasoning module | Simulation only, narrow task, too heavy for edge; dropped from the plan |
| EchoPilot | Voice → local LLM plans a mission → MCP tools over MAVSDK (PX4) | Upfront planning, GPS waypoints, no vision loop or latency handling |
| mavlink-mcp | MCP server letting an LLM fly ArduPilot and see through its camera, SITL-focused | No onboard executor (verify hardware claims before submission) |
| ardupilot-mcp | Safety-gated MCP server for ArduPilot, actuation off by default | Good safety reference; no perception or execution layer |
| LLM-controlled-drone | Local LLM + YOLO feedback controlling PX4 in Gazebo via ROS 2 | Simulation, fixed-class perception |

Techniques HORUS builds on: dual-system control (Figure's Helix, NVIDIA GR00T N1), set-of-mark visual prompting, supervisory control for delayed teleoperation, and Grounding DINO / Florence-2 for open-vocabulary grounding.

- [ ] Verify every entry above against the original papers and repos before submission

## History

- **2026-11-06 — commander and crew on DeepSeek 4.1 Flash at max effort** (branch `chinkmaxxing`): every model in the AI pilot and, once it exists, its crew runs `deepseek-v4.1-flash` with `reasoning_effort: max` through the opencode gateway (`https://opencode.ai/zen/go/v1`, OpenAI-compatible), so the profiles read `OPENCODE_API_KEY` and need the `commander-openai` extra instead of the Claude Code login. Two consequences are in the config: the gateway refuses a request without its `x-opencode-session` header (`commander.extra_headers`), and its reasoning tokens are charged to `max_tokens`, which the profiles raise from 800 to 8000 because a model that runs out mid-thought answers with no text and no tool call. The keyless `claude-code` provider stays the code default and remains selectable per profile.
- **2026-11-03 — crew, GTA gimbal and GTA route planner decided** (phase 7, `LOOP_GTA_CREW_PLANNER.md`): the AI pilot gains a crew of members with their own chats and drone board role tokens, starting with the planner; GTA gains a gimbal with pitch and pan, a whole-map height survey at 5 m and a `plan_route` tool with a fixed-altitude and a climb-over mode. Diagram: the Crew block and its arrows.
- **2026-11-02 — GTA always flies with collisions.** At the user's decision no session may start the GTA world without collisions: `sim/run.sh start --gta` is always the physics mode (the former `--gta --json-physics`). Removed: the camera-only pose mode, `horus-gta-bridge` (`horus/gta_bridge.py`) and its role as the GTA gimbal device, and the `--json-physics` option; `config/horus_gta.yaml` lost its `gimbal` connection. Accepted cost: the AI's `look` stays unavailable in GTA until a gimbal is built for physics mode. HorusGta.dll's unused pose receiver goes at the next mod rebuild.
- **2026-11-02 — ArduPilot geofence removed** everywhere at the user's request (no circle, ceiling or floor in any world or for a real drone); the indoor drone can now reach the ceiling.
- **2026-10-31 — HORUS control app built** (`LOOP_CONTROL_APP.md`). A web page served by the AI pilot service `horus-commander-serve` on the laptop (`horus/commander/web/`): mission box and news feed, live camera, drone state, map, and HOLD / LAND / HOME that go to `POST /emergency` and never through the AI. Login by the token in the URL fragment of the address the service prints. The service reads the drone board for the page on a second session with `passive=True` (new on `get_map`, `get_area_map` and `list_candidates`), so the page never counts as AI contact and never replaces the AI's last set-of-mark view. `scripts/demo.sh` now starts the service and prints the address (`--terminal` keeps the `horus` terminal); the service ends its Claude Code process on SIGTERM too.
- **2026-10-31 — target architecture names and plan.** The terminal is now the control app and the drone remote the drone board, matching the approved diagram; the diagram gains the AI model the AI pilot asks. "Built and planned" records what is still missing: HORUS's own control app, the drone board on the companion computer, and encryption built into its link. Removed: the old "Horus Commander" Gazebo GUI panel (`sim/gui/horus_commander/`) and its relay `horus-gz-bridge`; `scripts/demo.sh` now opens the `horus` terminal instead.
- **2026-10-29 — aligned to the approved architecture diagram** (`LOOP_DIAGRAM_ALIGN.md`). Removed: the target skills `approach`, `follow(id)` and `orbit` with their lost-target reflex, belief, ground track and trails; the area skills `scan` and `search`; the onboard plan runner `run_plan` (with `get_plan_status`, `wait_for_plan`, `cancel_plan`, the `plan_finished`/`plan_log` events and the TypeFly-style plan grammar); `get_lost_target` and `retarget`; re-identification of lost tracks and the validation grace window; target profiles and the embedding/tiny-head recognition modes (`set_target_profile`, recognition ladder steps 1–2); HORUS's own flight envelope (geofence polygon and inset, altitude band, leg-length clamps), the `set_speed` cap and the turn-rate maximum, which ArduPilot's fences, avoidance and yaw limits replace. Until then the must-have demo was "follow that person" (`follow(id)` surviving a 2 s occlusion); it is now "keep that person in view" with base moves and `point`.
