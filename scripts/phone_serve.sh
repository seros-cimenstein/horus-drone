#!/usr/bin/env bash
# Publish the HORUS control app on your tailnet for the phone app, through `tailscale serve`.
#
#   scripts/phone_serve.sh start    # https://<this machine>.<tailnet>.ts.net/ -> http://127.0.0.1:<port>
#   scripts/phone_serve.sh stop     # remove that configuration again
#   scripts/phone_serve.sh status
#   scripts/phone_serve.sh check    # exit 1 with the reason when `start` could not work
#
# The AI pilot service keeps listening on 127.0.0.1; `tailscale serve` forwards your tailnet's
# HTTPS address to it, so only your own tailnet devices can reach it. Never `tailscale funnel`
# (the public internet). The port comes from HORUS_COMMANDER_URL (default http://127.0.0.1:8765).
# With HORUS_COMMANDER_PHONE_TOKEN set (the service's phone token), `start` prints the phone
# address with the token in its fragment, plus a QR code when qrencode is installed.
# Prerequisite: HTTPS certificates enabled for the tailnet (Tailscale admin console, DNS).
set -Eeuo pipefail

usage() {
  printf 'Usage: %s start|stop|status|check\n' "${0##*/}"
  printf '%s\n' 'Publishes HORUS_COMMANDER_URL (default http://127.0.0.1:8765) on your tailnet over HTTPS for the phone app.'
}

action="${1:-status}"
case "${action}" in
  start|stop|status|check) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

COMMANDER_URL="${HORUS_COMMANDER_URL:-http://127.0.0.1:8765}"
if [[ ! "${COMMANDER_URL}" =~ ^http://127\.0\.0\.1:([1-9][0-9]{0,4})/?$ ]] \
  || (( BASH_REMATCH[1] > 65535 )); then
  printf 'HORUS_COMMANDER_URL must be http://127.0.0.1:<port> (got %s).\n' "${COMMANDER_URL}" >&2
  exit 2
fi
PORT="${BASH_REMATCH[1]}"
TARGET="http://127.0.0.1:${PORT}"

if ! command -v tailscale >/dev/null 2>&1; then
  printf '%s\n' 'tailscale is not installed; the phone app is reached over your tailnet.' >&2
  exit 1
fi

# This machine's tailnet name and whether the tailnet issues HTTPS certificates.
read -r DNS_NAME CERTS < <(tailscale status --json | python3 -c '
import json, sys
status = json.load(sys.stdin)
name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
print(name or "-", "yes" if status.get("CertDomains") else "no")')

# Which `tailscale serve` configuration exists: none, ours (443 -> TARGET only) or foreign.
serve_state() {
  tailscale serve status --json 2>/dev/null | python3 -c '
import json, sys
name, target = sys.argv[1], sys.argv[2]
raw = sys.stdin.read().strip()
config = json.loads(raw) if raw else {}
if not config.get("TCP") and not config.get("Web"):
    print("none")
else:
    ours = {"TCP": {"443": {"HTTPS": True}},
            "Web": {f"{name}:443": {"Handlers": {"/": {"Proxy": target}}}}}
    print("ours" if {k: v for k, v in config.items() if v} == ours else "foreign")' "${DNS_NAME}" "${TARGET}"
}

check_ready() {
  if [[ "${DNS_NAME}" == "-" ]]; then
    printf '%s\n' 'This machine has no tailnet name; is Tailscale up (`tailscale up`) with MagicDNS on?' >&2
    return 1
  fi
  if [[ "${CERTS}" != yes ]]; then
    printf '%s\n' 'HTTPS certificates are not enabled for your tailnet, and the phone app needs HTTPS to install.' \
      'Enable them in the Tailscale admin console: DNS, then "Enable HTTPS" (MagicDNS must be on), and run this again.' >&2
    return 1
  fi
  if [[ "$(serve_state)" == foreign ]]; then
    printf '%s\n' 'tailscale serve already has a configuration this script did not add; not touching it.' \
      'See `tailscale serve status`.' >&2
    return 1
  fi
}

phone_address() {
  local url="https://${DNS_NAME}/?view=phone"
  if [[ -n "${HORUS_COMMANDER_PHONE_TOKEN:-}" ]]; then
    local fragment
    fragment="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' \
      "${HORUS_COMMANDER_PHONE_TOKEN}")"
    url="${url}#token=${fragment}"
  fi
  printf '%s\n' "${url}"
}

case "${action}" in
  status)
    printf 'tailnet name: %s, HTTPS certificates: %s, serve configuration: %s\n' \
      "${DNS_NAME}" "${CERTS}" "$(serve_state)"
    tailscale serve status 2>/dev/null || true
    ;;
  check)
    check_ready
    ;;
  start)
    check_ready || exit 1
    if [[ "$(serve_state)" == none ]]; then
      # --bg keeps it after this script exits; `stop` removes it. A first use may ask to approve
      # Serve for the tailnet: tailscale then prints a link and this times out.
      if ! timeout 60 tailscale serve --bg --https=443 "${TARGET}"; then
        printf '%s\n' 'tailscale serve did not start; if it printed a link, approve Serve there and run this again.' >&2
        exit 1
      fi
    fi
    if ! (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
      printf 'Note: nothing listens on 127.0.0.1:%s yet; start the AI pilot service there.\n' "${PORT}"
    fi
    address="$(phone_address)"
    printf 'HORUS phone app: %s\n' "${address}"
    if [[ -z "${HORUS_COMMANDER_PHONE_TOKEN:-}" ]]; then
      printf '%s\n' 'HORUS_COMMANDER_PHONE_TOKEN is not set here: add #token=<the service phone token> to the address.'
    elif command -v qrencode >/dev/null 2>&1; then
      qrencode -t ANSIUTF8 -- "${address}"
    fi
    printf '%s\n' 'Open it on the phone (signed in to your tailnet), then "Add to home screen".'
    ;;
  stop)
    case "$(serve_state)" in
      none) printf '%s\n' 'No tailscale serve configuration; nothing to stop.' ;;
      foreign)
        printf '%s\n' 'tailscale serve has a configuration this script did not add; not touching it.' >&2
        exit 1
        ;;
      ours)
        tailscale serve --https=443 off
        printf 'Stopped publishing %s on %s.\n' "${TARGET}" "${DNS_NAME}"
        ;;
    esac
    ;;
esac
