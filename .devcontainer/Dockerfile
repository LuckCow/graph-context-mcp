# Dev container for graph-context-mcp
# Python 3.11 (matches requires-python >= 3.11) + git + Claude Code + egress firewall tooling.

FROM python:3.11-bookworm

ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=1000

# Base tooling. iptables/ipset/dnsutils/jq are needed by the egress firewall script.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        curl \
        ca-certificates \
        jq \
        dnsutils \
        iptables \
        ipset \
        iproute2 \
        sudo \
        less \
        nano \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 (runtime for Claude Code), then Claude Code itself.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

# Install the build backend + the project's dependencies at image build time,
# so the locked-down container never needs PyPI at runtime. BOTH the runtime
# `dependencies` and the `optional-dependencies.dev` lists are read straight
# from pyproject.toml (single source of truth): to add any package, add it to
# the appropriate list there and rebuild — no edits needed here.
COPY pyproject.toml /tmp/pyproject.toml
RUN pip install --no-cache-dir hatchling \
    && python -c "import tomllib,subprocess,sys; \
p=tomllib.load(open('/tmp/pyproject.toml','rb'))['project']; \
deps=list(p.get('dependencies',[])) + list(p.get('optional-dependencies',{}).get('dev',[])); \
subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir',*deps]) if deps else None" \
    && rm /tmp/pyproject.toml

# Non-root user. May only sudo the firewall script, nothing else.
RUN groupadd --gid ${USER_GID} ${USERNAME} \
    && useradd --uid ${USER_UID} --gid ${USER_GID} -m -s /bin/bash ${USERNAME} \
    && echo "${USERNAME} ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" \
       > /etc/sudoers.d/firewall \
    && chmod 0440 /etc/sudoers.d/firewall

COPY .devcontainer/init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod +x /usr/local/bin/init-firewall.sh

# Pre-create ~/.claude OWNED BY dev. Docker initializes a fresh named volume
# from the image's directory (content + ownership), so this ensures the
# claude-config volume is writable by the dev user instead of root. This is
# what lets interactive `claude` login persist credentials to
# ~/.claude/.credentials.json across rebuilds — required for Remote Control.
RUN mkdir -p /home/${USERNAME}/.claude \
    && chown -R ${USER_UID}:${USER_GID} /home/${USERNAME}/.claude \
    && chmod 700 /home/${USERNAME}/.claude

# Make the src-layout package importable in EVERY shell and `docker exec` session
# (including the one Claude Desktop spawns) without a per-launch `-e PYTHONPATH` flag.
# The source lives in the runtime volume mount at this path, not in the build context,
# so we set the path rather than `pip install -e .` (which would need the source at build time).
ENV PYTHONPATH=/workspaces/graph-context-mcp/src

USER ${USERNAME}
WORKDIR /workspaces/graph-context-mcp
