#!/usr/bin/env bash
# Shared procedural-world generation for `sim/run.sh start` and `scripts/demo.sh`.
# Sourced, not executed: callers get generate_procedural_world().

_procedural_sim_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
_procedural_project_dir="$(cd -- "${_procedural_sim_dir}/.." && pwd)"

procedural_world_stem() {
  printf 'horus_%s_p%s\n' "$1" "$2"
}

procedural_world_layout_stratum() {
  # Read dressing.stratum from a generated layout JSON; prints nothing when unavailable.
  local layout="$1" python_bin="$2"
  [[ -f "${layout}" ]] || return 0
  command -v "${python_bin}" >/dev/null 2>&1 || return 0
  "${python_bin}" -c \
    'import json,sys; print((json.load(open(sys.argv[1])).get("dressing") or {}).get("stratum") or "")' \
    "${layout}" 2>/dev/null || true
}

generate_procedural_world() {
  # Generate (or reuse) a randomized world and print its container SDF path on stdout.
  # Usage: generate_procedural_world <forest|urban> <seed> [<baseline|chaotic>]
  # Progress, the reproduce command and the config to use go to stderr.
  local kind="$1" seed="$2" stratum="${3:-}"
  local sim_dir="${_procedural_sim_dir}" project_dir="${_procedural_project_dir}"
  local stem
  stem="$(procedural_world_stem "${kind}" "${seed}")"
  local world_dir="${sim_dir}/generated/${stem}"
  local sdf="${world_dir}/${stem}.sdf"
  local scenario="${world_dir}/${stem}.yaml"
  local layout="${world_dir}/${stem}.layout.json"
  local worldgen_python="${HORUS_WORLDGEN_PYTHON:-python3}"
  local cached_stratum="" stale=0
  cached_stratum="$(procedural_world_layout_stratum "${layout}" "${worldgen_python}")"
  if [[ ! -f "${sdf}" || ! -f "${scenario}" || ! -f "${layout}" ]]; then
    stale=1
  elif [[ -n "$(find "${sim_dir}/worldgen" -type f -newer "${sdf}" -print -quit)" ]]; then
    stale=1
  elif [[ -n "${stratum}" && "${cached_stratum}" != "${stratum}" ]]; then
    # A forced stratum must not silently reuse a differently-dressed cached world.
    printf 'Cached %s is %s but --stratum %s was requested; regenerating.\n' \
      "${stem}" "${cached_stratum:-unknown}" "${stratum}" >&2
    stale=1
  fi
  printf 'Procedural world %s: seed %s, kind %s\n' "${stem}" "${seed}" "${kind}" >&2
  if (( stale == 1 )); then
    if ! command -v "${worldgen_python}" >/dev/null 2>&1; then
      printf 'World generation needs %s on this host (or set HORUS_WORLDGEN_PYTHON).\n' \
        "${worldgen_python}" >&2
      return 1
    fi
    printf '  Generating %s into sim/generated (about 10-40 s; deterministic for this seed).\n' \
      "${stem}" >&2
    local worldgen_args=(--procedural --seed "${seed}" --kind "${kind}" --out sim/generated)
    if [[ -n "${stratum}" ]]; then
      worldgen_args+=(--stratum "${stratum}")
    fi
    if ! ( cd -- "${project_dir}" && "${worldgen_python}" -m sim.worldgen "${worldgen_args[@]}" ) >&2; then
      printf 'World generation failed for %s.\n' "${stem}" >&2
      return 1
    fi
    cached_stratum="$(procedural_world_layout_stratum "${layout}" "${worldgen_python}")"
  else
    printf '  Reusing sim/generated/%s (generator unchanged; delete the directory or touch\n' \
      "${stem}" >&2
    printf '  a file under sim/worldgen to regenerate).\n' >&2
  fi
  if [[ ! -f "${sdf}" ]]; then
    printf 'World generation did not produce %s.\n' "${sdf}" >&2
    return 1
  fi
  printf '  Reproduce: ./sim/run.sh start --procedural --seed %s --kind %s%s\n' \
    "${seed}" "${kind}" "${stratum:+ --stratum ${stratum}}" >&2
  if [[ -n "${cached_stratum}" ]]; then
    printf '  Stratum:   %s%s\n' "${cached_stratum}" \
      "$([[ -n "${stratum}" ]] && printf ' (forced)' || printf ' (sampled from seed)')" >&2
  fi
  printf '  SDF:       /horus/sim/generated/%s/%s.sdf\n' "${stem}" "${stem}" >&2
  printf '  Config:    config/horus_procedural_%s.yaml (HORUS_CONFIG=/horus/config/horus_procedural_%s.yaml in the container)\n' \
    "${kind}" "${kind}" >&2
  printf '/horus/sim/generated/%s/%s.sdf\n' "${stem}" "${stem}"
}
