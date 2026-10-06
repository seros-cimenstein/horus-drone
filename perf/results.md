# Results after each step

Same mission and method as `perf/baseline.md`: `missions/S6_find_red_shirt.yaml`, three runs per
step on a fresh headless simulator, columns as defined there. Baseline median for reference:
94.8 s wall, 28 tool calls, 0 failed, model turn 2.7 s average / 27.6 s maximum, 3/3 success,
1/3 runs sent home by the hold timeout.

The baseline's own spread was 60 s to 138 s, so a change in the median smaller than that is not
evidence of an effect by itself.

## Step 2a: hold-to-return-home delay 30 s -> 120 s

Change: `executor.link_loss_rth_delay_s: 30.0 -> 120.0` in `config/horus.yaml`.

The request named `command_ttl_s`, but that key is only the default lifetime of a skill that is
called without `ttl_s` (`horus/executor/core.py`, `submit_skill`). The delay before a holding
vehicle returns home is `link_loss_rth_delay_s` (`core.py`, `tick`), so that is the key changed.
`command_ttl_s` stays at 30 s; raising it would only let a blocked skill run four times longer. The
return-home behavior itself is unchanged. The same delay also governs a real commander link loss,
which now waits 120 s instead of 30 s before returning home.

| Run | Wall s | RTF | Perception fps | Frame ms | Tool calls | Failed calls | Model turn avg s | Model turn max s | Tool round-trip avg s | Tool round-trip max s | Success | Home before commanded | Landing error m |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| step2a-run1 | 68.6 | 0.936 | 5.0 | 109.4 | 27 | 0 | 2.2 | 14.4 | 0.2 | 4.4 | yes | no | 0.004 |
| step2a-run2 | 75.8 | 0.958 | 4.99 | 111.7 | 23 | 0 | 2.8 | 18.9 | 0.3 | 5.0 | yes | no | 0.004 |
| step2a-run3 | 55.2 | 0.768 | 4.98 | 145.8 | 25 | 0 | 1.8 | 9.0 | 0.3 | 6.6 | yes | no | 0.008 |
| **median** | **68.6** | **0.936** | **4.99** | **111.7** | **25** | **0** | **2.2** | **14.4** | **0.3** | **5.0** | 3/3 yes | 0/3 yes | **0.004** |

Outcome: kept. No run was sent home by the hold timeout (baseline: 1 of 3). The longest thinking
pause in these runs was 18.9 s, which would not have triggered the old 30 s delay either, so this
set does not by itself prove the fix; the mechanism is confirmed by the baseline run where a 77.7 s
pause did trigger it. Mission time is within the baseline's spread.

## Step 2b: exact parameter names in the tool descriptions

Change: docstrings of the MCP tools in `horus/mcp_server/server.py` (these are the tool descriptions
Claude sees). No behavior change.

- `scan`: lists the exact `area` keys (`north_min_m`, `north_max_m`, `east_min_m`, `east_max_m`
  required; `altitude_m`, `spacing_m` optional) with defaults and an example.
- `goto`: lists the exact `pose` keys (`north_m`, `east_m`, `up_m`) with an example.
- `approach`, `follow`, `orbit`: `id` is a positive integer tracker ID; units of the distance argument.
- `point`: pass exactly one of `description` or `box` (the failed call in baseline run 1).
- `wait_for_task`: `task_id` must be the string returned by a skill; `hold`/`rth`/`land` return none
  (the `wait_for_task("pending")` error seen in the GUI session).
- `hold`, `rth`, `land`: take no arguments and return an action status, not a task ID.

| Run | Wall s | RTF | Perception fps | Frame ms | Tool calls | Failed calls | Model turn avg s | Model turn max s | Tool round-trip avg s | Tool round-trip max s | Success | Home before commanded | Landing error m |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| step2b-run1 | 83.1 | 0.836 | 4.98 | 135.0 | 29 | 0 | 2.5 | 24.9 | 0.3 | 6.6 | yes | no | 0.015 |
| step2b-run2 | 68.7 | 0.848 | 4.92 | 124.9 | 30 | 0 | 1.9 | 12.7 | 0.3 | 5.5 | yes | no | 0.036 |
| step2b-run3 | 53.8 | 0.865 | 4.97 | 126.6 | 23 | 0 | 1.9 | 8.2 | 0.3 | 6.5 | yes | no | 0.025 |
| **median** | **68.7** | **0.848** | **4.97** | **126.6** | **29** | **0** | **1.9** | **12.7** | **0.3** | **6.5** | 3/3 yes | 0/3 yes | **0.025** |

Outcome: kept. No failed calls in three runs (baseline: 1 in three runs, the `point` call), which is
too few events to call a measured improvement. S6 never calls `scan`, so the scan parameter fix is
exercised in the Stage 2 scan check below, not here. Mission time is unchanged within noise.

## Step 2c: commander effort high -> medium (measured, then reverted)

Change tested: `commander.claude_effort: high -> medium`, `claude_model: opus` unchanged. Three runs
were planned; three more were added after run 2 missed the success rule.

| Run | Wall s | RTF | Perception fps | Frame ms | Tool calls | Failed calls | Model turn avg s | Model turn max s | Tool round-trip avg s | Tool round-trip max s | Success | Home before commanded | Landing error m |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| step2c-run1 | 48.8 | 0.906 | 5.01 | 117.0 | 36 | 0 | 1.1 | 7.4 | 0.2 | 4.2 | yes | no | 0.033 |
| step2c-run2 | 85.0 | 0.948 | 5.0 | 117.1 | 38 | 1 | 2.0 | 11.2 | 0.2 | 5.0 | no | no | 0.029 |
| step2c-run3 | 55.1 | 0.854 | 5.0 | 127.7 | 27 | 0 | 1.7 | 8.6 | 0.2 | 4.7 | yes | no | 0.023 |
| step2c-run4 | 56.2 | 0.809 | 4.97 | 133.6 | 36 | 0 | 1.4 | 9.0 | 0.3 | 6.1 | yes | no | 0.028 |
| step2c-run5 | 62.2 | 0.785 | 4.86 | 143.2 | 29 | 0 | 1.8 | 12.0 | 0.3 | 6.2 | yes | no | 0.005 |
| step2c-run6 | 57.4 | 0.825 | 5.02 | 131.1 | 30 | 0 | 1.6 | 9.7 | 0.3 | 5.2 | yes | no | 0.022 |
| **median** | **56.8** | **0.839** | **5.0** | **129.4** | **33.0** | **0.0** | **1.65** | **9.35** | **0.25** | **5.1** | 5/6 yes | 0/6 yes | **0.026** |

Outcome: reverted to `high`, because the keep rule was "the mission still succeeds with no extra
failed calls" and run 2 did not meet it.

What the numbers show:

- Faster: median 56.8 s against 68.7 s for step 2b, and the longest thinking pause dropped from
  8-25 s to 7-12 s. Medium effort makes more, shorter calls (median 33 tool calls against 29).
- Run 2 missed the success rule narrowly: the drone travelled 0.40 m (rule: at least 0.5 m) and came
  within 1.99 m of the red-shirt person (rule: at most 2.0 m).
- In run 2 Claude picked the right person every time. The `approach` skill rejected it three times in
  a row with "failed appearance-embedding revalidation", and the fourth attempt reported success
  without moving. The same revalidation failure appears in 9 of the 12 runs before this step, at
  high effort too (one or two per run), so it is a property of the approach skill, not of the effort
  setting.
- The one failed call was `point` with a text description, rejected because the optional
  `grounding` extra is not installed. It was not a wrong-parameter error.

So the evidence that medium effort lowers quality is one marginal miss in six, with a cause that is
visible at high effort as well. That is not enough to say medium is worse, and not enough to say it
is safe. It is worth re-testing once the approach skill's revalidation failures are understood.

## Stage 2: scan progress check (and two follow-up fixes)

Check mission: `perf/missions/scan_room.yaml` ("Scan the room and tell me who you see."), same
harness, effort `high`, hold delay 120 s. In every run Claude asked for the full flight envelope
(north -2.8..2.8, east -3.8..3.8 at 1.5 m, 8 waypoints). "Crash" is ArduPilot's own crash-disarm
status text; "closest person" is Gazebo ground truth while airborne, ignoring the person on the bed.

| Code | Run | Wall s | Scan task outcome | Skipped waypoints | Crash | Closest person m |
|---|---|---|---|---|---|---|
| Before (no progress check) | scan-before-run1 | 240.2 | `expired` after 177 s, blocked at the cabinet | none | no | 2.26 |
| Progress check only | scan-after-run1 | 174.2 | `succeeded` after 131 s | 1, 6, 7 | yes | 0.22 |
| Progress check only | scan-after-run2 | 167.3 | `succeeded` after 128 s | 1, 7 | yes | 0.14 |
| Progress check only | scan-after-run3 | 163.9 | `succeeded` after 122 s | 1, 6, 7 | yes | 0.10 |
| + disarm failure, + face travel | scan-fixed-run1 | 236.2 | `failed` after 155 s (vehicle disarmed) | 0, 1, 6 | yes | 0.25 |
| + disarm failure, + face travel | scan-fixed-run2 | 215.1 | `failed` after 131 s (vehicle disarmed) | 0, 1, 4, 6 | yes | 0.30 |
| + disarm failure, + face travel | scan-fixed-run3 | 207.1 | `succeeded` after 138 s | 1, 4, 6, 7 | no | 0.19 |

In the two `scan-fixed` crash runs Claude first submitted a scan without `ttl_s`, which expired at the
30 s default, and then re-submitted it; the times above are for the second scan.

### What was built

- **Progress check** (`horus/executor/core.py`, scan loop): a waypoint is skipped when the distance to
  it has not dropped by `scan_progress_threshold_m` (0.15 m) within `scan_progress_timeout_s` (15 s).
  Each skip is logged with waypoint, vehicle position, remaining distance and time, and listed in the
  task's `skipped_waypoints`. A scan that skips every waypoint fails early. Both settings are in
  `config/horus.yaml`.
- **Disarm failure**: a scan whose vehicle is disarmed fails with a clear error instead of recording
  the remaining waypoints as skipped.
- **Face travel**: before each leg the vehicle turns toward the waypoint and holds that heading
  while flying, so the forward proximity sensor points along the path.

### What the runs show

- **The stall is fixed.** The south-east cabinet waypoint (index 1) is skipped after 15 s in all six
  runs with the check, with the reason logged. Before, the scan sat there until its 180 s limit.
- **The progress check alone exposed a collision.** With the stall gone the scan flies its remaining
  legs, which the old code never reached. On the last leg, west along the north side, the vehicle
  flew into the standing red-shirt person and ArduPilot crash-disarmed it, in 3 of 3 runs. The scan
  then reported `succeeded`, counting the crashed vehicle as one more skipped waypoint.
- **The disarm failure works.** In both later crash runs the scan ended `failed` with
  "scan vehicle is disarmed at waypoint 8/8 ..." instead of `succeeded`.
- **Facing the direction of travel is not enough to stop the collision.** The vehicle did turn to
  face west (heading 274-277 degrees) before the last leg, and still crashed in 2 of 3 runs; in the
  third it passed 0.19 m from the same person without a crash report. The autopilot log of
  scan-fixed-run1 shows why: the forward proximity reading flickered between about 1.2 m and 3 m
  while closing on the person (1.24, 1.93, 2.49, 2.95 m in the last seconds). Avoidance slowed the
  vehicle from 0.8 to 0.15 m/s while the reading was short, then released when it went long again,
  and the vehicle hit the person with the sensor reading 2.9 m.
- **Cause, as far as the logs show.** Each proximity sensor in `sim/models/horus_sensor_payload` is a
  single ray (one sample, zero beam width). At 1.5 m altitude that ray meets a 1.9 m person near the
  neck, so small pitch changes move it on and off the body. A wide obstacle such as the cabinet or a
  wall is detected steadily; a person is not. This is a simulator sensor property, which Stage 2 was
  told not to change.
- **More waypoints are skipped with the heading fix** (3-4 against 2-3), because the forward sensor
  now reports walls and furniture ahead that previously went unseen. Skips cost about 15 s each.
- **Time.** The request took 164-174 s with the progress check alone and 207-236 s with all fixes,
  against 240 s before. The scan task itself takes 122-155 s against 177 s.

### Tests

158 tests pass in the sim image (143 before Stage 2, 15 new). One existing test,
`test_take_off_retries_arming_after_prearm_rejection`, fails on any code when the machine has been up
for less than about 16 minutes: it submits a task with a 30 s lifetime and then pins the clock to
1000 s. It is unrelated to this work and was left unchanged.

Every S6 run so far passes its success rule only narrowly (travel 0.52-0.69 m against a 0.5 m
threshold; closest approach 1.7-1.9 m against the requested 1.5 m), which makes the success column
sensitive to this flake at any effort level.
