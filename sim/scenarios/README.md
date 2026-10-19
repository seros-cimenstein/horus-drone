# Civilian-life scenarios

`sim/scripts/civilian_life.py` reads a strict YAML scenario (parsed and
validated by `sim/campaign/civilian_spec.py`) and drives selected Gazebo models
with `/world/<world>/set_pose_vector`. The scenario `world` value must match the
Gazebo world name. Unknown keys and invalid values are errors.

## Schema

```yaml
world: horus_urban
rate_hz: 10                 # pose update rate in simulation time; default 10
seed: 7                     # stable per-model wander seed; default 7
ground:                     # optional; omitted means flat z=0
  origin_xy: [-75.0, -75.0]
  cell_m: 2.0
  file: horus_forest_ground.npy  # relative to this YAML file; heights[row=y][col=x]
agents:
  - model: civ_commuter_01
    kind: pedestrian        # pedestrian | vehicle
    group: ambient          # default ambient
    behavior: route         # route | wander | idle
    speed_m_s: 1.3
    mode: loop              # route only: loop (default) | pingpong | once
    start_offset_s: 0.0     # route only: initial distance is speed * offset
    accel_m_s2: 1.5         # optional; default instant for pedestrians, 1.5 for vehicles
    waypoints:              # route only; non-empty list
      - {x: 1.0, y: 2.0}    # z optional; hold_s optional (default 0)
      - {x: 10.0, y: 2.0, hold_s: 4.0}
    z_offset: 0.0           # ground-following offset; default 0
```

Every agent requires a unique `model`, `kind`, and `behavior`. Route agents
require positive `speed_m_s` and `waypoints`; `mode` and `start_offset_s` are
optional. A waypoint's explicit `z` is used as given; otherwise its height is
the sampled ground plus `z_offset`. Ground grids are NumPy 2D float arrays;
height lookup is bilinear and clamps to the nearest edge.

A `wander` agent uses `area: [[x,y], ...]`, positive `speed_m_s`, and optional
`pause_s: [minimum, maximum]` (default `[0, 0]`). It chooses its initial
position and reachable goals uniformly inside the polygon. Each model has an
independent RNG stream derived from `seed` and its name, so scenario ordering
does not change its path. A pedestrian `idle` agent uses
`pose: {x: ..., y: ..., yaw_deg: ...}`; optional `fidget_deg` (default 0) and
`fidget_period_s` (default 6) create smooth sinusoidal yaw motion.

Routes progress, waypoint dwell, acceleration, and wander pauses use Gazebo
simulation time. Headings turn at a limited rate; vehicles use a 5 m minimum
turn radius and a reduced speed at route corners. Pedestrians pass through
each other. Vehicles simply follow their routes with their configured
acceleration and the route-corner speed limit.

## CLI

```text
civilian_life.py --scenario PATH [--group NAME ...] [--trace PATH] [--duration-s S]
```

`--group` may be repeated; only matching agents are animated, and the default
is `ambient`. `--trace` appends JSON Lines at no more than 2 Hz, with each row
containing simulation time `t` and model poses `[x, y, z, yaw_rad]` under
`agents`. `--duration-s` stops after that many simulation seconds; without it,
the driver runs until SIGINT or SIGTERM. The driver paces ticks using the
Gazebo simulation clock, so motion duration is unaffected by real-time factor.

On startup the driver waits up to 120 wall-clock seconds for the selected
world's `/world/<world>/stats` topic and exits with that topic in the error if
no statistics arrive.
