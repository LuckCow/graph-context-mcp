#!/bin/bash
# Process control for the orchestrator -- `python -m graph_context.orchestrator.serve`:
# the Anytype bot + Discord (when bound) + the inspection server, one process.
#
#   gc-serve start | stop | restart | status | logs | boot
#
# After editing source:  gc-serve restart
# The source is a bind mount and PYTHONPATH points into it, so a restart picks
# up every change. No image rebuild, no compose down/up. (Only dependency or
# Dockerfile changes need a rebuild.)
#
# Runs the server as the CURRENT, unprivileged user -- never as root, and this
# script is deliberately absent from /etc/sudoers.d/firewall. Root is exempt
# from the egress firewall so that tailscaled can reach its control plane (see
# init-firewall.sh, "Tailscale"); a root-run orchestrator would inherit that
# exemption and silently bypass the allowlist the whole design rests on.

set -uo pipefail

APP_DIR="${GC_APP_DIR:-/workspaces/graph-context-mcp}"
LOG_FILE="${GC_SERVE_LOG:-${APP_DIR}/logs/serve.log}"
PID_FILE="${GC_SERVE_PIDFILE:-/tmp/gc-serve.pid}"
LOCK_FILE="${GC_SERVE_LOCKFILE:-/tmp/gc-serve.lock}"
STOP_GRACE_SECONDS=10

say()  { echo "[gc-serve] $*"; }
warn() { echo "[gc-serve] $*" >&2; }

# ANCHORED on purpose: an unanchored match also hits every shell whose command
# line merely mentions the module (a `grep`, an editor, this script's own
# caller), and `stop` would kill that bystander.
SERVE_CMD_RE='^python -m graph_context\.orchestrator\.serve'

# True when $1 is a live process that IS the orchestrator. Both callers below
# need this: a pid file can name a RECYCLED pid, and pgrep only proposes
# candidates -- /proc is what settles it. One rule, one home: nothing else in
# this script decides what counts as "the server".
is_serve_pid() {
    local cmd
    [ -n "${1:-}" ] || return 1
    kill -0 "$1" 2>/dev/null || return 1
    cmd="$(tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null)"
    [[ "$cmd" =~ $SERVE_CMD_RE ]]
}

# Echoes the live pid, or returns 1. The pid file is a CACHE, not the truth: a
# pid whose process is gone is stale (containers restart and pids do not
# survive), and a lost start race can delete the file of a server that is still
# running. So fall back to the process table -- status, stop and start then all
# agree with reality, and an orphaned server is adoptable rather than immortal.
running_pid() {
    local pid
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE" 2>/dev/null)"
        if is_serve_pid "$pid"; then
            echo "$pid"
            return 0
        fi
    fi
    for pid in $(pgrep -u "$(id -u)" -f "$SERVE_CMD_RE" 2>/dev/null); do
        if is_serve_pid "$pid"; then
            echo "$pid"
            return 0
        fi
    done
    return 1
}

# `boot` runs from BOTH the compose command and devcontainer's
# postStartCommand, so two starts race on a rebuild. Serializing them makes
# the second find the first's pid file and no-op, instead of spawning a
# duplicate that dies on the inspection server's port -- and whose cleanup
# then deleted the WINNER's pid file, leaving a healthy server that `status`
# reported as down and `stop` could not reach. Degrades to an unlocked start
# if flock is unavailable: a boot that runs is worth more than a tidy one.
do_start() {
    # -w: a lock we cannot take in 30s must never hang container boot -- fall
    # through and let the running_pid check do the unsynchronized guarding.
    ( flock -w 30 9; start_locked ) 9>"$LOCK_FILE"
}

start_locked() {
    local pid child
    if pid="$(running_pid)"; then
        say "already running (pid ${pid})"
        # Re-adopt: the file is missing whenever the process came from a
        # start whose pid file was lost. Writing it back makes the cheap
        # path (the file) true again for the next caller.
        echo "$pid" > "$PID_FILE"
        return 0
    fi

    if [ "$(id -u)" = "0" ]; then
        warn "refusing to run the orchestrator as root -- it would inherit the"
        warn "firewall's root egress exemption. Run this as the workload user."
        return 1
    fi

    cd "$APP_DIR" || { warn "no such directory: ${APP_DIR}"; return 1; }
    mkdir -p "$(dirname "$LOG_FILE")"

    say "starting: python -m graph_context.orchestrator.serve"
    # 9>&- : the server must NOT inherit the start lock. It would hold it for
    # its entire life, and every later `start` -- including the next boot's
    # second caller -- would block instead of reporting "already running".
    nohup python -m graph_context.orchestrator.serve >>"$LOG_FILE" 2>&1 9>&- &
    child=$!

    # A config error (bad spaces.toml, missing key, port in use) kills the
    # process in the first second or two. Surface it here instead of leaving a
    # stale pid file and a container that looks healthy. Check OUR child, not
    # running_pid: its process-table fallback would report someone else's
    # healthy server as proof that this start succeeded.
    #
    # The pid file is written AFTER that check, never before: a start that
    # writes it up front clobbers the entry of a server that is already
    # running, and then deletes it on the way out -- which is exactly how a
    # healthy orchestrator ended up with no pid file, invisible to `stop`.
    # A failed start now leaves the file untouched, so there is nothing to
    # undo. For the two seconds before it lands, running_pid's process-table
    # fallback still finds the server.
    sleep 2
    if ! kill -0 "$child" 2>/dev/null; then
        warn "ERROR: exited immediately. Last 20 lines of ${LOG_FILE}:"
        tail -n 20 "$LOG_FILE" >&2
        return 1
    fi
    echo "$child" > "$PID_FILE"
    say "running (pid ${child}) -- logs: ${LOG_FILE}"
}

do_stop() {
    local pid waited
    if ! pid="$(running_pid)"; then
        say "not running"
        rm -f "$PID_FILE"
        return 0
    fi
    say "stopping (pid ${pid})"
    kill -TERM "$pid" 2>/dev/null
    waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$STOP_GRACE_SECONDS" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        warn "did not exit in ${STOP_GRACE_SECONDS}s -- sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null
    fi
    rm -f "$PID_FILE"
    say "stopped"
}

do_status() {
    local pid
    if pid="$(running_pid)"; then
        say "running (pid ${pid})"
        say "log: ${LOG_FILE}"
        tail -n 3 "$LOG_FILE" 2>/dev/null
        return 0
    fi
    say "not running"
    return 1
}

case "${1:-status}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)  do_status ;;
    logs)    exec tail -n 100 -f "$LOG_FILE" ;;
    boot)
        # The container's boot path. GC_SERVE_AUTOSTART=0 (or off/false/no)
        # skips it, for when you would rather run the server in the foreground
        # yourself. NEVER fails the container: a boot that could not start the
        # orchestrator is still a container you can exec into and debug, which
        # matters much more on a remote VPS than a tidy exit code.
        autostart="${GC_SERVE_AUTOSTART:-1}"
        case "${autostart,,}" in
            0|off|false|no)
                say "GC_SERVE_AUTOSTART is off -- not starting the orchestrator"
                ;;
            *) do_start || warn "continuing anyway -- 'gc-serve start' to retry" ;;
        esac
        exit 0
        ;;
    *)
        warn "usage: gc-serve start|stop|restart|status|logs|boot"
        exit 2
        ;;
esac
