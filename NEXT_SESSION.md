# Notes for the next session (written 2026-10-29)

The diagram-alignment loop is finished: `LOOP_DIAGRAM_ALIGN.md` reads `STATUS: DONE`, all 33 items
are ticked with evidence, and `seref-loop-gate LOOP_DIAGRAM_ALIGN.md` exits 0. The ledger's bottom
summary has the full picture, and `LOOP_DIAGRAM_ALIGN.manager.md` holds the manager's decisions.

## State of HORUS
- Only the base moves reach the AI (take_off, goto, move, turn, look, hold, rth, land), plus the
  vision, map and task tools: 29 MCP tools. Approach/follow/orbit/scan/search, the plan runner,
  lost-target guessing, trails and target profiles are gone.
- HORUS sets no flight limits. ArduPilot fences bound every world (`config/ardupilot/*.parm`), and
  the tool texts say so.
- Gazebo or GTA, one at a time: `sim/run.sh` refuses to start a second world.
- Typed Terminal emergencies (`/hold`, `/rth`, `/home`, `/land`, `stop`, `go home`) skip the AI.
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
- **Do not run `scripts/koko/remote_fetch_logs.sh` as it is.** It copies all of Koko's
  `~/horus/runs/` (4.9 GB) into the laptop's `runs/`, and it filled the laptop disk once; I reverted
  that copy. Check logs on Koko directly, or change the script to fetch only `sim/logs/` and the
  current run.
- **Koko's root disk has about 1.2 GB free.** `start_sim.sh` stops Gazebo below 1 GB. The old
  `~/horus/sim/run/*` directories, about 2.5 GB, could be pruned after you review them.
- These files were not committed on purpose:
  - `docs/course/`, which includes the 36 MB `Deliverables.zip`;
  - the 18 MB `omp-session-*.html` session export.

  Add them yourself if you want them in git.
