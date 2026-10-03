# Session handoff (branch `fix/project-review-findings`, local commits only — NOT pushed)

Pick-up notes for the next session. Delete this file once its items are done.

## State
- Local commits ahead of origin: `25f1eed` … `cdb8785` + this handoff commit. Do not push without asking.
- Held on purpose (uncommitted, user hold on docs): `README.md`, `sim/README.md` edits. Untracked and
  never to commit: `annotated.jpg`, `docs/course/`.
- Commit pattern used: `git add -A -- . ':!README.md' ':!sim/README.md' ':!annotated.jpg' ':!docs/course'`.
- Koko: stopped (no `horus-sim` container). **Koko sysctls were loosened for profiling and must be
  restored** by the user: `sudo sysctl kernel.perf_event_paranoid=4 kernel.yama.ptrace_scope=1`
  (resets on reboot anyway).
- Procedural campaign is paused; relaunch when wanted:
  `HORUS_SIM_IMAGE=horus-sim:local-s1s2-gui HORUS_REMOTE_SIM_IMAGE=horus-sim:local-s1s2-gui ./scripts/koko/procedural_campaign.sh --kinds forest urban --seeds 21 --ttl-scale 2 --timeout-minutes 120`
  (earlier failure "goto ended expired" came from phantom ToF obstacles capping speed; fixed in `f394bb5`).

## RTF optimisation task (live stack on Koko, civilians + grounder + MCP + camera RTP)
Measure with `scripts/koko/profiling/measure.sh horus_forest_p21:forest horus_urban_p21:urban`
(regenerate first: `rm -rf sim/generated/horus_forest_p21 && python3 -m sim.worldgen --procedural --seed 21 --kind forest --out sim/generated`).
RTF = Δiterations / (Δwall·1000) from `/world/<w>/stats` (the stats `real_time_factor` field is wrong under lockstep).

| Step | Change | Forest | Urban | Kept |
|---|---|---|---|---|
| baseline | — | 0.228 | 0.475 | — |
| 4 | camera far clip 15000 → 400 m | 0.226 | 0.469 | no (reverted) |
| 3 | far-field tree thinning (full density ≤120 m of home/sites, 30 % beyond, no far saplings; 7357 → 2802 instances) | **0.601** | — | yes (`cdb8785`) |
| 5 | civilians off entirely (measurement) | 0.229 | 0.520 | no cap (≤10 %) |
| 6 | ToF sensors removed (upper bound for non-rendered ToF) | 0.646 | 0.497 | not built (≤7 % left) |
| 7 | profiling | see below | see below | findings only |

## Step 7 profiling findings (perf + gdb on Koko, urban_p21)
- gpu_lidar (ToF ring/down, `Ogre2GpuRays`) runs a pass-pre-execute material-switcher listener that,
  for **every Ogre item**, calls `BaseScene::VisualById`, which is a **linear scan** over all visuals
  (hot loop `libgz-rendering8-ogre2` 0x1caf30, `BaseObject::Id`). Cost per lidar pass ≈ items × visuals
  → quadratic in scene size. This is why tree count dominated forest RTF and why ToF cost scales with
  scenery. Lever: fewer Ogre items/visuals (merge static scenery/tree instances into tile meshes).
- Urban main thread (40 gdb samples): 70 % `Physics::Update` (DART contact solving:
  `BoxedLcpConstraintSolver`, `ContactConstraint`, `CollisionGroup::update`), 12 %
  `UserCommands::PreUpdate` (civilian set_pose traffic), 10 % waiting on render. Urban is
  **physics-bound**, not render-bound — next: find which bodies are in contact (drone on pad, vehicles,
  civilian models?) and make non-flying models static / drop collisions.
- perf can't symbolise container libs from the host; use gdb with `set sysroot /proc/<pid>/root`
  (`scripts/koko/profiling/gdb_sample.sh`, `koko_perf.sh`).

## Next steps
1. Urban: identify contacting bodies in DART; make them static / collision-free where not needed; re-measure.
2. Forest/urban: merge static scenery visuals (trees per tile) to cut the GpuRays item×visual scan; re-measure.
3. Run `tests/test_worldgen_procedural.py` (per file, docker, `timeout`) after worldgen changes; commit per step.
4. Docs (README.md, sim/README.md) only after the user lifts the hold.
