#!/usr/bin/env bash

horus_koko_init() {
  HORUS_REMOTE="${HORUS_REMOTE:-koko}"
  if [[ ! "${HORUS_REMOTE}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf 'HORUS_REMOTE must be an SSH alias (letters, digits, dot, underscore, hyphen).\n' >&2
    return 2
  fi

  HORUS_REMOTE_DIR="${HORUS_REMOTE_DIR:-horus}"
  local remote_path_guard="/${HORUS_REMOTE_DIR}/"
  if [[ ! "${HORUS_REMOTE_DIR}" =~ ^/?[A-Za-z0-9._/-]+$ \
    || "${remote_path_guard}" == *'/../'* \
    || "${remote_path_guard}" == *'/./'* ]]; then
    printf 'HORUS_REMOTE_DIR must be a simple relative or absolute path without dot segments.\n' >&2
    return 2
  fi

  HORUS_GZ_PARTITION="${HORUS_GZ_PARTITION:-${GZ_PARTITION:-horus}}"
  if [[ ! "${HORUS_GZ_PARTITION}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf 'HORUS_GZ_PARTITION contains unsupported characters.\n' >&2
    return 2
  fi
  HORUS_MCP_LOCAL_PORT="${HORUS_MCP_LOCAL_PORT:-8000}"
  if [[ ! "${HORUS_MCP_LOCAL_PORT}" =~ ^[0-9]{1,5}$ ]] \
    || (( 10#${HORUS_MCP_LOCAL_PORT} < 1024 || 10#${HORUS_MCP_LOCAL_PORT} > 65535 )); then
    printf 'HORUS_MCP_LOCAL_PORT must be an unprivileged TCP port from 1024 through 65535.\n' >&2
    return 2
  fi
  HORUS_GZ_RELAY_LOCAL="${HORUS_GZ_RELAY_LOCAL:-}"
  HORUS_GZ_RELAY_REMOTE="${HORUS_GZ_RELAY_REMOTE:-}"
  HORUS_REMOTE_GZ_IP="${HORUS_REMOTE_GZ_IP:-}"
  HORUS_LOCAL_GZ_IP="${HORUS_LOCAL_GZ_IP:-}"
  local peer_ip
  for peer_ip in "${HORUS_GZ_RELAY_LOCAL}" "${HORUS_GZ_RELAY_REMOTE}" \
    "${HORUS_REMOTE_GZ_IP}" "${HORUS_LOCAL_GZ_IP}"; do
    if [[ -n "${peer_ip}" && ! "${peer_ip}" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
      printf 'Gazebo peer IP overrides must be IPv4 addresses.\n' >&2
      return 2
    fi
  done
  HORUS_REMOTE_SIM_IMAGE="${HORUS_REMOTE_SIM_IMAGE:-${HORUS_SIM_IMAGE:-horus-sim:local-s1s2}}"
  HORUS_REMOTE_SIM_GPU="${HORUS_REMOTE_SIM_GPU:-amd}"
  HORUS_REMOTE_SIM_CONTAINER_NAME="${HORUS_REMOTE_SIM_CONTAINER_NAME:-horus-sim}"
  HORUS_GUI_CLIENT_CONTAINER_NAME="${HORUS_GUI_CLIENT_CONTAINER_NAME:-horus-gui}"
  if [[ -z "${HORUS_REMOTE_STATE_DIR:-}" ]]; then
    HORUS_REMOTE_STATE_ROOT="${HORUS_REMOTE_STATE_ROOT:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/horus-${UID:-$(id -u)}}"
    HORUS_REMOTE_STATE_DIR="${HORUS_REMOTE_STATE_ROOT}/${HORUS_REMOTE}"
  fi
  if (( ${#HORUS_REMOTE_STATE_DIR} >= 96 )); then
    printf 'HORUS_REMOTE_STATE_DIR is too long for the SSH control socket; set a shorter path.\n' >&2
    return 2
  fi
  export HORUS_REMOTE HORUS_REMOTE_DIR HORUS_GZ_PARTITION HORUS_MCP_LOCAL_PORT
  export HORUS_GZ_RELAY_LOCAL HORUS_GZ_RELAY_REMOTE HORUS_REMOTE_GZ_IP HORUS_LOCAL_GZ_IP
  export HORUS_REMOTE_SIM_IMAGE HORUS_REMOTE_SIM_GPU HORUS_REMOTE_SIM_CONTAINER_NAME
  export HORUS_GUI_CLIENT_CONTAINER_NAME HORUS_REMOTE_STATE_DIR
  if [[ -n "${HORUS_REMOTE_STATE_ROOT:-}" ]]; then
    export HORUS_REMOTE_STATE_ROOT
  fi
  return 0
}

horus_koko_remote_script() {
  # Run the bash script on stdin on HORUS_REMOTE with ARGS as "$@", after defining project_dir
  # as the remote project directory (a relative HORUS_REMOTE_DIR is relative to the remote
  # $HOME). Usage: horus_koko_remote_script [ARG...] <<'EOF' ... EOF
  {
    printf 'project_dir=%q\n' "${HORUS_REMOTE_DIR}"
    printf '%s\n' '[[ "${project_dir}" == /* ]] || project_dir="${HOME}/${project_dir}"'
    cat
  } | ssh -T "${HORUS_REMOTE}" bash -s -- "$@"
}

horus_koko_resolve_remote_ipv4() {
  if [[ -n "${HORUS_REMOTE_IP:-}" ]]; then
    printf '%s\n' "${HORUS_REMOTE_IP}"
    return 0
  fi

  local remote_host configured_host address
  remote_host="${HORUS_REMOTE}"
  configured_host="$(ssh -G "${HORUS_REMOTE}" 2>/dev/null | while read -r key value _; do
    if [[ "${key}" == hostname ]]; then
      printf '%s\n' "${value}"
      break
    fi
  done)"
  if [[ -n "${configured_host}" && "${configured_host}" != none ]]; then
    remote_host="${configured_host}"
  fi
  if [[ "${remote_host}" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    printf '%s\n' "${remote_host}"
    return 0
  fi
  while read -r address _; do
    if [[ "${address}" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
      printf '%s\n' "${address}"
      return 0
    fi
  done < <(getent ahostsv4 "${remote_host}" 2>/dev/null || true)
  printf 'Could not resolve IPv4 for SSH host %s; set HORUS_REMOTE_IP.\n' "${HORUS_REMOTE}" >&2
  return 1
}

horus_koko_route_source_ipv4() {
  local destination="$1" route index
  local -a fields=()
  route="$(ip -4 -o route get "${destination}" 2>/dev/null)" || return 1
  read -r -a fields <<< "${route}"
  for ((index = 0; index + 1 < ${#fields[@]}; index++)); do
    if [[ "${fields[index]}" == src ]]; then
      printf '%s\n' "${fields[index + 1]}"
      return 0
    fi
  done
  return 1
}

horus_koko_resolve_ips() {
  local resolved
  resolved="$(horus_koko_resolve_remote_ipv4)" || return 1
  HORUS_REMOTE_IP="${HORUS_REMOTE_IP:-${resolved}}"
  if [[ ! "${HORUS_REMOTE_IP}" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    printf 'HORUS_REMOTE_IP must be an IPv4 address.\n' >&2
    return 2
  fi
  if [[ -z "${HORUS_LOCAL_IP:-}" ]]; then
    HORUS_LOCAL_IP="$(horus_koko_route_source_ipv4 "${HORUS_REMOTE_IP}")" || {
      printf 'Could not select local IPv4 for route to %s; set HORUS_LOCAL_IP.\n' \
        "${HORUS_REMOTE_IP}" >&2
      return 1
    }
  fi
  if [[ ! "${HORUS_LOCAL_IP}" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    printf 'HORUS_LOCAL_IP must be an IPv4 address.\n' >&2
    return 2
  fi
  HORUS_LOCAL_GZ_IP="${HORUS_LOCAL_GZ_IP:-${HORUS_LOCAL_IP}}"
  export HORUS_REMOTE_IP HORUS_LOCAL_IP HORUS_LOCAL_GZ_IP
}

horus_koko_set_local_gz_env() {
  export GZ_IP="${HORUS_LOCAL_GZ_IP:-${HORUS_LOCAL_IP}}"
  export GZ_PARTITION="${HORUS_GZ_PARTITION}"
  if [[ -n "${HORUS_GZ_RELAY_LOCAL}" ]]; then
    export GZ_RELAY="${HORUS_GZ_RELAY_LOCAL}"
  else
    unset GZ_RELAY
  fi
}
