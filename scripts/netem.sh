#!/usr/bin/env bash
# Run where commander↔MCP traffic passes (a dedicated container/veth interface); never apply this to MAVLink.
# Usage: sudo scripts/netem.sh up <interface> [delay] [loss]
#        sudo scripts/netem.sh down <interface>
# Example: sudo scripts/netem.sh up veth-commander 500ms 5%

set -euo pipefail

usage() {
    printf 'Usage: %s up <interface> [delay] [loss]\n       %s down <interface>\n' "$0" "$0" >&2
    printf 'Defaults for up: delay=500ms, loss=0%%. Both directions receive the same netem settings.\n' >&2
}

if (( $# < 2 )); then
    usage
    exit 2
fi

ACTION=$1
IFACE=$2
case "$ACTION" in
    up)
        if (( $# > 4 )); then
            usage
            exit 2
        fi
        DELAY=${3:-500ms}
        LOSS=${4:-0%}
        ;;
    down)
        if (( $# != 2 )); then
            usage
            exit 2
        fi
        ;;
    *)
        usage
        exit 2
        ;;
esac

for tool in ip tc; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'netem: required command %q was not found\n' "$tool" >&2
        exit 1
    fi
done

if ! ip link show dev "$IFACE" >/dev/null 2>&1; then
    printf 'netem: interface %q does not exist in this network namespace\n' "$IFACE" >&2
    exit 1
fi
IFINDEX_PATH="/sys/class/net/${IFACE}/ifindex"
if [[ ! -r $IFINDEX_PATH ]]; then
    printf 'netem: cannot read interface index for %q\n' "$IFACE" >&2
    exit 1
fi
read -r IFINDEX < "$IFINDEX_PATH"
IFB="hifb${IFINDEX}"
if (( ${#IFB} > 15 )); then
    printf 'netem: generated IFB name %q exceeds Linux interface-name length\n' "$IFB" >&2
    exit 1
fi

if [[ $ACTION == down ]]; then
    tc qdisc del dev "$IFACE" root 2>/dev/null || true
    tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
    tc qdisc del dev "$IFB" root 2>/dev/null || true
    if ip link show dev "$IFB" >/dev/null 2>&1; then
        ip link set dev "$IFB" down 2>/dev/null || true
        ip link delete dev "$IFB" type ifb 2>/dev/null || true
    fi
    printf 'Removed netem shaping from ingress and egress on %s.\n' "$IFACE"
    exit 0
fi

if ! ip link show dev "$IFB" >/dev/null 2>&1; then
    ip link add dev "$IFB" type ifb
fi
ip link set dev "$IFB" up

# Redirect ingress packets through the IFB so netem can delay/drop that direction too.
tc qdisc replace dev "$IFACE" handle ffff: ingress
tc filter replace dev "$IFACE" parent ffff: protocol all u32 match u32 0 0 \
    action mirred egress redirect dev "$IFB"
tc qdisc replace dev "$IFB" root netem delay "$DELAY" loss "$LOSS"
# The interface's root qdisc shapes commander egress traffic.
tc qdisc replace dev "$IFACE" root netem delay "$DELAY" loss "$LOSS"

printf 'Applied delay %s and loss %s to ingress and egress on %s.\n' "$DELAY" "$LOSS" "$IFACE"
