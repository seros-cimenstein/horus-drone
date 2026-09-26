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
- **Safety is deterministic.** Avoidance, geofence and RTH never depend on a model.
- **The AI never sends raw motor or velocity commands.** Only bounded skills with clamped speeds and distances.
- **Simulation first.** Every feature runs in SITL + Gazebo before real flight.

```mermaid
flowchart TD
  R["REPL console<br/>terminal / web / voice"] --> C["Cloud commander<br/>frontier model"]
  C -->|MCP over tunnel| M["Onboard MCP server"]
  C -.fallback.-> G["Grounding model<br/>cloud or ground-station GPU"]
  C -.on demand.-> T["Training service<br/>cloud GPU + TIDL compile"]
  M --> E["Local executor<br/>skills, tracking, latency comp"]
  E --> P["Perception<br/>4 TOPS accelerator"]
  E --> A["ArduPilot<br/>avoidance, geofence, RTH"]
  A --> S["Sensors<br/>camera, flow, ToF"]
```

The commander plans and reports; the grounding model is called only when onboard proposals miss the target; the training service is called only when the recognition ladder needs a specialist (Section: Specialized recognition).

| Tier | Runs | Rate | Examples |
| --- | --- | --- | --- |
| Cloud commander | Frontier model (Claude / GPT-class / Qwen) | ≤ 1 Hz, event-driven | Plan, pick target ID, ask operator, report |
| Ground-station edge (optional) | Laptop GPU on the same LAN | 3–10 Hz | Grounding DINO / Florence-2 fallback pointing |
| Onboard executor | Gemstone A53 cores + accelerator | 10–30 Hz | Tracking, ReID, approach, orbit, scan |
| Flight control | ArduPilot on R5F cores | 400 Hz | Stabilization, avoidance, failsafes |

Executor state machine: `IDLE → EXECUTING → HOLDING → OFFLINE_MISSION → RETURNING_TO_LINK → RTH`. Every command has a time-to-live; when it expires with nothing fresh, the executor finishes the current safe action and holds.

## Scope tiers

The must-have core alone answers the research question; everything else is added only once the core flies reliably.

| Tier | Includes | Demo scenarios |
| --- | --- | --- |
| **Must-have core** | Dual-system loop, set-of-mark grounding, latency compensation, safety layer, bounded MCP tools, REPL, recognition ladder steps 1–2 (embeddings, tiny head) | Find the person in the red shirt · Follow that person with occlusion · Link cut with no package → hold → RTH → AprilTag landing |
| **Stretch** | Agent-triggered YOLOv8n fine-tune (ladder step 3), offline mission packages + store-and-forward, one thermal module, ground-station edge tier | Search room for someone lying down · Link cut mid-mission → continue → return to link zone → sync · Ambiguity: drone asks before acting |
| **Future work** | Full module manager with DroneCAN auto-discovery, local LLM for offline decisions, VIO, multi-drone, outdoor/GPS | — |

Scripted, replayable REPL missions are part of the core because every experiment depends on them.

## Real-time grounding and latency

The cloud picks a tracker ID, not a pixel box, so a late answer is still correct and the frontier model never outputs coordinates.

### Primary: set-of-mark ID pointing

1. Onboard, a class-agnostic detector proposes candidates and the tracker assigns persistent IDs.
2. The drone sends a timestamped, downscaled frame (e.g. 640 px JPEG) with numbered marks drawn on each candidate, plus a small table of IDs, boxes and attributes.
3. The commander answers with an ID ("approach ID 3 to 2 m").
4. The tracker has kept ID 3 alive the whole time, so the executor acts on the current position immediately.

### Fallback: grounding model

Used only when the target is not among the proposals (unusual object, small, partly hidden). The commander calls the pointer with the frame ID and a description, and the executor brings the returned box up to date:

- **Fast-forward tracking:** a 1–3 s ring buffer of frames onboard; initialize the tracker on the old frame and replay the buffer to the present.
- **Embedding re-match:** crop the returned box from the old frame, embed it, and match against current detections. Robust when the target moved a lot.
- **Check on arrival:** if the updated box matches nothing above a confidence threshold, discard it and re-query with a fresh frame.

### Reducing latency itself

- **Slow down while waiting:** with a query pending, the executor hovers or caps speed, so there is less to compensate.
- **Small payloads:** compressed low-res frames or region crops only.
- **Ground-station edge tier:** run the grounding model on a laptop GPU on the same LAN; the commander stays in the cloud.
- **Event-driven queries:** call the cloud when tracking confidence drops, a skill finishes, something unexpected appears, or the executor is unsure; never on a timer.

Every frame carries frame ID + timestamp + drone pose, and every observation sent to the cloud states its age.

## Specialized recognition

The manager agent climbs a three-step ladder and stops at the first step that works; fine-tuning is the last resort, not the default.

| Step | Method | Time to ready | Where it runs | Best for |
| --- | --- | --- | --- | --- |
| 1 | Class-agnostic detector + embedding match against reference crops | Seconds | Onboard, no training | A specific person (ReID / face), a specific object |
| 2 | Tiny classifier head on frozen-backbone embeddings, 20–100 crops | Seconds to 1 min | Onboard CPU or cloud | A small set of look-alike objects |
| 3 | Fine-tuned YOLOv8n, auto-labeled | \~15–45 min | Cloud GPU + TIDL compile | A *type* of object or shape (valve model, crack, debris), long offline missions |

For a specific person, stay on step 1: fine-tuning YOLO on one person from a few photos teaches "person", not "this person". Identity is an embedding problem.

### Step 3 pipeline (MCP tool `train_specialist`)

```mermaid
flowchart LR
  A["Collect<br/>refs + drone keyframes"] --> B["Auto-label<br/>Grounding DINO / SAM"]
  B --> C["Augment<br/>scale, blur, light"]
  C --> D["Fine-tune<br/>YOLOv8n"]
  D --> E["Validate<br/>held-out frames"]
  E --> F["ONNX → TIDL<br/>calibrate on real frames"]
  F --> G["Sign + load_model<br/>shadow mode"]
```

The commander calls `train_specialist(description, reference_images)`, which returns a `task_id`; the job runs in the cloud and the drone keeps working on step 1 or 2 meanwhile.

- **Frozen architecture:** one YOLOv8n configuration is proven once to compile on TIDL with no CPU fallback layers; afterwards only weights change, so compilation is predictable.
- **Data from the drone's own camera:** recent keyframes are included so the training data matches the real viewpoint and sensor.
- **Quality gate:** below a set mAP on held-out frames, the model is rejected and the agent falls back to embeddings.
- **Shadow mode:** the specialist runs next to the generic detector for a short check before its candidates are trusted.
- **Safe loading:** mode switches only while hovering; a specialist only proposes candidates and never touches flight or avoidance.

In simulation, step 3 is testable end to end except the TIDL compile, which is validated once on the real board.

## Disconnected autonomy (stretch)

While connected the brain prepares the drone; while disconnected the drone executes and records; on reconnect the brain and operator confirm.

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

A bad AI decision cannot produce a dangerous maneuver: the executor and ArduPilot clamp every speed, distance and geofence, and emergency tools bypass the task queue.

- **Collision avoidance:** ToF ring (4–6× VL53L1X) feeding ArduPilot proximity (`PRX_*`, `AVOID_ENABLE`). Monocular depth may add awareness but is never the only safeguard.
- **Verify avoidance in Guided mode first.** The executor drives the drone in Guided mode, and avoidance behaves differently per flight mode. Milestone S3 in the simulation plan proves the proximity sensors stop executor-issued motion before anything else is built on it.
- **Odometry and RTH:** optical flow + downward rangefinder into EKF3, home set at takeoff, AprilTag pad for precise landing, breadcrumb retracing as backup.
- **Failsafes:** cloud link lost → mission package if any, else hold → return to link zone → RTH. Low battery → RTH with enforced reserve. Hardware RC override always available.

| Category | Tools |
| --- | --- |
| State | `get_state`, `get_frame`, `list_candidates`, `get_map`, `get_link_status` |
| Skills | `take_off`, `goto(pose)`, `follow(id)`, `orbit(id)`, `scan(area)`, `approach(id, dist)` |
| Grounding | `point(frame_id, description)` (fallback) |
| Recognition | `set_target_profile(profile)`, `train_specialist(desc, refs)`, `load_model(package)`, `set_mode(name)` |
| Tasks | `get_task_status(task_id)`, `wait_for_task(task_id, timeout_s)`, `cancel(task_id)` |
| Missions (stretch) | `upload_mission`, `start_mission`, `get_mission_log`, `clear_mission` |
| Emergency | `hold`, `rth`, `land` |

Skills take tracker IDs rather than pixel regions. Long actions return a `task_id` immediately. Every state and frame response is timestamped. The server is authenticated, reached only through an encrypted tunnel, and built on the Python MCP SDK over streamable HTTP, with pymavlink or MAVSDK to ArduPilot.

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

### World and sensors

- **Indoor world:** a room and corridor with furniture, a textured floor and an AprilTag pad; Gazebo actors (animated people) in different shirt colors, one lying on the floor.
- **GPS-free flight:** disable GPS and fly on SITL's simulated optical flow + downward rangefinder into EKF3. Start with GPS on for milestones S1–S2, then switch. Check exact `FLOW_*`, `RNGFND*`, `EK3_SRC*` and `SIM_*` values against current ArduPilot docs.
- **ToF ring:** 4–6 short-range Gazebo lidar sensors, read by a small bridge that sends MAVLink `DISTANCE_SENSOR` messages, with ArduPilot proximity set to the MAVLink type.
- **Perception in sim** runs on the dev PC (PyTorch / ONNX). Cap its frame rate to what the Gemstone accelerator is expected to sustain, so behavior does not depend on desktop-class compute.

### Milestones

| # | Milestone | Done when |
| --- | --- | --- |
| S0 | Toolchain | Iris flies `takeoff 5` in `iris_runway.sdf` |
| S1 | MAVLink control | Python script (pymavlink or MAVSDK) takes off, flies a square, lands in Guided mode |
| S2 | MCP server + REPL | `/state`, `/goto`, `/hold`, `/rth` work from the REPL; skills return `task_id`s |
| S3 | Safety proof | Indoor world + ToF bridge: executor-issued `goto` toward a wall is stopped by proximity avoidance in Guided mode |
| S4 | GPS-free | Same missions on optical flow + rangefinder; RTH lands within a set error on the AprilTag pad |
| S5 | Perception + tracking | Camera stream → detector → tracker with persistent IDs; `follow(id)` survives a 2 s occlusion |
| S6 | Cloud loop | Set-of-mark frames to the commander; "find the person in the red shirt" succeeds end to end |
| S7 | Latency | Frame buffer, fast-forward tracking, embedding re-match; latency sweep 0–3 s with `netem` |
| S8 | Stretch | Mission package + link cut; `train_specialist` produces a model the executor loads in shadow mode |

Use `SIM_SPEEDUP` and headless Gazebo runs for batches of repeated trials once scripted REPL missions work.

## Experiments

Two headline experiments answer the research question directly; the rest support them and are run only as time allows.

| Priority | Experiment | Measure |
| --- | --- | --- |
| Headline | Latency sweep: 0, 0.5, 1, 2, 3 s added delay, with vs. without compensation | Task success rate, time to target, wrong-target rate |
| Headline | Cloud-only (LLM sends goto/velocity directly) vs. HORUS dual-system | Success rate, collisions, blocked unsafe commands |
| Secondary | Set-of-mark ID pointing vs. grounding-model boxes | Success rate, latency, pointer errors |
| Secondary | Tracking re-acquisition after occlusion | Re-acquisition rate, time to re-acquire |
| Secondary | Recognition ladder: embeddings vs. tiny head vs. fine-tuned YOLOv8n | Precision/recall, time to ready |
| Secondary | Generalization to target objects and descriptions not used during development | Success rate |
| Secondary | Commander model comparison on identical scripted missions | Success rate, API cost, latency |
| Secondary | RTH accuracy with vs. without AprilTag | Landing error (cm) |
| Stretch | Offline mission: detection and false-match rate offline vs. online; data recovered on sync | Rates, time to reconnect |

Every experiment runs as a scripted REPL mission, N ≥ 20 trials per condition in simulation and a smaller confirming set on hardware.

## Hardware and risks

The target board is the T3 Gemstone O1 (TI AM67A: 4× A53 for Linux, 2× R5F for ArduPilot, 4 TOPS accelerator, 4 GB RAM) on a 5–7" frame with prop guards; the default-core BOM from v1 (\~$785–1,190, or \~$510–915 with a free board) is unchanged.

| Risk | Mitigation |
| --- | --- |
| Gemstone ecosystem is new; ArduPilot on R5F is less proven than a Pixhawk | Fixed go/no-go date for switching to a Pixhawk + Raspberry Pi fallback (see Timeline) |
| TIDL operator support | Prove the frozen YOLOv8n and tracker/ReID models compile early, before any training |
| Indoor optical flow drift on plain floors | Textured floor mats, AprilTag landing, breadcrumb retracing |
| ReID trained on eye-level footage fails from above | Early test on drone-view footage; fall back to attributes + fine-tuning if needed |
| Avoidance does not act on Guided-mode motion | Milestone S3 proves it in simulation before hardware |
| Loud, large drone in a small room | Netted test area, props-off and tethered tests first, low speed caps |
| Wi-Fi instability | Command TTL, hold-by-default, failsafes |
| API cost and latency | Event-driven queries, low-res frames, ground-station edge tier |
| False matches offline | Conservative thresholds, confirmation after sync |
| Privacy | Mission-scoped profiles, deletion after mission, encrypted logs |

## Timeline and success criteria

**Minimum viable result:** in simulation and on real hardware, "find the person in the red shirt" and "follow that person" succeed reliably at up to 2 s added latency, with compensation clearly beating no compensation, and zero collisions caused by AI commands.

The timeline is in weeks from the start of simulation work; real dates depend on the submission deadline (open question).

| Weeks | Phase | Milestones |
| --- | --- | --- |
| 1–2 | Toolchain and control | S0, S1 |
| 3–4 | MCP server, REPL, safety proof | S2, S3 |
| 5–6 | GPS-free flight, perception, tracking | S4, S5 |
| 7–9 | Cloud loop and latency compensation | S6, S7 |
| 8 | **Hardware go/no-go:** Gemstone flies stable hover with flow + ToF, or switch to fallback | Props-off, tethered, low-altitude tests |
| 10–12 | Indoor flights of the core scenarios | Core demos on hardware |
| 12–14 | Stretch goals if the core is solid | S8, thermal module |
| 14–16 | Experiments and write-up | Headline experiments, thesis, demo video |

Hardware bring-up runs in parallel from week 5, so the go/no-go in week 8 is a real decision, not a surprise.

| Scenario | Success criterion |
| --- | --- |
| Find the person in the red shirt | ≥ 80% correct target over 20 sim trials; report names the right location |
| Follow that person | Re-acquires after a 2 s occlusion in ≥ 80% of trials |
| Link cut, no package | Holds, returns home and lands within a set distance of the AprilTag center in every trial |
| Unsafe command injected | Blocked or clamped in 100% of trials |

- [ ] Confirm the submission deadline and team size, then turn weeks into dates

## Related work

HORUS differs from prior LLM-drone work by combining ID-based grounding, onboard latency compensation, a deterministic ArduPilot safety layer and disconnected autonomy on real hardware.

| Project | Summary | Gap HORUS addresses |
| --- | --- | --- |
| [TypeFly](https://arxiv.org/abs/2312.14950v2) (Yale, 2023–24) | A cloud LLM writes programs in a small custom language (MiniSpec) that a drone executes, with edge-based vision; the revised version targets LLM response latency | Closest prior art. HORUS supervises goals continuously instead of generating a program, grounds by tracker ID, and adds an ArduPilot safety layer and offline autonomy |
| ChatGPT for Robotics (Microsoft, 2023) | ChatGPT generates Python code to fly a simulated drone toward an object, given skills and the object's precise location | Simulation only; object location given; no real-time layer |
| CognitiveDrone / -R1 (Skoltech, 2025) | 7B VLA trained on simulated trajectories; R1 adds a Qwen2.5-VL reasoning module | Simulation only, narrow task, too heavy for edge; dropped from the plan |
| EchoPilot | Voice → local LLM plans a mission → MCP tools over MAVSDK (PX4) | Upfront planning, GPS waypoints, no vision loop or latency handling |
| mavlink-mcp | MCP server letting an LLM fly ArduPilot and see through its camera, SITL-focused | No onboard executor (verify hardware claims before submission) |
| ardupilot-mcp | Safety-gated MCP server for ArduPilot, actuation off by default | Good safety reference; no perception or execution layer |
| LLM-controlled-drone | Local LLM + YOLO feedback controlling PX4 in Gazebo via ROS 2 | Simulation, fixed-class perception |

Techniques HORUS builds on: dual-system control (Figure's Helix, NVIDIA GR00T N1), set-of-mark visual prompting, supervisory control for delayed teleoperation, and Grounding DINO / Florence-2 for open-vocabulary grounding.

- [ ] Verify every entry above against the original papers and repos before submission
