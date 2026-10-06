# Baseline

Measured on 2026-10-06 (UTC) before any code or config change.

- Mission: `missions/S6_find_red_shirt.yaml`, run three times, each on a fresh simulator.
- Commit: `5de8186027` (`main`), with one uncommitted local change: `sim/run.sh` (WSL2 GPU rendering
  through Mesa D3D12). The measurement scripts in `perf/` were also untracked at the time.
- Config in use (`config/horus.yaml`): `claude_model: opus`, `claude_effort: high`,
  `command_ttl_s: 30.0`, `link_loss_rth_delay_s: 30.0`, `onnx_intra_op_num_threads: 2`, `max_fps: 5.0`.
- Host: Windows 11 + WSL2 (Ubuntu 24.04, 12 logical cores, 7.6 GB), RTX 3060 Laptop GPU.
- Simulator: headless (`./sim/run.sh start horus_indoor.sdf`, no Gazebo window). The GUI demo is
  slower: it ran at roughly 0.4 real-time factor on the same machine.

## Results

| Run | Wall s | RTF | Perception fps | Frame ms | Tool calls | Failed calls | Model turn avg s | Model turn max s | Tool round-trip avg s | Tool round-trip max s | Success | Home before commanded | Landing error m |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline-run1 | 137.8 | 0.852 | 4.97 | 140.1 | 22 | 1 | 5.7 | 77.7 | 0.3 | 5.5 | yes | yes | 0.03 |
| baseline-run2 | 60.0 | 0.937 | 5.0 | 111.0 | 28 | 0 | 1.8 | 8.9 | 0.2 | 5.0 | yes | no | 0.025 |
| baseline-run3 | 94.8 | 0.817 | 4.98 | 132.5 | 31 | 0 | 2.7 | 27.6 | 0.2 | 5.5 | yes | no | 0.04 |
| **median** | **94.8** | **0.852** | **4.98** | **132.5** | **28** | **0** | **2.7** | **27.6** | **0.2** | **5.5** | 3/3 yes | 1/3 yes | **0.03** |

Per-run minimum / average / maximum values are in `perf/runs/<run>/summary.md` (not committed).

## What the columns mean

- **Wall s**: wall-clock time of `horus --mission ...`, from start to exit.
- **RTF**: simulated seconds per wall-clock second over the mission, from `/world/horus_indoor/stats`.
- **Perception fps / Frame ms**: average of the MCP server's 10-second throughput reports during the
  mission; frame time is detector plus tracker/ReID.
- **Tool calls / Failed calls**: commander (Claude) MCP tool calls, and those returned as errors.
- **Model turn**: time from the previous tool result (or CLI start) until Claude issues its next tool
  call or final answer. This is thinking time, not flight time.
- **Tool round-trip**: time from a tool call to its result (includes `wait_for_task` blocking).
- **Success**: the rule from `sim/scripts/s6_analyze.py`, on Gazebo ground truth: the drone moved at
  least 0.5 m, came within 2.0 m of `person_red`, and `person_red` was the person it came closest to.
- **Home before commanded**: the drone touched down before Claude called `rth`/`land`, meaning the
  hold timeout sent it home while Claude was still thinking.
- **Landing error**: ground-truth horizontal distance between the drone and the home pad at the end.

## Observations

- Run-to-run variation is large on an identical config (60 s to 138 s), and almost all of it is
  Claude's thinking time: one 77.7 s turn in run 1, one 27.6 s turn in run 3. Differences smaller than
  that spread between later steps are not meaningful with three runs.
- In run 1 the hold timeout fired during the 77.7 s turn: the drone returned home and landed by itself
  before Claude asked for it. The mission still counted as a success because the approach had
  already happened.
- The one failed call (run 1) was `point` given both `description` and `box`, which the tool rejects.
- Perception sits at the configured `max_fps: 5.0` cap in headless mode.
