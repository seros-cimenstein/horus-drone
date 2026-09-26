# Koko bring-up (2026-09-26)

Koko (`ssh koko`, sero@10.0.0.2): Linux Mint 22.2 (Ubuntu Noble), Ryzen 7 5825U 8c/16t, 13 GB RAM,
AMD Barcelo iGPU (Mesa `radeonsi`), Docker 29.1.3. Laptop 10.0.0.1 ↔ Koko 10.0.0.2 over 1 GbE
(~0.5 ms RTT, ~80 MB/s over SSH).

| Check | Result |
|---|---|
| Docker without sudo, render node | `docker info` OK as `sero`; groups include `docker`, `render`; `/dev/dri/renderD128` rw |
| Simulator image | `horus-sim:local-s1s2` loaded via `docker save \| ssh koko docker load` |
| Laptop → Koko UDP 10317 / TCP 18080 | both received on Koko after `ufw allow from 10.0.0.0/24` |
| Gazebo renderer | `GL_RENDERER = RENOIR (radeonsi)` in `ogre2.log` (not llvmpipe) |
| Stack start (`scripts/koko/remote_up.sh`) | gz server + SITL + ToF bridge + horus-mcp (perception on), camera RTP confirmed flowing |
| MCP through SSH tunnel | `horus-mcp-call get_state` / `get_frame` from the laptop; JPEG saved locally with blue/red people detected |
| Cross-machine Gazebo | laptop container (`GZ_IP=10.0.0.1`) lists 41 Koko topics and receives `/world/horus_indoor/stats` |
| Real-time factor (perception on, 10 Hz lidars) | 0.40 in power-saver → **0.65** with `powerprofilesctl set performance` |

## Host changes made

Koko (with the provided sudo password):
- `ufw allow from 10.0.0.0/24 comment 'horus ethernet link'`
- `ip route replace 239.255.0.0/16 dev enp1s0` + `ip rule add to 239.255.0.0/16 lookup main priority 100`
  (not persistent; cleared on reboot)
- `powerprofilesctl set performance` (revert: `powerprofilesctl set balanced`)
- `warp-cli tunnel ip add-range 239.255.0.0/16` (Cloudflare WARP split-tunnel exclude; its nftables output
  chain otherwise drops Gazebo's multicast discovery; revert: `warp-cli tunnel ip remove-range 239.255.0.0/16`)

Laptop:
- `warp-cli tunnel ip add-range 239.255.0.0/16` (same WARP issue)
- `ufw allow from 10.0.0.0/24 comment 'horus ethernet link'` (with the provided laptop sudo password;
  revert: `sudo ufw delete allow from 10.0.0.0/24`)

## Bugs found during bring-up (fixed)

- `remote_up.sh` port-in-use check was inverted.
- SITL only advances once a MAVLink client holds port 5760, so the ToF bridge's 15 s first-scan timeout could
  expire before MCP connected; now 60 s (`HORUS_TOF_STARTUP_TIMEOUT`), MCP connects with retries, and the
  camera is enabled (with an RTP-flow check and re-publish) only after MCP is up.
