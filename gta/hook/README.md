# HORUS Script Hook: our Script Hook V for GTA V Enhanced

An open implementation of the Script Hook V plugin API, built as `ScriptHookV.dll`. ASI plugins
written against the Script Hook V SDK load it unchanged: Alexander Blade's Native Trainer,
ScriptHookVDotNet Enhanced and the .NET scripts it hosts (Los Santos FPV, `HorusGta.dll`). It finds
everything it needs through runtime memory patterns, so it does not wait for a per-build release;
a game update only breaks it when a pattern below stops matching.

Story mode only. If a GTA Online session starts, the hook closes the game, as Script Hook V does.

## How it works

- **Patterns** (in the unpacked game image, scanned once the game window exists): `ScriptThreads`,
  `InitNativeTables`, `RunScriptThreads` and `IsSessionStarted` are required; `Version`,
  `ScriptGlobals`, `HandlesAndPtrs` and the ped/vehicle/object pools are optional and only disable
  their API when missing. Patterns and structure layouts follow
  [YimMenuV2](https://github.com/YimMenu/YimMenuV2) (GPL-2.0); no code is taken from it.
- **Natives**: plugins pass the original (Legacy) hashes. `ScriptHookV.crossmap.txt` maps them to
  Enhanced's hashes (6595 entries; YimMenuV2's `crossmap.txt` at commit `c3604ca8`, unchanged since
  Enhanced shipped; see "Crossmap licence" below for how it is obtained). Each line is
  `0xORIGINAL,0xENHANCED` (spaces around the comma and CRLF allowed); blank lines are ignored and any
  other line is skipped and logged (`WARNING: crossmap: skipped N malformed line(s): <line numbers>`),
  so one bad line does not drop the entries after it. A hash becomes a handler
  by passing a one-entry fake `rage::scrProgram` through the game's own `InitNativeTables`, which
  reports unknown hashes. Handlers are cached; a call to an unknown native logs once and returns 0.
- **Scripts**: each `scriptRegister`ed main runs in its own fiber. After the game's
  `RunScriptThreads`, the hook makes the `main_persistent` script thread the current script thread
  in the game's TLS context and switches to every fiber whose `scriptWait` has expired. Scripts start
  when `main_persistent` appears (story mode) and restart when it is replaced (a new session).
- **Keyboard**: the `sgaWindow` window procedure is subclassed and key messages are passed to the
  registered handlers with Script Hook V's arguments.
- **Present callbacks**: `IDXGISwapChain::Present`/`Present1` are hooked through the shared probe in
  `gta/common/dxgi_probe.hpp`.
- **Globals, handles, pools**: `getGlobalPtr`, `getScriptHandleBaseAddress` and
  `worldGetAllPeds/Vehicles/Objects/Pickups` read the game structures directly (pools are decrypted
  as the game does). `worldGetAllPickups` lists pickup objects (the pool of `CPickup`, the entity a
  pickup spawns as): the handle `GET_PICKUP_OBJECT` returns, not the `CREATE_PICKUP` placement
  handle. See "Pickup pool".
- **Game version**: `getGameVersion` returns Script Hook V's own enum value for the executable's
  file version (read from its version resource, so it works before the patterns are scanned), and
  `VER_UNK` (-1) for builds Script Hook V 3889.0 does not list, 1.0.1158.16 included.
  `getGameVersionInfo` returns that file version itself (`{1, 0, 1158, 16}` on Koko). Each logs its
  result once. See "Game version".
- **Textures**: `createTexture` decodes the file with WIC (`hook/src/texture.hpp`; PNG, JPEG,
  BMP, … to RGBA8) on the calling script thread and returns the next id, counting from 0. A file
  that cannot be opened or decoded gives -1 and an `ERROR: createTexture("…")` line. The next
  Present uploads the texture to a `R8G8B8A8_UNORM` texture on the game's D3D12 device (the swap
  chain's), through the hook's own direct queue, and waits for that copy. Both steps log the size:
  `createTexture("…"): id N, WxH` and `texture N: WxH uploaded to the game's D3D12 device`. As in
  Script Hook V, textures are dropped when scripts restart (here: a new `main_persistent` thread,
  that is, a new session). Their ids are not reused.
- **drawTexture**: each call sets instance `index` (0–63) of a texture for `time` ms; a later call
  for the same pair replaces it. Every Present draws the live instances, lowest `level` first, on
  the queue that presents the swap chain, into the back buffer about to be shown. See "Drawing
  textures".

All of Script Hook V's exports are provided.

## Drawing textures

`drawTexture(id, index, level, time, sizeX, sizeY, centerX, centerY, posX, posY, rotation,
screenHeightScaleFactor, r, g, b, a)` follows Script Hook V's `main.h`.

- **Size**: the instance is `sizeX` of the screen width wide and `sizeY × screenHeightScaleFactor`
  of the screen height high. ScriptHookVDotNet passes the screen aspect ratio as the factor and
  `sizeY` already divided by it (`CustomSprite.InternalDraw`), so equal sizes give a square.
- **Placement**: (`centerX`, `centerY`) is the point of the texture, in 0–1, that lands on
  (`posX`, `posY`), with screen (0, 0) at the top left. The texture rotates about that point by
  `rotation` turns, clockwise (ScriptHookVDotNet passes degrees / 360).
- **Colour**: the texel is multiplied by `r, g, b, a`, and the result is blended with straight
  alpha over the frame.

The geometry is `texture_quad` and the instance store `TextureInstances` in `hook/src/hook_core.hpp`;
the renderer is `hook/src/texture_draw.hpp`:

- One root signature: 12 root constants (the 4 corners and the colour), one SRV table and a linear
  clamp sampler.
- One pipeline per back-buffer format. Its shaders are compiled at first use with `D3DCompile` from
  `d3dcompiler_47.dll`.
- A ring of 4 command allocators and a fence.

Ordering:

- **Queue**: the overlay goes on the queue that presents the swap chain, so it runs after the game's
  frame and before the Present. The hook learns that queue from `CreateSwapChain(ForHwnd)`. For a
  swap chain created before the hook's DXGI hooks (the usual case in GTA), it uses the last direct
  queue passed to `ExecuteCommandLists` on the same device. `gta/common/swapchain_queues.hpp` holds
  this lookup and is shared with `horus_capture.asi`.
- **Back buffer**: the overlay moves the back buffer from PRESENT to RENDER_TARGET and back.
- **Uploads**: they ran on the hook's own queue but finished before (the CPU waits for their fence),
  so the overlay never reads a texture that is still being written.
- **Releases**: textures dropped on a script restart are released only after the overlay's fence
  shows the GPU is done with them.
- **Resizes**: `ResizeBuffers(1)` waits for that fence too.

The hook's Present hook is installed after horus_capture's, so it runs first: captured frames
include the overlay.

Checked on 2026-10-21 (`horus_api_probe.asi`, `koko:~/horus-gta/run16/`). The hook logged
`drawTexture: overlay on queue … (last direct queue of ExecuteCommandLists), back buffer 1920x1080
format 87` (B8G8R8A8_UNORM). The probe drew a 32×32 texture (magenta, top-left quadrant yellow)
every frame for 20 s at (0.25, 0.30), size 0.2 × 0.2 with the aspect ratio 1.7778 and `time`
3000 ms, then stopped:

| | Expected | `horus_capture` frames |
| --- | --- | --- |
| Texture | x 0.150–0.350, y 0.122–0.478 (a 384 px square) | x 0.150–0.350, y 0.122–0.478 |
| Yellow quadrant | top left of the square | x 0.150–0.248, y 0.122–0.297 |
| First frame with it | after the first call, 03:39:01.860 | 03:39:02.291 |
| Gone | 3000 ms after the last call, 03:39:21.891 | last with it 03:39:24.998, first without 03:39:25.374 |

The 0.1 s past 3000 ms is the frame read's latency. A screen grab during the drawing shows the same
square over the game's HUD text, and one after the expiry shows none.

## Pickup pool

Enhanced keeps the pickup pool pointer encrypted, like the ped and object pools. Its decoder
function starts with code the hook finds by `kPickupPoolPattern` (`hook/src/hook_core.hpp`). The
pattern is ScriptHookVDotNet Enhanced 1.1.0.6's (`source/core/NativeMemory.cs`), with the
instructions its offsets imply written out:

1. A flag byte, whose bit 0 means the pool is set.
2. `rcx = rol(second, imm)` and `rax = first ^ rcx`.
3. `rax = rol(rax, (cl & mask) + add)`, then `rax = rol(rax, imm)`, and the pointer is `~rax`.

The hook reads the flag and both qwords at each call. The four constants come from the code, and
the final scan pass logs them; on 1158.16 it logged
`pickup pool decoder: rol 31, mask 0x1F, add 5, rol 32`. The same formula, with rol 30 and
add 2 or 3, decodes the ped and object pools.

The pool is an `rage::fwBasePool`, so the hook collects its handles like the ped and object
pools. In game it lists exactly the pickup object `GET_PICKUP_OBJECT` returns and drops it after
`REMOVE_PICKUP` (`horus_api_probe.asi`, see "Build, install, run").

## Game version

Script Hook V's `eGameVersion` numbering is not in a public header for Enhanced. It was read from
Alexander Blade's `ScriptHookV.dll` 3889.0 (`ScriptHookV_3889.0_1158.13.zip`, `bin/ScriptHookV.dll`,
1987072 bytes) with `objdump -d`/`-p` (RVAs relative to image base `0x180000000`):

- `getGameVersion` (export RVA `0x6f30`) is `mov eax,[rip+…] ; ret` and returns the global
  `0x1801e3218`. That global is written once at startup (`0x180007f91`/`0x180007fa1`): first from the
  Legacy detector `0x180001460`, then, if that returned -1, from the Enhanced detector
  `0x180001a80`. If both return -1, it logs `FATAL: Unknown game version`.
- The Enhanced detector reads the dword at game RVA `0x1870000` (`mov ecx,[rax+0x141870000]` with
  `rax` = image base − `0x140000000`) and maps 12 fingerprints to `0x3e9`…`0x3f4` (1001–1012); any
  other value gives -1.
- The `INIT: Success, game version is %s` log (`0x18000806a`) takes the name from
  `table[v]` at RVA `0x56f70` (103 pointers) for v < `0x67`, else `table[v − 1001]` at RVA `0x5daf0`
  (12 pointers, addressed as `0x5bba8 + v*8`). The pointers lead to the `VER_*` strings at RVA `0x56560`…,
  in the order shown below. The same 103/1001 split appears at `0x180008403` (`cmp eax,0x3e9`;
  `cmp eax,0x66`).

| Value | Name | Fingerprint at RVA 0x1870000 |
|---|---|---|
| 1001 | `VER_EN_1_0_811_8` | `0x50244c29` |
| 1002 | `VER_EN_1_0_812_8` | `0x75000000` |
| 1003 | `VER_EN_1_0_813_11` | `0x74000000` |
| 1004 | `VER_EN_1_0_814_9` | `0xc7cc0175` |
| 1005 | `VER_EN_1_0_889_15` | `0xcaaae8ce` |
| 1006 | `VER_EN_1_0_889_19` | `0x290f4400` |
| 1007 | `VER_EN_1_0_889_22` | `0x01f886f6` |
| 1008 | `VER_EN_1_0_1013_17` | `0x8949d489` |
| 1009 | `VER_EN_1_0_1013_29` | `0xf64e7504` |
| 1010 | `VER_EN_1_0_1013_33` | `0xaac8c60f` |
| 1011 | `VER_EN_1_0_1013_34` | `0x00008024` |
| 1012 | `VER_EN_1_0_1158_13` | `0x487850ff` |
| -1 | `VER_UNK` (any other build, e.g. 1.0.1158.16) | |

Legacy builds are 0–102 in release order, from `VER_1_0_335_2_STEAM` = 0 and
`VER_1_0_335_2_NOSTEAM` = 1 (`_STEAM`/`_NOSTEAM` pairs, plus `VER_1_0_1868_4_EGS` = 58) to
`VER_1_0_2699_16` and the unsuffixed builds through `VER_1_0_3889_0` = 102. This hook runs only on
Enhanced, so it never returns them.

The hook maps the file version `1.0.<build>.<revision>` of the running executable to the same
values, using `kShvEnhancedVersions` in `hook_core.hpp` instead of a code fingerprint, because a
fingerprint cannot be checked without each old build. Script Hook V names each build by the same
number (its readme lists `1.0.811.8, … 1.0.1013.17/29/33/34, 1.0.1158.13`). The game on Koko is
`1.0.1158.16` (FileVersion and `VS_FIXEDFILEINFO` of `GTA5_Enhanced.exe`), which is not listed, so
`getGameVersion` logs `getGameVersion: game 1.0.1158.16 is not in Script Hook V 3889.0's list;
returning VER_UNK (-1)`.

`getGameVersionInfo` (export RVA `0x6f40`, `jmp 0x1800125f0`) returns an 8-byte struct by value in
RAX:

```cpp
struct GameVersionInfo { uint16_t major, minor, build, revision; };  // hook_core.hpp
```

On its first call, Script Hook V takes `GetModuleHandleA(nullptr)` (the game), reads the resource
directory from the PE header (data directory 2 at NT header + `0x98`/`0x9c`) and searches it for
the UTF-16 key `FileVersion\0` (24 bytes compared as three qwords, from `0x1801d2dc0`). It skips the
zero bytes after the key, then calls the statically linked CRT routine `0x1800163ec` with the wide
format `L"%d.%d.%d.%d"` (at `0x1801d2dd8`) and four int pointers, i.e. `swscanf` (by its arguments).
If that returns 4, the four ints are stored as words at offsets 0/2/4/6 of the cached struct
(`0x1801e8780`, in `.bss`), and the result is returned with `mov rax,[0x1801e8780]`. The cache flag
`0x1801e3227` is set even when the search fails, so the struct then stays all zero. The hook
returns the same values from the shared `VS_FIXEDFILEINFO` read that `getGameVersion` uses rather
than from the `FileVersion` string. For `GTA5_Enhanced.exe` both are `1.0.1158.16`. With no
version resource, the hook also returns all zero.

## Diagnostics

`ScriptHookV.log` next to the DLL records the patterns, game version, crossmap size, how much of
it the build registers and which entries it does not ("Checked"), the script threads present when
scripts start, each script's first run, and three kinds of trouble with a module+offset stack of the
game thread: a script that has not yielded for 5 s, no script tick (`RunScriptThreads` call) for
10 s (both from a watchdog thread, once per occurrence; `script ticks resumed after N ms` follows
when ticks come back), and a fault raised on the game thread inside a script fiber. Every 60 s the
game thread logs a heartbeat:
`heartbeat: N ticks in M ms, host thread id …, scripts R running, W waiting, F finished`. At
normal speed N is the frame rate times 60 (1800 on Koko with the panel on, 2026-10-20). About 60
means the game presents once a second, which Koko does while its panel is DPMS-off; the user's
`~/.local/bin/screen-backlight-toggle` keybinding keeps it off by forcing DPMS off every 0.1 s, so
run that toggle once before a GTA session and once afterwards. Opening the pause menu stops script
ticks: the watchdog logs `no script tick for 10… ms` with the game thread inside
`GTA5_Enhanced.exe`, then `script ticks resumed after N ms` on resume, and the heartbeat that falls
due during the pause is logged at resume with the longer period.

## Memory

Koko (13.5 GiB RAM, 2 GiB swap, AMD APU with 2 GiB carved out as VRAM) runs GTA only with enough
headroom. If memory runs out, the game thread stops ticking entirely, which is different from the
1 Hz panel-off rate. Measured on 2026-10-20 in story mode with the GTA graphics settings already
at their lowest:

| What | Memory |
| --- | --- |
| GTA5_Enhanced.exe | 1.5 GiB PSS, about 2.0 GiB RSS, plus about 1 GiB GTT (GPU memory in system RAM) |
| SocialClubHelper.exe (Rockstar launcher's Chromium: browser, GPU, 2 renderers, 4 utilities) | 3.3 GiB PSS |
| Launcher.exe, RockstarService, PlayGTAV, error handler | 0.3 GiB PSS |
| whole stack, as a drop in `available` | about 7.1 GiB (7879 → 600–770 MiB) |

SocialClubHelper cannot be cut: killed while the game runs, it is restarted within a second with
a third renderer, and available memory fell to 497 MiB. The 8192 MiB guard set from this (the
stack plus 1 GiB) replaced a 6144 MiB one that let GTA start into a thrash. HORUS in GTA needs
more: on 2026-10-21 GTA, SITL and horus-mcp with a `horus` client took available memory down by up
to 8658 MiB, from 9106 to 448 MiB (`gta/horus_mod/README.md` "Checked in game", memory
budget). `gta_game.sh start` therefore requires 9728 MiB available (that plus 1 GiB, rounded up).
With Koko's usual desktop load (Firefox about 2.4 GiB, Steam's webhelper about 1.5 GiB PSS) that
requires closing something first; even with Firefox closed, Koko idles at about 9.1 GiB.

How a memory stall looks: available memory drops below about 250 MiB, memory pressure
(`/proc/pressure/memory` `some avg10`) rises past 25–50 %, load climbs past 50–200, the watchdog
logs `no script tick` and ticks stop for minutes. The UDP pose queue on 5611 fills to its 213120 B
limit (`net.core.rmem_default` 212992 plus overhead). That is about 221 datagrams, since Linux
charges about 960 B per 56-byte packet; at 1 Hz HorusGta drains the queue every tick, so a full
queue means no ticks at all. ScriptHookVDotNet may abort the script
(`deactivated: script aborted` in `HorusGta.log`). In the end, GTA itself invokes the kernel OOM
killer, which kills a steamwebhelper, and the game exits about 20 s later. Two soaks on 2026-10-20
(the 20 min one started at 7879 MiB available, the short one at 7463 MiB) and the 11:57–12:24
incident show this signature:

- 11:57–12:24: 249 MiB free, swap 2.0/2.0 GiB, load 69, the 5611 queue at 213120 B; at 12:23:07
  `GTA5_Enhanced.e invoked oom-killer`, steamwebhelper killed. The panel's state then is not
  recorded, and a 1 Hz panel-off rate cannot fill the queue. Memory was exhausted during this stall
  and at the OOM, but nothing recorded what started it at 11:57: the onset is not identified, and a
  contribution from our hook, mod or capture plugin is not excluded.
- Soak 14:06–14:26 (panel on throughout): 21 heartbeats at 60.000–60.035 s, 1600–1670 ticks each,
  with available memory at 440–770 MiB. Then at 14:20:53 the user's `reddit-yatirim` timer
  started (Camoufox, 1.6–1.75 GiB peak; runs every even hour at :20–:25), and available memory fell
  to 193–257 MiB. Ticks stopped from 14:22:27 to 14:25:37 (191 s), the queue reached 213120 B, and
  load reached 212. At 14:25:38 the OOM killer took a steamwebhelper, and the game exited at 14:26:11.
- Run from 14:49 (start at 7463 MiB): available memory was 180 MiB within 30 s with no timer
  running, and ticks stopped twice for 109 s and 178 s. HorusGta was aborted at 14:51:15; at
  14:55:28 GTA invoked the OOM killer, and it exited at 14:55:48.

Two clean 20 min soaks on 2026-10-20, each started with Firefox closed (the user's order), outside
the scraper window, with the panel on, the synthetic 50 Hz sender and one frame pulled every 10 s:

| | 17:04–17:24 (`koko:~/horus-gta/run1/`) | 17:37–17:57 (`koko:~/horus-gta/run2/`) |
|---|---|---|
| available at GTA start | 9057 MiB | 9446 MiB |
| available while running | 1791–2028 MiB | 1927–2241 MiB |
| load, at most | 9.8 | 10.0 |
| heartbeats (60.000–60.035 s) | 20, 1631–1793 ticks each | 20, 1780–1797 ticks each |
| `no script tick` during the soak | none | none |
| pulled frames that were drone frames | 108 of 108, pose seq 0–2 behind | 109 of 109, pose seq 0–2 behind |
| last packet → `deactivated` | 2.039 s (`timeout_s` 2) | 1.922 s (`timeout_s` 1.9) |

The 2.039 s release missed the 2 s requirement: the timeout is checked once per script tick, and
a packet counts from the tick that received it, so the release lands up to about two ticks after
`timeout_s`. The default is now 1.9 s (`gta/horus_mod/README.md` "Release"). With it, 20 more
stop/start cycles of the sender in the second run released 1.906–1.964 s after the last packet
(mean 1.934 s; `run2/cycles-sender.log`, `run2/cycles-horus.log`). The two `no script tick` warnings
at 17:36:34 and 17:36:51 in that run are ScriptHookVDotNet's first-start scan, before the soak.

What this shows and what it does not:

- Shown: memory exhaustion stalls and kills GTA on Koko. The 14:06 soak (the scraper started
  mid-run) and the 14:49 run (started at 7463 MiB) both ran out of memory, stopped ticking with the
  5611 queue full, and ended in the OOM killer and the game's exit.
- Shown: with about 9 GiB available at start, two 20 min runs of the same hook, mod and capture
  plugin ran clean, with no stall.
- Not shown: what started the 11:57–12:24 stall. Memory was exhausted during it and at the 12:23
  OOM, but its onset was not recorded. Clean runs at high memory do not exclude a contribution from
  our hook, mod or capture plugin to that stall, for example under memory pressure only.

Keep GTA sessions out of the scraper's window, and keep `available` above 1 GiB while GTA runs. In
practice that means closing Firefox (about 3.6 GiB with its tabs) before `gta_game.sh start`.

## Checked

GTA V Enhanced 1.0.1158.16 under Proton Experimental on Koko:

- all patterns found; `crossmap: 6588 of 6595 natives registered in this build` (the 7 missing
  ones are listed below);
- Native Trainer: menu opens with F4 and draws (keyboard handler, natives, fibers);
- ScriptHookVDotNet Enhanced 1.1.0.6 initialises (its own `NativeMemory` scan blocks the game thread
  for about 11 s on first start) and runs Los Santos FPV and `HorusGta.dll`;
- `HorusGta.dll` drives the camera from ArduPilot SITL (`gta/horus_mod/README.md`).

### Natives the crossmap maps but 1158.16 does not register

After the coverage count, the hook logs every crossmap entry whose Enhanced hash the game does not
register, as `original->enhanced` (sorted, at most 64). On 1158.16 (2026-10-21 02:41:09,
`koko:~/horus-gta/run13/d1-ScriptHookV.log`):

```text
crossmap: 7 not registered (original->enhanced): 0x1775961C2FBBCB5C->0x94796772E71D5123, ...
```

| Original hash | Enhanced hash | Name | Source of the name |
| --- | --- | --- | --- |
| `0x1775961C2FBBCB5C` | `0x94796772E71D5123` | `NETWORK::NETWORK_SET_PROPERTY_ID` | YimMenuV2 `natives_gen9.json`, SHVDNE `Hash` |
| `0x36391F397731595D` | `0x9E0B6B766CF810E5` | `NETWORK::NETWORK_START_COMMUNICATION_PERMISSIONS_CHECK` | SHVDNE `Hash` (unnamed in YimMenuV2, alloc8or and FiveM) |
| `0x4AFF7E02E485E92B` | `0x9CE7919ACC98C899` | `STATS::PLAYSTATS_KILL_YOURSELF` | SHVDNE `Hash` (unnamed in YimMenuV2, alloc8or and FiveM) |
| `0x6638C0F19DE692FE` | `0x6638C0F19DE692FE` | `VEHICLE::_IS_VEHICLE_GEN9_EXCLUSIVE_MODEL` | alloc8or `gta5-nativedb-data` (build 3095) |
| `0xA02E59562D711006` | `0xD0C8A7FF1FD5A461` | `NETWORK::NETWORK_SESSION_END` | YimMenuV2, SHVDNE `Hash` |
| `0xC2B82527CA77053E` | `0x5F1F0F7CE82B92D5` | `NETWORK::NETWORK_CLEAR_PROPERTY_ID` | YimMenuV2, SHVDNE `Hash` |
| `0xE4F6E8D07A2F0F51` | `0xDF0800F98C6064FB` | `SOCIALCLUB::SC_PAUSE_NEWS_INIT_STARTER_PACK` | alloc8or (build 1290), SHVDNE `Hash` |

Name sources:

- YimMenuV2 `src/game/gta/invoker/natives_gen9.json`, at the crossmap's pinned commit `c3604ca8`.
- alloc8or `gta5-nativedb-data` `natives.json`.
- FiveM's `runtime.fivem.net/doc/natives.json`.
- The `GTA.Native.Hash` enum of the deployed `ScriptHookVDotNet3.dll` (SHVDNE 1.1.0.6), decompiled
  with ilspycmd 9.1.0.7988.

All seven are online, Social Club or stats natives, or (`_IS_VEHICLE_GEN9_EXCLUSIVE_MODEL`) a
Legacy-only query whose hash the crossmap maps to itself.

None of our mods calls them:

- The deployed `ScriptHookVDotNet2.dll` and `ScriptHookVDotNet3.dll` contain four and six of the
  hashes, but only as `Hash` enum members (`GTA.Native/Hash.cs`). Nothing else in the decompiled
  sources refers to them.
- `HorusGta.dll`, `LosSantosFpv.dll`, `NativeTrainer.asi`, `ScriptHookVDotNet.asi` and
  `horus_capture.asi` contain none of the 14 values, neither as 8-byte constants nor as hex text.
- `gta/` source does not name them.

A script that did call one would get 0 and a single
`ERROR: native 0x… is not registered in this game build` line. None appeared in that session.

### "Pattern to enable MP cars in SP not found"

ScriptHookVDotNet Enhanced 1.1.0.6 logs this error once when it starts (`ScriptHookVDotNet.log`,
first seen 2026-10-20 11:31:20 on 1158.16). It comes from SHVDNE's own code, in the
`Enable All DLC Vehicles` block of its `NativeMemory` static constructor
(`source/core/NativeMemory.cs` lines 2771–2810 at tag `v1.1.0.6`, commit `d756d479`). On Enhanced it
searches the bytecode of the loaded `shop_controller` script (joaat `0x39DA738B`) for
`2D ? ? ? ? 2C ? ? ? 56 ? ? 71 2E ? ? 62`. It reads the script global at match+17 and sets it to 1
through `getGlobalPtr`; SHVDNE names the block "Enable All DLC Vehicles", so the global lets
online-only (DLC) vehicle models be used in story mode. `FindPatternInScript` returns nothing
without logging when any of these holds:

1. SHVDNE's own image pattern for the script table (`48 03 05 ? ? ? ? 4c 85 c0 0f 84 ? ? ? ? e9`)
   was not found.
2. `shop_controller` was not loaded when SHVDNE initialised.
3. The bytecode pattern is missing from the loaded script. In this case SHVDNE also logs
   `NativeMemory.FindPatternInScript could not find pattern: …`.

The hook is not involved in the lookup: SHVDNE scans the image itself and reads the game's script
table directly, and the hook's `getGlobalPtr` would only be called after a match. The 11:31 log was
overwritten by later runs, but the 2026-10-20 16:58 run (copy in `koko:~/horus-gta/run1/logs/`)
logged the error at 16:58:52 with no `FindPatternInScript could not find pattern` line before it,
so it is not case 3: either SHVDNE's script-table pattern no longer matches 1158.16 (case 1), or
`shop_controller` was not loaded when NativeMemory initialised, at 16:58:45–16:58:55, right after
the hook started scripts on `main_persistent` (case 2). SHVDNE logs neither case, so the log
cannot tell them apart. The pattern itself is unchanged between SHVDNE 1.1.0.5 and 1.1.0.6, and
1.1.0.6 targets 1158.13.

Impact: SHVDNE does not set that global, so online-only vehicles spawned by scripts in story mode are
not enabled (they may fail to spawn or be removed). No mod we install spawns a vehicle:

- `HorusGta.dll` (`gta/horus_mod/src`) calls 19 camera, focus, ground, HUD and wanted-level
  natives, and no vehicle native or `World.Create*`.
- Los Santos FPV 1.0.1 (decompiled with ilspycmd 9.1) spawns one prop with `World.CreateProp`:
  `ch_prop_casino_drone_02a`, falling back to `prop_tennis_ball`. It also creates a camera and
  raycasts. Its 15 direct natives are camera, focus, entity, ped ragdoll, weapon, `GET_HASH_KEY` and
  wanted-level calls, and none is a vehicle native.

Density multipliers and shape tests do not spawn vehicles either, so no item is
needed.

### Scripts while the game is paused

GTA does not call `RunScriptThreads` while its pause menu is open, so no script runs, ours included.
The hook keeps that behaviour: natives called during the pause menu are not safe. Measured on
2026-10-20 (1.0.1158.16, 100% save; copy of the log in `koko:~/horus-gta/run1/logs/`):

- Escape was pressed at 17:01:20.25. The watchdog logged
  `WARNING: no script tick for 10389 ms (game thread not running scripts)` at 17:01:30. The game
  thread's stack at that moment is `ntdll`/`kernelbase` waits under `GTA5_Enhanced.exe` frames,
  with no `ScriptHookV.dll` frame, so the game thread was alive but not running scripts.
- Escape again at 17:01:49.34 → `script ticks resumed after 29293 ms without a tick` at 17:01:49.
  The next heartbeat counted 1561 ticks in 84038 ms, against about 1790 per 60 s unpaused.
- Ten 4-second pause/resume cycles (17:02:38–17:04:09) left the game running. The 17:03:56
  heartbeat counted 948 ticks in 64669 ms, and each resume re-activated HorusGta: 10 `activated at
  seq` lines.

Alexander Blade's Script Hook V 3889.0 cannot be compared on this build: it lists Enhanced builds
only up to 1158.13 and stops with `FATAL: Unknown game version` on 1158.16 ("Game version").
HORUS relies on the capture plugin and client instead. Pause-menu frames are stamped
`camera active = 0` once the script misses two presents (`gta/capture/README.md` "Camera block"),
and `gta_stream.py` skips them until its read deadline. The camera supervisor then reports `stalled`
(in the same run, reads during the 29 s pause each timed out after 2 s with about 29 inactive
frames skipped).

## Crossmap licence

The crossmap is YimMenuV2's `src/game/gta/invoker/crossmap.txt`
([YimMenu/YimMenuV2](https://github.com/YimMenu/YimMenuV2), GPL-2.0, by the YimMenu authors). Decision:
it stays a runtime download and is never
committed to this repository or embedded in the hook. `gta_mods.sh install` fetches it once into
Koko's `~/horus-gta/staging/` from a URL pinned to commit
`c3604ca8195873c6b89008be5fefad517e554b18`. Every install checks the staged copy against the pinned
sha256 `66a5ca1e1ed092b5528073bd41705e4737f43535dfecb87236c1f85aa8d4968f` before deploying it, and stops if it
differs. The hook only reads the file at runtime (`ScriptHookV.crossmap.txt` next to the DLL); the
hook unit tests use synthetic lines. `/gta/build/` is git-ignored.

## Build, install, run

```bash
gta/build.sh                       # MinGW cross-build in Docker -> gta/build/ScriptHookV.dll
WINEDEBUG=-all wine gta/build/horus_hook_tests.exe gta/build/ScriptHookV.dll gta/hook/src/exports.def  # unit tests
scripts/koko/gta_mods.sh install   # hook + crossmap + ASI loader + SHVDN + mods into the game folder
scripts/koko/gta_mods.sh install --api-probe  # the same plus horus_api_probe.asi (in-game API checks)
scripts/koko/gta_game.sh start     # launch GTA on Koko and enter story mode
scripts/koko/gta_game.sh status    # processes, available memory, ScriptHookV.log tail
scripts/koko/gta_game.sh stop      # close GTA, the Rockstar launcher and Social Club helpers
```

`start` refuses while GTA already runs, while Gazebo runs (a HORUS simulation), while
the running Steam client is not signed in, when no display output is enabled (Koko's screen off:
GTA's swap chain then presents once a second, so scripts and captured frames run at 1 Hz), while a
screen lock covers the desktop (a visible `dark-screenlock` window or an active Cinnamon
screensaver: the STORY-tab check needs GTA's window focused, and on 2026-10-22 the Rockstar launcher
stalled under the lock until the 180 s start timeout), or when Koko has less than
`GTA_MIN_AVAILABLE_MIB` (default 9728, see "Memory") MiB available. The memory refusal lists the
six largest processes; Steam's web helpers grow over a long session, and restarting Steam frees
them but brings back its account picker.

Signed in means:

- the client in `~/.steam/steam.pid` runs;
- the newest connection state in `~/.steam/steam/logs/connection_log.txt` is `Logged On`;
- that line was written after this client started.

Steam on Koko asks which account to use every time it starts. While it waits at that "Who's
playing?" picker, `steam steam://rungameid/…` puts the URL in `~/.steam/steam.pipe` and exits, and
the client runs it as soon as someone signs in. On 2026-10-21 that started the Rockstar launcher
5 minutes after a failed `start`, with memory below the guard. A queued URL cannot be withdrawn, so
the check comes before the URL is sent. If GTA has still not started 180 s after the URL, `start`
closes any Rockstar process the launch started.

`start` deletes the old
`ScriptHookV.log`, launches through Steam, waits for `INIT: success` and `keyboard: hooked`, then
handles GTA's landing page: it presses E (next tab) until a pixel check of the tab bar shows STORY
selected and ONLINE and GTA+ not, and only then presses Enter, so it never enters GTA Online. GTA
can drop that Enter. On 2026-10-20 at 19:54 STORY stayed highlighted for 5 minutes, and a second
Enter loaded story mode. `start` therefore checks that the page leaves STORY after Enter. If STORY
is still highlighted 30 s after a press, it presses Enter again, at most 3 times in all, and never
after the page has shown anything else. It sends no key while the watchdog's last line is
`no script tick` without a `resumed` line after it; the landing page then shows a frozen frame.
Every press and the page leaving are logged with a time. It returns once the log shows
`host script thread main_persistent` (about 2 minutes from launch). Keys are held for 150 ms after
activating the game window; GTA ignores xdotool's instant taps and keys sent to an unfocused window.

`horus_hook_tests.exe` (`gta/hook/tests/hook_tests.cpp`, built by `gta/build.sh`) needs no game. It
checks the pure parts in `hook/src/hook_core.hpp` (pattern parsing and matching on a synthetic
buffer, RIP-relative targets, `joaat("main_persistent")` = `0x5700179C`, crossmap parsing with
malformed lines, the `getGameVersion` table (1158.13 → 1012, 811.8 → 1001, 1158.16 and Legacy builds →
none, values 1001–1012 in order with matching names), the pool pointer decoder (against a
step-by-step copy of ScriptHookVDotNet Enhanced's pickup decoder and against the hook's earlier
ped/object formula, 1000 random inputs each), `kPickupPoolPattern` and `pickup_pool_ref` on a
synthetic decoder prologue, and the version resource parser on a synthetic
buffer and on wine's `kernel32.dll` compared with `VerQueryValueW`). It also loads the built
`ScriptHookV.dll` and calls its version exports. The test executable has a version resource
(`hook/tests/hook_tests.rc`, 1.0.1158.16, the build on Koko), so it stands in for the game:
`getGameVersion` must return -1 and `getGameVersionInfo` `{1, 0, 1158, 16}`, also read as the raw
RAX value an MSVC-built plugin sees. Finally, it checks that the DLL's export names equal `exports.def`
and all 24 exports of Alexander Blade's `ScriptHookV.dll` 3889.0. It prints `all hook tests passed` and exits 0, or prints each failed check and exits 1.
Checked with wine 11.20 (`/usr/bin/wine` on the laptop).

`horus_api_probe.asi` (`gta/hook/tests/api_probe.cpp`) checks the hook's APIs that need the
running game. Only `gta_mods.sh install --api-probe` installs it, and the next plain `install`
removes it again. It registers one script through `ScriptHookV.dll`'s exports and writes
`horus_api_probe.log` in the game folder. Copy that log out before the next install, which deletes
it with the other runtime files.

The script waits 15 s in story mode. Then, for `createTexture`, it writes a 37x23 RGBA PNG
(`horus_api_probe.png`, made by the test's own PNG writer `hook/tests/test_png.hpp`). It loads that
file twice, expecting consecutive ids from 0, and then a missing file, expecting -1. The decoded
and uploaded sizes appear in `ScriptHookV.log`.

For `worldGetAllPickups`:

1. `CREATE_PICKUP` places a `PICKUP_HEALTH_STANDARD` pickup 3 m from the player.
2. It waits until `GET_PICKUP_OBJECT` names an existing object.
3. It lists `worldGetAllPickups` before, with the pickup, and 2 s after `REMOVE_PICKUP`. Each handle
   is logged with whether it exists and is an object, its model and its position.

The log ends with `worldGetAllPickups: PASS` when all of these hold:

- the pickup object is listed only while the pickup exists;
- every listed handle is an existing object.

For `drawTexture`, it draws a 32×32 texture (magenta, top-left quadrant yellow) every frame for
20 s, then stops calling for 20 s. The log gives the first and last call to the millisecond. Where
the texture shows and when it goes are read from captured frames ("Drawing textures").

`horus_texture_selftest.exe` (`gta/hook/tests/texture_selftest.cpp`) checks `createTexture`'s and
`drawTexture`'s GPU code (`hook/src/texture.hpp`, `texture_draw.hpp`) on a real D3D12 device,
without the game:

- It writes PNGs of 37x23, 1x1 and 256x64 with the test PNG writer and decodes them with WIC,
  comparing every byte. The 37x23 rows are 148 bytes, so the copy has to honour the 256-byte row
  pitch.
- It checks that a missing file and a non-image are rejected.
- It uploads each image, reads the texture back from the GPU and compares again.
- It draws a 2×2 red/green/blue/white texture with `QuadRenderer` into a cleared 64×64 target and
  checks the pixels for each case:
  - the corners and edges of a centred half-size quad;
  - a quarter turn clockwise (red top right);
  - alpha 0.5 over black (0x80 per channel);
  - two quads in order (the second covers the first).
- It asks for the D3D12 debug layer and counts error messages in the info queue when the runtime
  has one.

vkd3d-proton (Proton's D3D12) has neither the debug layer nor an info queue. Under Proton, its own
log (`VKD3D_DEBUG=warn`) is where D3D12 errors would show:

```bash
cd ~/horus-gta/run15  # any directory holding horus_texture_selftest.exe
export DISPLAY=:0 STEAM_COMPAT_CLIENT_INSTALL_PATH=$HOME/.steam/steam \
  STEAM_COMPAT_DATA_PATH=$HOME/horus-gta/selftest-compat VKD3D_DEBUG=warn VKD3D_LOG_FILE=$PWD/vkd3d.log
"$HOME/.steam/steam/steamapps/common/Proton - Experimental/proton" run ./horus_texture_selftest.exe . --log
cat texture_selftest.log; grep -c 'err:' vkd3d.log  # PASS; 0
```

`--log` writes the report to `texture_selftest.log`, because `proton run` drops the program's
stdout. Under plain wine, run it with a work directory and read stdout.

## Proton prefix setup

ScriptHookVDotNet runs its scripts on .NET Framework 4.8, which Proton's prefix for GTA V Enhanced
(`~/.steam/steam/steamapps/compatdata/3240220/pfx`) lacks. With .NET installed but no profile entry
for the prefix user, every script fails with `System.IO.FileLoadException: The specified user does
not have a valid profile`. Both are set up once, by hand, with the game closed:

```bash
pfx=~/.steam/steam/steamapps/compatdata/3240220/pfx
wine="$HOME/.steam/steam/steamapps/common/Proton - Experimental/files/bin/wine"
WINEPREFIX="$pfx" WINE="$wine" winetricks -q dotnet48
sid=$(sed -n 's/^;; All keys relative to REGISTRY\\\\User\\\\//p' "$pfx/user.reg")  # Koko: S-1-5-21-1490488230-3286124790-1000-1000
WINEPREFIX="$pfx" "$wine" reg add "HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\$sid" \
  /v ProfileImagePath /t REG_SZ /d 'C:\users\steamuser' /f
```

The SID is the prefix's own, not Wine's usual `S-1-5-21-0-0-0-1000` (which the prefix also lists).
Back up `system.reg`/`user.reg` to `~/horus-gta/` first.

`gta_mods.sh install` (full mode) checks both before it touches the game folder, by reading
`system.reg` and `user.reg` without starting wine: `NDP\v4\Full` `Release` ≥ 528040 (4.8; Koko has
528049, 4.8.03761), and a `ProfileList\<SID>` key with `ProfileImagePath` for the SID named in
`user.reg`'s header. If either is missing, it prints the command above and exits 1 with nothing
installed. It then checks four values in
`drive_c/users/steamuser/Documents/Rockstar Games/GTAV Enhanced/settings.xml`:

- `PauseOnFocusLoss` 0. Otherwise GTA pauses its script threads (and HorusGta) whenever its window
  loses focus.
- `ScreenWidth` 1280, `ScreenHeight` 720 and `Windowed` 0 (exclusive fullscreen): the drone camera
  size (`gta/horus_mod/README.md` "Drone camera size").

If any value differs, the file is first copied to `~/horus-gta/settings.xml.<timestamp>.bak` and
only the differing values are rewritten. A missing file or element is an error: start GTA once to
create the file. Each value is printed as one `check …` line.
