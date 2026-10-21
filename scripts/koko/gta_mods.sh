#!/usr/bin/env bash
# Install, remove or inspect the GTA V Enhanced drone-mod stack on Koko:
# HORUS Script Hook (gta/hook, our Script Hook V implementation, built as ScriptHookV.dll) +
# ScriptHookVDotNet Enhanced + Los Santos FPV + HorusGta.dll (HORUS drone camera, gta/horus_mod) +
# horus_capture.asi (renderer camera readback, gta/capture). The ASI loader, args.txt and the Native
# Trainer sample come from the Script Hook V archive; Alexander Blade's ScriptHookV.dll is not used.
# Third-party archives are staged on Koko in ~/horus-gta/staging (see SHV_ZIP etc. below); install
# first copies the locally built gta/build/{horus_capture.asi,ScriptHookV.dll} (and, in full mode,
# gta/horus_mod/build/HorusGta.{dll,ini}) there.
#
#   scripts/koko/gta_mods.sh status
#   scripts/koko/gta_mods.sh install
#   scripts/koko/gta_mods.sh install --capture-only   # ASI loader + horus_capture.asi only
#   scripts/koko/gta_mods.sh install --api-probe      # full install + horus_api_probe.asi (hook API
#                                                      # checks in game; the next plain install removes it)
#   scripts/koko/gta_mods.sh uninstall
#
# install also sets dinput8/xinput1_4 to native,builtin in the game's Proton prefix registry, so Wine
# loads the ASI loader without any Steam launch options. Full install first checks (read-only) that
# the prefix has .NET 4.8 and a ProfileList entry for its user SID, failing with the setup commands
# if not, and sets GTA's PauseOnFocusLoss to 0 (settings.xml backed up to ~/horus-gta first).
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
horus_koko_init

action="${1:-status}"
mode="${2:-}"
case "${action}:${mode}" in
  status:|uninstall:|install:|install:--capture-only|install:--api-probe) ;;
  *) printf 'usage: %s status|install [--capture-only|--api-probe]|uninstall\n' "$0" >&2; exit 2 ;;
esac

if [[ "${action}" == install ]]; then
  native_dir="${SCRIPT_DIR}/../../gta/build"
  for f in horus_capture.asi ScriptHookV.dll; do
    [[ -f "${native_dir}/${f}" ]] || { printf 'missing %s; run gta/build.sh\n' "${native_dir}/${f}" >&2; exit 1; }
  done
  rsync -a "${native_dir}/horus_capture.asi" "${HORUS_REMOTE}:horus-gta/staging/horus_capture.asi"
  rsync -a "${native_dir}/ScriptHookV.dll" "${HORUS_REMOTE}:horus-gta/staging/horus_ScriptHookV.dll"
  if [[ "${mode}" != --capture-only ]]; then
    mod_dir="${SCRIPT_DIR}/../../gta/horus_mod/build"
    for f in HorusGta.dll HorusGta.ini; do
      [[ -f "${mod_dir}/${f}" ]] || { printf 'missing %s; run gta/horus_mod/build.sh\n' "${mod_dir}/${f}" >&2; exit 1; }
    done
    rsync -a "${mod_dir}/HorusGta.dll" "${mod_dir}/HorusGta.ini" "${HORUS_REMOTE}:horus-gta/staging/"
  fi
  if [[ "${mode}" == --api-probe ]]; then
    [[ -f "${native_dir}/horus_api_probe.asi" ]] || { printf 'missing %s; run gta/build.sh\n' "${native_dir}/horus_api_probe.asi" >&2; exit 1; }
    rsync -a "${native_dir}/horus_api_probe.asi" "${HORUS_REMOTE}:horus-gta/staging/horus_api_probe.asi"
  fi
fi

ssh -o BatchMode=yes "${HORUS_REMOTE}" bash -s -- "${action}" "${mode}" <<'REMOTE'
set -Eeuo pipefail
action="$1"
mode="${2:-}"
game="${HOME}/.steam/steam/steamapps/common/Grand Theft Auto V Enhanced"
staging="${HOME}/horus-gta/staging"
manifest="${game}/.horus-mods.manifest"
SHV_ZIP="${staging}/ScriptHookV_3889.0_1158.13.zip"  # ASI loader, args.txt, Native Trainer only
HORUS_HOOK_DLL="${staging}/horus_ScriptHookV.dll"
# Legacy -> Enhanced native hash map from YimMenuV2 (GPL-2.0), pinned; read by the hook at startup.
CROSSMAP="${staging}/ScriptHookV.crossmap.txt"
CROSSMAP_URL="https://raw.githubusercontent.com/YimMenu/YimMenuV2/c3604ca8195873c6b89008be5fefad517e554b18/src/game/gta/invoker/crossmap.txt"
CROSSMAP_SHA256="66a5ca1e1ed092b5528073bd41705e4737f43535dfecb87236c1f85aa8d4968f"
SHVDN_ZIP="${staging}/ScriptHookVDotNetEnhanced-v1.1.0.6.zip"
FPV_ZIP="${staging}/LosSantosFpv-1.0.1.zip"
CAPTURE_ASI="${staging}/horus_capture.asi"
HORUS_MOD_DLL="${staging}/HorusGta.dll"
HORUS_MOD_INI="${staging}/HorusGta.ini"
API_PROBE_ASI="${staging}/horus_api_probe.asi"
pfx="${HOME}/.steam/steam/steamapps/compatdata/3240220/pfx"
settings="${pfx}/drive_c/users/steamuser/Documents/Rockstar Games/GTAV Enhanced/settings.xml"

# Read-only checks of the one-time prefix setup ScriptHookVDotNet needs (gta/hook/README.md
# "Proton prefix setup"): .NET Framework 4.8 and a ProfileList entry for the prefix user's SID.
# Parses the registry files directly so no wine process touches the prefix.
check_prefix() {
  python3 - "${pfx}" <<'PY'
import re, sys
pfx = sys.argv[1]

def section(reg, key):
    text = open(f"{pfx}/{reg}", encoding="utf-8", errors="replace").read()
    m = re.search(r"^\[" + re.escape(key.replace("\\", "\\\\")) + r"\][^\n]*\n(.*?)(?:\n\n|\Z)", text, re.M | re.S)
    return m.group(1) if m else None

ok = True
wine = '"$HOME/.steam/steam/steamapps/common/Proton - Experimental/files/bin/wine"'
ndp = section("system.reg", r"Software\Microsoft\NET Framework Setup\NDP\v4\Full") or ""
m = re.search(r'^"Release"=dword:([0-9a-f]{8})', ndp, re.M)
release = int(m.group(1), 16) if m else None
if release is not None and release >= 528040:
    print(f"check .NET 4.8: ok (NDP\\v4\\Full Release {release} >= 528040)")
else:
    ok = False
    print(f"check .NET 4.8: MISSING (NDP\\v4\\Full Release {release}, need >= 528040). Install it once, game closed:\n"
          f"  WINEPREFIX='{pfx}' WINE={wine} winetricks -q dotnet48")
user = open(f"{pfx}/user.reg", encoding="utf-8", errors="replace").read()
m = re.search(r"^;; All keys relative to REGISTRY\\\\User\\\\(S-[0-9-]+)", user, re.M)
sid = m.group(1) if m else None
prof = section("system.reg", "Software\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\" + sid) if sid else None
if prof and re.search(r'^"ProfileImagePath"=', prof, re.M):
    print(f"check ProfileList: ok ({sid})")
else:
    ok = False
    print(f"check ProfileList: MISSING for user SID {sid} (user.reg). Add it once, game closed:\n"
          f"  WINEPREFIX='{pfx}' {wine} reg add 'HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\{sid}'"
          " /v ProfileImagePath /t REG_SZ /d 'C:\\users\\steamuser' /f")
sys.exit(0 if ok else 1)
PY
}

# GTA pauses script threads (and with them HorusGta) while its window is unfocused unless
# PauseOnFocusLoss is 0. Backs settings.xml up to ~/horus-gta with a timestamp before changing it.
set_pause_on_focus_loss() {
  [[ -f "${settings}" ]] || { echo "check PauseOnFocusLoss: MISSING ${settings}; start GTA once to create it" >&2; return 1; }
  local value backup
  value="$(python3 - "${settings}" <<'PY'
import re, sys
m = re.search(rb'<PauseOnFocusLoss value="([^"]*)" />', open(sys.argv[1], "rb").read())
print(m.group(1).decode() if m else "")
PY
)"
  if [[ -z "${value}" ]]; then
    echo "check PauseOnFocusLoss: no <PauseOnFocusLoss value=\"…\" /> in ${settings}; set it to 0 by hand" >&2
    return 1
  fi
  if [[ "${value}" == 0 ]]; then echo "check PauseOnFocusLoss: ok (0)"; return 0; fi
  backup="${HOME}/horus-gta/settings.xml.$(date +%Y%m%dT%H%M%S).bak"
  cp -p -- "${settings}" "${backup}"
  python3 - "${settings}" <<'PY'
import re, sys
data = open(sys.argv[1], "rb").read()
open(sys.argv[1], "wb").write(re.sub(rb'<PauseOnFocusLoss value="[^"]*" />', b'<PauseOnFocusLoss value="0" />', data, count=1))
PY
  echo "check PauseOnFocusLoss: was ${value}, set to 0 (backup ${backup})"
}

game_version() {
  python3 - "${game}/GTA5_Enhanced.exe" <<'PY'
import re, sys
data = open(sys.argv[1], "rb").read()
key = "FileVersion".encode("utf-16le")
i = data.find(key)
m = re.match(rb"(?:\x00)*((?:[0-9.]\x00)+)", data[i + len(key):])
print(m.group(1).decode("utf-16le") if i >= 0 and m else "unknown")
PY
}

uninstall() {
  [[ -f "${manifest}" ]] || { echo "No HORUS mod manifest; nothing to remove."; return 0; }
  while IFS= read -r rel; do
    rm -f -- "${game}/${rel}"
  done < "${manifest}"
  rmdir -- "${game}/scripts" 2>/dev/null || true
  rm -f -- "${manifest}"
  echo "Removed HORUS mod files."
}

case "${action}" in
  status)
    echo "game version: $(game_version)"
    if [[ -f "${manifest}" ]]; then echo "installed files:"; sed 's/^/  /' "${manifest}"; else echo "mods: not installed"; fi
    for log in ScriptHookV.log ScriptHookVDotNet.log scripts/LosSantosFpv.log scripts/HorusGta.log horus_capture.log; do
      [[ -f "${game}/${log}" ]] && { echo "== ${log}"; tail -n 15 "${game}/${log}"; }
    done
    ;;
  install)
    tmp="$(mktemp -d)"; trap 'rm -rf -- "${tmp}"' EXIT
    stage="${tmp}/stage"
    mkdir -p -- "${stage}"
    # Generated at runtime by the mods; removed on uninstall too.
    runtime_files=(horus_capture.log asiloader.log)
    if [[ "${mode}" == --capture-only ]]; then
      [[ -f "${SHV_ZIP}" ]] || { echo "missing ${SHV_ZIP}" >&2; exit 1; }
      # args.txt carries -nobattleye: the anticheat must not see an injected plugin.
      unzip -q -j "${SHV_ZIP}" bin/args.txt bin/dinput8.dll bin/xinput1_4.dll -d "${stage}"
    else
      check_prefix || { echo "Prefix setup incomplete; nothing installed." >&2; exit 1; }
      set_pause_on_focus_loss
      for z in "${SHV_ZIP}" "${HORUS_HOOK_DLL}" "${SHVDN_ZIP}" "${FPV_ZIP}" "${HORUS_MOD_DLL}" "${HORUS_MOD_INI}"; do [[ -f "${z}" ]] || { echo "missing ${z}" >&2; exit 1; }; done
      unzip -q -j "${SHV_ZIP}" bin/args.txt bin/dinput8.dll bin/xinput1_4.dll bin/NativeTrainer.asi -d "${stage}"
      cp -- "${HORUS_HOOK_DLL}" "${stage}/ScriptHookV.dll"
      if [[ ! -f "${CROSSMAP}" ]]; then
        curl -fsSL -o "${tmp}/crossmap.txt" "${CROSSMAP_URL}"
        echo "${CROSSMAP_SHA256}  ${tmp}/crossmap.txt" | sha256sum -c --quiet
        mv -- "${tmp}/crossmap.txt" "${CROSSMAP}"
      fi
      echo "${CROSSMAP_SHA256}  ${CROSSMAP}" | sha256sum -c --quiet \
        || { echo "${CROSSMAP} does not match the pinned sha256; delete it so install downloads it again" >&2; exit 1; }
      cp -- "${CROSSMAP}" "${stage}/ScriptHookV.crossmap.txt"
      unzip -q "${SHVDN_ZIP}" ScriptHookVDotNet.asi ScriptHookVDotNet.ini ScriptHookVDotNet2.dll \
        ScriptHookVDotNet3.dll MinHook.x64.dll -d "${stage}"
      unzip -q "${FPV_ZIP}" 'scripts/*' -d "${stage}"
      cp -- "${HORUS_MOD_DLL}" "${HORUS_MOD_INI}" "${stage}/scripts/"
      runtime_files+=(ScriptHookV.log ScriptHookVDotNet.log scripts/LosSantosFpv.json scripts/LosSantosFpv.log scripts/HorusGta.log
        scripts/HorusGtaTrace.csv)
      if [[ "${mode}" == --api-probe ]]; then
        cp -- "${API_PROBE_ASI}" "${stage}/horus_api_probe.asi"
        runtime_files+=(horus_api_probe.log horus_api_probe.png horus_api_probe_draw.png)
      fi
    fi
    cp -- "${CAPTURE_ASI}" "${stage}/horus_capture.asi"
    uninstall
    : > "${manifest}"
    while IFS= read -r rel; do
      [[ -e "${game}/${rel}" ]] && { echo "refusing to overwrite foreign file ${rel}" >&2; exit 1; }
      mkdir -p -- "$(dirname -- "${game}/${rel}")"
      cp -- "${stage}/${rel}" "${game}/${rel}"
      printf '%s\n' "${rel}" >> "${manifest}"
    done < <(cd -- "${stage}" && find . -type f -printf '%P\n' | sort)
    printf '%s\n' "${runtime_files[@]}" >> "${manifest}"
    proton="${HOME}/.steam/steam/steamapps/common/Proton - Experimental/files/bin"
    for dll in dinput8 xinput1_4; do
      WINEPREFIX="${HOME}/.steam/steam/steamapps/compatdata/3240220/pfx" WINEDEBUG=-all "${proton}/wine" \
        reg add 'HKCU\Software\Wine\DllOverrides' /v "${dll}" /t REG_SZ /d native,builtin /f >/dev/null
    done
    echo "Installed into ${game}:"; sed 's/^/  /' "${manifest}"
    ;;
  uninstall) uninstall ;;
esac
REMOTE
