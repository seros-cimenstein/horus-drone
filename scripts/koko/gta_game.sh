#!/usr/bin/env bash
# Start, stop or inspect GTA V Enhanced on Koko with the HORUS mod stack (scripts/koko/gta_mods.sh).
#
#   scripts/koko/gta_game.sh start    # launch and enter story mode; returns once scripts run
#   scripts/koko/gta_game.sh stop     # close GTA and every Rockstar/Social Club process
#   scripts/koko/gta_game.sh status
#
# start refuses when Koko has less than GTA_MIN_AVAILABLE_MIB (default 9728: HORUS flying in
# GTA can take available memory down by about 8.5 GiB, plus 1 GiB of headroom; gta/hook/README.md
# "Memory"; the refusal lists the largest processes), while Gazebo runs (a HORUS
# simulation), while the running Steam client is not signed in (it would queue the launch and run it
# later, unguarded), with no display output enabled, or while a screen lock covers the desktop.
# On GTA's landing page it presses Enter only after a pixel check shows the STORY tab selected and
# the ONLINE and GTA+ tabs not: Enter on the ONLINE tab would start GTA Online. It sends no key
# while the hook's watchdog reports the game thread stalled, and presses Enter again (up to 3
# times) only if STORY is still highlighted 30 s after the last press.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
horus_koko_init

action="${1:-status}"
case "${action}" in
  start|stop|status) ;;
  *) printf 'usage: %s start|stop|status\n' "$0" >&2; exit 2 ;;
esac

horus_koko_remote_script "${action}" "${GTA_MIN_AVAILABLE_MIB:-9728}" <<'REMOTE'
set -Eeuo pipefail
action="$1"
min_available_mib="$2"
game="${HOME}/.steam/steam/steamapps/common/Grand Theft Auto V Enhanced"
hook_log="${game}/ScriptHookV.log"
export DISPLAY=:0 XAUTHORITY="${HOME}/.Xauthority"
# Process command-line patterns; the [x] keeps pgrep/pkill from matching this script itself.
patterns=("[G]TA5_Enhanced" "[P]layGTAV" "[S]ocialClubHelper" "[R]ockstarService" "[R]ockstarErrorHandler"
  'Rockstar Games\\[L]auncher')

running() { pgrep -f "[G]TA5_Enhanced.exe" >/dev/null; }
available_mib() { free -m | awk '/^Mem:/ {print $7}'; }

# Which landing-page tab is selected: prints story, online, gtaplus or none. While story mode loads
# GTA replaces its window, so the window found by name can be gone a moment later (X BadWindow
# from getwindowgeometry, then an empty screen grab); any failure here reads as none, which only
# makes the caller wait.
landing_tab() {
  local window geometry
  window="$(xdotool search --name '^Grand Theft Auto V$' 2>/dev/null | head -1)" || true
  [[ -n "${window}" ]] || { echo none; return; }
  geometry="$(xdotool getwindowgeometry --shell "${window}" 2>/dev/null)" || { echo none; return; }
  xwd -root -silent | python3 -c '
import struct, sys
geo = dict(line.split("=") for line in sys.argv[1].split())
wx, wy, ww, wh = (int(geo[k]) for k in ("X", "Y", "WIDTH", "HEIGHT"))
d = sys.stdin.buffer.read()
f = struct.unpack(">25I", d[:100])
bpp, bpl, off = f[11] // 8, f[12], f[0] + f[19] * 12
def bright(fx, fy):  # tab background at a fraction of the game window
    x, y = wx + int(fx * ww), wy + int(fy * wh)
    o = off + y * bpl + x * bpp
    return min(d[o], d[o + 1], d[o + 2]) > 200, max(d[o], d[o + 1], d[o + 2]) < 100
online, gtaplus, story = (bright(fx, 0.0667) for fx in (0.0547, 0.1474, 0.1833))
if story[0] and online[1] and gtaplus[1]: print("story")
elif online[0] and gtaplus[1] and story[1]: print("online")
elif gtaplus[0] and online[1] and story[1]: print("gtaplus")
else: print("none")' "${geometry}" 2>/dev/null || echo none
}

key() {  # GTA ignores xdotool's instant key taps; hold each key briefly
  local window
  window="$(xdotool search --name '^Grand Theft Auto V$' | head -1)"
  xdotool windowactivate --sync "${window}"
  sleep 0.3
  xdotool keydown "$1"; sleep 0.15; xdotool keyup "$1"; sleep 1.2
}

wait_for_log() {  # wait_for_log PATTERN SECONDS
  local deadline=$((SECONDS + $2))
  while (( SECONDS < deadline )); do
    grep -q -- "$1" "${hook_log}" 2>/dev/null && return 0
    running || { echo "GTA exited while waiting for: $1" >&2; return 1; }
    sleep 2
  done
  echo "timed out after $2 s waiting for: $1" >&2
  return 1
}

# True while the hook's watchdog reports the game thread stalled (a "no script tick" warning with
# no "resumed" line after it); the landing page then shows a frozen frame and drops input.
game_thread_stalled() {
  local last_stall last_resume
  last_stall="$(grep -n 'no script tick for' "${hook_log}" 2>/dev/null | tail -1 | cut -d: -f1)"
  [[ -n "${last_stall}" ]] || return 1
  last_resume="$(grep -n 'script ticks resumed after' "${hook_log}" 2>/dev/null | tail -1 | cut -d: -f1)"
  [[ -z "${last_resume}" || "${last_resume}" -lt "${last_stall}" ]]
}

stamp() { printf '%s %s\n' "$(date +%T)" "$*"; }

# Whether the running Steam client is signed in now. Steam writes its connection state into every
# connection_log.txt line ("[time] [Logged On, 4, 7] [U:1:…] …"); the state on the newest such line
# counts only if it was written after the running client (steam.pid) started, so a sign-in from an
# earlier Steam run does not count. Anything else (no client, Logged Off, Logging On, Connecting,
# no state line since the client started) prints the reason and fails. This must hold before the
# launch URL is sent: while Steam waits at its account picker, `steam steam://…` writes the URL into
# ~/.steam/steam.pipe and exits, and the client runs it once someone signs in, outside these checks
# (possibly minutes later, with memory below the guard by then). Nothing on this side can
# withdraw a URL the client has queued.
steam_signed_in() {
  python3 - "${HOME}/.steam/steam.pid" "${HOME}/.steam/steam/logs/connection_log.txt" <<'PY'
import os, re, sys, time
pid_path, log_path = sys.argv[1], sys.argv[2]
def fail(reason):
    print(f"Steam is not signed in ({reason}); not starting GTA. Select the account in Steam first.")
    sys.exit(1)
try:
    pid = int(open(pid_path).read().split()[0])
    if open(f"/proc/{pid}/comm").read().strip() != "steam":
        raise OSError
    stat = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
except (OSError, ValueError, IndexError):
    fail("no running Steam client")
boot = float(next(l.split()[1] for l in open("/proc/stat") if l.startswith("btime")))
started = boot + int(stat[19]) / os.sysconf("SC_CLK_TCK")
state = None
line_re = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[([A-Za-z ]+), -?\d+, -?\d+\]")
try:
    for line in open(log_path, errors="replace"):
        m = line_re.match(line)
        if m:
            state = m.groups()
except OSError:
    fail("no connection_log.txt")
if state is None:
    fail("no connection state logged")
# Log times are whole local seconds; allow the second the client started in.
if time.mktime(time.strptime(state[0], "%Y-%m-%d %H:%M:%S")) < int(started):
    fail(f"no connection state since the client started; last {state[1]!r} at {state[0]}")
if state[1] != "Logged On":
    fail(f"connection state {state[1]!r} at {state[0]}")
PY
}

stop_game() {
  local pattern
  for pattern in "${patterns[@]}"; do pkill -f "${pattern}" 2>/dev/null || true; done
  for _ in $(seq 1 15); do
    remaining=0
    for pattern in "${patterns[@]}"; do pgrep -f "${pattern}" >/dev/null && remaining=1; done
    (( remaining )) || return 0
    sleep 2
  done
  for pattern in "${patterns[@]}"; do pkill -9 -f "${pattern}" 2>/dev/null || true; done
  sleep 2
}

case "${action}" in
  status)
    if running; then echo "GTA: running"; else echo "GTA: not running"; fi
    echo "available memory: $(available_mib) MiB"
    for pattern in "${patterns[@]}"; do pgrep -af "${pattern}" | cut -c1-120 || true; done
    [[ -f "${hook_log}" ]] && { echo "== ScriptHookV.log"; tail -n 8 "${hook_log}"; }
    ;;
  stop)
    stop_game
    if pgrep -f "[G]TA5_Enhanced|[P]layGTAV|[S]ocialClub|[R]ockstar" >/dev/null; then
      echo "some GTA/Rockstar processes survived:" >&2
      pgrep -af "[G]TA5_Enhanced|[P]layGTAV|[S]ocialClub|[R]ockstar" >&2
      exit 1
    fi
    echo "GTA stopped; available memory: $(available_mib) MiB"
    ;;
  start)
    if running; then echo "GTA is already running; stop it first" >&2; exit 1; fi
    if pgrep -f "[g]z sim" >/dev/null; then echo "Gazebo is running (a HORUS simulation); not starting GTA" >&2; exit 1; fi
    steam_signed_in >&2 || exit 1
    # With every display output off (DPMS off, kernel connector disabled), the GTA swap chain only
    # presents once a second, so scripts and frames crawl at 1 Hz.
    if ! grep -qx enabled /sys/class/drm/card*-*/enabled 2>/dev/null; then
      echo "no display output is enabled on Koko (monitor off); GTA would run at 1 frame/s. Turn the screen on first." >&2
      exit 1
    fi
    # A screen lock covers GTA's window: the STORY-tab check and the Enter press need it focused,
    # and the Rockstar launcher can stall at start-up under the lock until the 180 s timeout.
    # dark-screenlock is the lock in use on Koko; Cinnamon's own screensaver is the other.
    if xdotool search --onlyvisible --name '^dark-screenlock$' >/dev/null 2>&1 \
      || XDG_RUNTIME_DIR="/run/user/$(id -u)" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus" \
        gdbus call --session --dest org.cinnamon.ScreenSaver --object-path /org/cinnamon/ScreenSaver \
        --method org.cinnamon.ScreenSaver.GetActive 2>/dev/null | grep -q true; then
      echo "Koko's screen is locked; GTA's window cannot be focused for the STORY tab. Unlock it first." >&2
      exit 1
    fi
    available="$(available_mib)"
    if (( available < min_available_mib )); then
      echo "only ${available} MiB available, need ${min_available_mib} MiB; not starting GTA. Largest processes (RSS MiB):" >&2
      # Steam's web helpers grow over a long session (hundreds of MiB per launch); restarting
      # Steam frees them, but it then asks which account to sign in.
      { ps -eo rss=,comm= --sort=-rss | head -6 | awk '{printf "  %6d %s\n", $1 / 1024, $2}' >&2; } || true
      exit 1
    fi
    [[ -f "${game}/ScriptHookV.dll" ]] || { echo "mods not installed; run scripts/koko/gta_mods.sh install" >&2; exit 1; }
    rm -f -- "${hook_log}"  # the hook recreates it; a fresh log keeps the waits below unambiguous
    nohup steam steam://rungameid/3240220 >/dev/null 2>&1 &
    echo "launching GTA (${available} MiB available)"
    for _ in $(seq 1 90); do running && break; sleep 2; done
    if ! running; then
      # Close whatever the launch did start (Rockstar launcher, Social Club) rather than leave it.
      stop_game
      echo "GTA5_Enhanced.exe did not start within 180 s; closed any Rockstar process it started" >&2
      exit 1
    fi
    wait_for_log "INIT: success" 240
    wait_for_log "keyboard: hooked" 240
    # Enter on STORY, then confirm that loading began: the landing page goes away, or the scripts
    # start. GTA sometimes drops the first Enter (STORY stays highlighted; a second Enter loads
    # story mode), so Enter is pressed again only after the page is
    # freshly seen still on STORY, LEAVE_CHECK_S after the last press, with the game thread ticking.
    deadline=$((SECONDS + 180))
    presses=0
    last_press=0
    leave_check_s=30
    left=0
    while (( SECONDS < deadline )); do
      running || { echo "GTA exited on the landing page" >&2; exit 1; }
      grep -q "host script thread main_persistent" "${hook_log}" 2>/dev/null && break
      tab="$(landing_tab)"
      # A stalled game thread shows a frozen frame and drops input: wait it out before any key.
      if game_thread_stalled; then sleep 2; continue; fi
      if (( presses > 0 )); then
        # After Enter only STORY may be pressed again, and only while the page has not left it;
        # any other reading (loading screen, or a tab misread during the transition) just waits.
        if (( left )) || [[ "${tab}" != story ]]; then
          (( left )) || stamp "landing page left after Enter #${presses}; loading story mode"
          left=1
          sleep 2
          continue
        fi
      fi
      case "${tab}" in
        story)
          if (( presses == 0 || SECONDS - last_press >= leave_check_s )); then
            if (( presses >= 3 )); then
              echo "STORY still highlighted after 3 Enter presses; giving up" >&2
              exit 1
            fi
            key Return
            presses=$((presses + 1))
            last_press=${SECONDS}
            if (( presses == 1 )); then stamp "STORY selected, Enter pressed"
            else stamp "STORY still highlighted ${leave_check_s} s after the last Enter; Enter pressed again (#${presses})"; fi
          else
            sleep 2
          fi
          ;;
        online|gtaplus) key e ;;
        *) sleep 2 ;;
      esac
    done
    (( presses > 0 )) || grep -q "host script thread main_persistent" "${hook_log}" 2>/dev/null || {
      echo "landing page with the STORY tab not found within 180 s; Enter not pressed" >&2; exit 1; }
    wait_for_log "host script thread main_persistent" 300
    stamp "story mode: scripts running"
    tail -n 4 "${hook_log}"
    ;;
esac
REMOTE
