# HorusGta.dll: GTA V as the drone camera renderer

Two modes; both serve frames through `horus_capture.asi` (`gta/capture`) to HORUS perception
(`perception.source: gta://127.0.0.1:5610`).

- **Physics** (`sim/run.sh start --gta --json-physics`, section "Physics"): ArduPilot SITL runs its
  JSON backend and HorusGta flies a physical quad in GTA from SITL's motor outputs. It sends back
  the quad's state, and SITL derives its IMU, GPS, barometer and compass from it. The camera rides
  on the quad.
- **Pose** (`sim/run.sh start --gta`): SITL flies its built-in quad over a flat plane, and GTA only
  draws the camera. `horus-gta-bridge` (`horus/gta_bridge.py`) reads the vehicle and gimbal attitude
  from SITL over MAVLink and sends the camera pose over UDP. `HorusGta.dll`, a ScriptHookVDotNet v3
  script, puts a scripted camera at that pose every frame.

```
physics: SITL (JSON) <--UDP 9002: servo frames / JSON state--> HorusGta.dll (quad + camera)
pose:    SITL --MAVLink tcp:5762--> horus-gta-bridge --UDP 5611--> HorusGta.dll (scripted camera)
         GTA renders --> horus_capture.asi --TCP 5610--> HORUS
```

## Pose packet

UDP, one datagram per pose, little-endian, 56 bytes:

| Field | Type |
| --- | --- |
| magic `HPOS` | 4 bytes |
| version (1) | u32 |
| sequence number | u64 |
| camera north, east, down (m, NED, relative to SITL home) | 3 x f64 |
| camera roll, pitch, yaw (deg, NED world frame; yaw clockwise from north, pitch positive up) | 3 x f32 |
| horizontal field of view (deg) | f32 |

The mod drops datagrams with a different size, magic or version, any non-finite field, or a field
of view outside (0, 180), and keeps only the newest packet by sequence number. A sequence more than
1000 below the newest one counts as a restarted bridge. `tests/pose_vector.json` is a test vector
that both the C# tests and `tests/test_gta_bridge.py` check.

## Bridge

```bash
horus-gta-bridge                        # SITL serial1 tcp:127.0.0.1:5762 -> udp 127.0.0.1:5611, 50 Hz
horus-gta-bridge --mavlink tcp:127.0.0.1:5762 --target 192.168.1.20:5611 --rate-hz 50 --hfov-deg 84
```

`sim/run.sh start --gta` starts SITL without Gazebo and this bridge in the horus-sim container,
and `sim/run.sh stop` stops both (`sim/README.md`, "GTA V renderer mode"). HORUS then flies on
tcp:5760. `HORUS_GTA_TARGET` sets `--target`, and the container's `HORUS_CONFIG` defaults to
`config/horus_gta.yaml`, so the bridge sends that profile's 84° FOV.

The bridge uses HORUS's own flight link (`horus/flight/link.py`), so it reads MOUNT_STATUS and
GIMBAL_DEVICE_ATTITUDE_STATUS (including `flight.gimbal_status_body_frame`) the same way HORUS does.
It asks for ATTITUDE, LOCAL_POSITION_NED and the gimbal messages at `--rate-hz`. Message rates are
set per MAVLink channel, so HORUS's streams on serial0 do not change.

- Position: the vehicle's LOCAL_POSITION_NED.
- Attitude: gimbal pitch and roll relative to the horizon, and camera yaw = vehicle heading +
  gimbal yaw (relative to the nose). Vehicle roll and pitch are not used: the gimbal is stabilised.
- If no gimbal sample is under 1 s old, the camera is level, faces the nose and uses
  `executor.default_gimbal_pitch_deg` (or `--default-pitch-deg`). HORUS's range geometry assumes
  the same pitch.
- If no position or heading sample is under 1 s old, the bridge stops sending, and the mod hands
  the screen back to the game after `timeout_s`.
- `--hfov-deg` defaults to `executor.camera_horizontal_fov_deg` from the HORUS config
  (`--config`, `HORUS_CONFIG` or `config/horus.yaml`), so the rendered view matches HORUS's camera
  model. With no config it is 90.

## Mod

`HorusGta.ini` goes next to the DLL in the game's `scripts/` folder. Defaults:

```ini
[pose]
bind=127.0.0.1
port=5611
timeout_s=1.9   ; release this long after the last valid pose (see Release below)

[origin]
x=195            ; GTA world metres of SITL home (Legion Square)
y=-933
heading_deg=0    ; GTA heading of NED north, counter-clockwise from GTA +Y
z=               ; GTA height of home; empty = probe the ground height at x, y

[scene]
weather=EXTRASUNNY ; SET_WEATHER_TYPE_NOW_PERSIST on activation; empty = the game's weather
hour=12           ; SET_CLOCK_TIME hour on activation; empty = the game's time
freeze_time=true  ; PAUSE_CLOCK while active
```

```ini
[physics]
bind=127.0.0.1    ; SITL's JSON backend sends servo frames here
port=9002
budget_ms=10      ; wall ms per script tick spent answering SITL (the game frame waits meanwhile)
hfov_deg=84       ; drone camera horizontal FOV, as config/horus_gta.yaml
trace_hz=0        ; rows/s of scripts/HorusGtaTrace.csv (camera and drone pose read back); 0 = off
```

`[scene]` is applied when the camera activates and undone on release. Releasing clears the weather
persistence (`CLEAR_WEATHER_TYPE_PERSIST`, so the game's weather cycle resumes), unpauses the clock,
and sets it back to the time read at activation. Invalid values log a warning and keep the default.
Activation logs e.g. `scene: weather EXTRASUNNY, clock 12:00 (was 17:05:12), frozen`.

GTA's world frame has X east, Y north and Z up, and headings turn counter-clockwise. With
h = `heading_deg` in radians:

```
x = origin.x - north*sin(h) + east*cos(h)
y = origin.y + north*cos(h) + east*sin(h)
z = ground_z - down
camera rotation (order 2): x = pitch, y = roll, z = heading_deg - yaw (wrapped to (-180, 180])
vertical FOV = 2*atan(tan(hfov/2) * screen_height / screen_width), clamped to GTA's 1..130
```

Behaviour:

- **Idle** until a valid packet arrives. If `z` is empty, the mod first streams the origin
  (`SET_FOCUS_POS_AND_VEL`, `REQUEST_COLLISION_AT_COORD`) and retries `GET_GROUND_Z_FOR_3D_COORD`
  every frame until it returns a height. Meanwhile it shows one notification and does not activate.
- **Active** while packets keep arriving. Every frame it sets the scripted camera's position,
  rotation and vertical FOV and renders script cams. World streaming is focused on the camera, and
  the HUD and radar are hidden. The player is held frozen and invincible, with no wanted level.
- **Intersections**: SITL flies over a flat plane and does not know GTA's buildings, so nothing
  stops the camera from passing through a wall. Every tick on which the camera moved, the mod runs
  a synchronous shape test (`World.Raycast`, all collision types, the player's ped ignored) from
  the previous camera position to the new one. Each hit is logged as `intersection #N: camera moved
  through map|ped|vehicle|object 0x<model> at (x, y, z), normal (…); segment (…) -> (…)`. No hit means
  no line. A hit within 1 m and 1 s of the previous hit counts as a repeat: a camera hovering at a
  surface crosses it back and forth every tick. Repeats are counted, not logged (`Core/CameraPath.cs`).
  The first pose after activation places the camera and is not tested. Nothing is sent back to
  SITL; the log is for checking flights afterwards. The probe reports surfaces that face the
  direction of travel. Flying through a building therefore logs its entry wall, not its exit wall,
  and flying back through it logs the other side.
- **Receive**: every tick drains the UDP socket until it is empty (`PoseLink.Drain`), with a 2 ms
  budget per tick. The newest seq wins, and a backlog over budget continues on the next tick and
  is logged at most every 5 s.
- **After a tick gap** (the script did not tick for more than `timeout_s`, e.g. game paused): the
  socket then holds the poses sent just after the pause began, because a full UDP queue drops the
  newest datagrams. On loopback that is about 221 of the 56-byte packets: Linux charges about
  960 B each against `rmem_default` 212992, which is the 213120 B recv-Q seen in the stall logs.
  They are read and discarded (`discarded N pose datagrams …`), the camera is released ("pose
  stream restarted after a tick gap"), and the next fresh packet reactivates it.
- **Release** after `timeout_s` without a valid packet, or when the script is aborted: script cams
  stop rendering, the camera is destroyed, focus is cleared, and the player's frozen/invincible
  state and the maximum wanted level are restored. The timeout is checked once per script tick
  (30–40 ms), and a packet counts from the tick that received it, so the release lands up to about
  two ticks after `timeout_s`. With the old 2 s default it came 2.039 s after the bridge's last
  packet; the default 1.9 s keeps the release within 2 s.
- **Camera block**: at the end of every tick (and once more on abort, inactive) the mod writes
  `Local\HorusGtaCamera` (`Core/CameraBlock.cs`): active, the seq of the packet it applied, and
  `Game.FrameCount`. Active is 1 only on ticks that applied a received pose. `horus_capture.asi`
  stamps these into each frame header, and marks frames inactive when the script stops ticking
  (pause menu); see `gta/capture/README.md` "Camera block".

`scripts/HorusGta.log` records the configuration and any ini warnings, the listening socket, the
camera block, the ground height, each activation and release (with the intersection and repeat
counts so far), each new intersection, discarded gap backlogs, drains that hit the budget, and the
bad-packet count (at most every 5 s).

## Physics

With `sim/run.sh start --gta --json-physics`, SITL runs `--model JSON:127.0.0.1`
(`HORUS_GTA_PHYSICS_ADDRESS`) with `FRAME_CLASS 1`, `FRAME_TYPE 1` (quad X) and `SIM_RATE_HZ 800`
(at 400, arming fails: `PreArm: Gyro 0 rate 400Hz < loop rate*1.8 720Hz`), and no bridge.
HorusGta listens on `[physics]` udp 127.0.0.1:9002:

- **Wire format** (`Core/SitlJson.cs`, ArduPilot `libraries/SITL/examples/JSON/readme.md`):
  SITL sends a binary servo frame (magic 18458, frame rate, frame count, 16 PWM values; 32 with
  magic 29569). HorusGta answers each new frame with one JSON line: `timestamp` (physics time),
  `imu.gyro` (body FRD rad/s), `imu.accel_body` (specific force, body FRD), `position` and
  `velocity` (NED from home), and `attitude` (roll, pitch, yaw).
- **Lockstep** (`Core/PhysicsLink.cs`): SITL steps only when its frame is answered. Each new frame
  advances the quad by 1/frame rate. A repeated frame (lost answer) gets the last answer again.
  A lower frame count means SITL restarted, and the quad goes back to its start.
- **Pacing**: each script tick, the sim may run up to the wall time since the last tick, at most
  0.1 s ahead, and for at most `budget_ms`. A frame that arrives once the sim is at its target is
  held for the next tick. A slow tick therefore lowers the real-time factor instead of causing a
  burst afterwards.
- **Quad** (`Core/QuadPhysics.cs`, adapted from Los Santos FPV, see below):
  - Default airframe: an F450-class 1.5 kg quad with 10x4.5 two-blade props and 920 Kv motors on
    4S. That gives about 11 100 rpm maximum and hovers at 51 % throttle.
  - Motors follow PWM 1000–2000 through a 40 ms lag, in ArduPilot's quad X order: 1 front right
    CCW, 2 back left CCW, 3 front left CW, 4 back right CW.
  - Yaw comes from prop drag torque.
  - The rigid body steps once per frame.
- **Collisions** (G3): every step the quad's path is tested against GTA's world.
  - GTA's only synchronous world test is a ray (`World.Raycast` = `START_EXPENSIVE_SYNCHRONOUS_SHAPE_TEST_LOS_PROBE`;
    SHVDN marks the capsule test obsolete because its result may come a frame later). So
    `WorldProbe` casts one ray along each step's motion, extended by the 0.12 m radius plus a
    2 mm skin. Ground support uses a ray down over the same reach, cast every step only within
    1 m of the ground height cached by a 50 m ray down. That cache is refreshed every 100 steps
    or 0.5 m of horizontal travel. Casting the down ray every step had doubled the cost; each
    in-game ray costs about 0.25 ms.
  - The rays hit map, vehicles, peds, objects, glass and foliage, and ignore the player's ped,
    water and the drone prop (see Airframe).
  - A contact puts the body one radius off the surface and removes the velocity into it, with no
    bounce. Ground contact (normal up) stops the body; walls keep 30 % of the sliding speed. Body
    rates are halved.
  - The step's change in velocity is what the accelerometer reads, saturated at ±16 g like a
    flight-controller IMU, with the gyro saturated at ±2000°/s. ArduPilot therefore sees an impact
    as a clipped acceleration spike, and sees the position stop at the wall.
  - Grazing contacts beside the path are not tested.
  - Each new contact is logged as `physics: contact #N: quad hit map|ped|vehicle|object 0x<model>
    at (x, y, z) = ned (…), normal (…), V m/s into it`. Contacts within 1 m and 1 s of the
    previous one only count as repeats, as when resting on the ground or pressed against a wall.
  - The 5 s log adds world rays/s and `on ground` / `against a wall`.
- **Frames**: NED (0, 0, 0) is the resting centre at `[origin]` (the probed ground plus the 0.12 m
  body radius). `heading_deg` turns NED as in pose mode.
- **Airframe** (G4): `ch_prop_casino_drone_02a`, Los Santos FPV's drone, is spawned when SITL
  frames start (its model is requested without blocking the tick) and posed every tick at the
  body's position with `SET_ENTITY_QUATERNION` from the body axes. Its model box is right
  -0.40..0.40, forward -0.45..0.45, up -0.36..0.38 m; the collision sphere stays 0.12 m. Seen
  from the camera mount, its legs reach into both lower corners of the drone camera's 84° view
  (HORUS listed one as an `umbrella` candidate), so the prop is drawn only while the outside view
  is on. The prop has its collision turned off, so GTA's rays pass through it; a ray that still
  hits it is counted and cast again ignoring it, and the 5 s log reports that count and what a
  ray straight down through the prop hits. The prop is deleted when SITL frames stop and when the
  script aborts.
- **Camera**: at the airframe's camera mount, 0.07 m forward and 0.03 m up from the body centre
  (Los Santos FPV's camera point on that prop; `Airframe.CameraMountM`), so it moves with the
  body's tilt and sits 0.15 m above the ground when landed. Its orientation is the body's turned
  by the servo gimbal, chained like a three-axis gimbal from the body out: pan, then roll, then
  tilt (`PhysicsLink.CameraAxes`). The angles are ArduPilot's mount servo outputs: SERVO9 roll
  over -30..30° (`MNT1_ROLL_MIN`/`MAX` defaults), SERVO10 tilt over -135..45° (so 1500 µs is
  -45°), SERVO11 pan over -160..160°, each 1100–1900 µs. ArduPilot's servo mount subtracts the
  vehicle's roll and pitch from those outputs, which on this chain levels the camera exactly; the
  servos are relative to the body (G2 applied the tilt output as an angle from the horizon, so a
  braking 18.9° nose-up pitch pointed the camera at -63.9° instead of -45°). The FOV is
  `hfov_deg`. Activation, release after `timeout_s` without frames, the camera block and C3's
  intersection log work as in pose mode. While SITL frames arrive, pose packets are ignored.
- **Outside view**: F10 while SITL frames arrive renders a camera 2.5 m behind and 0.8 m above
  the drone, pointed at it; F10 again returns to the drone camera. Frames captured meanwhile are
  marked as not the drone camera's, so HORUS skips them.
- **Trace**: with `trace_hz` > 0, `scripts/HorusGtaTrace.csv` gets that many rows per second (at
  most one per tick): the wall time, sim time, the drone camera's coordinates and rotation as read
  back with `GET_CAM_COORD`/`GET_CAM_ROT` and as given to `SET_CAM_*` this tick (`cam_set_*`),
  the prop's coordinates and quaternion, the physics NED and attitude sent to ArduPilot, the
  gimbal angles, and the origin. `GET_CAM_*` return the previous tick's values (G4: over 6410
  consecutive ticks the readback matched the previous tick's set rotation to 0.008° RMS and this
  tick's only to 1.06° RMS), so `cam_set_*` is the pose the frame renders with.
- **Log**: `physics: SITL frames arriving`, `physics: driving the camera, SITL frame rate N Hz`,
  the prop's spawn with its model box, each contact, and every 5 s the sim time, real-time
  factor, steps/s, budget hits, idle ticks, world rays/s, NED, attitude, gimbal roll/pitch/yaw,
  motor rpm and the prop's ray check.

`build/physics_sim/horus-gta-physics-sim` (built by `build.sh` from `physics_sim/`) runs the same
Core over flat ground without GTA, for testing the SITL side alone:
`./horus-gta-physics-sim [--bind 127.0.0.1] [--port 9002] [--tick-ms 33] [--budget-ms 10]`.

## Drone physics source: Los Santos FPV

Besides the pose-only mode, HorusGta flies a physical quad (see "Physics"). ArduPilot SITL runs its
JSON backend, and HorusGta steps the quad from ArduPilot's motor outputs (`LOOP_GTA.md` section G).
The quad's flight model comes from Los Santos FPV, checked 2026-10-20:

- Source: <https://github.com/zeitiv/LosSantosFPV>, author zeitiv. Releases v1.0.0 and v1.0.1 were
  both published 2026-10-10. The pinned commit is `15800d0d9a34954121797a11fba881edaa9f1dc0`
  (tag v1.0.1). No gta5-mods.com page was found; GitHub is its home.
- Licence: MIT, "Copyright (c) 2026 zeitiv" (`LICENSE`, also `LICENSE.txt` in the release zip),
  which allows reuse and modification if the notice is kept.
- The `scripts/LosSantosFpv.dll` installed on Koko (sha256 `e63a31d0…`) is byte-identical to the
  one in the v1.0.1 release zip (`LosSantosFpv-1.0.1.zip`, sha256 `8fc32100…`, also in
  `~/horus-gta/staging/`).
- What its flight model does (`src/Flight.cs`, `src/Tune.cs`, `src/DroneMod.cs`):
  - The physics is engine-neutral and self-integrated at a fixed 125 Hz in GTA's frame (X east,
    Y north, Z up), with body axes right/forward/up.
  - A deadbeat rate controller turns stick rates into torques, and an airmode mixer turns those
    into four motor forces.
  - Thrust per motor is `k0(blades) + kS·pitch·D³·rpm²`, reduced by axial speed against the prop's
    pitch speed. Max rpm comes from motor Kv, cell count and prop load.
  - Also modelled: ground effect from a ray straight down, body and rotor drag, the gyroscopic
    term, angular drag of 1.6/s, and a 36 rad/s rate limit.
  - Collisions sweep a 0.10 m sphere with `World.RaycastCapsule` and a ray. At contact it stops,
    keeps 30 % of the tangential velocity and halves the rates. Impacts over 5 m/s raise a callback.
  - The drone is the `ch_prop_casino_drone_02a` prop, posed with `SET_ENTITY_QUATERNION` at a pose
    interpolated between physics steps, and the camera is hard-attached to it.
- Decision (G1, by the user's rule: open licence → fork or adapt): adapt it. ArduPilot is the
  flight controller, so the stick input and the deadbeat rate loop are dropped. ArduPilot's PWM
  outputs drive motor rpm (with a motor lag), and from there Los Santos FPV's thrust, rigid-body,
  aero, ground-effect and collision code applies. The step runs at ArduPilot's rate (≥400 Hz)
  instead of 125 Hz, and the prop and camera handling are reused. Any file adapted from it keeps
  the MIT notice and names the source commit. This README credits it here, as `gta/hook/README.md`
  credits YimMenu for the crossmap. Los Santos FPV itself stays installed as a separate mod.
- Los Santos FPV's own flight was never tried. It flies only from a radio, joystick or Xbox pad,
  and Koko has none (`lsusb`: a camera and a Bluetooth radio; no `/dev/input/js*`). Its flight
  model is what HorusGta adapted, and that was checked in game with ArduPilot flying it (G2–G4 in
  `LOOP_GTA.md`), so the separate controller test (F1) was closed as superseded.

## Build, install

```bash
gta/horus_mod/build.sh            # .NET 8 SDK in Docker: core tests, then build/HorusGta.{dll,ini}
scripts/koko/gta_mods.sh install  # full stack: HORUS Script Hook, SHVDN, Los Santos FPV, HorusGta
```

`build.sh` downloads ScriptHookVDotNet Enhanced v1.1.0.6 (sha256-checked) into the gitignored
`.cache/` and uses it only as a compile reference. `src/Core/` has no game dependency (packet,
mapping, ini, receive state). `tests/` runs it headless with `dotnet run`. The script itself is
`src/HorusGtaScript.cs`.

## Drone camera size

The drone camera is the game's own frame: `horus_capture.asi` copies the back buffer about to be
presented (`gta/capture/README.md`), so its size is GTA's render size. `gta_mods.sh install` sets
exclusive fullscreen 1280x720 in GTA's `settings.xml` (`ScreenWidth` 1280, `ScreenHeight` 720,
`Windowed` 0; backed up first, `gta/hook/README.md` "Proton prefix setup").

Exclusive fullscreen is needed because in borderless windowed mode (`Windowed` 3, Koko's
setting until 2026-10-21) GTA renders at the desktop's 1920x1080 whatever `ScreenWidth`/`ScreenHeight` say.
Checked on Koko on 2026-10-21:

- **Windowed 3, 1280x720 set**: frames still 1920x1080, 14.95 fps, about 0.08 s old.
- **Windowed 0, 1280x720**: frames 1280x720. The window still covers the 1920x1080 panel, scaled
  up.

HORUS reads all 15 frames/s the plugin serves: `perception.max_fps` is 15 in
`config/horus_gta.yaml` (5 by default). The detector runs on each frame it reads, one at a time;
with yolov8n at about 140 ms per frame on Koko, horus-mcp processes about 6.3 frames/s
(`get_state` `processing.fps` 6.2–6.6 at 8 m), against 5.0 before.

Measured on 2026-10-21 with SITL on the JSON backend and the drone camera on screen. A client
(`koko:~/horus-gta/run18/m2_rate.py`, `GtaFrameStream` with `require_active=True`) pulled gta:// for
60 s: 898 distinct frames, 14.96 frames/s, all 1280x720 and all `camera_active`, aged 0.007–0.033 s
(median 0.017) at arrival (`run18/m2_rate_60s.txt`). horus-mcp's `last_frame_age_s` stayed at or
below 0.20 s over 60 s while the drone held at 8 m.

## Checked in game

GTA V Enhanced 1.0.1158.16 under Proton, on the HORUS Script Hook (`gta/hook`): ArduPilot SITL
(built-in quad physics, servo gimbal at -30°) flew 40 m up and 80 m north; `horus-gta-bridge`
sent the pose; the mod probed the origin ground (z = 29.69 m at Legion Square, the player being in
the prologue far away), activated, hid the HUD, and `horus_capture.asi` served the rendered view
looking down over the streets north of the square. The pitch sign is right (negative looks down)
and the city streams around the camera.

Camera geometry, measured 2026-10-20 18:09–18:10 (`koko:~/horus-gta/run3/`). A temporary debug
script spawned a road cone at the origin, read its `GET_ENTITY_COORDS` (195.000, -933.000, 29.691),
and drew a 40 m magenta cylinder marker on it. A synthetic sender placed the camera 30 m from the
cone and 25 m above the ground at pitch 0, with the yaw computed from the NED offset. The marker's
centroid was measured in the captured 1920x1080 frames:

| Pose (north, east; roll, yaw) | GTA camera (x, y; rot y, rot z) | Marker x / width | Expected |
|---|---|---|---|
| -30, 0; 0, 0 (looking north) | 195, -963; 0, 0 | 0.4997 | 0.5 |
| 0, -30; 0, 90 (east) | 165, -933; 0, -90 | 0.4998 | 0.5 |
| 30, 0; 0, 180 (south) | 195, -903; 0, -180 | 0.5004 | 0.5 |
| 0, 30; 0, -90 (west) | 225, -933; 0, 90 | 0.4998 | 0.5 |
| -30, 0; 0, -30 (cone 30° right of the axis) | 195, -963; 0, 30 | 0.8206 | 0.8206 |
| -30, 0; 0, 30 (cone 30° left) | 195, -963; 0, -30 | 0.1789 | 0.1794 |
| -30, 0; +20, 0 | 195, -963; 20, 0 | marker top leans left 20.06° | |
| -30, 0; -20, 0 | 195, -963; -20, 0 | marker top leans right 20.05° | |

- Yaw reference: NED north is GTA +Y at `heading_deg` 0, and GTA headings turn counter-clockwise.
  The marker is centred from all four sides. With a clockwise heading the east and west views would
  face away from it.
- FOV: `SET_CAM_FOV` takes the vertical FOV. The expected x values are the pinhole prediction for
  hfov 84° at 16:9, 0.5 ± 0.5·tan 30°/tan 42°. `GET_FINAL_RENDERED_CAM_FOV` read back 53.72, the
  vertical FOV `PoseMapping` sends. Had GTA treated it as horizontal, a 30° offset would be off
  screen. GTA's own `GET_SCREEN_COORD_FROM_WORLD_COORD` agrees: a point 15 m below the camera
  at 30 m range projects at y 0.9933–0.9940, and the prediction is 0.9936.
- Roll sign: positive roll puts the camera's right side down, as NED requires. In the image this
  makes vertical edges lean left at the top and the horizon rise toward the right; negative roll
  mirrors it.

Pose-to-frame timing, measured 2026-10-20 at 20:00 with the real chain (`koko:~/horus-gta/run4/`).
ArduCopter SITL from `sim/run.sh start --gta` and `horus-gta-bridge` sent 50 Hz poses to the mod,
and 300 frames were read the way HORUS reads them (`GtaFrameStream`, active frames only; the
plugin's `max_hz` 15 makes one frame every 66.7 ms). Each frame's header `pose_seq` was joined
with that packet's kernel send time, taken from a packet tap on `lo` (`SO_TIMESTAMPNS`). 293 of
the 300 frames joined; the tap missed 12 of 1220 packets.

| Interval (ms) | min | p50 | p95 | max |
|---|---|---|---|---|
| bridge send → Present (header `capture_unix_ns`) | 26.7 | 33.8 | 40.5 | 57.4 |
| Present → frame received by HORUS | 25.7 | 28.4 | 33.8 | 49.2 |
| bridge send → frame received by HORUS | 53.0 | 62.6 | 72.2 | 106.6 |

- What is measured: the end-to-end send → arrival interval, both stamped by Linux `CLOCK_REALTIME`.
  The split at Present uses the plugin's `GetSystemTimePreciseAsFileTime` under Proton. A Wine
  probe echoing that clock over UDP (100 pings, round trips of 0.17–0.35 ms) put it 0.03–0.05 ms
  ahead of Linux time, so the split is good to about 0.1 ms.
- What is inferred: the split inside send → Present, that is waiting for the next script tick,
  the game's simulation and render of that frame, and the GPU copy. Those stages are not stamped
  separately.
- When the frame was presented, the bridge had sent 1 newer pose (p50; at most 2).
- HORUS stamps a GTA frame with `time.monotonic()` on arrival and pairs it with the latest
  telemetry (`horus/perception/camera.py`, `factory.py`). The pose it uses is therefore about
  63 ms (p95 72 ms) newer than the pose rendered. That is under B6's 150 ms threshold, so there is
  no telemetry history lookup. For example, at 5 m/s and 30°/s yaw, 63 ms is about 0.3 m and 2°.
  The vehicle stood still on the ground during this measurement.

Intersections, flown 2026-10-20 20:01–20:07 with synthetic poses (`koko:~/horus-gta/run5/`):

- 15 m above home, east 150 m, west 300 m and back at 5 m/s, along GTA y = -933: 14
  `intersection` lines, all `map`. They are wall faces at x 283, 310, 318 and 344 on the way east
  (normals facing west), the same buildings from the other side on the way back, and buildings
  at x 48–110 to the west.
- Two 18 m circles around home: at 30 m above ground, one hit per lap at (208.06, -920.61, 59.69),
  NED (12.4, 13.1, -30). That is the grey and red sculpture at the north-east of the square, inside
  the then ±20 m fence of `config/horus_gta.yaml` and at least 30 m tall (frames
  `koko:~/horus-gta/run3/frames/c3-hit-*.png`). The same circles at 45 m logged none. The 15 m
  flight's first hit, at x 213.9 (NED 0, 18.9) inside the fence, was not looked at in a frame. So
  the fenced square was not all open air below about 45 m; C7 (below) mapped it and moved the
  fence.

HORUS perception on GTA frames, 2026-10-20 20:46–21:02 (`koko:~/horus-gta/run6/c4-*`). The chain was
`sim/run.sh start --gta`, GTA in story mode, and `horus-mcp` in the container with
`config/horus_gta.yaml` (detector-only, no grounder). Results:

- `get_state` reported camera `streaming` with `last_frame_age_s` 0.16–0.29 throughout.
  Perception ran at 3.3–5.0 fps with yolov8n at 166–288 ms per frame on Koko's CPU.
- With the vehicle at 10–25 m and the gimbal at -45°, at the origin and at the fence edges, the
  candidate tables held benches, umbrellas and a bird, but no person. Pedestrians are visible in
  those frames, but at 25 m they are about 50 px tall in the 1920x1080 frame, which is about 17 px
  at the detector's 640 input. That is too small for yolov8n.
- From NED (15, -20) at 15 m, heading 0, a car on the street at the north-west corner was candidate
  139, class `car`, confidence 0.55 (`runs/manager/20261020T180048.549294Z_list_candidates.jpg` on
  Koko).
- The person search in a mission (C5) therefore needs a lower altitude, a larger detector input,
  or the grounder.

JSON physics hover, 2026-10-20 (`koko:~/horus-gta/run7/`). `sim/run.sh start --gta --json-physics`,
GTA in story mode, and a pymavlink client on tcp:5760 that waits for the EKF, arms in GUIDED, takes
off to 5 m, holds 60 s and lands:

| | flat ground, no GTA (`horus-gta-physics-sim`) | in GTA at Legion Square (21:59–22:04) |
|---|---|---|
| SITL frame rate, steps/s | 800, 799 | 800, 797–801 |
| real-time factor (5 s windows) | 0.999 | 0.996–1.001 |
| roll / pitch RMS during the hold | 0.11° / 0.11° | 0.11° / 0.11° (max 0.18° / 0.21°) |
| height during the hold (NED down) | -4.99 to -4.98 m | -4.98 to -4.97 m |
| horizontal drift during the hold | 2 cm | 2 cm |
| EKF flags / failsafe or variance texts | 0x33f / none | 0x33f / none |
| motor PWM at hover / mount pitch PWM | 1511 / 1499 (-45°) | 1511 / 1499 (-45°) |

- In GTA, the budget was hit on 0–30 ticks of about 150 per 5 s window, and the real-time factor
  stayed at about 1. The game thread ran 1784–1800 script ticks per minute, its normal rate.
- A frame captured during a second 5 m hover (22:08) shows the plaza paving 45° below the camera,
  with active 1.
- After landing, the frame was uniformly white (pixel values 253–255). G4 found the cause: the
  camera was placed with pose mode's mapping, whose down 0 is the ground, but ArduPilot's down 0
  in physics mode is the resting body centre 0.12 m above it, so the camera sat in the ground. The
  camera now sits at the airframe's mount (see Physics, Camera).
- Offline at `SIM_RATE_HZ` 1200, the same hover gave 0.10° / 0.11° RMS at 1197 steps/s.

Collisions, 2026-10-20 22:51–23:01 (`koko:~/horus-gta/run8/`). Physics mode, GTA, and `horus-mcp`
in the container with `config/horus_gta.yaml`. HORUS took off to 20 m, went to NED (12.4, -5)
heading east, then was sent by `goto` to (12.4, 19) or (12.4, 17), straight into the grey and red
sculpture at the north-east of the square. A pymavlink tap on SITL serial1 recorded ArduPilot's
own view.

- **GTA:** at 3 m/s, `physics: contact #2: quad hit map at (208.12, -920.55, 49.82) = ned
  (12.45, 13.12, -20.01), normal (-0.82, 0.57, 0.00)`. The second pass hit at ned (12.54, 13.18),
  2.47 m/s into the face. The body stopped one radius off the face, at east 13.7–14.0 including
  the slide. Frames taken while the goto pushed show the sculpture's grey and red faces filling
  the view at arm's length (`runs/manager/20261020T19574*`, `…195757…` on Koko).
- **ArduPilot:**
  - Its LOCAL_POSITION_NED east velocity fell from 2.99 to 2.80 m/s at the contact time.
  - Its estimate then coasted on to east 18.4 for about 5 s while the IMU clipped. VIBRATION
    clipping went 0 → 2 → 139 over the push, with vibration levels up to 24 m/s².
  - It reported `Vibration compensation ON`, `GPS Glitch or Compass error` and
    `EKF3 IMU0/1 emergency yaw reset`, then snapped back to east 14.07, at the wall.
  - On the second pass, the goto kept pushing for 45 s and the body slid around the block's
    north corner (contact normal (0.57, 0.82)). ArduPilot then raised `EKF variance` →
    `EKF Failsafe: changed to Land Mode` and landed by itself, 5 m off the physics position.
    HORUS reported it as `pilot override: vehicle switched GUIDED -> LAND outside HORUS`.
  - So a collision looks to ArduPilot like a real crash into a structure: clipped
    accelerometers, GPS/EKF disagreement, and an EKF failsafe if it goes on.
- **Rate:** the world rays cost real time. With HORUS perception and GTA sharing the CPU, the
  10 ms budget gave a real-time factor of 0.5–0.8 in flight, versus 1.0 in G2, which cast no rays.
  The sim then runs slower than real time, which ArduPilot's lockstep accepts. The hover stayed
  stable (G2 decision: slower than real time is allowed).
- **Found on the way and fixed:**
  - `horus/flight/link.py` took ArduPilot's first `GLOBAL_POSITION_INT` as its local origin even
    before the EKF had a position (lat = lon = 0). In physics mode HORUS connects before SITL
    boots, so its pose was 4 000 km off. The first `take_off` then flew the quad 130 m ESE toward
    that bogus target. Messages without a fix are now ignored
    (`tests/test_flight_link.py::test_global_position_without_a_fix_is_neither_pose_nor_origin`).
  - `scripts/koko/gta_game.sh` `landing_tab` failed when GTA replaced its window during loading
    (X BadWindow); it now reads `none` and waits.

Airframe and camera mount, 2026-10-20 23:16–23:50 (`koko:~/horus-gta/run9/`; final DLL in the
last run, 23:46–23:50). Physics mode, GTA, `horus-mcp` with `config/horus_gta.yaml`, `trace_hz`
100, and a tap on SITL serial1 for LOCAL_POSITION_NED, ATTITUDE and
GIMBAL_DEVICE_ATTITUDE_STATUS. HORUS took off to 10 m and flew four `goto`s at headings 45, 135,
270 and 0 (NED (-8, -8), (-8, 8) at 12 m, (4, -10), (0, 0), clear of the sculpture), then landed.
F10 frames were taken before take-off, in the hover and after landing.

- **Outside view:** the frames show the drone at the centre, standing on its legs on the paving
  and hovering over the square's planter (`run9/frames/outside-*.png`).
- **Drone camera:** landed, the frames show the paving 0.15 m away (pixel values 157–226 and
  87–237, against 253–255 in G2). In flight, after the prop was hidden from its own camera, the
  candidate table held only the planter (a 0.61 `bench`). Before that, the legs showed in both
  lower corners and one was a 0.33 `umbrella` candidate.
- **Pose vs ArduPilot** (sim time matched to `time_boot_ms`; SITL's JSON clock is the sum of
  HorusGta's timestamp steps). Over the 60 s of the `goto` pattern, at 1690 traced ticks:

  | Quantity | RMS | max |
  | --- | --- | --- |
  | Camera position vs LOCAL_POSITION_NED + R(ATTITUDE) · mount (m) | 0.052 | 0.095 |
  | Camera orientation vs ATTITUDE, then gimbal pan, roll, tilt (°) | 0.88 | 1.55 |
  | Prop position vs LOCAL_POSITION_NED (m) | 0.052 | 0.095 |
  | Prop orientation vs ATTITUDE (°) | 0.87 | 1.49 |

  The orientation difference is ArduPilot's own estimate error: its ATTITUDE differed from the
  physics attitude by the same 0.87° RMS (about 1° of yaw), while the camera as set matched the
  physics state plus servo gimbal to 0.008° RMS and the prop's read-back quaternion to 0.12°.
  - Before `EKF3 IMU0 MAG0 in-flight yaw alignment complete` (6 s after arming), ArduPilot's yaw
    was up to 2.4° off.
  - From the alignment to landing (109 s), everything was within 0.5 m and 2° except two 3-tick
    windows of up to 25°. In both, ArduPilot swung the gimbal 45° between two 10 Hz
    GIMBAL_DEVICE_ATTITUDE_STATUS samples: to -90° at `land`, and back to -45° after disarming.
- **Gimbal:** braking at 18.9° nose up, the tilt servo went to -63.9° and the camera stayed at
  -45.0° (roll 0.35°). At 17.4° of body roll, the roll servo went to 17.2° and the camera rolled
  -0.30°. With G2's model, the first case would have looked at -63.9°.
  GIMBAL_DEVICE_ATTITUDE_STATUS carries these body-relative servo angles, with the earth-frame
  lock flags set (flags 44).
- **Rays:** the 5 s log said `drone prop: 0 ray hits so far, a ray down through it hits nothing`
  throughout. The only contacts were the two ground contacts at rest and at touchdown. The
  real-time factor was 0.73–0.98.

Physics chain re-run (G5), 2026-10-20 23:57–00:04 (`koko:~/horus-gta/run10/`). This repeats B6's
timing and C4's perception check with SITL on its JSON backend and HorusGta flying the quad.
There is no bridge, so the frame header's `pose_seq` is the SITL frame count of the physics state
the tick applied.

Timing was measured during a 5 m hover from a pymavlink client (roll/pitch RMS 0.10°/0.10° over
120 s, EKF 0x33f). 300 active frames were read the way HORUS reads them (`GtaFrameStream`).
Each frame was joined three ways:

- through `HorusGtaTrace.csv` (`trace_hz` 100, one row per tick), to the physics time of the state
  it shows;
- through a packet tap of the physics link on `lo` (kernel `SO_TIMESTAMPNS`, 14 557 HorusGta
  answers), to the wall time that state was answered to SITL;
- through a tap on SITL serial1, to ArduPilot's LOCAL_POSITION_NED (50 Hz) as received.

All 300 frames joined. Over the 19.9 s wall window the sim advanced 15.2 s, so the real-time
factor was 0.762. Wall time and physics time are reported separately below.

| Interval | min | p50 | p95 | max |
|---|---|---|---|---|
| state answered to SITL → Present (wall ms) | 3.4 | 14.8 | 16.0 | 39.6 |
| tick that applied it → Present (wall ms, both Wine clock) | 4.1 | 15.6 | 17.1 | 39.4 |
| Present → frame received by HORUS (wall ms) | 31.7 | 38.7 | 50.0 | 86.1 |
| state answered to SITL → frame received (wall ms) | 42.0 | 53.0 | 63.0 | 125.7 |
| newest state answered at arrival − rendered state (physics ms) | 13.7 | 25.0 | 35.0 | 52.5 |
| ArduPilot's newest LOCAL_POSITION_NED at arrival − rendered state (physics ms) | -8.0 | 10.7 | 24.5 | 42.0 |

- **Fresher frames:** the rendered state is now the one the same tick computed. No more states
  were answered between it and Present (p95 0, max 16 when a tick ran late), against 1 newer pose
  (p50) with the bridge. State → Present fell from 33.8 to 14.8 ms (p50), and state → arrival from
  62.6 to 53.0 ms.
- **Slower handoff:** Present → arrival rose from 28.4 to 38.7 ms (p50). The game thread now also
  steps the physics; that cause is inferred, not separately stamped.
- **Physics time:** physics time runs slower than wall time, at a real-time factor of 0.76 here.
  So the 53 ms (p50) of wall time between answering a state and the frame arriving advance the
  physics by only 25 ms.
- **Telemetry lag:** ArduPilot's telemetry adds its own lag. Its `time_boot_ms` trailed the newest
  answered state by 4.3–9.3 physics ms at receipt (p50 6.8). The telemetry HORUS pairs with a
  frame on arrival was 10.7 ms (p50) and at most 42 ms of physics time newer than the rendered
  state, against B6's 63 ms p50 / 72 ms p95.
- **No history lookup:** B6's conclusion stands. The p95 is well under 150 ms, so there is still
  no telemetry history lookup.

Perception, same session: `horus-mcp` in the container with `config/horus_gta.yaml`, through HORUS MCP.

- **Camera:** `get_state` reported the camera `streaming` throughout, with `last_frame_age_s`
  0.111 on the ground, 0.102 at 15 m, 0.105–0.231 at the NW corner and 0.205 after landing.
- **Flight:** the vehicle took off to 15 m and went to NED (15, -19) facing north, then north-west
  (heading 315). Every task succeeded.
- **Candidates:**
  - **Facing north:** the three frames had no candidate.
  - **Facing north-west:** a car candidate: id 34, `class_id` 2, confidence 0.35, bbox
    [144, 9, 196, 32], on a dark car in the street at the top of the frame. JPEG
    `runs/manager/20261020T210232.014723Z_list_candidates.jpg` on Koko.
  - **Pedestrian:** the frame also shows one on the pavement, too small for the detector, as in
    C4.
- **Contacts and rate:** HorusGta logged only ground contacts (rest and two touchdowns). Its real-time
  factor ranged 0.46–1.00 over the session (5 s windows).

Obstacles in the fence (C7), 2026-10-21 00:49–01:36 (`koko:~/horus-gta/run11/`):

- **Mapping:** done in pose mode, with synthetic camera poses on 5611 and no SITL. HorusGta's C3
  probe logs every surface the camera path crosses. Three passes:
  - vertical sweeps from 60 m down to 2 m above home and back, at 20 m/s, on a 4 m grid over the
    old ±20 m fence (`map11.py`);
  - the same on a 2 m grid over north 6..24, east 6..24;
  - horizontal plane sweeps at 6, 8, 10 and 12 m, with lines 1 m apart in both directions over
    north -16..20, east -20..10 (`sweep11.py`).

  The vertical sweeps logged 185 intersections (`obstacles.py`), all `map`.

  | Obstacle (NED m from home) | Height above home ground |
  |---|---|
  | Grey colonnade structure at the NE, north 0..17, east 13..24 and beyond | horizontal slabs at 4, 16, 22–24 and 29–30 m; top 30 m |
  | Trees along north -20, east 8..16 (sloped canopy normals) | tops 18.3–20.7 m, undersides from 7.7 m |
  | Walls at (18, 16), (20, 14), (24, 8) | 5 m |
  | Kiosk roof at (0, -12) | 3.3–4.4 m |

  The structure is what C3 hit at (12.4, 13.1, 30 m) and what G3 flew into at (12.5, 13.1). It
  extended far into the old fence. The plane sweeps crossed nothing.
- **Fence:** `config/horus_gta.yaml` is now the convex rectangle north -15..20, east -20..8, with
  `geofence_margin_m` 2. Targets are therefore inset to north -13..18, east -18..6, and a straight
  leg between two points inside stays inside.
  - **Clearances from the inset fence:** the structure's nearest mapped cell is 8 m away, and
    its face at least 6 m allowing for the 2 m grid. The tree cells are 7 m away, and no tree
    was found at north -16. The walls are at least 6 m away.
  - **Scan altitude:** 6 m or higher. Below that are only the kiosk and low objects the sweeps
    did not resolve (planters, benches, lamps).
- **Check on the JSON-physics chain:** `horus-mcp` flew `fly11.sh` at 10 m. It took off and
  went round the inset perimeter (-13, -18) → (-13, 6) → (18, 6) → (18, -18) → (-13, -18). It
  then flew a raster along north -5, 3 and 11, returned home and landed.
  - **Tasks:** all 13 succeeded with no clamp reason. `get_map` reported the inset polygon.
  - **Flown extent:** the physics body reached north -13.3..18.2 and east -18.3..6.3 at 10 m.
  - **Contacts:** HorusGta logged no intersection; the count stayed at 185 from mapping. The only
    contacts were the ground at rest and at touchdown.
  - **Rate:** the real-time factor was 0.45–0.67.
- **Tests:** `tests/test_config_profiles.py` passed on Koko (20).

HORUS mission in GTA (C5), 2026-10-21 02:06–02:23 (`koko:~/horus-gta/run12/`). Physics mode, GTA,
`horus-mcp` with `config/horus_gta.yaml`, and `missions/S14_gta_find_person.yaml` run through the
REPL mission runner:

```bash
docker exec -e HORUS_MCP_TOKEN="$token" -w /horus horus-sim /opt/horus-venv/bin/horus \
  --server-url http://127.0.0.1:8000/mcp --config config/horus_gta.yaml \
  --mission missions/S14_gta_find_person.yaml
```

- **Mission:** take off, `search` the inset fence for a `person`, `resolve_candidate`, `approach`
  to a 12 m slant, hold 20 s. The `finally` steps (go home, land) run after every outcome, and
  each run writes `runs/S14_gta_find_person-run-001-<UTC>-result.json`.
- **Runs:** five, each landing at home (ground contacts within 0.36 m of home):

  | Run | Altitude | search | resolve_candidate |
  |---|---|---|---|
  | 02:06 | 10 m | found person 31 (frame 276, waypoint 3) | abstain `unstable` |
  | 02:09 | 8 m | covered the fence, no validated person | abstain `no_match` |
  | 02:13 | 8 m | found person 42 (frame 252, waypoint 5) | abstain `unstable` |
  | 02:16 | 6 m (one-off variant) | no validated person | abstain `no_match` |
  | 02:19 | 8 m | no validated person | abstain `no_match` |

- **Why no resolution:** `unstable` means the person was seen under one ID but was not in the
  decision frame, so it was not seen in two fresh frames through the settle window. Frames taken
  every 15 s by a second MCP client show the pedestrians on the walkways at the square's edges,
  near the top of the -45° view. There they are 15–20 px tall at the detector input, and yolov8n
  scored them 0.28–0.43 (`runs/manager/20261020T232114.556635Z_list_candidates.jpg`: `person`
  0.28 at the frame's top-left corner).
- **Approach:** never reached, so the executor's new geofence velocity limit was exercised only by
  the tests.
- **Contacts:** HorusGta logged only the six ground contacts (rest and five touchdowns). The
  real-time factor was 0.68–1.01.

Claude-commanded mission in GTA, 2026-10-21 21:50–21:52 (`koko:~/horus-gta/run18/attempt1/`).
`missions/S15_gta_claude_find_person.yaml` holds one `text:` step with the operator's request:
"find a person on the plaza, hover near them, then come back and land". The REPL ran on the laptop
and reached horus-mcp on Koko through an SSH tunnel (`ssh -N -L 18000:127.0.0.1:8000 koko`). It
started Claude Code (Opus, high effort) with only the HORUS MCP tools, and Claude chose every
call. Before the run the GTA profile's altitude floor went from 1 m to 6 m, so no agent-chosen
target can go below the clear air C7 measured; `get_map` reported the range 6–48 m.

- **Claude's calls**: `get_state`, `get_map`, then one onboard `run_plan`:
  - take off to 6 m;
  - `search` the inset fence for `person`;
  - if a person is found: `approach` it to 3 m, `follow` it for 25 s if the approach arrives,
    otherwise wait 5 s;
  - in every branch: `rth()` and `land()`.

  Then `wait_for_plan`, and `get_state` to check.
- **Flight**: take-off succeeded, and the search stopped on person 4. About 2 s later that
  person left the frame, so the approach expired ("tracked target 4 is not visible") and the
  hover near them did not happen. The plan's fallback waited 5 s, then ArduPilot's RTL climbed to
  15 m, flew home and landed. Final state: disarmed, NED (−0.01, 0.03).
- **Claude's report** stated exactly that: person 4 found, approach expired, returning home. It
  offered to re-fly the hover leg with a longer approach TTL and `follow`.
- **Contacts**: HorusGta logged only the two ground contacts (rest, and touchdown at
  (−0.06, 0.03)) and no intersection.
- **Records**: the REPL transcript and Claude's event stream (`runs/s15/attempt1/`, 5 tool calls,
  `final_result`), the server's tool log (`runs/s15-attempt1/events.jsonl` in the container),
  and the run result `S15_gta_claude_find_person-run-001-20261021T185147.095605Z-result.json`
  (outcome `completed`, with Claude's final answer). The grounder is not started in GTA mode,
  which Claude noted; `person` is a detector class, so the search did not need it.

Memory budget of a HORUS mission in GTA (C6), from the C5 session. A throwaway sampler
(`koko:~/horus-gta/run12/memsample.py`, CSV `run12/mem.csv`) logged every 10 s from 01:48:44 to
02:26:48:

- `/proc/meminfo`: `MemAvailable`, `MemFree` and swap use.
- The summed RSS of each process group, matched by command line: GTA5_Enhanced.exe,
  SocialClubHelper.exe, the Rockstar launcher/service/error handler/PlayGTAV, other wine/Proton
  processes, Steam, arducopter, horus-mcp, the `horus --mission` runner, and every process in the
  horus-sim container's cgroup.

Before launch, Koko had 9070–9163 MiB available (mean 9106) with Steam (2118–2172 MiB RSS) and
the idle horus-sim container (45 MiB). Swap was already at 2044 of 2048 MiB, filled before the
session, so it could not absorb anything. It stayed at 2047 MiB from 02:04:57.

| Phase | Lowest available | Taken from the 9106 MiB baseline |
| --- | --- | --- |
| GTA launch, SocialClubHelper at its peak (02:04:27) | 3780 MiB | 5326 MiB |
| GTA in story mode, no HORUS yet (02:05:47) | 3159 MiB | 5947 MiB |
| Missions, horus-mcp and mission runner up (02:06–02:22:30) | 897 MiB (02:18:34) | 8209 MiB |
| After the last mission, everything still up (02:22:57) | 448 MiB | 8658 MiB |

Peak summed RSS per group (each at its own time):

| Group | Peak RSS | When |
| --- | --- | --- |
| SocialClubHelper.exe | 4536 MiB (3406–3674 once the game runs) | 02:04:27 |
| Steam | 2605 MiB | 02:04:47 |
| GTA5_Enhanced.exe | 1927 MiB | 02:06:07 |
| horus-sim container | 1202 MiB | 02:22:06 |
| horus-mcp (perception, executor) | 1140 MiB | 02:22:06 |
| other wine/Proton | 638 MiB | 02:05:07 |
| Rockstar launcher, service, PlayGTAV | 585 MiB | 02:04:47 |
| `horus --mission` | 82 MiB | 02:11:20 |
| arducopter (SITL) | 6 MiB | 02:05:57 |

At the lowest sample (02:22:57: 448 MiB available, 289 MiB free, swap 2047 MiB), the groups held:
GTA 1919 MiB, SocialClubHelper 3406, Rockstar 376, other wine 438, Steam 2240, horus-sim container
999 (horus-mcp 970, SITL 4).

Summed RSS is not host memory use:

- **RSS overcounts.** At 02:05:47 the groups' RSS rose by 6854 MiB over the baseline while
  available memory fell by only 5947 MiB. Pages shared between processes, such as the Wine and
  Chromium libraries of the many SocialClubHelper and wine processes, count once per process.
- **Not all use shows up in RSS.** At the lowest sample RSS had risen by 7193 MiB but available
  memory had fallen by 8658 MiB. The extra 1.5 GiB is not in any sampled process's RSS.
  `gta/hook/README.md` "Memory" measured about 1 GiB of GTA's GPU memory in system RAM (GTT),
  which RSS does not count.

So the launch guard is based on the fall in available memory, not on RSS. The worst fall, 8658 MiB,
plus 1 GiB of headroom is 9682 MiB, rounded up to 9728 MiB (9.5 GiB). That is now
`GTA_MIN_AVAILABLE_MIB`'s default in `scripts/koko/gta_game.sh`; the old 8192 was GTA alone plus
1 GiB. Koko's idle level of about 9.1 GiB (Steam running) is below it, so `gta_game.sh start` now
refuses. Before the next GTA run about 600 MiB has to be freed on Koko.

The game must not pause: GTA's `PauseOnFocusLoss` setting pauses script threads, and with them
the mod, whenever the window loses focus. `scripts/koko/gta_mods.sh install` sets it to 0 in
`settings.xml` (see `gta/hook/README.md` "Proton prefix setup").
