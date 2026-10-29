# Loop ledger: reshape HORUS to the approved architecture diagram (2026-10-29)

STATUS: DONE

Source: the third (approved) `/diagramify` SVG in the 2026-10-29 session, plus the user's corrections on the two earlier versions. Its codes: You -> Terminal `#63bb` (`horus/repl/`) -> AI pilot `#61d9` (`horus/commander/`); Drone remote `#03a1` (`horus/mcp_server/`); Flight moves `#a3f1` (`horus/executor/`), ONLY take off · go to · move · turn · tilt camera (`look`) · hold · land · home (`rth`), one move at a time; Camera vision `#31b9` (`horus/perception/`); Autopilot link `#d27f` (`horus/flight/`); Test world: Gazebo `#71dd` (`sim/`) OR GTA V `#85c8` (`gta/`), never both, with ArduPilot `#12e6`. The user's corrections: approach/follow/orbit/scan/search and "guesses where a lost person went" are gone; "no settings on board", because ArduPilot already handles collisions, limits and failsafes; Gazebo and GTA are strict alternatives. Every finding was re-checked on 2026-10-29 by five read-only scouts (transcripts `history://ExecutorSkills`, `history://LimitsLayer`, `history://PerceptionScope`, `history://SimSeparation`, `history://DecideLayerDocs`), and the high-severity claims were re-read by hand. Baseline: all 41 `tests/test_*.py` files pass on Koko (`test_executor.py` 135 passed, `test_worldgen_procedural.py` 132 passed in 138 s; total wall time 249 s). This file is the loop's memory and its stop switch: the gate `~/projects/seref_omp_tools/seref_loop/bin/seref-loop-gate LOOP_DIAGRAM_ALIGN.md`
(run by `/seref_manager run LOOP_DIAGRAM_ALIGN.md` or `/loop --until`) stops the loop only when the line
above reads `STATUS: DONE`, no `- [ ]` is left, every `- [x]` carries `DONE:`/`INVALID:` evidence and
every `- [!]` a `BLOCKED:` reason.

## Protocol (every iteration)

1. Read this file. Take the first `- [ ]` item in order (earlier sections first; respect
   deadlines and the dependencies named in items).
2. Before editing, read the code the item names and re-check that the finding still holds. Already
   fixed or wrong: mark `- [x] … — INVALID: <evidence>`.
3. Fix it at the root with the project's existing patterns; migrate every caller; update the docs
   that describe the changed behavior in the same step.
4. Verify: tests run on Koko only. The laptop has no `horus-sim` image, and `.venv-host` has no pytest or pymavlink. `pytest-timeout` is not installed, so run each affected file on its own under a shell `timeout`. Step 1 syncs the working tree to the Koko test copy `~/horus-loop`, which is not the deploy copy. Koko's root disk is nearly full: start_sim.sh stops Gazebo below 1 GB free. So the sync leaves out build caches, docs and generated worlds, none of which a test reads: `rsync -a --delete --exclude='/.git/***' --exclude='/.venv-host/***' --exclude='/**/__pycache__/***' --exclude='/**/.cache/***' --exclude='/.pytest_cache/***' --exclude='/runs/***' --exclude='/sim/logs/***' --exclude='/sim/run/***' --exclude='/sim/generated/***' --exclude='/models/***' --exclude='/data/***' --exclude='/docs/***' --exclude='/gta/build/***' --exclude='/gta/horus_mod/build/***' --exclude='/gta/horus_mod/physics_sim/bin/***' --exclude='/gta/horus_mod/physics_sim/obj/***' --exclude='/omp-session-*.html' ./ koko:horus-loop/`. Check `ssh koko df -h /` before a live smoke; it needs well over 1 GB free. Step 2 runs one file: `ssh koko 'cd horus-loop && timeout 900 docker run --rm -v "$PWD":/horus -w /horus --entrypoint /opt/horus-venv/bin/python horus-sim:local-s1s2-gui -m pytest -q -p no:cacheprovider tests/<file>.py'`. Full suite: the same command inside `for f in tests/test_*.py; do …; done`, with every file expected to pass. After every Python change, also run `grep -rn` for the removed names across `horus/ tests/ sim/ scripts/` and expect no hits. Add a regression test when the bug can be shown in one. Smoke the changed path where it really runs, on Koko (check first with `ssh -o BatchMode=yes -o ConnectTimeout=10 koko true`):
   - **Gazebo:** `export HORUS_MCP_TOKEN=$(openssl rand -hex 24); scripts/koko/remote_up.sh horus_indoor.sdf`. It syncs to `koko:horus`, starts SITL, Gazebo, the grounder and the drone server, and tunnels it to local port 8000. Call tools inside the container with `ssh koko "docker exec -e HORUS_MCP_TOKEN=$HORUS_MCP_TOKEN -e HORUS_MCP_URL=http://127.0.0.1:8000/mcp -w /horus horus-sim /opt/horus-venv/bin/python -m horus.mcp_server.client <tool> --args '<json>'"` (`list-tools`, `get_state`, `take_off`, `goto`, `wait_for_task`, `land`, …). Read ArduPilot params through `docker exec horus-sim` with pymavlink on TCP 5762. Stop with `scripts/koko/remote_down.sh`. Logs come back with `scripts/koko/remote_fetch_logs.sh`.
   - **GTA, manual:** `rsync` the tree, including `data/maps/gta/`, to `koko:horus`. Then on Koko run `HORUS_SIM_IMAGE=horus-sim:local-s1s2 ./sim/run.sh start --gta`. Pose mode works without the game for SITL, bridge and param checks. For frames, run `scripts/koko/gta_game.sh start` from the laptop; it needs Steam, an enabled display and no screen lock. Start the server with `printf '%s\n' "$HORUS_MCP_TOKEN" | bash scripts/koko/start_mcp.sh horus-sim`. Stop with `./sim/run.sh stop` and `scripts/koko/gta_game.sh stop`.
5. Deploy: none. HORUS runs only in simulation, and a smoke run is the deployment. Before any start, check `ssh koko 'docker ps; pgrep -af "[g]z sim|[G]TA5_Enhanced"'`. Never start a world while the other world, or another `horus-sim` container, is running: running both overloads Koko. Never run Gazebo/SITL episodes on the laptop (RTF 0.2-0.35 against 0.87 on Koko). Do not touch Koko's sysctls (HANDOFF.md asks the user to restore them). Always end a smoke with `remote_down.sh` or `run.sh stop`, so no container is left running.
   Back up any DB before a schema change or backfill.
6. Edit this file: `- [ ]` -> `- [x]` and append ` — DONE: <files>; <test/smoke evidence>`.
   One item per iteration is fine; finish an item completely or leave it `- [ ]`.
7. Blocked only for a user decision or an unreachable host/credential. For a decision, call the `ask`
   tool; nobody may be at the keyboard, so under `/seref_manager` the loop manager decides it for the
   user, and its answer is the user's decision (also where a rule reserves a choice for the user or
   operator): act on it instead of marking the item blocked. Never end a turn waiting for a reply.
   Mark a blocker `- [!] … — BLOCKED: <exact missing thing, what was tried>`. Difficulty, size or
   scope pressure is not a blocker.
   A `— MANAGER(itN): …` note is the loop manager's review: an item it reopened stays open until
   you fix what the note names and tick it again with new evidence; a decision in the note is the
   user's decision.
8. When no `- [ ]` is left: run every test suite and the final check, which is: all `tests/test_*.py` files pass on Koko (step 4), `grep -rnE 'approach|follow|orbit|\bscan\b|search|target_belief|TargetBelief|trails|TrailGrid|geofence|Envelope|run_plan|set_speed|get_lost_target|retarget' horus/` shows only intended survivors (each one listed in the summary), and H1-H3 are DONE. Then replace the STATUS line with
   `STATUS: DONE`, append a summary (done / invalid / blocked) at the bottom, then run the gate
   command above. It must exit 0; if it does not, fix what its message names before yielding.

Rules:
- Do not `git commit`, `git add`, `git stash`, `git checkout` or `git reset`. The tree has the user's uncommitted work: 226 `git status` entries on branch `fix/project-review-findings`, including staged deletions and new untracked modules. Never push.
- Do not touch `annotated.jpg`, `docs/course/`, `docs/paf/` (lecture deliverables), `omp-session-*.html`, `HANDOFF.md`, `models/`, `data/`, `runs/`, `.venv-host/`, or Koko's `~/horus-dev-worlds`, `~/horus-gta` and `~/horus-head`. `README.md` and `sim/README.md` carry held user edits (HANDOFF.md:7). Edit them only in section G, after the `ask` named there, and always on top of the working-tree text.
- No destructive commands on live data without a backup.
- Keep every kept base move, precision landing, the emergency path and HORUS's AI-link-loss hold-then-home working. The approved diagram's footer promises the last one: "If the AI goes quiet, the drone holds still, then flies home." ArduPilot's GCS failsafe cannot replace it, because HORUS sends no MAVLink heartbeat (no `heartbeat_send` under `horus/flight/`).

## A. Who decides: Terminal #63bb and AI pilot #61d9

- [x] A1 The Terminal still accepts the removed skills as typed commands (`horus/repl/app.py:58-65` parses `/approach`, `/follow` and `/orbit` into MCP calls; no test covers them). Delete the branch, so these lines fall through to the `return None` path like any other unknown slash command. Acceptance: `tests/test_repl.py` passes, with a new assertion that `_interactive_arguments("/follow 3")` is `None`. — DONE: `horus/repl/app.py` (deleted the `/approach|/follow|/orbit` branch, old 58-65; the start-up help at :99 never listed them); `tests/test_repl.py` (new `test_removed_target_skill_commands_are_not_mcp_calls` for `/follow 3`, `/approach 2 1.5` and `/orbit 4 2` -> `None`). On Koko `~/horus-loop`, image `horus-sim:local-s1s2-gui`: `test_repl.py` 18 passed (15 before + 3 new), including the subprocess REPL against a real MCP server; `test_commander_server.py` 15 passed. In the deployed venv, `_interactive_arguments` gives `/follow 3` -> None, `/goto 1 2 3` -> goto, `/take_off` -> take_off 2.0, `/hold` -> hold. Grep finds no other `/approach|/follow|/orbit` command use in `horus/ tests/ scripts/`; Gazebo's `/gui/follow` service is unrelated. README.md:286 still lists the three commands; it is held and listed in G1.
- [x] A2 The diagram's "hold / land / home commands skip the AI", but "home" and "go home" are not emergency words (`horus/commander/intents.py:5-14` has only `rth` and `return home`; `app.py:40` accepts only `/hold`, `/rth` and `/land`), so a bare `home` goes to the model. Add `home` and `go home` -> `rth` to `_EMERGENCY_WORDS`, and accept `/home` in `app.py:40`. The commander server shares this grammar (`commander/server.py:137`). Acceptance: tests in `tests/test_repl.py` show `emergency_action("home")`, `emergency_action("go home!")` and `_interactive_arguments("/home")` all map to `rth`; `test_repl.py` and `test_commander_server.py` pass. — DONE: `horus/commander/intents.py` (`home` and `go home` map to `rth`; the commander server's free-text path, `server.py:137`, uses the same grammar); `horus/repl/app.py` (`_SLASH_EMERGENCIES` adds `/home` -> `rth`; the start-up help lists `/home`); `tests/test_repl.py` (new cases `/home`, `/HOME`, `home`, `go home!`, `Go home now` -> rth); `tests/test_commander_server.py` (`home` and `go home!` -> rth; `go home after the scan` and `home is to the north` stay free text). On Koko: `test_repl.py` 23 passed and `test_commander_server.py` 15 passed. Smoke: a throwaway real REPL subprocess against a real MCP server with FakeFlight and a fake Claude Code; typing each of `/home`, `go home` and `home` during a running model turn sent `('rth',)` to the flight link and interrupted the model. The model saw only `[operator emergency] rth executed`, never the typed text (3 passed; the throwaway file was deleted). The live-stack check is H3. The README's REPL line (`:286`) is held for G1.

## B. Drone remote #03a1: only base moves reach the AI

- [x] B1 The drone server still offers the AI approach/follow/orbit/scan/search and the lost-target tools (`horus/mcp_server/flight.py:170-277` and the `register` tuple at `:295-302`; `horus/mcp_server/tasks.py:48-81` `get_lost_target` and `retarget`, registered at `:208`). Remove these 7 tools and their registrations. The executor internals stay until section C. `tests/test_mcp_startup.py:96` calls `follow`; switch it to a kept move. Acceptance: `test_mcp_startup.py` passes, and a Gazebo smoke `list-tools` shows none of the 7 names. — DONE: `horus/mcp_server/flight.py` (removed `approach`, `follow`, `orbit`, `scan`, `search` and their registrations); `horus/mcp_server/tasks.py` (removed `get_lost_target` and `retarget` and their registrations); `tests/test_mcp_startup.py:96` (`follow` -> `get_frame` in the perception-unavailable check). On Koko `~/horus-loop`: `test_mcp_startup.py` 16, `test_maps.py` 13, `test_scene.py` 19, `test_mcp_client.py` 5, `test_mcp_auth.py` 1, `test_plan.py` 22, `test_repl.py` 23 and `test_executor.py` 135 all passed. Grep finds no caller of the 7 tools in `horus/ scripts/ sim/ gta/`; the only mentions left are in `guidance.py`, which is B3. Live Gazebo smoke (`remote_up.sh horus_indoor.sdf`): `list-tools` = get_state get_map get_link_status take_off goto move turn look set_speed hold rth land get_frame get_raw_frame list_candidates point set_vocabulary set_target_profile set_mode get_candidate_motion watch_region list_region_watches remove_region_watch get_task_status wait_for_task wait_events wait_for_event cancel run_plan get_plan_status wait_for_plan cancel_plan find_place get_area_map mark_place, with none of the 7. `follow` -> `Unknown tool: follow`; `take_off 1.5` -> wait_for_task `succeeded`; `land` -> accepted. horus-mcp.log has 0 tracebacks; `remote_down.sh` left no container. Side finding: Koko's root disk had only 480 MB free, so start_sim.sh's 1 GB watchdog killed SITL at start. I trimmed my test copy `~/horus-loop` (695 MB -> 49 MB, and tightened the Protocol rsync excludes) and, by manager decision, ran `docker builder prune -f` (421 MB). Now 1.5 GB free.
- [x] B2 The onboard plan runner is not in the diagram, which says "runs one move at a time" (`horus/executor/plan.py`, 820 lines; MCP `run_plan`/`get_plan_status`/`wait_for_plan`/`cancel_plan` at `tasks.py:156-212`; `PlanRunner` in `context.py:20,130` plus `plans.require_idle()` at `context.py:264`; contact at `plan.py:471`; events `plan_finished`/`plan_log` at `events.py`; `"plan"` in `commander/agent.py:34`; `tests/test_plan.py`, 289 lines / 22 tests). Decision (default): remove `run_plan` and everything behind it, and delete `tests/test_plan.py`. Call `ask` first, with the options "remove run_plan (default)" and "keep run_plan trimmed to base moves". Acceptance: `grep -rn 'run_plan\|PlanRunner\|plan_log\|plan_finished' horus/ tests/` is empty; `test_mcp_startup.py`, `test_commander_server.py` and `test_events.py` pass. — MANAGER(it4): reopened: The required `grep -rn 'run_plan\|PlanRunner\|plan_log\|plan_finished' horus/ tests/` ran and returned matches in stale `__pycache__` files; only the narrower source-filtered grep was empty. Remove the stale generated caches within these two trees and rerun the exact acceptance command before reticking. — DONE: (it5) `find horus tests -type d -name __pycache__ -prune -exec rm -rf {} +` removed every generated cache under `horus/` and `tests/` (0 `.pyc` left). The exact acceptance command `grep -rn 'run_plan\|PlanRunner\|plan_log\|plan_finished' horus/ tests/` printed nothing, exit 1. The it4 source edits, Koko test runs and live smoke below stand, as the manager note says. Original it4 evidence: decision from the manager note (it3): remove. Deleted `horus/executor/plan.py` and `tests/test_plan.py`.
  - `horus/mcp_server/tasks.py`: removed the plan imports, `_RUN_PLAN_DESCRIPTION`, `run_plan`, `get_plan_status`, `wait_for_plan` and `cancel_plan` with their registrations; the module and `wait_for_event` docstrings now name no plan.
  - `horus/mcp_server/context.py`: removed the `PlanRunner` import, `self.plans` and the `require_idle()` gate in `submit`.
  - `horus/events.py`: removed `PLAN_EVENT_KINDS` and `MODEL_WAKE_EVENT_KINDS`, which equalled `EVENT_KINDS` once `plan_log` was gone.
  - `horus/commander/agent.py`: dropped the `"plan"` echoed-emergency source and the then-redundant wake filter.
  - `horus/executor/core.py`: removed the public `emergency_epoch` property, whose only reader was `plan.py`; the internal `_emergency_epoch` preemption stays.
  - `guidance.py`: removed the `run_plan`/`wait_for_plan` sentences; the rest of the prompt is B3.
  - Comments: `skills.py:20` and `core.py:85`.
  - Tests: `test_commander_server.py` swaps the `plan_log` test for "wait-only kinds are neither mirrored nor wake the mission" (vocabulary_changed vs task_finished); `test_gz_bridge.py:408` uses an `object_detected` sample instead of `plan_log`; `test_executor.py:1480` drops a plan comment.

  Acceptance grep over `*.py/*.sh/*.qml/*.cc` in `horus/ tests/` exits 1 (no hits; stale `__pycache__` only). No `wait_for_plan|get_plan_status|cancel_plan|last_result|.emergency_epoch` remains in `horus/ tests/ sim/ scripts/`. On Koko all passed: `test_mcp_startup` 16, `test_commander_server` 15, `test_events` 3, `test_gz_bridge` 13, `test_claude_code_commander` 10, `test_commander_api` 5, `test_executor` 135, `test_precision_landing` 14, `test_repl` 23, `test_scene` 19, `test_maps` 13, `test_mcp_client` 5. AI-link-loss hold-then-home is untouched, still covered by `test_mcp_startup` and `test_executor`.

  Live Gazebo indoor smoke:
  - `list-tools` has no plan tools; `run_plan` -> `Unknown tool: run_plan`; `wait_for_event ["plan_finished"]` -> `unknown event kinds ['plan_finished']`.
  - `take_off 1.5` succeeded; a `goto` 3 m north was preempted by `hold` -> `cancelled preempted by emergency hold`; `land` was accepted and the `landed` event arrived.
  - 0 tracebacks in horus-mcp.log; `remote_down.sh` left no container.
- [x] B3 The AI's operating instructions still teach the removed features (`horus/mcp_server/guidance.py`, one string: `:28` set_speed is left to E3; `:32-44` search, lost target, `get_lost_target`, `retarget`; `:47-54` "where a lost person can have gone", walkways, searched ground; `:56-58` clamped targets and `wait_for_plan`; `:63-67` `run_plan`, `last_result`, `fence_limit`). Rewrite those lines for the base moves only; E2 and E3 rewrite the limit and speed lines. Do B1 and B2 first. Acceptance: `get-prompt horus_commander` on the Gazebo smoke names only existing tools, checked by a throwaway script that compares every tool name in the prompt against `list-tools`; `test_mcp_startup.py` passes. — DONE: `horus/mcp_server/guidance.py`:
  - Added an opening line: HORUS flies one move at a time (take_off, goto, move, turn, look, plus the emergency hold, rth and land), and no skill chases a target or sweeps an area by itself.
  - Deleted the search, lost-target, `get_lost_target` and `retarget` paragraph (old 32-44).
  - The map sentence keeps only the `own_map` marks: no "where a lost person can have gone", walkways or searched ground.
  - "requested versus clamped/executed" now reads "requested versus executed"; the `run_plan`/`wait_for_plan` text went in B2.
  - Left on purpose for E2/E3: "bounded HORUS skills and their configured clamps", `get_map` flight envelope, `set_speed`. Left for C5: none (the prompt names no event kind of the removed skills).

  Tests on Koko all passed: `test_mcp_startup` 16 (mission brief equals prompt), `test_mcp_client` 5 (get-prompt asserts "event-driven" and "current tracker IDs"), `test_claude_code_commander` 10, `test_commander_api` 5.

  Live Gazebo indoor smoke, with a throwaway script inside the container that reads the served `horus_commander` prompt and `list_tools` over MCP:
  - Call-form names `find_place`, `get_area_map`, `mark_place`, `point` and `wait_for_event` are all tools; none is outside the tool list.
  - Removed names present: none (the 12 checked are approach, follow, orbit, scan, search, get_lost_target, retarget, run_plan, get_plan_status, wait_for_plan, cancel_plan and last_result).
  - The only snake_case words that are neither tool nor event are arguments and fields: after_id, by_deg, camera_motion, east_m, frame_id, heading_deg, last_id, north_m, own_map, pitch_deg, radius_m, rate_deg_s, within_tolerance.
  - 22 of 31 tools are mentioned; script exit 0. 0 tracebacks; `remote_down.sh` left no container. The throwaway script was deleted on the laptop, on Koko and in the container.

## C. Flight moves #a3f1: only take off · go to · move · turn · look · hold · land · home

- [x] C1 The lost-target estimate ("guesses where a lost person went") still runs (`horus/executor/target_belief.py`, 408 lines; `horus/maps/prior.py`, 111 lines; imports at `core.py:32-33`, `core.py:185-187`, `target_skills.py:13`, `context.py:23,136-137`; lost pursuit at `target_skills.py:~470-563`; `tests/test_target_belief.py`, 8 tests). Delete both modules, their wiring and the pursuit branch, and delete `test_target_belief.py` plus the `test_executor.py` lost-pursuit tests (about 2129-2316 and 3072-3112). Acceptance: `test_executor.py` and `test_maps.py` pass; `grep -rn 'TargetBelief\|MapPrior\|lost_pursuit' horus/ tests/` is empty. — DONE: Deleted `horus/executor/target_belief.py`, `horus/maps/prior.py` and `tests/test_target_belief.py`.
  - `target_skills.py`: removed the belief import and update, `_pursue_lost_target` (flying to the last-seen spot), `_belief_report`, `_update_lost_target_belief`, `_update_belief` and `_remember_target_position`. A lost target now only triggers `_hold_facing_last_target`, a zero-velocity hover turned to the last validated bearing; `target_lost` event data keeps `last_seen` and has no expected position.
  - `core.py`: removed the `TargetBelief`/`MapPrior` imports, the `_last_target_ned`/`_last_target_stop_m`/`_belief`/`_belief_frame_id`/`_lost_footprint` fields, `map_prior` and the executor's `get_lost_target`, which had no caller since B1.
  - `context.py`: removed the `MapPrior` import and build.
  - `perception_port.py`: removed `trails` from the port; the executor no longer reads it.
  - Docstrings: `ground.py` and `trails.py`.
  - Tests:
    - `test_trails.py`: deleted the 5 belief and map-prior tests; the 2 TrailGrid/TrailRecorder tests stay for C4.
    - `test_executor.py`: rewrote two tests. `test_a_lost_target_is_held_facing_where_it_was_seen_and_resumed_on_re_identification` checks no translation after the loss, a zero-velocity command facing the spot, and resuming on re-id (the old code flew more than 2 m north, so it would fail there). `test_a_follow_lost_before_its_first_validation_reports_where_the_target_was_last_seen` checks the ground_track and seeded frame, the `target_lost` last_seen, no `expected_north_m`, and the vehicle holding near 0. Deleted the 3 belief tests and the `get_lost_target` test, plus their `_lose_moving_person` helper; removed the `get_lost_target` asserts from the 2 retarget tests, which C3 deletes.

  Exact acceptance grep: no output, exit 1 (caches cleared first). No `get_lost_target|target_belief|maps.prior|map_prior|_pursue_lost|belief` remains in `horus/ tests/ sim/ scripts/` (`*.py`/`*.sh`). On Koko all passed: `test_executor` 131 (135 − 4 deleted), `test_maps` 13, `test_trails` 2, `test_mcp_startup` 16, `test_precision_landing` 14, `test_perception` 35, `test_ground` 5, `test_scene` 19, `test_repl` 23.

  Live Gazebo indoor smoke: server start (ToolContext without the map prior) ok; `get_area_map` -> provider none, own_map keys marks/searched/walkways (C2/C4 remove the last two); take_off 1.5, goto (1,0,1.5) and land all succeeded and `landed` arrived; 0 tracebacks; `remote_down.sh` left no container. Found during C1: `data/maps/gta/` on the laptop has only `walkable.json` and `source/`, no `map.json`. `horus_gta.yaml:67` needs it, so the GTA server cannot start until it is built; I added that to H2.
- [x] C2 scan/search are still executor skills (`horus/executor/area.py`, 190 lines; `core.py:17,156,601-602,647-648,893-896`; `geofence.scan_waypoints` at `geofence.py:370-420`; `own_map.CoverageGrid`/`detectable_footprint` at `own_map.py:63-115`, fed only by `area.py:65`; `executor.coverage` at `core.py:34,184`; `maps.py:98-99` "searched" layer; `target_found` event at `area.py:147`). Delete all of these, drop scan/search from `SKILLS`, and remove the searched-ground layer from `get_area_map`. Delete the `test_executor.py` scan/search tests (about 313-336, 2750-2982, 3026-3070) and the `searched` assertions in `tests/test_maps.py` (21, 104, 274-292). Acceptance: `test_executor.py` and `test_maps.py` pass; `HorusExecutor.SKILLS` lacks `scan` and `search`. — DONE: Deleted `horus/executor/area.py`.
  - `core.py`: removed the `step_area` import and dispatch, the scan/search start branch and the `_finish` waypoint rewrite; removed the `_waypoints`, `_waypoint_index`, `_coverage_frame_id` and `_search_frame_id` fields and `self.coverage`/`CoverageGrid`. `SKILLS` drops scan and search.
  - `skills.py`: removed `TRACKING_SKILLS` (it equalled `TARGET_SKILLS` once search was gone; core now uses `TARGET_SKILLS`), the search `tracking_record` branch, `_prepare_area`, `_prepare_search_cue`, the scan/search preparers and `SkillContext.has_perception`, whose only reader was search.
  - `geofence.py`: removed `scan_waypoints` and the unused `Mapping` import.
  - `own_map.py`: removed `CoverageGrid`, `detectable_footprint` and their imports; `Marks` stays.
  - `mcp_server/maps.py`: `get_area_map` has no `searched` layer; docstrings updated.
  - Tests: `test_executor.py` deleted 18 scan/search tests (`test_scan_that_expires_in_the_queue…`, the 16 search cases with the `_SearchScene`/`_search_executor`/`_SEARCH_AREA` helpers, `test_a_scan_records_the_ground…` with `_FramesAhead`). `test_maps.py` drops `CoverageGrid`/`_Executor.coverage`, the no-provider `own_map` is `{marks, walkways}`, and the walkways test no longer checks searched ground.
  - `target_found` now has no publisher; C5 removes the kind.

  On Koko all passed: `test_executor` 113 (131 − 18), `test_maps` 13, `test_mcp_startup` 16, `test_precision_landing` 14, `test_config_profiles` 20, `test_trails` 2, `test_ground` 5, `test_repl` 23. In the container, `sorted(HorusExecutor.SKILLS)` is approach, follow, goto, move, orbit, take_off, turn. Source grep for `"scan"|"search"|_SEARCH_AREA|.coverage|step_area|CoverageGrid|detectable_footprint|scan_waypoints|TRACKING_SKILLS|has_perception` in `horus/ tests/` is empty.

  Live Gazebo indoor smoke: `get_area_map` own_map = marks, walkways; take_off 1.5 (ttl 120) succeeded after 42 s; move forward 1 m succeeded, pose (1.0, 0, 1.63); turn by 90 succeeded; `scan` -> client error (unknown tool); land -> `landed`; 0 tracebacks; `remote_down.sh` left no container. Side finding: the first smoke's take_off with the default TTL (command_ttl_s, about 28 s) expired because SITL's cold-start pre-arm ("Arm: Accels/Gyros inconsistent") lasted past it. That is pre-existing and not caused by C2; the commander can pass `ttl_s`. Later smokes should start take_off with `ttl_s` 120.
- [x] C3 approach/follow/orbit are still executor skills (`target_skills.py`, 632 lines; `target_range.py`, 414; `ground_track.py`, 85; `skills.py:13-16,47,61-100,214-287,296-300` `TARGET_SKILLS`/`tracking_record`/`_STANDOFFS`/`SkillFailure`; `core.py:31,35-36,89-131,149-150,182,188,921-922,933-991`; `geofence.limit_velocity` at `geofence.py:274-368`). Delete them. `HorusExecutor._fresh` (`core.py:268`) calls `self._ranger.fresh`, and precision landing uses it, so inline `TargetRanger.fresh` (`target_range.py:114-119`) into the executor first. Delete the `test_executor.py` target tests (about 195-245, 456-842, 1080-1303, 1326-2513, 2565-2625, 2693-2749) and their orphaned helpers. Do C1 and C2 first. Acceptance: `test_executor.py` and `test_precision_landing.py` pass; `SKILLS == {"take_off","goto","move","turn"}`, with look/hold/land/rth as direct calls. — DONE: Deleted `horus/executor/target_skills.py`, `target_range.py` and `ground_track.py`.
  - `core.py`:
    - `_fresh` now inlines `TargetRanger.fresh`: None never fresh; age ≤ `telemetry_max_age_s`.
    - Removed `_ranger`, the target imports, every target `SkillTask` field (`tracking`, `_target_lost_at*`, `_last_target_facing_deg`, `_ground_track`, `_last_fix`, `_retarget_id`, `_fence_blocked_since`, `_stall_*`, `_range_note_warned`), the `tracking` output in results and status, `retarget()`, and the `failure_reason` plumbing whose only source was `SkillFailure`.
    - `_step` raises on an unknown skill instead of calling `step_target`. `SKILLS = {take_off, goto, move, turn}`; hold/rth/land stay direct calls in `submit_skill`.
  - `skills.py`: removed `TARGET_SKILLS`, `SkillFailure`, `tracking_record`, `_STANDOFFS`, `_prepare_target`, `_require_target_id` and the target preparers; `outcome_reason(status, clamped)` returns succeeded/clamped or the status.
  - `geofence.py`: removed `FENCE_LOOKAHEAD_S`, `_outside`, `limit_velocity`, `_limit_inside_polygon`, `_first_exit` and the `_edges` build; `clamp_pose` and `as_dict` stay for E2.
  - Docstrings in `ground.py`, `mcp_server/flight.py` (set_speed) and `tasks.py` (wait_for_task, wait_for_event).
  - Tests:
    - `test_executor.py` went from 113 to 47 tests by AST-dropping every top-level test or helper naming approach, follow, orbit, retarget, tracking, ranger, limit_velocity or the target stubs, then the helpers that became orphans (`_box_at`, `_fly`, `_front_tof`, `_down_range`, `_INTERMITTENT_CANDIDATE`, …) and unused imports.
    - Kept: `test_emergency_arriving_during_a_step_command_is_sent_after_it`, a goto whose only hit was the word "follows" in a comment, and `test_look_tilts…`.
    - `test_gz_bridge.py:398` uses an `mcp__horus__turn` sample instead of follow.

  Source grep for `target_skills|target_range|ground_track|TargetRanger|FrameFix|TARGET_SKILLS|SkillFailure|tracking_record|retarget|step_target|limit_velocity|FENCE_LOOKAHEAD|_ranger` in `horus/ tests/` is empty. On Koko all passed: `test_executor` 47, `test_precision_landing` 14, `test_mcp_startup` 16, `test_maps` 13, `test_gz_bridge` 13, `test_repl` 23, `test_perception` 35, `test_ground` 5, `test_trails` 2, `test_config_profiles` 20, `test_commander_server` 15, `test_scene` 19. In the container, `sorted(HorusExecutor.SKILLS)` is goto, move, take_off, turn.

  Live Gazebo indoor smoke:
  - take_off 1.5 (ttl_s 120) succeeded, and the status has no `tracking` key.
  - goto (2,0,1.5) succeeded, reason succeeded; look -30 gave pitch -30; turn by 45 succeeded.
  - rth -> state RTH, then `landed`. Precision landing (which uses the inlined `_fresh`) acquired the tag with 181 detections, then fell back to LAND: "made no descent progress for 12.0s wall (0.32 m over 10.3s autopilot time)". The same fallback is in a pre-change run, `runs/horus/20261003T092002…/events.jsonl` on Koko, so it is pre-existing, not caused by C3.
  - 0 tracebacks; `remote_down.sh` left no container.
- [x] C4 Trail recording and ground projection only fed the removed skills (`horus/trails.py`, 135 lines, wired at `perception/pipeline.py:13,58-65,139-142`, `perception/factory.py:13,35,97-104`, `mcp_server/server.py:22,182-189` and `mcp_server/maps.py:95-97` walkways; `horus/ground.py`, 166 lines, left only with its own test once C1-C3 are done). Delete both, plus `tests/test_trails.py`, `tests/test_ground.py` and the walkways part of `tests/test_maps.py`. Keep `own_map.Marks`, which `mark_place`/`find_place` use. Do C3 first. Acceptance: `test_maps.py`, `test_mcp_startup.py` and `test_perception.py` pass; `grep -rn 'horus.ground\|horus.trails\|TrailRecorder' horus/ tests/` is empty. — DONE: Deleted `horus/trails.py`, `horus/ground.py`, `tests/test_trails.py` and `tests/test_ground.py`.
  - `perception/pipeline.py`: removed the `TrailRecorder` import, the parameter, `_trail_recorder`/`trails` and the per-frame observe call.
  - `perception/factory.py`: removed the parameter, its pass-through and the docstring sentence.
  - `mcp_server/server.py`: removed the `TrailGrid`/`TrailRecorder` import and construction.
  - `mcp_server/maps.py`: `own_map` is `{marks}`; removed walkways, `_cell_outlines`, the now-unused `merge_polygons` import (`maps/model.merge_polygons` stays; the navmesh layer and `test_maps` use it) and the `tolerance` parameter of `_own_map`.
  - `own_map.py` docstring; `tests/fakes.py` drops `PerceptionStub.trails`.
  - `test_maps.py`: the no-provider `own_map` is `{"marks": []}`; deleted the walkways test, the `SimpleNamespace` import and the unused `_own_map_mcp` parameters.

  Acceptance grep: the literal `grep -rn 'horus.ground\|horus.trails\|TrailRecorder' horus/ tests/` still prints 5 lines, all from the kept `horus.grounder` package (`grounder/__init__.py:3`, `grounder/server.py:19`, `perception/factory.py:80`, `tests/test_grounder.py:13-14`), because the unescaped `.ground` matches `.grounder`. The precise `grep -rnE 'horus\.ground\b|horus\.trails\b|TrailRecorder' horus/ tests/` is empty (exit 1), as is a source grep for `TrailGrid|trail_recorder|walkway|.trails|GroundCamera|pixel_to_ground|camera_footprint|_cell_outlines`. `min_ground_range_depression_deg` stays in `config.py:113,159` for C6.

  On Koko all passed: `test_maps` 12 (13 − walkways), `test_mcp_startup` 16, `test_perception` 35, `test_perception_generalization` 13, `test_grounding` 3, `test_scene` 19, `test_live_view` 1, `test_executor` 47, `test_precision_landing` 14, `test_gta_stream` 8.

  Live Gazebo indoor smoke:
  - perception `streaming` (last frame 0.15 s old); `list_candidates` returned a 640×480 frame with its table.
  - `mark_place` "door" (3,1), then `get_area_map` own_map = `{'marks': [door]}`.
  - take_off (ttl_s 120) succeeded; `get_frame` while airborne showed 2 numbered person detections; land -> `landed`.
  - 0 tracebacks, no perception-unavailable warning; `remote_down.sh` left no container.

  The client saved its two JPEGs under the container's `runs/manager/`, i.e. Koko `~/horus/runs/manager/`, as `horus-mcp-call` always does. They were left in place: no deletions in `runs/`.
- [x] C5 Removed-skill event kinds are still announced to the AI (`horus/events.py:17-32` `target_lost`, `target_reacquired`, `target_found`, `fence_limit`; `commander/agent.py:32` `_REPEAT_LIMITED_KINDS`; the `tasks.py:115` `wait_for_event` docstring). Shrink `EVENT_KINDS` to what is still published: task_finished, landed, link_lost/restored, emergency, pilot_override/released, auto_rth, object_detected, region_*, precision_landing_stalled. Switch the example kind in `test_commander_server.py:509-520`, `test_mcp_startup.py:566-584`, `test_gz_bridge.py:406` and `test_claude_code_commander.py:194,203,382` to a kept kind. Do C1-C3 first. Acceptance: those 4 test files and `test_events.py` pass; every kind in `EVENT_KINDS` has a live publisher, checked by grep. — DONE: the 4 kinds were removed from `events.py`, all 16 remaining kinds have publishers, the 4 named test files plus `test_events` pass on Koko, and the live smoke rejects `target_lost`/`fence_limit`. Details:
  - `horus/events.py`: `EXECUTOR_EVENT_KINDS` drops `target_lost`, `target_reacquired`, `target_found` and `fence_limit`.
  - `commander/agent.py`: `_REPEAT_LIMITED_KINDS` = {link_lost, link_restored}, with its comment. The `wait_for_event` docstring was already fixed in C3.
  - Tests:
    - `test_commander_server.py:514-524`: `target_lost` -> `pilot_override`.
    - `test_mcp_startup.py:566,583-584`: the "not wanted" kind is `pilot_override`.
    - `test_gz_bridge.py:406`: a `link_lost` sample.
    - `test_claude_code_commander.py:194,203`: a `link_lost` event text; `:381-382,392` use `[operator] go to the door` and a `link_restored` event.

  Publishers (grep of `_publish_event(`/`.publish(`/`_sink(`):
  - executor `core.py`: task_finished :817, landed :316, link_lost/link_restored via `kind` :304, auto_rth :476, emergency :918, pilot_override :373, pilot_released :392.
  - `precision_landing.py:298`: precision_landing_stalled.
  - `perception/open_vocab.py:188`: object_detected.
  - `perception/scene.py`: region_enter :724, region_exit :705, region_dwell :731, region_watch_paused :611, region_watch_resumed :646, region_watch_expired :580.

  So every kind in `EVENT_KINDS` (16, printed in the container) has a live publisher. `grep -rn 'target_lost|target_reacquired|target_found|fence_limit' horus/ tests/ scripts/` is empty; the only hits left are old run logs under `sim/logs/` and `sim/run/`, untouched. On Koko all passed: `test_commander_server` 15, `test_mcp_startup` 16, `test_gz_bridge` 13, `test_claude_code_commander` 10 (after updating the `:392` expectation), `test_events` 3, `test_scene` 19, `test_executor` 47, `test_precision_landing` 14, `test_commander_api` 5.

  Live Gazebo indoor smoke: `wait_for_event ["target_lost"]` -> `unknown event kinds ['target_lost']`, same for `fence_limit`; take_off (ttl_s 120) -> `task_finished` event (take_off, succeeded); land -> `landed`; `wait_events` saw emergency, landed and task_finished; 0 tracebacks; `remote_down.sh` left no container.
- [x] C6 Settings for the removed skills are still parsed (`horus/config.py:101-120` `follow_distance_m`, `target_width_m`, `target_height_m`, `min_ground_range_depression_deg`, `forward_tof_vertical_half_fov_deg` and their validators at `:149-160`; YAML keys in `config/horus.yaml:25`, `horus_forest.yaml:19-20` and `horus_gta.yaml:36`; `tests/fakes.py:119-120`). `target_match_threshold` is left to D4 and `camera_horizontal_fov_deg` stays. Strict parsing (`config.py:418-433`) means the fields and their YAML keys go in one step. Acceptance: `test_config_profiles.py` passes, and `load_config` succeeds for every `config/*.yaml`, checked with a throwaway loop in the container. — DONE: the five settings and their YAML keys were removed in one step. All 7 profiles load, `test_config_profiles` passes, and the live indoor smoke loaded `horus.yaml` and flew. Details:
  - `horus/config.py`: removed `follow_distance_m`, `target_width_m`, `target_height_m`, `min_ground_range_depression_deg` and `forward_tof_vertical_half_fov_deg`, with their comments and validators (`target_width_m`, `min_ground_range_depression_deg`, `forward_tof_vertical_half_fov_deg`). `target_match_threshold` stays for D4; `camera_horizontal_fov_deg` stays.
  - YAML keys removed, in more files than the item listed:
    - `follow_distance_m` from `horus_forest`, `horus_urban`, `horus_procedural_forest`, `horus_procedural_urban` and `horus_gta`.
    - `target_height_m` from `horus`, `horus_forest`, `horus_urban`, `horus_nogps`, `horus_procedural_forest`, `horus_procedural_urban` and `horus_gta`, with the GTA pedestrian-height comment.
  - Tests: `tests/fakes.py` drops `follow_distance_m`/`target_width_m`. In `test_config_profiles.py`, the module docstring example is now `camera_horizontal_fov_deg`, and the out-of-range case `{"target_width_m": 0.0}` became `{"camera_horizontal_fov_deg": 0.0}`.

  Grep for the 5 names across `horus/ tests/ config/ sim/ scripts/ gta/` (`.py/.yaml/.sh/.cs/.ini/.parm`) is empty. Throwaway loop in the container: `load_config` succeeded for all 7 `config/*.yaml`: horus, forest, gta (`gta://127.0.0.1:5610`), nogps, procedural_forest, procedural_urban, urban.

  On Koko all passed: `test_config_profiles` 20, `test_executor` 47, `test_mcp_startup` 16, `test_precision_landing` 14, `test_world_profiles` 11, `test_worldgen_procedural` 132 (201 s; it reads the YAMLs), `test_gta_bridge` 25, `test_maps` 12.

  Live Gazebo indoor smoke: the server started with `horus.yaml` under strict parsing (get_state IDLE); take_off (ttl_s 120), goto (1,1,1.5) and land all succeeded and `landed` arrived; 0 tracebacks; `remote_down.sh` left no container.

## D. Camera vision #31b9: spot things and number them for the AI

- [x] D1 The executor's perception interface still carries target-skill methods (`horus/executor/perception_port.py`, 36 lines: `trails`, `latest_observation` users, `get_candidate`, `validate_candidate*`, `reidentify_candidate`, `last_seen`; `tests/fakes.py:190-206`; `perception/pipeline.py:166-179,224-258`). Keep only what precision landing needs (`get_frame`), plus anything another caller still uses (check with grep). Do C3 first. Acceptance: `test_executor.py`, `test_precision_landing.py` and `test_mcp_startup.py` pass. — DONE: `PerceptionPort` is now only `get_frame`; the executor's sole perception read is `precision_landing.py:384`, `perception.get_frame()`. Details:
  - `tests/fakes.py`: `PerceptionStub` has only `get_frame`.
  - `perception/pipeline.py`: removed the zero-caller wrappers `PerceptionSystem.last_seen`, `validate_candidate_failure` and `reidentify_candidate`; the tracker-level versions stay for D2.
  - Kept on `PerceptionSystem`, because callers remain: `latest_observation` (MCP `context.py:216`, `flight.py:45`, `test_scene`, `test_perception_generalization`), and `get_candidate`/`validate_candidate` (`test_recognition.py` and `test_perception.py:591,595,737-741`). The last two have no production caller left, so D4 removes them with the recognition code; I noted that in D4.

  On Koko all passed: `test_executor` 47, `test_precision_landing` 14, `test_mcp_startup` 16, `test_perception` 35, `test_recognition` 9, `test_grounding` 3, `test_perception_generalization` 13, `test_scene` 19, `test_maps` 12.

  Live Gazebo indoor smoke of the one port user: take_off (ttl_s 120) succeeded; rth -> `landed`, with precision landing stats `tag_detections` 107, `acquisitions` 1, `completed_landings` 1, `last_fallback` None (frames read through `get_frame`). 0 tracebacks; `remote_down.sh` left no container.
- [x] D2 Re-finding a lost track only served the removed skills (`horus/perception/reid_log.py`, 193 lines; `tracker.reidentify_candidate` at `tracker.py:652-711`; `_retired` at `:61-63,196-209`; `last_seen` at `:125-130`; `reacquisition_confirm_s` at `tracks.py:50-52`, `config.py:278` and `factory.py:71`). Delete it, along with the tests at `test_perception.py:228-336,525-584` and the reid tests near 646 and 685. Decision (default): keep the OSNet embedder, the multi-view gallery and `_recover_by_appearance`, because `point()` (`grounding.py:170-180,241-292`) and tracker ID stability through image jumps (`tracker.py:313-393`) use them. Acceptance: `test_perception.py`, `test_grounding.py`, `test_perception_generalization.py` and `test_config_profiles.py` pass. — DONE: lost-track re-identification and its logging/confirmation are gone, and the embedder, gallery and `_recover_by_appearance` are kept as decided. All 4 acceptance files pass, and the live `point()` smoke works. Details:
  - Deleted `horus/perception/reid_log.py` (`ReidSimilarityLog`, `ReacquisitionGate`).
  - `tracker.py`:
    - removed the reid_log import, `MAX_RETIRED_TRACKS`, `_retired` (with its `OrderedDict` import), `_reid_log`/`_reacquisition`, `last_seen()`, `reidentify_candidate()` and `_log_similarity()`;
    - `_validate_candidate` drops its `log_similarity` parameter and the per-comparison log call;
    - `_expire_tracks` simply drops expired tracks; the module docstring now says "appearance validation".
  - Settings: removed `reacquisition_confirm_s` from `tracks.TrackerConfig`, `config.PerceptionSettings` (with its validator) and `factory.py`; comments no longer mention re-acquisition. No YAML set it.
  - `test_perception.py`: AST-dropped the 9 re-id, last_seen, reacquisition and reid-logging tests (`test_lost_reference_embedding_rematches…`, `…hidden_longer…still_reidentified`, `…last_seen`, `test_reidentification_logs…`, the 3 `test_reacquisition_*`, `test_reid_similarity_logging_is_one_summary_per_second`, `test_reid_summary_windows_are_capped_per_reference`) and the now-unused `json`/`logging` imports. `test_live_validation_recovers_a_marginal_dip_through_a_stored_view` keeps its behaviour check without the removed log records; it runs with `revalidation_grace_s=0.0`, so only the stored 0.80 view can accept the 0.7196 dip.
  - `validate_candidate_failure` stays on the tracker: it is tested and shares `_validate_candidate`.

  Source grep for `reid_log|ReidSimilarityLog|ReacquisitionGate|reacquisition_confirm_s|reidentify_candidate|_retired|MAX_RETIRED|.last_seen(` in `horus/ tests/ config/ sim/ scripts/` is empty. On Koko all passed: `test_perception` 26 (35 − 9), `test_grounding` 3, `test_perception_generalization` 13, `test_config_profiles` 20, `test_recognition` 9, `test_mcp_startup` 16, `test_scene` 19, `test_gta_stream` 8.

  Live Gazebo indoor smoke: take_off (ttl_s 120) succeeded; `get_frame` frame 126 had candidates 1 and 2. `point(frame_id=126, box=[174,209,206,347])` resolved to candidate id 1 on frame 132, a fast-forwarded old-frame box through the kept embedder path. land -> `landed`; 0 tracebacks; `remote_down.sh` left no container.
- [x] D3 The track grace window only served approach/follow (`tracks.py:30-36,66-67,105-106`; `tracker.py:622-642`; consumer `target_range.py:141`, removed in C3). Delete it and its settings and tests. Also drop the `revalidation_grace_s=0.0` override that D2 added to `test_live_validation_recovers_a_marginal_dip_through_a_stored_view`, which then holds by default. Do D2 first. Acceptance: `test_perception.py` passes; `grep -rn grace horus/perception` is empty. — DONE: the grace window is gone, and validation now needs the full threshold against the best stored view. The acceptance grep is empty and `test_perception.py` passes. Details:
  - Removed:
    - `TrackerConfig.revalidation_grace_s`/`revalidation_grace_margin` and their comment and validator (`tracks.py`);
    - `Track.last_validated_monotonic`, which only the grace branch used;
    - the grace branch in `_validate_candidate` (`tracker.py`), so below-threshold returns `appearance_mismatch`;
    - `PerceptionSettings.revalidation_grace_s` and its `_number` validator (`config.py`), and the `factory.py` pass-through. No YAML set it.
  - Kept: the embedder, the multi-view gallery, `_admit_reference_view` and `_recover_by_appearance`.
  - Tests (`test_perception.py`):
    - deleted `test_revalidation_grace_keeps_a_track_through_a_marginal_dip`;
    - `test_reference_views_are_admitted_only_at_the_full_threshold_from_the_creation_view` now asserts that the 0.70 frame fails validation (it used to pass by grace) and is still not admitted;
    - the D2 `revalidation_grace_s=0.0` override is gone, so `test_live_validation_recovers_a_marginal_dip_through_a_stored_view` passes by default through the stored 0.80 view.

  After clearing `__pycache__`, `grep -rn grace horus/perception` is empty (exit 1), and `revalidation_grace|last_validated_monotonic` has no hits in `horus tests config sim scripts gta`. The precision-landing `precision_landing_target_grace_s` is unrelated and kept.

  On Koko all passed: `test_perception` 25 (26 − 1), `test_grounding` 3, `test_perception_generalization` 13, `test_config_profiles` 20, `test_recognition` 9, `test_mcp_startup` 16, `test_scene` 19, `test_precision_landing` 14, `test_gta_stream` 8.

  Live Gazebo indoor smoke: take_off (ttl_s 120) succeeded; `get_frame` frame 131 had candidates 1 and 2. `point(frame_id=131, box=[354,199,388,383])` resolved to candidate 2 on frame 137 through tracker validation without grace. land -> `landed`; 0 tracebacks; `remote_down.sh` left no container.
- [x] D4 Target profiles and the "embedding/classifier" recognition modes only picked the target for follow-type skills (`horus/perception/recognition.py`, 321 lines; MCP `set_target_profile`/`set_mode` at `mcp_server/perception.py:159-186,193`; `pipeline.py:19,78,278-312,361-374`; `context.py:78` `recognized_class`; `target_match_threshold` at `config.py:107,155`; `perception/__init__.py:12`). Delete them, and keep `set_mode` with only `active`/`paused`. Move the `point` test at `tests/test_recognition.py:259` into `tests/test_grounding.py`, then delete `test_recognition.py`. Also remove `PerceptionSystem.get_candidate` and `PerceptionSystem.validate_candidate` (`pipeline.py:157-206`), which have no production caller after D1; move `test_perception.py:591,595,737-741` to `system.tracker.validate_candidate`. Acceptance: `test_grounding.py`, `test_mcp_startup.py` and `test_config_profiles.py` pass; Gazebo smoke `list-tools` lacks `set_target_profile`. — DONE: target profiles and recognition modes are deleted, and `set_mode` accepts only `active`/`paused`. The 3 acceptance files pass, and the live `list-tools` lacks `set_target_profile`. Details:
  - Deleted `horus/perception/recognition.py` (`TargetProfile`, `TinyEmbeddingClassifier`, `PROFILE_KEYS`) and `tests/test_recognition.py`.
  - `pipeline.py`:
    - removed the `TargetProfile` import and `_profile`, `set_target_profile`, the embedding/classifier branches of `set_mode`, `get_candidate`, `validate_candidate` and the now-unused `FRESH_CANDIDATE_MAX_AGE_S` import;
    - `set_mode` raises `ValueError("mode must be active or paused")` for anything else; the old `normal`/`step_1`/`step_2`/`tiny_head` aliases are gone;
    - `_candidate_dict(candidate)` drops its frame/profile scoring, so candidate records lose `target_match_confidence`/`recognized_class` and keep `reference_views`.
  - MCP `mcp_server/perception.py`: removed the `set_target_profile` tool; the `set_mode` docstring now says active/paused only; updated the module docstring. `context.candidate_result` quotes only `class_name`/`label`.
  - Removed `ExecutorSettings.target_match_threshold` and its validator (`config.py`); no YAML set it. Dropped the `TinyEmbeddingClassifier` export from `perception/__init__.py` and "recognition" from the `geometry.py` docstring.
  - Tests:
    - moved the `point` look-alike test and its `_PatchImage`/`_PatchDetector`/`_PatchEmbedder` helpers into `test_grounding.py`;
    - `test_perception.py:414,418,459,461,463` now call `system.tracker.validate_candidate`;
    - `test_config_profiles` swapped its `target_match_threshold` cases: the out-of-range row is dropped, and the edge test uses `default_gimbal_pitch_deg=45`.

  After clearing `__pycache__`, the grep for `set_target_profile|TargetProfile|TinyEmbeddingClassifier|recognized_class|target_match|horus.perception.recognition|step_2|tiny_head` over `horus tests config sim scripts gta` is empty. `image_size`, `validate_box` and `normalize_embedding` still have callers. On Koko all passed: `test_grounding` 4 (3+1 moved), `test_mcp_startup` 16, `test_config_profiles` 19, `test_perception` 25, `test_perception_generalization` 13, `test_scene` 19, `test_gta_stream` 8, `test_mcp_client` 5, `test_commander_server` 15, `test_claude_code_commander` 10, `test_live_view` 1, `test_executor` 47, `test_precision_landing` 14.

  Live Gazebo indoor smoke:
    - `list-tools` has 0 `set_target_profile` matches;
    - `set_mode classifier` -> tool error "mode must be active or paused"; `set_mode paused` -> "paused"; `set_mode active` -> "active", and `list_candidates` then served frame 54;
    - take_off (ttl_s 120) succeeded; `get_frame` frame 121 had candidates 1 and 2;
    - `point` with candidate 1's box -> candidate 1 on frame 127, whose keys have no `target_match_confidence`/`recognized_class`;
    - land -> `landed`; 0 tracebacks; `remote_down.sh` left no container.

## E. No settings on board: ArduPilot #12e6 holds the limits

- [x] E1 Two world profiles have no ArduPilot fence at all, so deleting the HORUS limits would leave them unbounded (`sim/scripts/world_profiles.py:32-38`: indoor loads `common`+`indoor`, and `config/ardupilot/indoor.parm` has no `FENCE_*`; `sim/scripts/start_gta_sitl.sh:57-81` writes no `FENCE_*` for GTA; only `outdoor.parm:6-10` sets one, with FENCE_ALT_MAX 55 and a radius of 95 or 870). Add, as ArduPilot params: — DONE: indoor and GTA now have ArduPilot fences, including the GTA 6 m floor (the SITL floor check passed). Live MAVLink reads return the values below, and take_off then land succeeded in both worlds. `test_world_profiles.py` passes. Details follow the item text.
  - **Indoor:** `FENCE_ENABLE 1`, `FENCE_TYPE 1`, `FENCE_ALT_MAX 2.2` (from `horus.yaml:10`), `FENCE_ACTION` = land.
  - **GTA:** `FENCE_ENABLE 1`, `FENCE_TYPE 3`, `FENCE_RADIUS 40`, `FENCE_ALT_MAX 48`, `FENCE_ACTION 1`, `FENCE_MARGIN 2` (from `horus_gta.yaml:19,25`).
  - **GTA 6 m floor** (`horus_gta.yaml:23`, which keeps it above the 4.4 m kiosk roof): add `FENCE_TYPE` bit 8 with `FENCE_ALT_MIN 6` only if Copter 4.7.1 SITL still takes off from the ground and lands with it. If it does not, call `ask` (options: "no floor, rely on AI + avoidance" (default), "keep a HORUS take-off floor for GTA").
  - **No-GPS:** altitude only.

  Decision (default): no polygon fence upload; the indoor room polygon (`horus.yaml:12-24`) is replaced by the altitude fence plus ToF avoidance. Acceptance: on a Gazebo indoor smoke and a GTA pose-mode start (no game needed), reading the `FENCE_*` params over MAVLink on TCP 5762 returns the values above; take_off then land succeeds in both; `test_world_profiles.py` passes.

  Evidence for E1:
  - **Indoor** (`config/ardupilot/indoor.parm`): `FENCE_ENABLE 1`, `FENCE_TYPE 1`, `FENCE_ACTION 2` (Land), `FENCE_ALT_MAX 2.2`, plus `FENCE_MARGIN 0.2`. The extra key is needed because `AVOID_ENABLE 3`'s fence bit stops a climb at `FENCE_ALT_MAX − FENCE_MARGIN`, so the 2 m default margin would cap climbs at 0.2 m.
  - **No-GPS:** `common`+`indoor`+`nogps` inherits the indoor altitude-only fence, and `nogps.parm` sets no `FENCE_*`.
  - **GTA:** new `config/ardupilot/gta.parm` (`FENCE_ENABLE 1`, `FENCE_TYPE 11`, `FENCE_ACTION 1`, `FENCE_MARGIN 2`, `FENCE_RADIUS 40`, `FENCE_ALT_MAX 48`, `FENCE_ALT_MIN 6`). `start_gta_sitl.sh` appends it, comments stripped, to `sitl-overrides.parm` in pose and physics mode, and its old "fence fragments are left out" comment is updated.
  - **Floor check** on Koko GTA pose-mode SITL, with a throwaway pymavlink probe (since deleted) that set `FENCE_TYPE 11`/`FENCE_ALT_MIN 6` at run time:
    - a 10 m take-off from the ground armed and climbed (peak 9.72 m); STATUSTEXT "Min Alt fence enabled (auto enable)";
    - then LAND: "Min Alt fence auto-disabled", disarmed, min_alt -0.01;
    - a second run with RTL: same auto-disable, disarmed, min_alt -0.03.

    The floor stayed, so no `ask` was needed. Limit (in the parm comment): Copter arms the floor only after the first climb above 6 m and drops it when LAND or RTL starts.
  - **GTA pose-mode start** (`sim/run.sh start --gta`, no game, from `~/horus-loop`):
    - `FENCE_*` read on TCP 5763: `{ENABLE 1, TYPE 11, ACTION 1, MARGIN 2, RADIUS 40, ALT_MAX 48, ALT_MIN 6}`. 5762 is horus-gta-bridge's serial1 in pose mode, so serial2 5763 was read instead.
    - HORUS was started with a throwaway `config/zz_e1_smoke_gta.yaml` on the Koko test copy only: `horus_gta.yaml` without its `map:` block, because `data/maps/gta/map.json` is absent. The file was deleted after the run.
    - take_off `altitude_m 10` -> succeeded (`executed_target` 10.0); land -> `landed`; 0 tracebacks; `run.sh stop` left no container.
  - **Gazebo indoor smoke** (`remote_up.sh horus_indoor.sdf`):
    - `FENCE_*` read on TCP 5762: `{ENABLE 1, TYPE 1, ACTION 2, MARGIN 0.2, ALT_MAX 2.2}` (RADIUS 150 and ALT_MIN -10 are unused defaults);
    - take_off 1.5 m (ttl_s 120) succeeded; land -> `landed`; 0 tracebacks; `remote_down.sh` left no container.
  - **Tests:** `bash -n start_gta_sitl.sh` OK. On Koko `test_world_profiles` 11, `test_gta_bridge` 25 and `test_config_profiles` 19 pass.
  - **Left for later items:** the HORUS polygon, inset and `min_altitude_m` keys stay until E2. The README, `sim/README.md`, `common.parm` and YAML comments describing them are G1/G3 work.
- [x] E2 HORUS still clamps every move to its own envelope (`horus/executor/geofence.py`: what is left of 420 lines after C2/C3, the `Envelope` used at `core.py:18,175,188,665` and `skills.py:10,45,143,152,227,246`; `config.py:92-99,149-156,174-202` `geofence_*`, `max_distance_m`, `min_altitude_m`/`max_altitude_m`; keys in all 7 `config/*.yaml`; `get_map.flight_envelope` at `mcp_server/flight.py:38-56`; `guidance.py:45-46`; `skills.py:152` clamps take-off). Delete `Envelope` with its export, the fields and keys, and the `flight_envelope` field. Keep plain input checks, which are not limits: finite numbers; take_off `altitude_m > 0`; a goto/move whose resolved altitude would be at or below 0 m above home is rejected. Move the YAML values that `tests/test_worldgen_procedural.py:47-48` reads into constants. Update `tests/fakes.py:107-123`, `test_executor.py:129-244,965-995`, `test_config_profiles.py:34-145`, `test_mcp_startup.py:509-527` and the guidance lines. Do E1, C2 and C3 first. Acceptance: those tests and `test_worldgen_procedural.py` pass; `grep -rn 'Envelope\|geofence\|max_distance_m\|min_altitude_m\|max_altitude_m' horus/ config/*.yaml tests/` is empty; the Gazebo smoke flies `take_off` then `goto` to a point inside the room and `get_map` has no `flight_envelope`. — DONE: HORUS no longer clamps any move: `Envelope`, its settings and the YAML keys are gone. The plain input checks stay. The acceptance tests pass, the precise grep (manager decision) is empty, and the live take_off plus in-room goto ran unclamped with no `flight_envelope` in `get_map`. Details:
  - **Code:**
    - deleted `horus/executor/geofence.py` (`Envelope`, polygon inset and clamping) and its `horus/executor/__init__.py` export;
    - `core.py` drops `self.envelope`; `_start_move` resolves the target without clamping and calls `require_above_home`;
    - `skills.py`: `SkillContext(settings)` only; goto uses the requested pose as executed and raises "goto target altitude must be above home (up_m > 0…)" at or below 0; take_off raises "altitude_m must be above 0" and never clamps;
    - `config.py` removed `max_distance_m`, `min_altitude_m`, `max_altitude_m` and `geofence_radius_m`/`_polygon_ned`/`_margin_m` with their validators; `cruise_min_altitude_m` is now checked only as >= 0;
    - `mcp_server/flight.py` drops `get_map.flight_envelope` (and its mention in the `set_speed` docstring, left for E3);
    - `guidance.py`: "Use only bounded HORUS skills and their configured clamps" now says ArduPilot holds the limits, and the get_map envelope sentence now lists pose, proximity and candidates;
    - `flight/link.py` `DistanceSensor.min/max_distance_m` -> `min_range_m`/`max_range_m` (MAVLink sensor range, no name users), and its stale ground-range comment is fixed.
  - **YAML:**
    - removed the envelope keys and polygons from all 7 profiles; `horus.yaml` and `horus_nogps.yaml` no longer need an `executor:` block;
    - comments now point at the ArduPilot fences (`gta.parm`, `indoor.parm`, `outdoor.parm`/`procedural_fence.parm`);
    - the procedural TTL comment now describes an 850 m leg rather than a clamp; stale "person height" removed from the `horus_gta.yaml` header.
  - **Tests:**
    - `fakes.executor_settings` drops the envelope keys;
    - `test_executor`: the 4 clamp/polygon tests are replaced by `test_goto_and_take_off_run_their_targets_unclamped_and_reject_targets_at_or_below_home`; the move test now asserts the far move runs unclamped; new `test_move_that_would_end_at_or_below_home_fails_when_it_starts`. Total 45 tests.
    - `test_config_profiles`: the procedural test now reads `FENCE_RADIUS`/`FENCE_ALT_MAX` from the profile's parm files (the cruise gate lies below `FENCE_ALT_MAX`; the TTL covers home to `FENCE_RADIUS`); the polygon/radius exclusivity test is gone; the out-of-range rows use `command_ttl_s`; there is a new negative cruise gate case. Total 17 tests.
    - `test_mcp_startup`: removed `test_get_map_reports_the_enforced_inset_geofence…`, so 15 tests.
    - `test_worldgen_procedural`: `MAX_ALTITUDE` became constants (forest 36, urban 48: the generators' transit ceilings); the test-local constant became `ACTOR_INSET_M` and the test is renamed `…stay_inside_the_actor_area…`.
  - Also `sim/worldgen/forest_procedural.py:376` docstring: "profile's max_altitude" -> "36 m transit ceiling".

  Grep (manager decision, iteration 17: precise grep keeping the E3 cruise gate and the worldgen manifest key): after clearing `__pycache__`, `grep -rn 'Envelope\|geofence\|max_distance_m\|min_altitude_m\|max_altitude_m' horus/ config/*.yaml tests/ | grep -v 'cruise_min_altitude_m\|actor_geofence_m'` is empty (exit 1). The literal grep's only survivors are the intended `cruise_min_altitude_m` (E3) and `actor_geofence_m` (the worldgen actor-area manifest key in `sim/worldgen/scenario.py`, read by `test_worldgen_procedural`). `horus/` alone has no `geofence`/`Envelope` hits.

  On Koko all passed: `test_executor` 45, `test_config_profiles` 17, `test_mcp_startup` 15, `test_worldgen_procedural` 132 (137 s), `test_precision_landing` 14, `test_flight_link` 14, `test_commander_server` 15, `test_claude_code_commander` 10, `test_world_profiles` 11, `test_maps` 12, `test_gta_bridge` 25, `test_repl` 23, `test_events` 3, `test_live_view` 1, `test_mcp_client` 5.

  Live Gazebo indoor smoke:
  - take_off 1.5 m (ttl_s 120) succeeded;
  - `goto up_m 0` -> tool error "goto target altitude must be above home (up_m > 0, got 0)";
  - `goto (-2, 2, 1.5)` succeeded with executed = requested and clamp_reason None, so it was not clamped (vehicle_pose -1.98/1.96/1.5);
  - `get_map` keys are `candidates, observation_age_s, observed_at, proximity, vehicle_pose`, with no `flight_envelope`;
  - 0 tracebacks; `remote_down.sh` left no container each time.

  An earlier goto to (2, 2) also succeeded; its LAND put the drone on the 0.84 m `table_occluder`, where it disarmed at up_m 1.03, so no `landed` event. The off-pad LAND finding is tracked as H0.
- [x] E3 HORUS caps speed as a safety limit (`core.py:719-788` `max_speed_m_s`, the cruise gate `cruise_speed_m_s`/`cruise_min_altitude_m`, the `set_speed` cap, `speed_cap_m_s` in `get_state` at `core.py:281`; MCP `set_speed` at `mcp_server/flight.py:145-153`; `guidance.py:28`; YAML keys). GUIDED velocity setpoints bypass `WPNAV_SPEED` (`outdoor.parm:22-28`), so the go-to controller still needs a speed of its own. Decision (default): keep the controller's speed as fixed config, renaming `max_speed_m_s` -> `speed_m_s` and `max_vertical_speed_m_s` -> `vertical_speed_m_s`, and keep the altitude-gated cruise as controller behaviour (`cruise_speed_m_s`/`cruise_min_altitude_m`). Remove the `set_speed` tool, the cap and `speed_cap_m_s`, since `set_speed` is not one of the diagram's base moves. Update every YAML and `test_executor.py:998+,3000-3030` and `test_config_profiles.py:56-145`. Acceptance: those tests pass; Gazebo smoke `list-tools` lacks `set_speed`; a goto still reaches its target. — DONE: the `set_speed` tool and speed cap are gone, and the controller speeds are fixed config under the new names (manager decision: the default). The acceptance tests pass, `list-tools` lacks `set_speed`, and a goto reached its target live. Details:
  - **Code:**
    - `config.py`: `max_speed_m_s` -> `speed_m_s` and `max_vertical_speed_m_s` -> `vertical_speed_m_s`, with a comment that they are go-to controller speeds, not safety limits (GUIDED velocity bypasses the waypoint speed parameters). The cruise gate is kept, and its check now reads "cruise_speed_m_s must exceed speed_m_s".
    - `core.py`: removed `_speed_cap_m_s`, `set_speed_limit`, `_capped_speed` and `get_state.speed_cap_m_s`; `_horizontal_speed_limit` became `_horizontal_speed` (cruise or `speed_m_s`); `_goto_step` reads `vertical_speed_m_s`.
    - MCP `flight.py`: removed the `set_speed` tool and its registration. `guidance.py` drops the "set_speed lowers the horizontal speed limit" clause.
  - **YAML:** renamed both keys in `horus_forest`, `horus_urban`, `horus_gta` and `horus_procedural_forest`/`_urban`; the procedural comments say "horizontal speed rises to 8 m/s … the controller flies at 3 m/s" instead of "limit". `horus` and `horus_nogps` set neither key.
  - **Tests:**
    - `fakes.executor_settings` uses the new names;
    - `test_executor` deleted `test_speed_cap_lowers_translation_speed_at_once…`, renamed the two cruise tests (`…keeps_speed_m_s…`, `…drops_back_to_speed_m_s…`) and updated the move test comment. Total 44 tests.
    - `test_config_profiles` uses the new names; `test_cruise_speed_must_exceed_max_speed` is now `…exceed_speed_m_s`. Total 17 tests.

  After clearing `__pycache__`, the word-grep for `max_speed_m_s|max_vertical_speed_m_s|set_speed|set_speed_limit|speed_cap_m_s|_capped_speed|_horizontal_speed_limit` over `horus tests config scripts gta sim/scripts` has no hits outside the civilian-actor spec (`sim/scripts/civilian_spec.py`/`civilian_life.py` and `test_civilian_life.py`'s pedestrian `max_speed_m_s`, unrelated to HORUS flight). `grep -rn 'speed_cap\|set_speed' horus` is empty.

  On Koko all passed: `test_executor` 44, `test_config_profiles` 17, `test_mcp_startup` 15, `test_precision_landing` 14, `test_commander_server` 15, `test_claude_code_commander` 10, `test_repl` 23, `test_worldgen_procedural` 132.

  Live Gazebo indoor smoke:
  - `list-tools` has 0 `set_speed`. The 29 tools: get_state get_map get_link_status take_off goto move turn look hold rth land get_frame get_raw_frame list_candidates point set_vocabulary set_mode get_candidate_motion watch_region list_region_watches remove_region_watch get_task_status wait_for_task wait_events wait_for_event cancel find_place get_area_map mark_place.
  - Calling `set_speed` -> "Unknown tool: set_speed".
  - take_off 1.5 m (ttl_s 120) succeeded; `goto (-2, 2, 1.5)` succeeded and reached `vehicle_pose` -1.98/1.96/1.50; `get_state` has no `speed_cap_m_s`.
  - rth -> `landed` (home pad); 0 tracebacks; `remote_down.sh` left no container.
- [x] E4 `turn` clamps the AI's rate to a HORUS maximum (`horus/executor/skills.py:183-192`, `max_turn_rate_deg_s`), which ArduPilot's yaw-rate limits already bound (`ATC_RATE_Y_MAX`/`ATC_SLEW_YAW`). Rename the setting to `turn_rate_deg_s`, use it as the default only, and drop the clamp; keep the finite and > 0 checks. Update YAMLs and `test_executor.py:932-963`. Acceptance: `test_executor.py` and `test_config_profiles.py` pass; `turn` with a rate above the old maximum is accepted unchanged in `executed_target`. — DONE: `turn_rate_deg_s` is now only the default rate and the HORUS clamp is gone. Both acceptance files pass, and a live 60 deg/s turn (twice the old 30 deg/s maximum) ran with `executed_target.rate_deg_s` 60. Details:
  - **Code:**
    - `config.py`: `max_turn_rate_deg_s` -> `turn_rate_deg_s`, documented as the default when the commander gives no rate; the 180 deg/s upper bound is dropped, and the validator keeps finite and > 0.
    - `skills.py` `_prepare_turn`: a given `rate_deg_s` is still checked finite and > 0, and is then executed unchanged; the "turn rate clamped" reason is gone. The one-revolution sweep bound stays, since it describes the move's shape, not a speed limit.
    - MCP `turn` docstring: "default and upper bound" -> "default `executor.turn_rate_deg_s`; ArduPilot's yaw-rate limits bound how fast it actually turns".
  - **YAML:** no YAML set the key, so there was nothing to update.
  - **Tests:** `test_executor` `…clamps_rate_and_sweep` is now `test_turn_to_a_heading_goes_the_shorter_way_at_an_unclamped_rate_and_limits_the_sweep`. It checks:
    - the configured 20 deg/s default is used when no rate is given;
    - 100 deg/s is executed as 100 with `clamp_reason` None, the setpoint reaching 320 after 0.5 s, and reason `succeeded`;
    - an infinite rate is rejected.

  After clearing `__pycache__`, `grep -rn max_turn_rate horus tests config scripts gta sim` is empty; `README.md:12` still says "upper bound `executor.max_turn_rate_deg_s`", which is added to G1. On Koko all passed: `test_executor` 44, `test_config_profiles` 17, `test_mcp_startup` 15.

  Live Gazebo indoor smoke:
  - take_off 1.5 m (ttl_s 120) succeeded;
  - `turn by_deg 90 rate_deg_s 60`: requested = executed `{by_deg 90, rate_deg_s 60}`, clamp_reason None, then succeeded (heading 0 -> 90);
  - `turn by_deg -45` with no rate executed at the default 30 and succeeded (heading 45.96);
  - rth -> `landed`; 0 tracebacks; `remote_down.sh` left no container.

- [x] E5 (found it30 during H1, when `list-tools` showed the tool text) The MCP tool descriptions the AI pilot reads still promise HORUS clamps, although E2 removed them and `guidance.py:28` says "Moves are not clamped": — DONE: (it30) the tool texts now say moves are flown as given and ArduPilot holds the limits; the evidence follows the item text.
  - `horus/mcp_server/flight.py:73-78`: take_off's "``altitude_m`` is clamped by executor limits … clamps";
  - `:89-94`: goto's "bounded … subject to executor clamps … requested versus clamped targets";
  - `:109-115`: move's "bounded … then clamped … the clamped ``pose``";
  - `:1` and `:157`: the module's "bounded flight skills" and "bounded emergency hold";
  - `tasks.py:36`: get_task_status's "requested/clamped targets"; `server.py:1`: "bounded HORUS skills"; `scene.py:48`: "choose bounded skills yourself"; `executor/core.py:106`: "bounded velocity";
  - `README.md:87`: "bounded flight/perception tools".

  The only limits HORUS still applies to a move are turn's one-revolution sweep (`skills.py:120-123`) and look's gimbal range (`core.py:755`), and both report `clamp_reason`. Rewrite the texts: targets are flown as given, a goto/move target at or below home altitude and a take-off at or below 0 m are rejected as input errors, ArduPilot holds the limits, and `clamp_reason` is named only for turn/look. Acceptance: `grep -rn 'clamp\|bounded' horus/mcp_server horus/executor/core.py` leaves only turn/look, payload-size and gimbal-travel survivors (listed in DONE); `test_mcp_startup.py` passes; a live `list-tools` shows the new take_off/goto/move text. Evidence (it30):
  - **Texts:**
    - take_off: "flown as given; at or below 0 it is rejected. ArduPilot's fence and failsafes hold the flight limits";
    - goto: "flown as given. A target at or below home altitude (`up_m` <= 0) is rejected; ArduPilot's fence, altitude limits and proximity avoidance hold the flight limits";
    - move: "then flown like goto (rejected if it would end at or below home altitude) … `executed_target` the `pose` flown";
    - hold: "hold position in GUIDED mode (an emergency stop)";
    - get_task_status: "requested/executed targets"; `server.py`/`flight.py` module docstrings: "flight moves"; `scene.py`: "choose the moves yourself"; `core.py:106`: "command velocity setpoints";
    - `README.md:87`: "its flight moves, perception tools", edited on top of the held text, as G1 was.
  - **Grep survivors** (`clamp|bounded` over `horus/mcp_server` and `horus/executor/core.py`), all intended:
    - `guidance.py:28` "Moves are not clamped";
    - `maps.py:15,73` "bounded payload" (the get_area_map radius and outline size);
    - `core.py:68,93,100,258,808`, the task `clamp_reason` field that only turn's sweep sets;
    - `core.py:708,755`, look's gimbal pitch range.
  - **Tests:** on Koko `test_mcp_startup` 15, `test_executor` 44 and `test_scene` 19 pass.
  - **Live** (run `20261029T184658Z-885764`):
    - `list-tools` shows the new take_off/goto/move/hold/get_task_status texts, with 0 lines matching `clamp|bounded`;
    - `goto up_m 0` -> "goto target altitude must be above home (up_m > 0, got 0)";
    - `take_off 1.5` succeeded with requested = executed and `clamp_reason` null; `land` -> `landed` 10.6 s;
    - 0 tracebacks; `remote_down.sh` left 0 containers.

## F. Test world: Gazebo #71dd OR GTA V #85c8, never both

- [x] F1 Only one direction refuses to run both worlds (`scripts/koko/gta_game.sh:176` refuses GTA while `gz sim` runs). `sim/run.sh start` (`:245-248`) refuses only a container with the same name, so Gazebo can start while the GTA game runs, and two containers with different `HORUS_SIM_CONTAINER_NAME` can run side by side. In `run.sh start`, before `docker run` and skipped for `--gui-client`, refuse Gazebo while `pgrep -f '[G]TA5_Enhanced'` matches (with a hint to run `gta_game.sh stop`), and refuse `--gta` while `pgrep -f '[g]z sim'` matches. Add `--label horus.world=gazebo|gta` to `docker run`. Acceptance: a new `tests/test_run_sh_world_guard.py` runs `bash sim/run.sh start …` with stub `docker`/`pgrep` on PATH (pattern of `tests/test_world_profiles.py:40-44`) and covers: Gazebo refused while GTA runs; `--gta` refused while `gz sim` runs; `--gui-client` not blocked. It passes. — DONE: `run.sh start` now refuses the other world both ways and refuses any second HORUS world container whatever its name (MANAGER it19). World containers carry `horus.world=gazebo|gta`, and `--gui-client` is exempt. The new test file passes, and live Koko checks refused a second world in both directions. Details:
  - **`sim/run.sh`:** new `refuse_second_world gazebo|gta`, called after the same-name container check and before any GUI or `docker run` setup, only when `gui_client_enabled == 0`. It refuses:
    - Gazebo while `pgrep -f '[G]TA5_Enhanced'` matches (hint: `scripts/koko/gta_game.sh stop`);
    - `--gta` while `pgrep -f '[g]z sim'` matches (hint: `./sim/run.sh stop`);
    - any running container from `docker ps --no-trunc --format '{{.Names}}|{{.Label "horus.world"}}|{{.Command}}'` that has a `horus.world` label or, for older unlabelled ones, a `start_sim.sh`/`start_gta_sitl.sh` command. The message names that container and the stop command.

    `docker run` gets `--label horus.world=gazebo|gta` for world starts; the GUI client gets no label.
  - **New `tests/test_run_sh_world_guard.py`** (8 tests): it runs `bash sim/run.sh …` with stub `docker` (logs calls; `docker run` exits 97, so nothing starts) and `pgrep` on PATH. Cases:
    - Gazebo refused while the GTA game runs;
    - `--gta` refused while `gz sim` runs;
    - a second world under another container name (`HORUS_SIM_CONTAINER_NAME=horus-sim-2`) refused for labelled GTA vs Gazebo, labelled Gazebo vs `--gta`, and an unlabelled legacy `start_sim.sh` container;
    - a lone Gazebo or `--gta` start reaches `docker run` with its `--label horus.world=…`, with an unrelated container running;
    - `run.sh gui-client` with GTA running and a world container up: no `pgrep`/`docker ps` call, no guard message, no world label.

  On Koko `test_run_sh_world_guard` 8 and `test_world_profiles` 11 pass; `bash -n sim/run.sh` OK.

  Live Koko checks:
  - `remote_up.sh horus_indoor.sdf`: `docker inspect` shows `/horus-sim horus.world=gazebo`. Then from `~/horus-loop` with `HORUS_SIM_CONTAINER_NAME=horus-sim-2`:
    - `run.sh start --gta` -> "Gazebo (gz sim) is running on this host; stop it with ./sim/run.sh stop before starting --gta." (exit 1);
    - `run.sh start horus_indoor.sdf` -> "HORUS world container horus-sim (gazebo) is already running; stop it first: HORUS_SIM_CONTAINER_NAME=horus-sim ./sim/run.sh stop" (exit 1);
    - `docker ps` still showed only `horus-sim`.
  - `run.sh start --gta` (pose mode): `horus.world=gta`. A Gazebo start as `horus-sim-2` -> "HORUS world container horus-sim (gta) is already running…" (exit 1).
  - Both stacks were stopped (`remote_down.sh`, `run.sh stop`), and no container was left.
  - Not exercised live: refusal against a real GTA5_Enhanced process (the game was not started); the stub tests cover it.
- [x] F2 A run can load the other world's config, and nothing checks it (`sim/run.sh:225` keeps any inherited `HORUS_CONFIG`, so `HORUS_CONFIG=…/horus_forest.yaml run.sh start --gta` waits forever for UDP video; only `horus_gta.yaml:47` has `gta://`; `world_profiles.py:31-38` has no GTA entry). Add a `gta` profile, with config `horus_gta`, to `world_profiles.py`, and resolve `run.sh:225` through it. In `run.sh`, refuse a config whose `perception.source` does not match the world (`gta://` exactly for `--gta`), via a small `world_profiles.py check-config` helper. Acceptance: new cases pass in `test_world_profiles.py`, and `test_config_profiles.py` asserts that only `horus_gta.yaml` uses `gta://` and the `gta` map provider. — DONE: the `gta` world profile exists, `run.sh --gta` resolves its default config through it, and `run.sh start` refuses a config from the other world before `docker run`. Both acceptance files pass, and live Koko starts refused mismatches in both directions. Details:
  - **`sim/scripts/world_profiles.py`:**
    - new first profile row `gta` -> `horus_gta.yaml` with `/horus/config/ardupilot/gta.parm`, the fragment `start_gta_sitl.sh` appends itself;
    - new `config_world_mismatch(world, config)` and the CLI `world_profiles.py check-config WORLD CONFIG` (exit 1 plus reason). It maps a `/horus/…` path to the repo and imports `yaml` lazily, so the profile lookup stays dependency-free. Rules:
      - `gta` needs `perception.source` starting `gta://`;
      - a Gazebo world refuses a `gta://` source and the `gta` map provider;
      - an unreadable config is refused with its reason.
  - `sim/scripts/procedural_world.sh`: new `horus_check_world_config`.
  - **`sim/run.sh`:** the `--gta` default config is read from `horus_world_profile gta`, not a literal path. Before the image checks (skipped for `--gui-client`), any set `HORUS_CONFIG` is checked against `gta` or the Gazebo world, and a mismatch exits 2.
  - **Tests:**
    - `test_world_profiles`: a `gta` profile row; own-config accepted for gta, indoor and procedural urban; refusal of forest/indoor configs under `gta`, the GTA config under indoor, and a missing file; a Gazebo config with the `gta` map provider; the bash helper's exit 1/0. Total 21 tests.
    - `test_config_profiles`: new `test_only_the_gta_profile_reads_gta_frames_and_the_gta_map` (exactly `{horus_gta.yaml}` has a `gta://` source or the `gta` provider, and it has both). Total 18 tests.
    - `test_run_sh_world_guard`: `--gta` with `horus_forest.yaml` and indoor with `horus_gta.yaml` exit 2 with no `docker run`; a lone `--gta` start passes `--env HORUS_CONFIG=/horus/config/horus_gta.yaml`. Total 10 tests.

  All three files pass on Koko. Live Koko (`~/horus-loop`, host python3 with PyYAML 6.0.1):
  - `HORUS_CONFIG=/horus/config/horus_forest.yaml run.sh start --gta` -> "…streams perception.source the default Gazebo UDP video; the GTA world needs a gta:// source (config/horus_gta.yaml)" (exit 2), and no container was created;
  - `HORUS_CONFIG=/horus/config/horus_gta.yaml run.sh start horus_indoor.sdf` -> "…is a GTA config (gta://127.0.0.1:5610); Gazebo world horus_indoor.sdf cannot feed it" (exit 2);
  - plain `run.sh start --gta` started with `HORUS_CONFIG=/horus/config/horus_gta.yaml` in the container env;
  - Gazebo regression `remote_up.sh horus_indoor.sdf`: take_off succeeded, land -> `landed`, 0 tracebacks;
  - `run.sh stop` and `remote_down.sh` left no container.
- [x] F3 The Koko drone-server starter overrides the world's config (`scripts/koko/start_mcp.sh:15` always exports `HORUS_CONFIG=/horus/config/horus.yaml`, so in a GTA container it replaces `horus_gta.yaml`, set by `run.sh:225`, with the indoor Gazebo config). Pass `--env HORUS_CONFIG` only when argument 2 is given, so the container's own default applies otherwise; `remote_up.sh:287-289` already passes it. Acceptance: on a GTA pose-mode start, `start_mcp.sh horus-sim` with no config argument logs `horus_gta.yaml` (`/horus/sim/logs/horus-mcp.log`), and `get_state` reports the `gta://` source. — DONE: with no config argument, `start_mcp.sh` no longer overrides the container's own config, so a GTA container keeps its GTA profile. The server log names the loaded config, and `get_state` reports the `gta://` source, both live on Koko. Details:
  - **`scripts/koko/start_mcp.sh`:** argument 2 is optional. When given (non-empty), it is validated against `^/horus/…\.yaml$` and passed with `--env HORUS_CONFIG`; otherwise nothing is passed, and the container's `HORUS_CONFIG` from `run.sh` applies (`horus_gta.yaml` for `--gta`, the procedural profile for `--procedural`), else horus-mcp's `config/horus.yaml`. Argument 3 (log level) is unchanged; pass `""` as argument 2 to set it without a config. Callers `remote_up.sh:288` and `profiling/stack.sh:21` already pass a config.
  - **Supporting changes**, so the acceptance is observable (until now the server logged no config and `get_state` showed no source):
    - `horus/mcp_server/server.py` logs `HORUS config <path> (perception.source <source>)` at start-up;
    - `ToolContext.camera_status` (and so `get_state.perception`) adds the configured `source` when perception runs (the disabled branch is unchanged).
  - **Tests:** on Koko `test_mcp_startup` 15, `test_mcp_client` 5, `test_commander_server` 15 and `test_live_view` 1 pass; `bash -n start_mcp.sh` OK.
  - **Scratch map** (MANAGER it21, the H2 approach; nothing written under `data/`):
    - built with `scripts/maps/fetch_gta_map.py --out /horus/sim/run/gta-scratch-map/map.json` in the container on Koko (pinned SHA-256s matched; 1571 places, 1283 ways, 35 areas; source cache 6.1 MB in the same scratch dir);
    - `walkable.json` copied (read) from the laptop's `data/maps/gta/` into that scratch dir;
    - a temporary `sim/run/gta-scratch-map/horus_gta_smoke.yaml` is `horus_gta.yaml` with only `map.data_path`/`map.walkable_path` pointed at the scratch files.

    The dir sits in both Koko copies (`~/horus-loop/sim/run/`, `~/horus/sim/run/`) and is kept for H2, which deletes it afterwards.
  - **GTA pose-mode smoke** (`~/horus`, which has `models/`):
    - `HORUS_CONFIG=/horus/sim/run/gta-scratch-map/horus_gta_smoke.yaml run.sh start --gta`, then `printf token | bash scripts/koko/start_mcp.sh horus-sim` with no config argument;
    - the log reads `HORUS config /horus/sim/run/gta-scratch-map/horus_gta_smoke.yaml (perception.source gta://127.0.0.1:5610)`: the container's GTA profile, not `horus.yaml`. It is the scratch copy of `horus_gta.yaml`, because the real one cannot start without the missing map;
    - `get_state.perception` = `{source: gta://127.0.0.1:5610, camera: stalled, reason: "[Errno 111] Connection refused"}` (no game running);
    - take_off 8 m succeeded; land -> `landed`; 0 tracebacks; `run.sh stop` left no container.

    An earlier run from `~/horus-loop` (no `models/`) loaded the same config but had perception disabled ("ReID ONNX model does not exist"), so no source was shown; `find_place Legion` returned Legion Square from the scratch map.
  - **Gazebo regression** (`remote_up.sh horus_indoor.sdf`, explicit config argument): the log reads `HORUS config /horus/config/horus.yaml (perception.source udp://0.0.0.0:5600)`, and `get_state` gives `source udp://0.0.0.0:5600`, camera streaming. 0 tracebacks; `remote_down.sh` left no container.
- [x] F4 The drone server publishes Gazebo images during GTA runs (`horus/mcp_server/server.py:190,209` always build `GazeboImagePublisher`; `live_view.py:50-62` renders and converts every frame for it; the GTA container has the Gazebo libraries, `sim/docker/Dockerfile:34,38-39`, so this costs real CPU on Koko). Create the publishers only when `perception.source` is not `gta://`, and let `LivePerceptionView` accept `publisher=None` while keeping its throughput log. Acceptance: `test_live_view.py` (new `None`-publisher case), `test_mcp_startup.py` and `test_mcp_gz_image.py` pass; a GTA pose-mode server log shows no gz publisher advertised. — DONE: a `gta://` source now creates neither Gazebo image publisher, and the live view takes `publisher=None` and keeps its throughput log. The three acceptance files pass, and a GTA pose-mode server with perception initialized advertised no gz publisher, while Gazebo still advertises both. Details:
  - **`horus/mcp_server/server.py`:** `gazebo_images = not config.perception.source.startswith("gta://")`. The live view publisher (`/horus/perception/live`) and the set-of-mark publisher (`/horus/som_image`) are built only then; otherwise both are `None`, and the server logs "GTA frame source: Gazebo image publishers are off". `create_mcp` already accepted `image_publisher=None` (`perception.py:25`).
  - **`live_view.py`:** `publisher: Any | None`; with `None` it never renders or converts a frame and still logs the periodic throughput summary.
  - **`gz_image.py`:** logs "Publishing Gazebo images on <topic>" on a successful advertise, so the log shows whether a publisher exists.
  - **Tests:** new `test_without_a_publisher_nothing_is_rendered_and_throughput_is_still_logged` (`render_live_view` is stubbed and never called; two INFO summaries on the 10 s interval over three frames). On Koko `test_live_view` 2, `test_mcp_startup` 15 and `test_mcp_gz_image` 3 pass.
  - **GTA pose-mode smoke** (MANAGER it22: `~/horus`, which has `models/`, and `sim/run/gta-scratch-map/horus_gta_smoke.yaml`):
    - `run.sh start --gta`, then `start_mcp.sh horus-sim` with no config argument;
    - perception initialized: no "Perception unavailable" line, and `get_state.perception` = `{source gta://127.0.0.1:5610, camera stalled, reason "[Errno 111] Connection refused"}` (camera thread running, no game);
    - the log has "HORUS config …horus_gta_smoke.yaml (perception.source gta://127.0.0.1:5610)" and "GTA frame source: Gazebo image publishers are off", with 0 "Publishing Gazebo images" lines and 0 tracebacks;
    - `run.sh stop` left no container.
  - **Gazebo regression** (`remote_up.sh horus_indoor.sdf`): the log has "Publishing Gazebo images on /horus/perception/live" and "…on /horus/som_image", and `gz topic -l` lists both. 0 tracebacks; `remote_down.sh` left no container.
- [x] F5 The GTA path still carries Gazebo leftovers (`sim/run.sh:385-391,431` pass `GZ_IP`/`GZ_PARTITION` into GTA containers; `run.sh frame` at `:528-531` grabs the Gazebo camera with no mode check; the `:62` `require_running_container` message and the `logs` help text say "Gazebo"). Build the gz env only when `gta==0`, make `frame` refuse in a GTA container (read the F1 label), and make the messages world-neutral. Do F1 first. Acceptance: new cases in `tests/test_run_sh_world_guard.py` show `frame` refused on a `horus.world=gta` container and no `GZ_` env in the GTA `docker run` arguments; the test file passes. — DONE: a GTA `docker run` now gets no `GZ_` env, `frame` refuses a `horus.world=gta` container, and the messages are world-neutral. The test file passes (new cases included), and live Koko checks confirmed the GTA container env and the `frame` refusal, with Gazebo `frame` still working. Details:
  - **`sim/run.sh`:**
    - `gz_args` (`GZ_IP`, `GZ_PARTITION`, optional `GZ_RELAY`, plus `GZ_SIM_RESOURCE_PATH`, which moved there from the shared `docker run` list) is built only when `gta == 0`, so Gazebo worlds and the GUI client keep it;
    - `frame` reads `{{index .Config.Labels "horus.world"}}`, and on `gta` exits 2 with "frame grabs the Gazebo camera; <container> runs GTA, whose frames come from the game (get_frame on the HORUS MCP server)";
    - `require_running_container` now says "Start it with ./sim/run.sh start [world] or ./sim/run.sh start --gta" (it named the Gazebo-only `start_sim.sh`);
    - help text: `logs` "Follow the simulation container log (Gazebo or GTA SITL)", and `frame` "…(Gazebo worlds only)".
  - **`tests/test_run_sh_world_guard.py`:**
    - the docker stub answers `inspect` (`State.Running`, `horus.world` label) and `exec`;
    - new `test_gta_container_gets_no_gazebo_environment_and_gazebo_keeps_it` (with `GZ_IP`/`GZ_PARTITION` set, the GTA run has no `GZ_`, and the Gazebo run has both plus `GZ_SIM_RESOURCE_PATH`);
    - new `test_frame_is_refused_on_a_gta_container[gta|gazebo]` (gta: exit 2, no `grab_frame.py` exec; gazebo: one exec).

    On Koko the file (13) and `test_world_profiles` (21) pass; `bash -n sim/run.sh` OK.
  - **Live Koko:**
    - `GZ_IP=10.1.2.3 GZ_PARTITION=horus run.sh start --gta`: the container env has no `GZ_IP`/`GZ_PARTITION`. Only the image's own Dockerfile ENV `GZ_*` remain (`GZ_VERSION`, `GZ_SIM_SYSTEM_PLUGIN_PATH`, `GZ_SIM_RESOURCE_PATH` from `sim/docker/Dockerfile:13`, `GZ_GUI_PLUGIN_PATH`), not run arguments;
    - `run.sh frame` -> the GTA refusal (exit 2); `run.sh logs` followed the GTA SITL log;
    - after `run.sh stop`, `run.sh frame` -> "…is not running. Start it with ./sim/run.sh start [world] or ./sim/run.sh start --gta." (exit 1);
    - Gazebo regression `remote_up.sh horus_indoor.sdf`: the container has `GZ_IP=10.0.0.2 GZ_PARTITION=horus`, and `run.sh frame` -> "Enabled camera stream … (RTP flowing after 1 attempt(s))", exit 0;
    - 0 tracebacks; no container left.
- [x] F6 The GTA range bridge imports private helpers from the Gazebo ToF CLI (`horus/tof_bridge/gta.py:26-27` imports `_open_mavlink`, `_PymavlinkSender` and `PAYLOAD_SENSORS` from `tof_bridge/cli.py`, plus `laser_scan_to_distance_sensor` from `bridge.py`). Move the world-neutral MAVLink sender and sensor identities into a shared module (e.g. `tof_bridge/mavlink_out.py`), and make `gta.py` and `cli.py` both import from it. Acceptance: `tests/test_tof_bridge.py` passes; `gta.py` imports nothing from `cli.py`. — DONE: the world-neutral MAVLink side now lives in the new `horus/tof_bridge/mavlink_out.py`, and both bridges import from it. `gta.py` imports nothing from `cli.py`, `test_tof_bridge.py` passes, and both bridges ran live on Koko. Details:
  - **Moved out of `cli.py`:** `BRIDGE_SOURCE_SYSTEM`/`BRIDGE_SOURCE_COMPONENT`, `PAYLOAD_DOWN_SENSOR`, `PAYLOAD_SENSORS`, the sender (now public as `PymavlinkSender`) and the pymavlink opener (now public as `open_mavlink`).
  - **Imports now:** `cli.py` imports these, keeping only the Gazebo topic layout and `_serve`/`main`; `gta.py` imports them plus `bridge.py`'s converter (`bridge.py` has no Gazebo dependency). `grep -n cli horus/tof_bridge/gta.py` is empty. No `_PymavlinkSender`/`_open_mavlink`/`tof_bridge.cli import PAYLOAD_SENSORS` is left in `horus tests sim scripts gta`. `__main__.py` still runs the Gazebo `cli.main`.
  - **Tests:** `test_tof_bridge.py` imports `PAYLOAD_SENSORS`/`PymavlinkSender` from `mavlink_out`, and the ring-layout CLI test patches `cli.open_mavlink`; `test_sensor_payload.py` imports `PAYLOAD_SENSORS` from `mavlink_out`.

  On Koko, each file run on its own under `timeout 900` (MANAGER it24): `test_tof_bridge` 27, `test_sensor_payload` 4 and `test_gta_bridge` 25 pass.

  Live Koko:
  - **GTA pose-mode container:** `python -m horus.tof_bridge.gta --listen 127.0.0.1:5612 --mavlink tcp:127.0.0.1:5760`, fed 20 HRNG packets by a throwaway sender (deleted) -> "Connected to tcp:127.0.0.1:5760 as sysid=254 compid=196 … first range packet forwarded (seq 1, sim 50 ms)". Physics mode, which runs this bridge on serial2, needs the game.
  - **Gazebo** (`remote_up.sh horus_indoor.sdf`): `tof_bridge.log` has "Connected to tcp:127.0.0.1:5763 as sysid=254 compid=196" and the seven subscriptions, and `get_map.proximity` = `[(1, 1.92), (4, 3.58)]`.
  - 0 tracebacks; `run.sh stop` and `remote_down.sh` left no container.

## G. Docs match the diagram

- [x] G1 `README.md` describes removed features and the HORUS limit layer (`:3,9,12,13,16,17-26,27-32,33-44,56-57,62,264,266-278,286,301,320,329,382`; `:12` calls the turn rate "default and upper bound `executor.max_turn_rate_deg_s`", but E4 renamed it to `turn_rate_deg_s`, a default only; `:264` also documents `set_target_profile`, recognition modes and the removed `perception.revalidation_grace_margin`, and D3 deleted that grace window; it also names parameter files that no longer exist: `horus_sitl.parm` at `:60,301`, `horus_sitl_procedural.parm` at `:382`; and `RNGFND1_MIN_CM` at `:246`, where `common.parm:19` uses `RNGFND1_MIN`). The user put a hold on README edits (HANDOFF.md:7), so call `ask` first, with the options "edit on top of the held working-tree text (default)" and "leave README for the user". Then rewrite the architecture, skills, limits and REPL sections to match the diagram: base moves, ArduPilot owns limits, Gazebo OR GTA, `/home`. Do A-F first. Acceptance: every tool and config key named in README exists in code, checked by a throwaway grep script; the user's held edits outside these sections are preserved, checked with `git diff README.md` before and after. — DONE: README's architecture, skills, limits and REPL text now match the diagram (MANAGER it25 decision: edit on top of the held working-tree text). The throwaway checker finds every named tool, config key, ArduPilot parameter and path in the code, and the before/after diff shows every held edit outside the rewritten lines intact. Details:
  - **Intro and `## Architecture`:**
    - the intro (`:3`) reads: an AI pilot flies one base move at a time; HORUS has no limit layer, since ArduPilot holds limits, avoidance and failsafes; Gazebo or GTA V, one per run;
    - bullets in diagram order, each named for its block: terminal `horus/repl/`, AI pilot `commander/`, drone remote `mcp_server/` (the current tools grouped flight, camera vision, tasks/events, maps; alerts = task/link/emergency/landing/pilot-override, `object_detected` and region events), flight moves `executor/`, camera vision `perception/`, autopilot link `flight/`, gimbal, `tof_bridge/` (`cli.py`/`gta.py` through `mavlink_out.py`), and the test world `sim/` OR `gta/`;
    - the executor bullet: base moves `take_off`/`goto`/`move`/`turn`/`look`/`hold`/`rth`/`land` one at a time; input checks only; no clamping; fixed controller speeds `speed_m_s`/`vertical_speed_m_s` plus the cruise gate; `turn_rate_deg_s` as default only; reasons `succeeded`/`clamped` (turn revolution)/`expired`/`failed`/`cancelled`; the kept emergency-preemption and telemetry-freshness text;
    - removed: approach/follow/orbit/scan/search, the lost-target belief, trails, `horus/ground.py`, `run_plan`/`wait_for_plan`, `get_lost_target`/`retarget`, `set_speed`, recognition profiles, and the `walkways`/`searched` own_map layers;
    - the safety paragraph (`:62` -> `:35`) names each world's ArduPilot fence fragment, and states the AI-link-loss hold-then-home ("if the AI goes quiet, the drone holds still, then flies home").
  - **Connect, step 2:** the proximity parameter block now quotes `common.parm` (`AVOID_MARGIN 1.0`, `RNGFND1_MIN 0.05`, `RNGFND1_MAX 4.0` instead of `RNGFND1_MIN_CM`/`MAX_CM`) and names the per-world `FENCE_*` fragments.
  - **Step 3:** "local geofence/altitude/speed limits" -> "controller speeds".
  - **Perception paragraph (`:264`):** the `set_target_profile`, recognition-mode, grace-window, re-acquisition and reid-log text is replaced by what exists now: lazy embedding, full-threshold validation against the reference-view gallery, no re-identification after a track expires.
  - **Removed:** the `search(...)` paragraph and `search` in the label-filter sentence.
  - **REPL (`:286`):** `/state`, `/goto`, `/take_off`, `/hold`, `/rth`, `/home`, `/land`, `/quit`, plus the plain emergency words (`stop`, `halt`, `abort`, `hold`, `freeze`, `land`, `rth`, `return home`, `home`, `go home`, with an optional `now`/`immediately`); `/approach`/`/follow`/`/orbit` removed. Throwaway check of `horus.repl.app._interactive_arguments` on every documented form: `/home`, `home`, `go home` -> `rth`; `land now!` -> `land`; `land next to the hiker`, `/follow 3`, `/set_speed 1` -> `None`.
  - **Parameter files:** `horus_sitl*.parm` at `:293` -> `common.parm`, and the GTA fence at `:295` -> `gta.parm`.
  - **`:301`:** the geofence polygon and velocity-fence paragraph is now the per-world ArduPilot fence description (indoor altitude-only plus avoidance, outdoor circle, GTA circle/ceiling/floor and its arming behaviour).
  - **Smaller fixes:**
    - `:303`: nogps "indoor envelope and polygon fence" -> `indoor.parm`'s altitude fence;
    - `:320`: the range-source/`target_width_m`/`forward_tof_vertical_half_fov_deg` config text (C6 keys) is removed;
    - `:329`: the test-coverage sentence is updated;
    - `:372`: the stale "needs `target_size_m`" clause is dropped;
    - `:382`: the procedural profile now reads `common`/`outdoor`/`procedural_fence` with the 870 m fence, `speed_m_s` and the controller-speed wording.

  **Throwaway checker** (Python, run in the session, nothing written):
  - tools registered in `horus/mcp_server/*.py`: 29, all documented;
  - every tool name in the drone-remote bullets is a tool; the other backticked names in those bullets are argument or field names (`after_id`, `last_id`, `active`, `paused`, `object_detected`, `link_loss_rth_delay_s`);
  - removed tool names mentioned: none;
  - all 32 `section.key` config references exist in the `config.py` dataclasses;
  - snake-case keys missing from settings: only tool argument/result fields (`rate_deg_s`, `from_heading_deg`, `camera_pitch_deg`);
  - `FENCE_`/`AVOID_`/`PRX1_`/`RNGFND1_`/… parameters not in `config/ardupilot/*.parm`: none;
  - missing `config/`, `sim/`, `scripts/`, `horus/`, `gta/` paths: none.

  **Held-edit check:** I captured `/tmp/README.before.md` and `git diff README.md` before editing (367 lines) and compared after.
  - Every changed range of the working-tree text lies in the listed lines: `:3`, `7-62` (architecture), `239-251` (step 2/3 limits), `264`, `276-278`, `286`, `293`, `295`, `301`, `303`, `320`, `329`, `372`, `382`;
  - 335 of 382 baseline lines are byte-identical;
  - of the 80 `+` lines in the user's held diff, the 51 no longer present all sit inside those rewritten ranges; none outside.

  Live: the 29 tools documented here match the Gazebo `list-tools` output recorded in E3.

  Left for later: `:350` "60 m geofence" is the worldgen actor area, not a HORUS limit, and sits in the unlisted "Procedural worlds" text, so it is left as is; `sim/README.md`/`gta` docs are G3.
- [x] G2 `HORUS — Design Document v2.md` describes removed features (`:28-29,50,75,129-150,154,164-183,255,299,331`: bounded skills with clamps, approach/orbit/scan, `run_plan`, `follow(id)` milestones), and never mentions GTA. Update it to the diagram's architecture, and add one paragraph on "Gazebo or GTA, one per run". Acceptance: every tool named in it exists (throwaway grep); no `follow`/`orbit`/`run_plan` remains except in a dated history note. — MANAGER(it27): reopened: Acceptance requires every tool named in the document to exist, but the checker found six unregistered tools: train_specialist, load_model, upload_mission, start_mission, get_mission_log and clear_mission. Labelling them planned does not satisfy that criterion; describe those future capabilities wit… — DONE: (it28, after MANAGER it27) the six invented tool names are gone, replaced by capability descriptions. A strict checker with no planned-tool exemption finds no tool-shaped name outside History that is not a current registration. The earlier it27 rewrite below stands. Details:
  - **it28 fix:**
    - `:112` heading "(planned MCP tool `train_specialist`)" -> "(planned specialist training)";
    - mermaid node `Sign + load_model` -> `Sign + load onboard`;
    - `:124` now describes asking for a specialist from a description and reference images, cloud training, and loading the signed result onboard in shadow mode, with "No MCP tool exists for either step yet";
    - the tool-table row "Planned (stretch; not registered)" listing six names -> "Planned (stretch; no tool yet) | Specialist training and loading a trained model onboard; uploading, starting, reading the log of and clearing an offline mission package";
    - S8 "`train_specialist` (planned tool)" -> "planned specialist training".
  - **Strict checker (it28, Python, nothing written)** over the document text before `## History`:
    - it takes every snake_case identifier anywhere (backticks, mermaid labels, prose), plus every `name(` call, and keeps the tool-shaped ones (prefixes `get_`/`set_`/`wait_`/`list_`/`remove_`/`watch_`/`mark_`/`find_`/`cancel`/`take_`/`train_`/`load_`/`upload_`/`start_`/`clear_`/`run_`, the base-move verbs, or call syntax);
    - all 26 tools found are registrations in `horus/mcp_server/*.py`. The two other names are not tools: `start_gta_sitl` is the script `sim/scripts/start_gta_sitl.sh`, and `watch_id` is the real argument of `remove_region_watch(ctx, watch_id)` (`scene.py:65`);
    - removed or formerly-planned tool names in the body: none;
    - `follow|orbit|run_plan` outside History: only "followed by `indoor.parm`".

    Documentation only, so no runtime smoke (MANAGER it27).
  - **Principles:** "Safety is deterministic" now says ArduPilot holds limits, avoidance and failsafes, with no HORUS limit layer; "the AI never sends raw commands" means one base move at a time, flown with velocity setpoints that ArduPilot bounds.
  - **Architecture diagram:** You -> Terminal -> AI pilot -> Drone remote -> Flight moves / Camera vision (grounder fallback) -> Autopilot link -> ArduPilot -> test world Gazebo OR GTA V; emergency words skip the AI; the training service is gone. It is followed by the alerts sentence; the executor tier row lists base moves/tracking/`point`/precision landing.
  - **Scope:** the core no longer lists recognition ladder steps 1–2, and the core demo is "Keep that person in view through an occlusion with base moves" (manager `ask` decision, iteration 27: keep the demo via base moves plus explicit `point` re-seeding, without implying pursuit or re-identification).
  - **Grounding:** the commander answers with an ID plus a base move; objects that leave the view are re-pointed, not searched onboard. "Slow down while waiting / caps speed" -> "Hold while waiting".
  - "Specialized recognition" -> "(future work)", with a note that steps 1–2 were removed. In disconnected autonomy, "Built so far: run_plan" -> only the AI-link-loss failsafe.
  - **Safety and MCP:**
    - the clamping paragraph now says ArduPilot bounds every move and HORUS checks inputs only; the failsafe bullet drops "a running plan counts as supervision" and adds the fence breach action;
    - the tool table is regrouped as State / Base moves / Grounding (with `set_mode` active/paused) / Scene / Maps / Tasks (with `wait_for_event`) / Emergency / Planned-not-registered;
    - the event-kind list is the current 16 `EVENT_KINDS` in `horus/events.py`, without target_*/plan_*;
    - the "Provider-written plans (`run_plan`)" section is deleted and replaced by one sentence pointing to History.
  - **Navigation source:** adds the `gta` world and `gta.parm`, and says each fragment set carries its world's ArduPilot fence.
  - **Simulation stack:** new "**Gazebo or GTA, one per run.**" paragraph: pose versus JSON-physics mode; `run.sh start --gta` with no Gazebo and its own `gta.parm` fence; `run.sh` refuses a second world container under any name, the other world's process and a mismatched-world config; `gta_game.sh` refuses GTA while Gazebo runs. There is also a new "GTA V world (`gta`)" bullet in World and sensors.
  - **Milestones and success criteria:**
    - S5 is now "persistent IDs while in view; after a 2 s occlusion the commander re-seeds the ID with `point`";
    - the re-acquisition experiment is now "Re-pointing after occlusion", and the recognition-ladder experiment is marked Future work;
    - risk "low speed caps" -> "low controller speeds";
    - the MVR and scenario "Follow that person" -> "Keep that person in view" (keep it framed with `turn`/`move`/`look`, re-seed with `point`, ≥ 80%);
    - "Unsafe command … Blocked or clamped" -> "Stopped by ArduPilot's fence or proximity avoidance";
    - TypeFly's "gap" cell no longer claims `run_plan`.
  - **New `## History` note dated 2026-10-29:** lists every removal (target and area skills, `run_plan` and its tools/events/grammar, `get_lost_target`/`retarget`, lost-track re-ID and grace, target profiles, the HORUS envelope, `set_speed` and the turn-rate maximum) and the old "follow that person" demo.

  **Throwaway checker** (Python, nothing written):
  - outside History, `follow|orbit|run_plan` matches only "followed by `indoor.parm`" (ordinary English);
  - 29 registered tools are named, and no removed tool;
  - (it27, superseded by the it28 strict checker above) six planned tool names were then named in a "not registered" row; it28 removed them;
  - no other `get_`/`set_`/`wait_`/… name that is not registered, and no removed event kind;
  - `ExecutorState` states `IDLE`/`EXECUTING`/`HOLDING`/`RTH`/`PILOT` match `core.py`.

  All three mermaid blocks render with `mmdc` (exit 0). `git diff --no-index` against the pre-edit copy (since deleted): 49 insertions, 45 deletions; the rest of the user's working-tree text is unchanged. No runtime changed, so no live smoke was run; the tool set named here matches the Gazebo `list-tools` output recorded in E3.
- [x] G3 The sim/GTA docs and parameter comments describe the HORUS limits and the old coupling (`sim/README.md:103,146-172,169,196,265-266,284-288`; `gta/horus_mod/README.md:350-352,722-726`; `config/ardupilot/common.parm:9-11`, `procedural_fence.parm:1-4` and the `horus*.yaml` comments, which still describe the HORUS inset/polygon). `sim/README.md` is under the same hold as G1, so ask in the same way. State that `run.sh` refuses to run both worlds, that ArduPilot fences bound every world, and how a GTA run is smoked on Koko. Do F and E first. Acceptance: no doc mentions `geofence_polygon_ned`, `max_distance_m` or a HORUS fence inset (grep); every command quoted in these sections runs as written in H1/H2. — MANAGER(it29): reopened: The documented no-config start_mcp.sh invocation failed in create_map_provider because data/maps/gta/map.json is absent; only a different invocation using the scratch config succeeded. The quoted gta_game.sh start/stop and list_candidates commands were not executed. Document a runnable scratch-map/… — DONE: (it30, after MANAGER it29) the GTA smoke block in `sim/README.md` now builds the scratch map and config itself whenever `data/maps/gta/map.json` is absent, and passes that config explicitly. The H2 run executed the block as written; the it30 evidence is at the end of this item. The earlier evidence below stands.
  - **`sim/README.md`:**
    - new paragraph after Build and run: one world per host; `run.sh start` refuses Gazebo while GTA5_Enhanced runs, `--gta` while `gz sim` runs, any second world container whatever its name (`horus.world` label), and a config from the other world (`world_profiles.py check-config`); `gui-client` is exempt; every world's limits are its ArduPilot `FENCE_*` fragments, and HORUS clamps nothing;
    - the profile text (`:103`, `:142-149`) names the real fragments (`common`+`outdoor`+`canonical_fence`/`procedural_fence`) instead of the deleted `horus_sitl_*.parm`;
    - "Procedural flight profile" (`:153-172`): the polygon/`max_distance_m`/clamp text is gone, the 870 m `FENCE_RADIUS` plus 55 m `FENCE_ALT_MAX` circle is the bound, and `speed_m_s`/cruise are described as controller speeds;
    - the outdoor ground-plane ranging text (`:188-194`, removed with C3/C6) is cut;
    - GTA pose-mode overrides (`:252-254`) now include `gta.parm`, and the GTA profile bullet (`:263-267`) describes the `gta.parm` fence (40 m circle, 48 m ceiling, 6 m floor and its arming behaviour), not the old rectangle;
    - the in-container HORUS commands (`:275-288`) are replaced by a quoted Koko GTA smoke: `run.sh start --gta`, `start_mcp.sh horus-sim` with no config argument, `client get_state`, `gta_game.sh start`, `client list_candidates`, `gta_game.sh stop`, `run.sh stop`. A note covers the map requirement and the second-argument config override, no gz publisher for `gta://`, and `run.sh frame` refusing GTA;
    - indoor SITL (`:480-488`) loads `common`+`indoor` automatically (`horus_sitl.parm` was gone), describing `indoor.parm`'s altitude fence;
    - outdoor avoidance (`:518-522`) names the real fragments.
  - **`gta/horus_mod/README.md`:**
    - `:350-352` operating area -> `gta.parm`, with no HORUS fence;
    - `:722-733`: the 2026-10-21 HORUS rectangle is now explicitly history ("removed on 2026-10-29"), with no "inset" wording; "Scan altitude" -> "Flight altitude … `FENCE_ALT_MIN 6` keeps it".
  - **`config/ardupilot/common.parm:9-11`:** the comment cites doorways and avoidance, not the HORUS inset or orbit/scan margins. **`procedural_fence.parm:1-4`:** the fence is the operating area, and HORUS sends targets unclamped. The `horus*.yaml` comments were already cleaned in E2 (grep below).

  **Grep** (after `__pycache__` cleanup):
  - `geofence_polygon_ned|max_distance_m|fence inset|geofence inset|inset polygon|inset fence|horus_sitl` over every `*.md` outside protected and ledger files: the only hits are the env var `HORUS_SITL_PARAM_FILE` (case-insensitive match) in `sim/README.md` and the design doc;
  - `inset|polygon|geofence` in `config/*.yaml` and `config/ardupilot/*.parm`: none (exit 1).

  **Held-edit check** (captured `sim/README.md` text and `git diff` before editing; compared after; scratch copies since deleted): 519 of 578 baseline lines are byte-identical; every changed range is one listed above; of the 64 `+` lines in the user's held diff, the 11 replaced all lie inside those ranges, none outside. `gta/horus_mod/README.md` and the `.parm` files were edited on top of their working-tree text.

  Tests: on Koko `test_world_profiles` 21, `test_config_profiles` 18 and `test_run_sh_world_guard` 13 pass.

  **Commands run as written** on Koko `~/horus` (pose mode, no game):
  - `HORUS_SIM_IMAGE=horus-sim:local-s1s2 ./sim/run.sh start --gta` -> started;
  - `export HORUS_MCP_TOKEN=$(openssl rand -hex 24)`, then `printf … | bash scripts/koko/start_mcp.sh horus-sim` (no config): it logged `HORUS config /horus/config/horus_gta.yaml (perception.source gta://127.0.0.1:5610)` and then refused to start in `create_map_provider` (no `data/maps/gta/map.json`), as the note says;
  - the same with the F3 scratch config as argument 2 -> `Uvicorn running`;
  - the quoted `docker exec -e HORUS_MCP_TOKEN … client get_state` -> `perception.source gta://127.0.0.1:5610`, camera stalled (no game);
  - `./sim/run.sh frame` -> the GTA refusal (exit 2); `./sim/run.sh stop` -> no container left.

  **it30, after MANAGER it29:**
  - **`sim/README.md` GTA smoke block** (`:288-327`), run on Koko in `~/horus`:
    - `config=""` keeps the container's `horus_gta.yaml`;
    - when `data/maps/gta/map.json` is missing, it builds `sim/run/gta-scratch-map/map.json` with `docker exec … fetch_gta_map.py --out` (download cache beside it) and writes `horus_gta_smoke.yaml` with `sed` (data_path pointed at the scratch map, `walkable_path` dropped), then sets `config` to that file;
    - `start_mcp.sh horus-sim "$config"`;
    - the two `gta_game.sh` lines are marked "laptop", because the script reaches Koko over SSH;
    - it ends with `rm -rf sim/run/gta-scratch-map`.

    The paragraph after the block explains the empty-versus-path config argument, why there is no map on Koko (`rsync` leaves out `data/`), and the dropped walkable layer (`fetch_gta_walkable.py --out` needs a 466 MB archive plus a 659 MB extracted file). Nothing was written under `data/` or `config/`.
  - **Run as written (H2, run `20261029T183817Z-868665`):**
    - `HORUS_SIM_IMAGE=horus-sim:local-s1s2 ./sim/run.sh start --gta` -> started;
    - the token export and the `if` block -> "1571 places, 1283 ways, 35 areas within 600 m", then `config=/horus/sim/run/gta-scratch-map/horus_gta_smoke.yaml`;
    - `start_mcp.sh horus-sim "$config"` -> log "HORUS config /horus/sim/run/gta-scratch-map/horus_gta_smoke.yaml (perception.source gta://127.0.0.1:5610)", "GTA frame source: Gazebo image publishers are off", then `Uvicorn running`;
    - the quoted `client get_state` -> `{source gta://127.0.0.1:5610, camera stalled, reason "[Errno 111] Connection refused"}`;
    - `scripts/koko/gta_game.sh start` (laptop) ran and refused with "Steam is not signed in (no running Steam client); not starting GTA. Select the account in Steam first." (H2's blocked game part);
    - the quoted `client list_candidates` ran -> "perception unavailable: [Errno 111] Connection refused" (no game);
    - `scripts/koko/gta_game.sh stop` (laptop) -> "GTA stopped; available memory: 10514 MiB";
    - `./sim/run.sh stop` -> "Stopped and removed horus-sim";
    - `rm -rf sim/run/gta-scratch-map` -> gone. The same dir in `~/horus-loop` was deleted too.
  - **The other quoted commands:**
    - `./sim/run.sh start horus_indoor.sdf` (indoor SITL paragraph) was refused while the GTA container ran (H2, F1 guard);
    - the indoor profile load `common.parm`+`indoor.parm` is the H0/H1 runs' `MCP config … (SITL params: …common.parm:…indoor.parm)` line;
    - `pgrep -f '[G]TA5_Enhanced'` is the guard F1 runs, exercised by the refusal above.
  - **Tests:** documentation only, plus `common.parm` (H0), whose tests pass (H0).

## H. End-to-end proof on Koko

- [x] H0 Off-pad LAND never completes indoors (found in the E2 smoke; must be resolved before H1-H3, manager decision iteration 17). After `take_off 1.5` and `goto (-2, 2, 1.5)` (clear floor), `land` descends to up_m 0.19 (ground_distance 0.23), then stays armed in LAND for 20+ s, so no `landed` event. This was traced in 3 runs. It is the same with `FENCE_ENABLE 0` set at run time, so the E1 fence is not the cause; E2 does not touch LAND. The run's dataflash has "PrecLand: Failsafe Measures" (ArduPilot PLND, which no repo parm sets; check the SITL/gazebo-iris defaults) and no land-complete event. LAND at home on the pad does complete. Find the root cause (PLND enabled with no target off the pad, the land detector, or the 0.19 m resting altitude) and fix it in the ArduPilot params or HORUS. Acceptance: on a Gazebo indoor smoke, `land` after a goto to a clear off-pad point disarms, with a `landed` event within 30 s, and `land`/`rth` at home still land (precision landing stats show the tag acquisition). — DONE: `config/ardupilot/common.parm` (`PLND_ENABLED 0` with its reason); `sim/README.md` indoor SITL paragraph (PLND, the NED/world mapping, the clear test point and why `(-2, 2)` fails). Fix decided by the manager (it30 ask "h0fix": "PLND off + correct test point", no HORUS code change).
  - **Root cause:** NED `(-2, 2)` is not clear floor. Gazebo's frame is ENU, so it is world `(2, -2)` (`gz model -m horus_iris -p` read `[2.026730 -2.031380 1.502010]`), directly above the static `person_laying` model at world `(2.4, -2.2)`. That is an `ElderMalePatient.obj` collision mesh up to 1.09 m tall, and the rangefinder alternated between 0.97/1.11 and 1.54 m while hovering there. Dataflash of the earlier E2 run `20261029T171411Z-709663`:
    - descending at about 0.9 m, the rangefinder dropped to 0.41 m and the attitude went to roll −24°/−36° and pitch +22° against a commanded roll of about 1°: the drone struck the mesh;
    - it came to rest on the floor at NED `(-1.00, 1.79)`, Alt 0.19, SAlt 0.23 (the normal resting reading; the pad gives Alt 0.21 and SAlt 0.23), with ThO 0;
    - LAND's position target stayed at `(-2.03, 2.03)`, so DesRoll/DesPitch held at 16°/−25°. That exceeds `LAND_CHECK_LARGE_ANGLE_RAD` (15°, `ArduCopter/land_detector.cpp`), so there was no EV 17/18 and no disarm.
  - **Reproduced live** (run `20261029T182037Z`): about 7 s hover, then deflected to `(-0.96, 1.83)`, armed in LAND at up 0.19 for 20+ s. A later `rth` left the model tipped (pitch 0.98 rad) on the mesh, so the stack was restarted.
  - **PLND:** the image's `Tools/autotest/default_params/gazebo-iris.parm` sets `PLND_ENABLED 1` and `PLND_TYPE 3`, a Gazebo IR-lock source that nothing feeds. In TARGET_NEVER_SEEN, AC_PrecLand_StateMachine returns FAILSAFE ("PrecLand: Failsafe Measures"), and at the default strictness that means HOLD_POS for 7 s before descending. GTA's `-f quad` is not affected.
  - **Smoke with the old params** at the clear point NED `(2, -3)` (world `(-3, 2)`): `land` -> `landed` after 12.4 s, including the 7 s hover.
  - **Smoke after the fix** (run `20261029T182626Z-844652`):
    - read back over TCP 5762: `PLND_ENABLED 0.0`, `FENCE_ENABLE 1.0`, `FENCE_ALT_MAX 2.2`;
    - `take_off 1.5` succeeded and `goto (2, -3, 1.5)` succeeded; `land` -> `landed` 6.3 s after the call at `(2.00, -3.13, 0.19)`. The dataflash shows LAND at 42.64 s and disarm at 47.62 s;
    - back at the pad (`goto (0.3, 0.3)`), `land` -> `landed` after 10.2 s at `(0.04, 0.00)`, precision `acquisitions 1`, `completed_landings 1` ("acquired AprilTag at 0.34 m from home, 1.33 m up");
    - re-armed on the pad, `goto (0, -2, 1.5)`, then `rth` -> `landed` after 24.3 s at `(0.01, -0.01)`, `acquisitions 2`, `completed_landings 2`, `tag_detections 337` ("acquired AprilTag at 1.97 m from home, 2.20 m up");
    - event history 1-15: task_finished/emergency (source mcp)/landed, with a `landed` after each of the 4 emergencies;
    - the dataflash has no "PrecLand" text; 0 tracebacks in `horus-mcp.log`; `remote_down.sh` left 0 containers.
  - Also seen: ArduPilot resets home at every arming, so an `rth` after re-arming at `(2, -3)` landed there rather than at the pad. That is ArduPilot behaviour; H1 arms on the pad.
  - Tests on Koko `~/horus-loop`: `test_world_profiles` 21, `test_config_profiles` 18 and `test_run_sh_world_guard` 13 pass.
- [x] H1 Gazebo live smoke after A-F. `remote_up.sh horus_indoor.sdf`, then: — DONE: (it30) every output below was observed; the evidence follows the item text.
  - `list-tools` shows exactly the kept set, recorded in DONE;
  - `take_off {"altitude_m":1.5}`, `goto` inside the room, `turn {"by_deg":90}`, `look {"pitch_deg":-30}`, `get_frame`, `land` all succeed, with `wait_for_task` statuses recorded;
  - `rth` from 2 m away uses precision landing;
  - `FENCE_*` params read back as set in E1;
  - no HORUS process logs a traceback (`remote_fetch_logs.sh`);
  - `remote_down.sh` leaves no container.

  Acceptance: those outputs are pasted, in short form, into DONE. Evidence (it30, run `20261029T183143Z-855855`, after the H0 fix):
  - `list-tools`, 29 tools, the same set as E3: get_state get_map get_link_status take_off goto move turn look hold rth land get_frame get_raw_frame list_candidates point set_vocabulary set_mode get_candidate_motion watch_region list_region_watches remove_region_watch get_task_status wait_for_task wait_events wait_for_event cancel find_place get_area_map mark_place.
  - Base moves, each `wait_for_task` status:
    - `take_off {"altitude_m":1.5}` succeeded (4.01 s);
    - `goto {"pose":{"north_m":0,"east_m":-2,"up_m":1.5}}` succeeded (2.01 s);
    - `turn {"by_deg":90}` succeeded (2.5 s), heading 358.83;
    - `look {"pitch_deg":-30}` -> `{requested_pitch_deg -30, pitch_deg -30}`, and `get_frame` frame 207 reported `gimbal_pitch_deg -30.01`;
    - `land` -> `landed` after 6.1 s at `(-0.01, -2.00, 0.19)`. The tag was not in view, so it landed in place.
  - Re-home: take_off, `goto (0.3, 0.3)`, `land` -> `landed` 10.4 s at `(0.01, -0.04)`, precision `acquisitions 1`.
  - `rth` from 2 m: take_off on the pad, `goto (0, -2, 1.5)`, `rth` -> `landed` after 20.3 s at `(-0.01, -0.04, 0.21)`, precision `acquisitions 2`, `completed_landings 2`, `tag_detections 299`.
  - FENCE readback over TCP 5762: `FENCE_ENABLE 1`, `FENCE_TYPE 1`, `FENCE_ACTION 2`, `FENCE_ALT_MAX 2.2`, `FENCE_MARGIN 0.2` (E1's `indoor.parm`), plus `PLND_ENABLED 0` (H0).
  - **Tracebacks:** `remote_fetch_logs.sh` failed. Its second rsync copies all of Koko's `~/horus/runs/` (4.9 GB) into the laptop's `runs/`, and the laptop disk filled ("No space left on device"; 7.9 MB free). I reverted that write by birth time:
    - the 6 files it had replaced were restored from their fresh `*.before-koko` backups (`sim/logs/horus-mcp.log`, `horus-grounder.log`, `horus-grounder.pid`, `s5_run1_gt.jsonl`, and 2 under `runs/`);
    - the 192 files and 144 directories it had created (all born in the last 30 min) were deleted;
    - the 58 older `*.before-koko` files are untouched, and 2.4 GB is free again.

    The same logs were then checked on Koko directly: `grep -c Traceback` is 0 in `sim/logs/horus-mcp.log` and `horus-grounder.log`, and in the run's `gimbal.log`, `tof_bridge.log`, `sitl.log` and `gazebo.log`.
  - `remote_down.sh` -> "Stopped and removed horus-sim"; `docker ps -a` 0 and `pgrep gz sim` 0.
- [x] H2 GTA live smoke after A-F, with Gazebo stopped. (F3 already built the scratch map and temporary config at `sim/run/gta-scratch-map/{map.json,walkable.json,horus_gta_smoke.yaml}` in both Koko copies; run from `~/horus`, which has `models/`, and delete that dir from both copies afterwards.) `horus_gta.yaml:67` needs `data/maps/gta/map.json`, which is missing (only `walkable.json` and `source/` exist) and without which the server refuses to start. `data/` is a no-touch path (Rules), so neither generate nor copy anything into it, locally or on Koko. Instead, build the map outside protected paths (e.g. run `scripts/maps/fetch_gta_map.py` with its output and source cache redirected to a scratch dir such as `/tmp/horus-gta-map/`, adding an output option only if the script lacks one). Then point a temporary smoke config (a copy of `horus_gta.yaml` outside `config/` and `data/`, with `map.data_path` and `map.walkable_path` at the scratch files) at it, pass that config explicitly for this smoke only, and delete both afterwards. Then `run.sh start --gta`, `start_mcp.sh horus-sim` (the explicit temporary config replaces "no config argument" only when the map is needed), then: — DONE: (it30) proven in pose mode, with the game-frame part BLOCKED (no signed-in Steam client, display outputs off); the evidence follows the item text.
  - `get_state` shows `gta://`;
  - `take_off` and `land` succeed, and `FENCE_*` read back as set in E1;
  - with the game up (`gta_game.sh start`), `list_candidates` returns a numbered frame;
  - `run.sh start horus_indoor.sdf` is refused while GTA runs (F1).

  A screen lock or a missing Steam sign-in blocks only the game-frame part: mark that part BLOCKED, with what was tried, and still prove the rest in pose mode without the game. Acceptance: outputs in DONE; `run.sh stop` and `gta_game.sh stop` leave nothing running. Evidence (it30, Koko `~/horus`, run `20261029T183817Z-868665`; Gazebo stopped and 0 containers before the start), run with the `sim/README.md` GTA smoke block as written (G3):
  - **Start:**
    - `run.sh start --gta` -> GTA pose stream on 5762;
    - the scratch map was rebuilt into `sim/run/gta-scratch-map/` (1571 places, 1283 ways, 35 areas), and its `horus_gta_smoke.yaml` was passed explicitly as `start_mcp.sh horus-sim "$config"`;
    - the scratch config leaves out `map.walkable_path`: the navmesh build needs about 1.1 GB, against Koko's 1.3 GB free (the README states this).
  - `get_state` -> `perception {source gta://127.0.0.1:5610, camera stalled, reason "[Errno 111] Connection refused"}`. The log has "HORUS config /horus/sim/run/gta-scratch-map/horus_gta_smoke.yaml (perception.source gta://127.0.0.1:5610)" and "Gazebo image publishers are off".
  - FENCE readback on serial2 TCP 5763 (5762 is the bridge): `FENCE_ENABLE 1`, `TYPE 11`, `ACTION 1`, `MARGIN 2`, `RADIUS 40`, `ALT_MAX 48`, `ALT_MIN 6` (E1's `gta.parm`); `PLND_ENABLED 0` (the `-f quad` default).
  - `take_off {"altitude_m":10,"ttl_s":120}` succeeded (8.53 s), with pose up 10.0. `land` -> `landed` after 24.1 s at `(0.00, -0.01, -0.02)`.
  - **F1:**
    - `HORUS_SIM_IMAGE=horus-sim:local-s1s2 ./sim/run.sh start horus_indoor.sdf` -> "Container horus-sim already exists…", exit 1;
    - with `HORUS_SIM_CONTAINER_NAME=horus-sim-h2` -> "HORUS world container horus-sim (gta) is already running; stop it first…", exit 1;
    - `docker ps` then showed only `horus-sim gta`, and `pgrep gz sim` was 0.
  - **Game-frame part — BLOCKED:** Koko has no running Steam client, so signing in needs someone to select the account in Steam. Also, every display output is disabled, and Xorg DPMS reports "Monitor is Off".
    - Tried: `xset dpms force on` on `:0`, after which the outputs were still `disabled`; the screensaver is inactive (`cinnamon-screensaver-command -q`).
    - Then the documented `scripts/koko/gta_game.sh start` (laptop) -> "Steam is not signed in (no running Steam client); not starting GTA. Select the account in Steam first."
    - The quoted `client list_candidates` -> "perception unavailable: [Errno 111] Connection refused".
    - I did not start Steam: a fresh client stops at its account picker, and its updates would compete for Koko's 1.3 GB of free disk.
  - **Stop:**
    - `scripts/koko/gta_game.sh stop` (laptop) -> "GTA stopped; available memory: 10514 MiB";
    - `./sim/run.sh stop` -> "Stopped and removed horus-sim";
    - afterwards `docker ps -a` is 0, and `pgrep` finds no GTA5_Enhanced, `gz sim` or arducopter process;
    - 0 tracebacks in `sim/logs/horus-mcp.log` and in the run's `gta_bridge.log`, `mavlink_keeper.log` and `sitl.log`;
    - `sim/run/gta-scratch-map` was deleted from both `~/horus` and `~/horus-loop`.
- [x] H3 Terminal emergency path on the live Gazebo stack (A2). In the container, run `printf '/home\n/quit\n' | docker exec -i -e HORUS_MCP_TOKEN=… -w /horus horus-sim /opt/horus-venv/bin/horus --server-url http://127.0.0.1:8000/mcp` while airborne, after a `take_off`. It must trigger `rth` without starting a model turn, shown in `$HORUS_EVENT_LOG` with an `emergency` event, source operator, and no commander turn. Acceptance: the event log lines are in DONE. — DONE: (it30) the REPL now logs its own operator emergencies, and the live `/home` record is below.
  - **First live run** (H1 stack): `/home` ran `rth` -> `landed` 12.8 s later, with no commander records. But nothing named the operator. The REPL without a commander calls MCP `rth` directly, so the log held only the MCP tool-call record, and the executor event carried `source: mcp`. Only `commander/server.py` writes `source: operator`.
    - Manager decision (it30 ask "h3source"): record operator emergencies in the REPL using the existing event-log pattern, test it and re-run H3.
  - **Code:**
    - `horus/event_log.py`: new `event_log_from_environment()` (the shared log, or `None` when no log is configured), moved from `commander/agent.py`'s private `_event_log_from_environment`, which `HorusCommander` now imports instead;
    - `horus/repl/app.py`: `_record_emergency` appends `{kind: emergency, action, source: operator, status: executed, result}` (or `status: error, error`) for every emergency, with or without a commander. This is the shape `commander/server.py` uses. A failed append only warns and never blocks the emergency;
    - `README.md:116` says so (on top of the held text, as G1 was).
  - **Tests:**
    - `test_repl.py`: new `test_repl_home_without_a_commander_is_logged_as_an_operator_emergency`. A real REPL subprocess runs `/home` then `/quit` against an in-process MCP server; the flight gets `rth`, and the event log holds exactly one record, the operator `rth` with `executed`;
    - the commander-path test now also asserts one operator `hold`/`executed` record.

    On Koko: `test_repl` 24, `test_commander_server` 15, `test_claude_code_commander` 10, `test_mcp_startup` 15 and `test_events` 3 pass.
  - **Live** (run `20261029T184340Z-878778`, `remote_up.sh horus_indoor.sdf`): `take_off 1.5` succeeded (up 1.62), then the exact command above printed `{"action": "rth", "state": "RTH", "preempted": true}`. `/runs/horus/20261029T184346.363336Z_f88e6a3c/events.jsonl` (`$HORUS_EVENT_LOG` resolved from `runs/horus/current-run`):
    - line 6: `{"timestamp": "2026-10-29T18:44:19.844960+00:00", "tool": "rth", "result": "{\"action\":\"rth\",\"state\":\"RTH\",\"preempted\":true}"}`;
    - line 7: `{"kind": "emergency", "action": "rth", "source": "operator", "status": "executed", "result": {"action": "rth", "state": "RTH", "preempted": true}, "timestamp": "2026-10-29T18:44:19.858646+00:00"}`;
    - records with a commander provider source (`claude-code`/`anthropic`/`openai`): 0, so no model turn.

    MCP events: `emergency` (`source: mcp`) at 18:44:19, then `landed` 12.1 s after the REPL call at `(0.00, 0.00, 0.21)`, precision `acquisitions 1`, `completed_landings 1`. 0 tracebacks; `remote_down.sh` left 0 containers and no gz/arducopter process.

## Summary (iteration 30)

**Done: 33 of 33 items**, every one ticked `- [x]` with `DONE:` evidence: A1-A2, B1-B3, C1-C6, D1-D4, E1-E5, F1-F6, G1-G3 and H0-H3.
- **E5** is a new item, found during H1: the MCP tool texts still promised HORUS clamps.
- **H2** is done in pose mode, with its game-frame part BLOCKED inside the item. Koko has no signed-in Steam client and every display output is off. `gta_game.sh start` refuses with "Steam is not signed in (no running Steam client)", and `xset dpms force on` did not enable an output. Someone has to select the Steam account and turn the screen on before `list_candidates` can return game frames.

**Invalid:** none. **Blocked items:** none.

**What HORUS is now:**
- The Terminal and AI pilot decide. Emergencies skip the model, including `/home`/`home`, and the REPL logs them with `source: operator`.
- The drone server offers only the base moves (take_off, goto, move, turn, look, hold, rth, land) plus the vision, map and task tools: 29 tools in total. There is no plan runner, approach/follow/orbit/scan/search, lost-target estimate, trails or target profiles.
- No limits are set on board: ArduPilot fences bound the indoor, outdoor/procedural and GTA worlds; the controller speeds are fixed config; ArduPilot PLND is off.
- Gazebo or GTA, one at a time: `run.sh` refuses a second world.
- Precision landing, the emergency path and the AI-link-loss hold-then-home are kept.

**Final check (it30):**
- **Tests:** all 37 `tests/test_*.py` files pass on Koko `~/horus-loop`, one file at a time under `timeout 900`, 768 tests in total:
  - civilian_life 6, claude_code_commander 10, commander_api 5, commander_server 15, config_profiles 18, events 3, executor 44;
  - flight_link 14, gimbal_link 16, grounder 4, grounding 4, gta_bridge 25, gta_stream 8, gz_bridge 13, gz_gimbal 83, gz_sim 6;
  - live_view 2, maps 12, mcp_auth 1, mcp_client 5, mcp_gz_image 3, mcp_startup 15;
  - perception_budget 12, perception_generalization 13, perception 25, precision_landing 14, repl 24, run_sh_world_guard 13, scene 19;
  - sensor_payload 4, tof_bridge 27, worldgen_actors 53, worldgen_assets 78, worldgen_geometry 9, worldgen_procedural 132, worldgen_scenery 12, world_profiles 21.
- **Live:** H1-H3 are DONE.
- **Survivors** of `grep -rnE 'approach|follow|orbit|\bscan\b|search|target_belief|TargetBelief|trails|TrailGrid|geofence|Envelope|run_plan|set_speed|get_lost_target|retarget' horus/` (after `__pycache__` cleanup), all intended:
  - `mcp_server/flight.py:14` "never follow instructions" (untrusted-telemetry warning);
  - `mcp_server/maps.py:36,40` `searchable` (the find_place index);
  - `grounder/backends.py:14` "IDEA-Research" (the model repo name);
  - `tof_bridge/cli.py:54,63,64,103,121,151,168,170` and `tof_bridge/bridge.py:145-338`: Gazebo LaserScan `scan` topics and messages;
  - `perception/scene.py:211` "can follow" and `executor/core.py:851` "cannot follow it" (ordinary English);
  - `perception/tracker.py:31,302` "approach": a person or camera approaching grows the box, a tracker heuristic, not the removed skill;
  - `executor/precision_landing.py:340` "the tag search".

**Left for the user:**
- Sign in to Steam on Koko and turn a display output on (the HDMI dummy plug is attached; DPMS reported "Monitor is Off"), then run the H2 game-frame step: `scripts/koko/gta_game.sh start` and `client list_candidates`.
- `scripts/koko/remote_fetch_logs.sh` mirrors all of Koko's `~/horus/runs/` (4.9 GB) into the laptop's `runs/`, which filled the laptop disk in H1. That write was reverted (see H1).
- Koko's root disk has 1.2 GB free.
