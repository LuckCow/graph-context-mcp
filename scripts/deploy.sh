#!/bin/bash
# Deploy the current `main` onto THIS host (ADR 060).
#
#   scripts/deploy.sh [--build] [--to SHA] [--force] [--branch NAME]
#
# Runs on the deployment host, OUTSIDE the container: it needs git and the
# docker CLI, neither of which the workload container has. `scripts/gc-prod`
# is the remote front end -- it ssh's in and runs exactly this.
#
# What it does not touch: spaces.toml and channels.toml. They are
# deployment-scoped and git-ignored (ADR 060), so a deploy can never
# repoint this host at another deployment's spaces -- which is the whole
# reason they left the index.
#
# Restart depth is DERIVED from the diff, not guessed: source is a bind
# mount, so `gc-serve restart` picks it up, and only a dependency or
# container change needs the (multi-GB, several-minute) image rebuild.

set -uo pipefail

REPO_DIR="${GC_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_FILE="${GC_COMPOSE_FILE:-${REPO_DIR}/.devcontainer/docker-compose.yml}"
CONTAINER="${GC_CONTAINER:-graph-context-mcp-dev}"
BRANCH="${GC_DEPLOY_BRANCH:-main}"
# How long the orchestrator gets to answer after a restart before we call the
# deploy failed. A rebuild's container boot (firewall, tailscale) eats most of
# it; a plain restart is up in a couple of seconds.
HEALTH_TIMEOUT="${GC_DEPLOY_HEALTH_TIMEOUT:-90}"

say()  { echo "[deploy] $*"; }
warn() { echo "[deploy] $*" >&2; }
die()  { warn "$*"; exit 1; }

BUILD=0
FORCE=0
TARGET=""

while [ $# -gt 0 ]; do
    case "$1" in
        --build)  BUILD=1; shift ;;
        --force)  FORCE=1; shift ;;
        --to)     TARGET="${2:-}"; [ -n "$TARGET" ] || die "--to needs a commit"; shift 2 ;;
        --branch) BRANCH="${2:-}"; [ -n "$BRANCH" ] || die "--branch needs a name"; shift 2 ;;
        -h|--help)
            # The header comment IS the help text, so the two cannot drift.
            awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
                "${BASH_SOURCE[0]}"
            exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

cd "$REPO_DIR" || die "no such directory: ${REPO_DIR}"
git rev-parse --git-dir >/dev/null 2>&1 || die "${REPO_DIR} is not a git repository"
command -v docker >/dev/null || die "docker is not on PATH -- this script runs on the HOST, not in the container"

# A dirty tree means someone edited code on the server. Deploying over it
# either loses the edit or wedges the pull, and both are worse discovered
# later. spaces.toml/channels.toml are ignored now, so this fires only on
# real source drift.
if [ -n "$(git status --porcelain)" ]; then
    if [ "$FORCE" = "1" ]; then
        warn "working tree is dirty -- continuing anyway (--force)"
    else
        warn "working tree is dirty; deploying would fight these edits:"
        git status --short >&2
        die "commit, stash, or re-run with --force"
    fi
fi

OLD="$(git rev-parse HEAD)"

say "fetching origin"
git fetch --quiet origin || die "git fetch failed"

if [ -n "$TARGET" ]; then
    # Rollback path. Detached HEAD on purpose: the next ordinary deploy puts
    # the branch back, and a detached HEAD is a visible "this host is pinned".
    git rev-parse --verify --quiet "${TARGET}^{commit}" >/dev/null \
        || die "no such commit after fetch: ${TARGET}"
    say "checking out ${TARGET} (detached -- rollback)"
    git checkout --quiet --detach "$TARGET" || die "checkout failed"
else
    git checkout --quiet "$BRANCH" || die "cannot check out ${BRANCH}"
    # --ff-only: a merge commit minted on a server nobody develops on is a
    # divergence you would find weeks later.
    git pull --quiet --ff-only origin "$BRANCH" \
        || die "cannot fast-forward ${BRANCH} -- the local branch has diverged"
fi

NEW="$(git rev-parse HEAD)"

if [ "$OLD" = "$NEW" ] && [ "$BUILD" = "0" ] && [ "$FORCE" = "0" ]; then
    say "already at $(git rev-parse --short HEAD) -- nothing to deploy"
    say "(--force restarts anyway)"
    exit 0
fi

# Rebuild only when the image's inputs moved: dependencies (pyproject) or the
# container definition itself. Everything else is source, and source is a bind
# mount. Reading the ANSWER off the diff beats asking the deployer to remember.
if [ "$BUILD" = "0" ] && [ "$OLD" != "$NEW" ]; then
    if git diff --name-only "$OLD" "$NEW" \
        | grep -qE '^(pyproject\.toml|\.devcontainer/)'; then
        say "dependency/container changes in this range -- rebuilding the image"
        BUILD=1
    fi
fi

if [ "$BUILD" = "1" ]; then
    say "docker compose up -d --build"
    docker compose -f "$COMPOSE_FILE" up -d --build || die "compose build/up failed"
else
    say "gc-serve restart (source is a bind mount; no rebuild needed)"
    docker exec "$CONTAINER" gc-serve restart || die "gc-serve restart failed"
fi

# The container command starts the orchestrator itself after a rebuild, so
# poll rather than assume: `gc-serve status` exits non-zero until it is up.
say "waiting for the orchestrator (up to ${HEALTH_TIMEOUT}s)"
waited=0
while ! docker exec "$CONTAINER" gc-serve status >/dev/null 2>&1; do
    if [ "$waited" -ge "$HEALTH_TIMEOUT" ]; then
        warn "ERROR: the orchestrator is not running ${HEALTH_TIMEOUT}s after deploy"
        docker exec "$CONTAINER" gc-serve status
        warn "last 30 log lines:"
        docker exec "$CONTAINER" tail -n 30 logs/serve.log >&2 2>/dev/null
        exit 1
    fi
    sleep 3
    waited=$((waited + 3))
done

docker exec "$CONTAINER" gc-serve status
say "deployed $(git rev-parse --short "$OLD") -> $(git rev-parse --short "$NEW") ($(git log -1 --format=%s))"
if [ "$OLD" != "$NEW" ]; then
    say "$(git rev-list --count "${OLD}..${NEW}" 2>/dev/null || echo '?') commit(s) applied"
fi
