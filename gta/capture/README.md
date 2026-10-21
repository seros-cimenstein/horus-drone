# horus_capture.asi: drone camera from GTA V's renderer

An ASI plugin that copies the back buffer GTA V Enhanced is about to present, on the GPU, and serves
it to HORUS perception over TCP. It does not use screen capture: frames come from the game's D3D12
swap chain, so they do not depend on window placement, focus or the compositor. While a scripted
drone camera is active (Los Santos FPV / the HORUS mod), the back buffer is the drone's view.

## How it works

- A throwaway D3D12 device and swap chain give the addresses of `IDXGISwapChain::Present`/`Present1`,
  `ResizeBuffers`/`ResizeBuffers1`, `IDXGIFactory::CreateSwapChain`/`CreateSwapChainForHwnd` and
  `ID3D12CommandQueue::ExecuteCommandLists`, which are hooked with MinHook.
- The queue the game presents on comes from its `CreateSwapChain*` call. If the plugin loads after
  the swap chain exists, the last direct queue seen in `ExecuteCommandLists` on the same device is used.
- Only while a client is connected, and at most `max_hz` times per second, inside `Present`, a copy
  of the back buffer into a readback buffer is recorded and submitted on that queue (PRESENT ->
  COPY_SOURCE -> PRESENT). Without a client the hook only counts presents. Three slots rotate, so
  the render thread never waits for the GPU; when all three are in flight the frame is skipped.
- A worker thread waits on each copy's fence and converts it to packed BGR8: RGBA8/BGRA8 directly,
  RGB10A2 rounded to 8 bits, RGBA16F (scRGB) clamped to [0, 1] and sRGB-encoded. HDR10 (PQ) output is
  not decoded; keep the game's HDR off.
- `ResizeBuffers` waits until no copy still references the old back buffers.
- Every `Present` (client or not) also reads HorusGta's camera block (below), so each frame says
  whether it shows the drone camera.

The frame contains everything drawn into the back buffer, including GTA's HUD and any mod OSD;
hide them for clean perception frames.

## Protocol

TCP, one client at a time. The client sends the byte `F`; the plugin answers with the newest frame
it has not sent that client yet (blocking until one exists), so a slow consumer never builds up a
backlog. Each answer is a 64-byte little-endian header followed by `width * height * 3` BGR bytes:

| Field | Type |
| --- | --- |
| magic `HGTA` | 4 bytes |
| header bytes (64) | u32 |
| present index (presents since the hooks were installed) | u64 |
| capture time, Unix ns | u64 |
| width, height | u32, u32 |
| pixel format (1 = BGR8) | u32 |
| payload bytes | u32 |
| camera active (1 = HorusGta's drone camera is on screen) | u32 |
| script age: presents since HorusGta last ticked (`0xFFFFFFFF` = never) | u32 |
| pose seq: seq of the last pose packet HorusGta applied | u64 |
| game frame: `GET_FRAME_COUNT` at that tick | u64 |

Plugins built before the camera fields were added (2026-10-20) send a 40-byte header (the first
eight fields). `gta_stream.py` rejects any size other than 64, so deploy plugin and client together.
The only exception is the selftest checker's explicit `check_stream.py --legacy-header`, which
reads such a plugin's frames without camera fields. HORUS never accepts the 40-byte header.

`horus/perception/gta_stream.py` implements the client; `perception.source: gta://host:port` makes
the camera reader use it. HORUS gets drone-camera frames only. Each read skips frames with
`camera active = 0` (pause menu, landing page, gameplay camera, no bridge) until an active frame
arrives. If none arrives within one `read_timeout_s` deadline, the read raises `TimeoutError`, even
while inactive frames keep coming. The camera supervisor then reports `stalled`, and nothing
reaches perception. The pose seq of the returned frame is on the stream's `last_header.pose_seq`.

### Camera block

HorusGta (`gta/horus_mod`, `Core/CameraBlock.cs`) writes the shared-memory block
`Local\HorusGtaCamera` (64 bytes, layout in `gta/common/camera_block.hpp`) on every script tick:
active flag, applied pose seq, game frame, under a seqlock whose sequence grows by 2 per tick. The
plugin maps it at startup and reads it at every `Present`. A frame is stamped `camera active = 1`
only if the block says active **and** its sequence changed within the last 2 presents. GTA does not
run script threads while paused, so the block keeps its last `active = 1` during the pause menu.
The stale sequence turns pause-menu frames (and frames after HorusGta stopped or crashed) into
`camera active = 0`, except for at most the first two presents after the last tick. Pose seq and
game frame keep their last values in inactive frames, for diagnosis. Without HorusGta every frame
reads `camera active = 0`, script age `0xFFFFFFFF`.

## Configuration

Optional `horus_capture.ini` next to the plugin (defaults shown):

```ini
[capture]
bind=127.0.0.1
port=5610
max_hz=15
```

The plugin writes `horus_capture.log` next to itself.

## Build, install, verify

```bash
gta/build.sh                                      # MinGW cross-build in Docker -> gta/build/
scripts/koko/gta_mods.sh install --capture-only   # ASI loader + plugin, any game version
scripts/koko/gta_mods.sh install                  # full stack incl. HORUS Script Hook and the mods
```

`install` sets the `dinput8`/`xinput1_4` Wine DLL overrides in GTA's Proton prefix; no Steam launch
options are needed.

`horus_capture_selftest.exe` stands in for the game: a D3D12 window that clears its back buffer to
a fixed palette. `selftest/check_stream.py` pulls frames through `gta_stream.py` and checks size,
uniformity, exact colour per format, freshness, and that the camera fields read inactive. On Koko
(Proton needs a real X display; Xvfb has no DRI3 and the Vulkan swap chain fails), a display output
must also be on. On 2026-10-21, with Koko's panel off (connector disabled, as its
`screen-backlight-toggle` leaves it), the selftest presented at about 1 Hz. In every format the
checker got no frame before the selftest exited ("GTA capture stream closed"); with the panel on,
all passed. `gta_game.sh start` refuses for the same reason.

```bash
export STEAM_COMPAT_CLIENT_INSTALL_PATH=$HOME/.steam/steam STEAM_COMPAT_DATA_PATH=$HOME/horus-gta/selftest-compat
export DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority
"$HOME/.steam/steam/steamapps/common/Proton - Experimental/proton" run ./horus_capture_selftest.exe bgra8 20 &
python3 gta/capture/selftest/check_stream.py
```

Formats: `bgra8`, `rgba8`, `rgb10a2`, `rgba16f`; add `--late-load` to create the swap chain before
the plugin loads (the `ExecuteCommandLists` fallback).

`--camera` (on both the selftest and `check_stream.py --camera --frames 60`) makes the selftest
stand in for HorusGta too. It writes the camera block before each `Present` of an even palette
colour and stops writing during odd colours, like a game paused with the block still saying
active. Frames of even colours must then be active with that frame's pose seq; frames of odd
colours must be inactive, apart from the two-present grace.

While GTA itself holds port 5610, put a `horus_capture.ini` with `port=5620` next to the selftest
copy of the plugin and pass `--port 5620` to `check_stream.py`.

In GTA V Enhanced 1.0.1158.16 under Proton Experimental the plugin loads through the ASI loader,
hooks the game's swap chain and serves its 1920x1080 BGRA8 back buffer at the configured 15 Hz
(checked on the startup screens; the frames match the display).
