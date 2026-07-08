# Headless Anytype node (bot account) -- the "anytype" compose sidecar.
#
# Starts with every compose up/rebuild. First-time bootstrap (bot
# account creation, space joins, flipping the dev container's
# ANYTYPE_API_BASE_URL to http://anytype:31012) lives in
# docs/WORK_PACKAGES.md WP14.
#
# The CLI is installed at BUILD time (builds have network; this container
# is intentionally NOT behind init-firewall.sh -- its whole job is the
# outbound sync connection to Anytype's network that the dev container
# is firewalled against).
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# anytype-cli's official installer (github.com/anyproto/anytype-cli).
# TODO(cutover): pin a release tag/checksum once we adopt the sidecar for
# real -- HEAD is fine while the service is opt-in and unused.
# NOTE: the anytype CLI needs to run in bash, not dash!
RUN curl -fsSL https://raw.githubusercontent.com/anyproto/anytype-cli/HEAD/install.sh | bash \
    && mv /root/.local/bin/anytype /usr/local/bin/anytype

# The CLI splits its state (live-confirmed): ~/.config/anytype holds the
# identity/config/API-key store, ~/.anytype the rest. BOTH are volumes in
# docker-compose.yml -- a rebuild once wiped the API keys when only
# ~/.anytype was mounted. The account key backup in .devcontainer/secrets
# restores identity if the volumes are ever lost.
VOLUME ["/root/.anytype", "/root/.config/anytype"]

# gRPC 31010 / gRPC-Web 31011 / HTTP API 31012. Bind beyond loopback so
# the dev container can reach it across the compose network.
EXPOSE 31012
ENTRYPOINT ["anytype", "serve", "--listen-address", "0.0.0.0:31012"]
