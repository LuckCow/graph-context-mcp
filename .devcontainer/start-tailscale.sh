#!/bin/bash
# Join the tailnet and publish the inspection server on it.
# Companion to init-firewall.sh -- see its "Tailscale" section for the egress
# seam that lets the daemon out through the default-deny policy.
#
# ORDER MATTERS: this runs AFTER init-firewall.sh, which flushes all of
# netfilter -- a tailscaled started first would have its rules wiped out from
# under it. Userspace networking keeps that from mattering ever again:
# tailscaled installs NO iptables rules of its own, so the firewall stays safe
# to re-run at any time (VS Code's postStartCommand does exactly that).
#
# NOT fatal, by design: `set -e` is deliberately off and every failure path
# warns and exits 0. A container whose tailnet failed to come up is one you can
# still reach another way to fix it; a container that exits on a stale auth key
# is one you cannot reach at all.

set -uo pipefail

AUTHKEY_FILE="${TS_AUTHKEY_FILE:-/run/secrets/tailscale_authkey}"
STATE_DIR="/var/lib/tailscale"
SOCKET_DIR="/var/run/tailscale"
SOCKET="${SOCKET_DIR}/tailscaled.sock"
NODE_HOSTNAME="${TS_HOSTNAME:-graph-context-mcp}"
# Hands the tailscale CLI to the unprivileged workload user, whose sudo is
# scoped to this script and the firewall's. Without it `tailscale status` from
# a dev shell is permission-denied.
CLI_OPERATOR="${TS_OPERATOR:-dev}"
# The inspection server (ADR 025). An off-value joins the tailnet but publishes
# nothing on it.
SERVE_PORT="${TS_SERVE_PORT:-8765}"
# Deployment-specific `tailscale up` flags, e.g. --advertise-tags=tag:vps,
# --ssh, --advertise-exit-node. Word-split deliberately.
read -ra UP_EXTRA <<< "${TS_UP_EXTRA_ARGS:-}"

say()  { echo "[tailscale] $*"; }
warn() { echo "[tailscale] $*" >&2; }

if ! command -v tailscaled >/dev/null; then
    warn "tailscaled is not installed -- skipping (rebuild the image to add it)"
    exit 0
fi

# An absent or empty key is the "this deployment does not use tailscale"
# configuration, not a misconfiguration: the container behaves exactly as it
# did before tailscale existed.
if [ ! -s "$AUTHKEY_FILE" ]; then
    say "no auth key at ${AUTHKEY_FILE} -- not joining a tailnet"
    exit 0
fi

mkdir -p "$STATE_DIR" "$SOCKET_DIR"

if pgrep -x tailscaled >/dev/null; then
    say "daemon already running -- reapplying configuration"
else
    say "starting tailscaled (userspace networking)"
    # --tun=userspace-networking: no /dev/net/tun and no netfilter rules, so
    # this coexists with the egress firewall instead of fighting it. Inbound
    # tailnet TCP is proxied to 127.0.0.1 inside the container, which is all
    # the inspection server needs. (The cost: OUTBOUND connections to tailnet
    # peers are not transparent -- they would need --socks5-server, or TUN mode
    # with /dev/net/tun mounted and --netfilter-mode=off.)
    #
    # Output inherits the container's stdout/stderr, so it lands in `docker logs`.
    tailscaled \
        --tun=userspace-networking \
        --state="${STATE_DIR}/tailscaled.state" \
        --socket="$SOCKET" &

    for _ in $(seq 1 100); do
        [ -S "$SOCKET" ] && break
        sleep 0.1
    done
    if [ ! -S "$SOCKET" ]; then
        warn "ERROR: tailscaled did not create ${SOCKET} within 10s -- giving up"
        exit 0
    fi
fi

# `file:` keeps the key off the process command line (`ps`, /proc) -- the same
# discipline the app code applies to every other secret in this stack.
#
# --accept-dns=false is load-bearing in a container: MagicDNS would rewrite
# /etc/resolv.conf and clobber Docker's embedded resolver at 127.0.0.11, which
# init-firewall.sh goes out of its way to preserve and which is how `anytype`
# and every other compose sibling resolves.
if ! tailscale --socket="$SOCKET" up \
        --authkey="file:${AUTHKEY_FILE}" \
        --hostname="$NODE_HOSTNAME" \
        --operator="$CLI_OPERATOR" \
        --accept-dns=false \
        --accept-routes=false \
        "${UP_EXTRA[@]}"; then
    warn "ERROR: 'tailscale up' failed -- the node is not on the tailnet."
    warn "       Check the auth key (expired? already used? wrong tailnet?) and"
    warn "       that init-firewall.sh logged its tailscale egress rule."
    exit 0
fi

say "up as $(tailscale --socket="$SOCKET" status --self --peers=false 2>/dev/null | head -1)"

case "${SERVE_PORT,,}" in
    ""|0|off|false|no)
        say "TS_SERVE_PORT is off -- publishing nothing on the tailnet"
        exit 0
        ;;
esac

# Terminates TLS with a real cert on <hostname>.<tailnet>.ts.net and proxies to
# the inspection server. Requires MagicDNS + HTTPS certificates enabled for the
# tailnet (admin console -> DNS); without them this is the one thing here that
# legitimately fails, so it degrades to a note rather than a silent no-op.
# `serve` config persists in the state file, so re-running is idempotent.
if tailscale --socket="$SOCKET" serve --bg "$SERVE_PORT"; then
    say "serving :${SERVE_PORT} at https://${NODE_HOSTNAME}.<your-tailnet>.ts.net/"
else
    warn "NOTE: 'tailscale serve' failed -- enable MagicDNS and HTTPS certificates"
    warn "      in the tailnet admin console (DNS tab). The node is still on the"
    warn "      tailnet; reach it at http://${NODE_HOSTNAME}:${SERVE_PORT}/ meanwhile."
fi
