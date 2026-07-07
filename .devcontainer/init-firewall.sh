#!/bin/bash
# Default-deny egress firewall for the graph-context-mcp dev container.
# Adapted from Anthropic's Claude Code reference devcontainer.
#
# Allowed:
#   - DNS, loopback
#   - GitHub (git over HTTPS/SSH, API) via GitHub's published IP ranges
#   - Anthropic / Claude Code (API, login, telemetry) + npm registry (CLI self-update)
#   - VS Code server + extension marketplace (so the host can attach)
#   - The Docker host on ports 31009/31012 only (Anytype local API)
#   - The local container subnet (port forwarding to the host)
# Everything else: REJECT.

set -euo pipefail
IFS=$'\n\t'

echo "[firewall] flushing existing rules..."

# Docker's embedded DNS (127.0.0.11) depends on NAT rules that Docker installs
# inside the container. Save them before flushing, restore right after —
# otherwise DNS dies and nothing below can resolve.
DOCKER_DNS_RULES="$(iptables-save 2>/dev/null | grep 127.0.0.11 || true)"

iptables -F
iptables -X
iptables -t nat -F
iptables -t nat -X
iptables -t mangle -F
iptables -t mangle -X
ipset destroy allowed-domains 2>/dev/null || true

if command -v ip6tables >/dev/null && ip6tables -L >/dev/null 2>&1; then
    ip6tables -F 2>/dev/null || true
    ip6tables -X 2>/dev/null || true
    ip6tables -P INPUT ACCEPT 2>/dev/null || true
    ip6tables -P FORWARD ACCEPT 2>/dev/null || true
    ip6tables -P OUTPUT ACCEPT 2>/dev/null || true
fi

# Reset policies to ACCEPT while we build the allowlist (matters on re-runs,
# when the previous run left them as DROP).
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT

if [ -n "$DOCKER_DNS_RULES" ]; then
    echo "[firewall] restoring Docker DNS rules..."
    {
        echo "*nat"
        echo ":DOCKER_OUTPUT - [0:0]"
        echo ":DOCKER_POSTROUTING - [0:0]"
        echo "$DOCKER_DNS_RULES"
        echo "COMMIT"
    } | iptables-restore --noflush
fi

# --- Baseline (must be open while we resolve the allowlist) -----------------
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT   # DNS
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
iptables -A INPUT  -p udp --sport 53 -j ACCEPT

ipset create allowed-domains hash:net

# --- GitHub: use their published IP ranges (covers git, api, web) ------------
echo "[firewall] fetching GitHub IP ranges..."
gh_ranges="$(curl -fsS https://api.github.com/meta)"
if [ -z "$gh_ranges" ] || ! echo "$gh_ranges" | jq -e '.git and .api and .web' >/dev/null; then
    echo "[firewall] ERROR: could not fetch valid GitHub IP ranges" >&2
    exit 1
fi
echo "$gh_ranges" | jq -r '(.git + .api + .web)[]' | grep -v ':' | sort -u | while read -r cidr; do
    ipset add allowed-domains "$cidr" 2>/dev/null || true
done

# --- Named domains: Anthropic/Claude Code, npm, VS Code ----------------------
ALLOWED_DOMAINS=(
    # Claude Code
    "api.anthropic.com"
    "console.anthropic.com"
    "claude.ai"
    "app.claude.com"                # Remote Control session host (claude.ai/code, app.claude.com/rc)
    "statsig.anthropic.com"
    "statsig.com"
    "sentry.io"
    "registry.npmjs.org"            # Claude Code self-update
    # VS Code server download + marketplace (needed when the host attaches)
    "update.code.visualstudio.com"
    "vscode.download.prss.microsoft.com"
    "marketplace.visualstudio.com"
    # Discord bot transport (WP8): REST API + Gateway websocket, both outbound.
    "discord.com"
    "gateway.discord.gg"
    # Uncomment if you want pip access at runtime (dev deps are pre-baked in the image):
    # "pypi.org"
    # "files.pythonhosted.org"
)

for domain in "${ALLOWED_DOMAINS[@]}"; do
    ips="$(dig +short A "$domain" | grep -E '^[0-9]+\.' || true)"
    if [ -z "$ips" ]; then
        echo "[firewall] WARNING: could not resolve $domain" >&2
        continue
    fi
    while read -r ip; do
        ipset add allowed-domains "$ip" 2>/dev/null || true
    done <<< "$ips"
done

# --- Discord anycast block ----------------------------------------------------
# After connecting, Discord hands the client a per-session resume_gateway_url
# (e.g. gateway-us-east1-b.discord.gg) that is dialed on every reconnect and
# can't be resolved at firewall-init time. All Discord hosts — REST, gateway,
# and resume gateways — resolve into this Discord-dedicated Cloudflare /20,
# so the CIDR covers resumes and IP rotation across restarts.
ipset add allowed-domains "162.159.128.0/20" 2>/dev/null || true

# --- Docker host (Anytype desktop local API only) ----------------------------
HOST_IPV4="$(getent ahostsv4 host.docker.internal 2>/dev/null | awk '{print $1; exit}' || true)"
HOST_IPV6="$(getent ahostsv6 host.docker.internal 2>/dev/null | awk '{print $1; exit}' || true)"
if [ -z "$HOST_IPV4" ]; then
    HOST_IPV4="$(ip route 2>/dev/null | awk '/default/ {print $3; exit}' || true)"
fi
if [ -n "$HOST_IPV4" ]; then
    echo "[firewall] docker host (v4) is $HOST_IPV4 (allowing tcp/31009, tcp/31012 only)"
    iptables -A OUTPUT -d "$HOST_IPV4" -p tcp -m multiport --dports 31009,31012 -j ACCEPT
fi
if [ -n "$HOST_IPV6" ] && command -v ip6tables >/dev/null; then
    echo "[firewall] docker host (v6) is $HOST_IPV6 (allowing tcp/31009, tcp/31012 only)"
    ip6tables -A OUTPUT -d "$HOST_IPV6" -p tcp -m multiport --dports 31009,31012 -j ACCEPT 2>/dev/null || true
fi

# --- Container subnet (lets Docker forward published ports from the host) ----
# This rule also lets the dev container reach compose siblings -- deliberately
# including the WP14 `anytype` sidecar at http://anytype:31012 (the sidecar
# runs OUTSIDE this firewall; its outbound sync to the Anytype network is its
# whole job).
HOST_NETWORK="$(ip route 2>/dev/null | grep -E '^[0-9]+\.' | awk '{print $1; exit}' || true)"
if [ -z "$HOST_NETWORK" ] && [ -n "$HOST_IPV4" ]; then
    # Fallback if iproute2 is unavailable: assume a /24 around the gateway.
    HOST_NETWORK="$(echo "$HOST_IPV4" | awk -F. '{printf "%s.%s.%s.0/24", $1, $2, $3}')"
fi
if [ -n "$HOST_NETWORK" ]; then
    echo "[firewall] allowing container subnet $HOST_NETWORK"
    iptables -A INPUT  -s "$HOST_NETWORK" -j ACCEPT
    iptables -A OUTPUT -d "$HOST_NETWORK" -j ACCEPT
fi

# --- Lock it down (IPv6) -------------------------------------------------------
# The allowlist above is IPv4-only, so IPv6 must be denied wholesale or it
# becomes a bypass. Allow only loopback, established flows, DNS, the published
# MCP port, and the Anytype rule added above.
if command -v ip6tables >/dev/null && ip6tables -L >/dev/null 2>&1; then
    ip6tables -P INPUT DROP
    ip6tables -P FORWARD DROP
    ip6tables -P OUTPUT DROP
    ip6tables -A INPUT  -i lo -j ACCEPT
    ip6tables -A OUTPUT -o lo -j ACCEPT
    ip6tables -A OUTPUT -p udp --dport 53 -j ACCEPT
    ip6tables -A OUTPUT -p tcp --dport 53 -j ACCEPT
    ip6tables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
    ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    ip6tables -A INPUT  -p tcp --dport 8000 -j ACCEPT
    ip6tables -A OUTPUT -j REJECT --reject-with adm-prohibited 2>/dev/null \
        || ip6tables -A OUTPUT -j DROP
    echo "[firewall] IPv6 locked down."
fi

# --- Lock it down (IPv4) -------------------------------------------------------
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

iptables -A INPUT  -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Your MCP server, published to the host as 127.0.0.1:8000
iptables -A INPUT -p tcp --dport 8000 -j ACCEPT

# Allowlisted destinations
iptables -A OUTPUT -m set --match-set allowed-domains dst -j ACCEPT

# Reject (not drop) everything else so failures are immediate, not hangs.
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited

# --- Verify -------------------------------------------------------------------
echo "[firewall] verifying..."
if curl --connect-timeout 5 -s https://example.com >/dev/null 2>&1; then
    echo "[firewall] ERROR: example.com is reachable — lockdown failed" >&2
    exit 1
fi
if ! curl --connect-timeout 5 -s https://api.github.com/zen >/dev/null 2>&1; then
    echo "[firewall] ERROR: api.github.com is NOT reachable — allowlist broken" >&2
    exit 1
fi
echo "[firewall] OK: default-deny active, GitHub reachable."
# WP14 sidecar reachability (warn-only: the opt-in `anytype` compose service
# may be absent or still booting; the container-subnet rule above covers it).
if curl --connect-timeout 3 -s -o /dev/null http://anytype:31012/v1/spaces 2>/dev/null; then
    echo "[firewall] anytype sidecar reachable at anytype:31012"
else
    echo "[firewall] note: anytype sidecar not reachable (fine unless cutover is done)"
fi
