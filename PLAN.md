# HORUS plan

The target architecture is the diagram in [`HORUS — Design Document v2.md`](<HORUS — Design Document v2.md#principles-and-architecture>),
approved on 2026-10-31. Its "Built and planned" table lists what is still missing. This file says in
what order to build it and how each phase counts as done. Update the status lines when a phase moves.

## Blocks

The block codes are stable (`~/.omp/agent/easify/ezcode`), so a code pasted in chat always means the
same block.

| Code | Block | Path | One line |
| --- | --- | --- | --- |
| `#63bb` | Control app | `horus/commander/web/`, `horus/repl/` | Where you give missions: a web page or the terminal; emergencies skip the AI |
| `#61d9` | AI pilot | `horus/commander/` | One chat per mission with the AI model; HTTP API for apps; serves the web page |
| `#5c06` | Crew | `horus/commander/crew.py` | Members the commander asks (planner first), each its own chat and drone board role token |
| `#a581` | AI model | outside | Claude Code, Anthropic or OpenAI; picks the next single move |
| `#03a1` | Drone board | `horus/mcp_server/` | The tool server on the drone's computer; the AI's only control panel |
| `#a3f1` | Flight moves | `horus/executor/` | take off, go to, move, turn, look, hold, land, home; one at a time |
| `#31b9` | Camera vision | `horus/perception/` | Detects, numbers and tracks things in the camera picture |
| `#9295` | Grounder | `horus/grounder/` | Finds things by description; called by camera vision |
| `#d27f` | Autopilot link | `horus/flight/` | MAVLink to ArduPilot; velocity setpoints only |
| `#12e6` | ArduPilot | outside | Stabilization, proximity avoidance, failsafes, RC override; no geofence |
| `#71dd` | Test world (Gazebo) | `sim/` | Gazebo + ArduCopter SITL; heavy runs on Koko |
| `#85c8` | Test world (GTA V) | `gta/` | GTA V, the quad always flown in the game with collisions (JSON physics) |

## Rules every phase keeps

- The AI chooses one base move at a time; no skill chases a target or sweeps an area by itself.
- HORUS checks only its inputs and sends velocity setpoints only. No world, and no real drone, has
  a geofence: no boundary, altitude ceiling or floor (removed 2026-11-02 at the user's request).
- Emergency commands (hold, land, home) never wait for the AI, in every control app, and the AI
  cannot undo them: its flight tools stay refused until the operator's next mission.
- If the AI goes quiet, the drone holds, then returns home (`executor.link_loss_rth_delay_s`).
- One test world per run: Gazebo or GTA V, never both.
- Live runs and test suites run on Koko (see `CLAUDE.md`).

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

## How to run a phase as a loop

Use a loop only once a phase is split into many atomic, testable items:

1. `/seref_loop <goal>` checks the claims and writes `LOOP_<SLUG>.md` from the seref_loop template.
2. `/seref_manager run LOOP_<SLUG>.md --max N` works through it; the manager reviews each iteration.
3. `seref-loop-gate LOOP_<SLUG>.md` exits 0 only when every item is ticked with evidence.

`LOOP_DIAGRAM_ALIGN.md` is the worked example of the ledger format and the Koko test protocol.
