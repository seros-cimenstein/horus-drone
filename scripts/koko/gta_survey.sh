#!/usr/bin/env bash
# Run HorusGta's height survey in Koko's GTA (gta/horus_mod/README.md "Survey"): the mod casts one
# ray straight down per 5 m cell, tile by tile, and writes the top surface's GTA z.
#
#   scripts/koko/gta_survey.sh start --box MIN_N,MIN_E,MAX_N,MAX_E [options]
#   scripts/koko/gta_survey.sh start --whole-map [options]   # box = bounds of data/maps/gta/tiles/index.json
#   scripts/koko/gta_survey.sh status
#   scripts/koko/gta_survey.sh stop
#   scripts/koko/gta_survey.sh fetch [--output NAME] DEST_DIR
#
# start options: --cell M (default 5), --output NAME (default HorusGtaSurvey, a folder in the game's
# scripts folder), --settle S, --load-timeout S, --load-attempts N (the request's settle_s,
# load_timeout_s and load_attempts). Box values are metres north/east of SITL home.
#
# start writes scripts/HorusGtaSurvey.request.json atomically; the mod starts within a second,
# never while SITL is connected (it suspends then and resumes after). start refuses while a request
# is pending or a horus-sim container runs. A survey resumes after the tiles already in its folder,
# so start again after stop (or a game restart) continues it and retries unloaded tiles.
# stop removes the request file and waits for the mod to report "stopped". status prints the mod's
# progress file and its last survey log lines. fetch copies the output folder (tiles and survey.json)
# and the progress file into DEST_DIR.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
horus_koko_init

usage() { sed -n '5,9p' "${BASH_SOURCE[0]}" | sed 's/^# //' >&2; exit 2; }

action="${1:-}"
[[ -n "${action}" ]] || usage
shift
game_rel=".steam/steam/steamapps/common/Grand Theft Auto V Enhanced"
output="HorusGtaSurvey"
name_re='^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
number_re='^-?[0-9]+(\.[0-9]+)?$'

case "${action}" in
  start)
    box="" cell=5 extra=()
    while (($#)); do
      case "$1" in
        --box) box="${2:-}"; shift 2 ;;
        --whole-map)
          index="${SCRIPT_DIR}/../../data/maps/gta/tiles/index.json"
          [[ -f "${index}" ]] || { printf 'missing %s (scripts/maps/build_gta_tiles.py)\n' "${index}" >&2; exit 1; }
          box="$(python3 -c 'import json, sys; print(",".join(repr(float(v)) for v in json.load(open(sys.argv[1]))["bounds"]))' "${index}")"
          shift ;;
        --cell) cell="${2:-}"; shift 2 ;;
        --output) output="${2:-}"; shift 2 ;;
        --settle) extra+=(settle_s "${2:-}"); shift 2 ;;
        --load-timeout) extra+=(load_timeout_s "${2:-}"); shift 2 ;;
        --load-attempts) extra+=(load_attempts "${2:-}"); shift 2 ;;
        *) usage ;;
      esac
    done
    IFS=, read -r -a corners <<<"${box}"
    ((${#corners[@]} == 4)) || { printf 'start needs --box MIN_N,MIN_E,MAX_N,MAX_E or --whole-map\n' >&2; exit 2; }
    for v in "${corners[@]}" "${cell}"; do
      [[ "${v}" =~ ${number_re} ]] || { printf 'not a number: %s\n' "${v}" >&2; exit 2; }
    done
    for ((i = 1; i < ${#extra[@]}; i += 2)); do
      [[ "${extra[i]}" =~ ${number_re} ]] || { printf 'not a number: %s\n' "${extra[i]}" >&2; exit 2; }
    done
    [[ "${output}" =~ ${name_re} ]] || { printf 'output must be one folder name (letters, digits, . _ -)\n' >&2; exit 2; }
    # The mod validates the request again (box order, cell size, ranges) and reports a refusal in
    # its progress file.
    request="{\"box\": [${corners[0]}, ${corners[1]}, ${corners[2]}, ${corners[3]}], \"cell_m\": ${cell}, \"output\": \"${output}\""
    for ((i = 0; i < ${#extra[@]}; i += 2)); do request+=", \"${extra[i]}\": ${extra[i + 1]}"; done
    request+="}"
    ;;
  status|stop) (($# == 0)) || usage ;;
  fetch)
    dest=""
    while (($#)); do
      case "$1" in
        --output) output="${2:-}"; shift 2 ;;
        -*) usage ;;
        *) [[ -z "${dest}" ]] || usage; dest="$1"; shift ;;
      esac
    done
    [[ -n "${dest}" ]] || usage
    [[ "${output}" =~ ${name_re} ]] || { printf 'output must be one folder name (letters, digits, . _ -)\n' >&2; exit 2; }
    mkdir -p -- "${dest}"
    rsync -a --protect-args "${HORUS_REMOTE}:${game_rel}/scripts/${output}/" "${dest}/"
    rsync -a --protect-args "${HORUS_REMOTE}:${game_rel}/scripts/HorusGtaSurvey.progress.json" "${dest}/progress.json"
    tiles="$(find "${dest}" -maxdepth 1 -name '*.bin' | wc -l)"
    printf 'fetched %s tiles, survey.json and progress.json into %s\n' "${tiles}" "${dest}"
    exit 0
    ;;
  *) usage ;;
esac

# ssh joins its arguments into one remote command line, so each is shell-quoted (the request is JSON).
ssh -o BatchMode=yes "${HORUS_REMOTE}" "bash -s -- $(printf '%q ' "${action}" "${HORUS_REMOTE_SIM_CONTAINER_NAME}" "${request:-}")" <<'REMOTE'
set -Eeuo pipefail
action="$1"
container="$2"
request="$3"
scripts="${HOME}/.steam/steam/steamapps/common/Grand Theft Auto V Enhanced/scripts"
request_file="${scripts}/HorusGtaSurvey.request.json"
progress="${scripts}/HorusGtaSurvey.progress.json"
log="${scripts}/HorusGta.log"
game_running() { pgrep -f "[G]TA5_Enhanced.exe" >/dev/null; }
state() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("state", "?"))' "${progress}" 2>/dev/null || echo none; }

[[ -d "${scripts}" ]] || { echo "no ${scripts}; install the mods first (scripts/koko/gta_mods.sh install)" >&2; exit 1; }
case "${action}" in
  start)
    [[ ! -e "${request_file}" ]] || { echo "a survey request is pending (${request_file}); stop it first" >&2; exit 1; }
    if docker ps --format '{{.Names}}' | grep -qx -- "${container}"; then
      echo "the ${container} container runs (SITL); stop the world before surveying" >&2
      exit 1
    fi
    game_running || echo "note: GTA is not running; the survey starts once the game runs story mode"
    printf '%s\n' "${request}" > "${request_file}.tmp"
    mv -- "${request_file}.tmp" "${request_file}"
    echo "request written: ${request}"
    for _ in $(seq 1 10); do
      sleep 1
      # The mod answered: a progress file newer than the request, or the request consumed (refused or finished).
      if ! [[ -e "${request_file}" ]] || [[ "${progress}" -nt "${request_file}" ]]; then break; fi
    done
    echo "progress state: $(state)"
    [[ -f "${progress}" ]] && cat -- "${progress}"
    ;;
  status)
    if game_running; then echo "GTA: running"; else echo "GTA: not running"; fi
    if [[ -e "${request_file}" ]]; then echo "request pending: $(cat -- "${request_file}")"; else echo "request: none"; fi
    if [[ -f "${progress}" ]]; then cat -- "${progress}"; else echo "no progress file yet"; fi
    [[ -f "${log}" ]] && { echo "== last survey lines of HorusGta.log"; grep -a 'survey:' "${log}" | tail -n 8 || true; }
    ;;
  stop)
    if [[ ! -e "${request_file}" ]]; then echo "no survey request pending"; echo "progress state: $(state)"; exit 0; fi
    rm -f -- "${request_file}"
    for _ in $(seq 1 10); do
      sleep 1
      s="$(state)"
      [[ "${s}" == running || "${s}" == suspended ]] || break
    done
    echo "progress state: $(state)"
    ;;
esac
REMOTE
