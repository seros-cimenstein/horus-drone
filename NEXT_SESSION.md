# Notes for the next session (written 2026-10-29, updated 2026-11-03)

- 2026-11-03: phase 7 decided with the user and written as `LOOP_GTA_CREW_PLANNER.md` (21 items, not
  run): a GTA gimbal with pitch and pan, a whole-map GTA height survey at 5 m, a `plan_route` drone
  board tool (fixed-altitude and climb-over modes) and the crew with the planner as its first member
  (own chat, own drone board role token). Design: design document "Crew, GTA gimbal and GTA route
  planner"; the diagram gained the Crew block `#5c06`. Run it with
  `/seref_manager run LOOP_GTA_CREW_PLANNER.md --max 42`.
- 2026-11-02: `sim/run.sh start --gta` is always GTA's physics mode (collisions and ToF avoidance
  on); pose mode, `horus-gta-bridge` and `--json-physics` are gone, and `config/horus_gta.yaml` has
  no gimbal, so `look` is refused in GTA until a gimbal is built. HorusGta.dll's unused pose
  receiver (`PoseProtocol.cs`, `PoseLink.cs`, `[pose]` bind/port) goes at the next mod rebuild.
- 2026-11-02: ArduPilot geofence removed everywhere at the user's request (no circle, ceiling or
  floor in any world or for a real drone); the indoor drone can now reach the ceiling.
- 2026-11-02: Koko runs two X screens (`scripts/koko/display/`): `:0.0` the laptop panel, off by
  default (Ctrl+Alt+] switches only the panel), `:0.1` the HDMI dummy for simulations, LightDM
  autologin. Proven: autologin and the panel-off login entry, GTA started with Steam on `:0.1`,
  frames reached HORUS (12 m take-off frame of the plaza), the panel stayed off and the dummy on.
  Not yet proven: a Gazebo `--gui` window on `:0.1`, and the 1920x1080 login entry for the dummy
  (it came up at 3840x2160; set by hand this time).
- 2026-11-02: the control app shows what the AI saw: pictures the AI's tools returned to the model
  are kept in the service (newest 30, 8 MB) and fetched by id (`/api/ai-view/<id>`); feed
  thumbnails on laptop and phone, a phone Camera tab with history and an optional Live toggle.

## Next step (2026-11-01)
- The phone app is built and installed (2026-11-01, `PLAN.md` phase 6, ledger `LOOP_PHONE_APP.md`):
  the same page in a phone view over the tailnet (`scripts/phone_serve.sh start` →
  `https://<laptop>.<tailnet>.ts.net/?view=phone#token=<phone token>`; tailnet HTTPS
  certificates are now enabled), its own token `HORUS_COMMANDER_PHONE_TOKEN`, no camera, the AI's
  commands folded into "Show commands (N)", the static street map stored on the phone (IndexedDB,
  ETag/304) with only ~200-byte live updates. Installed on the user's Galaxy XCover Pro through
  Brave ("Install app"); the phone is reachable over adb at `100.64.0.2:5555`. Open: the user's
  own GTA test (ledger E1/E2), the `demo.sh --phone` Ctrl-C cleanup smoke (D1), and the camera
  questions in the design document ("Phone app design").
- Fixed 2026-11-01: "Claude Code commander exceeded timeout_s=900". Claude Code folds a message
  that arrives during a turn into that turn (each turn starts with one queued message), so one
  `result` can answer several messages; `ClaudeCodeSession` had matched results to messages one
  to one, so every event folded into a turn left a reply waiting out `commander.timeout_s`, then
  the mission was reset. It now answers a turn's starting message at its result and the folded
  ones 2 s later unless a new turn starts (`_resolve_turn`). E3 had also made the commander's own
  HOLD/LAND/HOME wake the model twice (the event's source became `operator`); the pump now skips
  the echo of this commander's own operator emergencies, while another app's still wake it.
- The architecture diagram is approved, and the docs use its names (Control app, Drone board).
  `PLAN.md` holds the phases: 0, 1, 2 (control app) and 3 (Codex) are done.
- The old Gazebo commander panel is gone. HORUS's own control app is built: a web page served by
  the AI pilot service (`horus/commander/web/`). `scripts/demo.sh` starts the service, which prints
  `http://127.0.0.1:<port>/#token=<token>` (the token travels in the URL fragment); `--terminal`
  opens the `horus` terminal instead, never both. On this laptop an unrelated service holds 8765:
  use `HORUS_COMMANDER_URL=http://127.0.0.1:8766` (by hand: `--port 8766`). Screens: mission box,
  news feed, camera, drone state, map, HOLD / LAND / HOME. The page's reads are passive drone board
  calls made by the service (`/api/state`, `/api/frame`, `/api/map/static`, `/api/map/live`), so
  they never count as AI contact. HOLD / LAND / HOME lock out the AI's flight tools until the
  operator's next mission (`operator_emergency` / `operator_release` on the drone board). The map
  (laptop and phone, 2026-11-01) pans and zooms like a street-map app and has Mission Planner-style
  selections: points P1.. and polygon/rectangle areas A1.., with two-tap "Fly here" / "Search area"
  quick missions through the AI; a mission naming P1/A1 carries their coordinates. Proven in GTA:
  "Fly here" on P1 made the AI take off and `goto(14.1, 39.1)`, P1's exact position. User guide:
  root README, "The control app".
- Koko: the 44 GB offline Wikipedia was deleted (45 GB free), and the simulator images were rebuilt
  without the panel. The log fetch now copies only the current session's files.
- Phase 2 is done (2026-11-01): `LOOP_CONTROL_APP.md` reads `STATUS: DONE`. E1 proved it in Gazebo
  (AI mission, HOLD during a model turn, HOME, and the hold-then-home failsafe with the page
  polling), E2 in GTA V with live game frames, E3 added the operator lock after the AI undid a
  HOME in the GTA run. Koko's GTA quirks (backlight guard, the game always on the laptop panel,
  `xwd` failing) are in `CLAUDE.md`.
- `AGENTS.md` points Codex and other agents to `CLAUDE.md`.

## Diagram-alignment loop (2026-10-29)

The diagram-alignment loop is finished: `LOOP_DIAGRAM_ALIGN.md` reads `STATUS: DONE`, all 33 items
are ticked with evidence, and `seref-loop-gate LOOP_DIAGRAM_ALIGN.md` exits 0. The ledger's bottom
summary has the full picture, and `LOOP_DIAGRAM_ALIGN.manager.md` holds the manager's decisions.

## State of HORUS
- Only the base moves reach the AI (take_off, goto, move, turn, look, hold, rth, land), plus the
  vision, map and task tools: 29 MCP tools. Approach/follow/orbit/scan/search, the plan runner,
  lost-target guessing, trails and target profiles are gone.
- HORUS sets no flight limits, and no world has a geofence. Proximity avoidance (`common.parm`)
  and ArduPilot's failsafes remain; the tool texts say so.
- Gazebo or GTA, one at a time: `sim/run.sh` refuses to start a second world.
- Typed emergencies in the control app's terminal (`/hold`, `/rth`, `/home`, `/land`, `stop`, `go home`) skip the AI.
  Each one is logged to the event log with `source: operator`.
- All 37 `tests/test_*.py` files pass on Koko (768 tests).

## Last session's changes (iteration 30)
- `config/ardupilot/common.parm`: `PLND_ENABLED 0`. The gazebo-iris SITL defaults turned on
  ArduPilot's precision landing with no target source, which made every LAND hover for 7 s.
  HORUS's own AprilTag landing is unaffected.
- Indoor test points: NED `(n, e)` is Gazebo world `(e, n)`.
  - NED `(-2, 2)` is above the `person_laying` patient mesh. LAND hits it and then stays armed on
    the floor.
  - For an off-pad landing test, use NED `(2, -3)`.
  - ArduPilot resets home at every arming, so re-arm on the pad before testing `rth`.
- `sim/README.md`: the GTA smoke steps now build a scratch map and config under
  `sim/run/gta-scratch-map/` whenever `data/maps/gta/map.json` is missing (it always is on Koko,
  because rsync leaves out `data/`). The steps delete the scratch directory at the end.
- `horus/repl/app.py`: logs operator emergencies. `event_log_from_environment()` moved into
  `horus/event_log.py`.
- MCP tool texts (`horus/mcp_server/flight.py` and others) no longer promise HORUS clamps.

## Open items
- **The H2 game-frame step was never run.** Koko had no signed-in Steam client, and every display
  output was off (DPMS "Monitor is Off"; `xset dpms force on` did not help). To finish it:
  1. On Koko, select the Steam account and turn the screen on.
  2. From the laptop, run `scripts/koko/gta_game.sh start`.
  3. Run the `sim/README.md` GTA smoke steps; `client list_candidates` should then return a
     numbered frame.
- `scripts/koko/remote_fetch_logs.sh` was fixed on 2026-10-31: it now copies only the files in
  Koko's `sim/logs/` and `runs/` written since the last `remote_up.sh`, not all of `runs/`
  (4.9 GB). It is safe to run again.
- **Koko's root disk has about 1.2 GB free.** `start_sim.sh` stops Gazebo below 1 GB. The old
  `~/horus/sim/run/*` directories, about 2.5 GB, could be pruned after you review them.
- These files were not committed on purpose:
  - `docs/course/`, which includes the 36 MB `Deliverables.zip`;
  - the 18 MB `omp-session-*.html` session export.

  Add them yourself if you want them in git.
