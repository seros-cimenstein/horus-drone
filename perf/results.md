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
