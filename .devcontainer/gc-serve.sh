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
STOP_GRACE_SECONDS=10

say()  { echo "[gc-serve] $*"; }
warn() { echo "[gc-serve] $*" >&2; }

# Echoes the live pid, or returns 1. A pid file whose process is gone is stale,
# not running -- containers restart and pids do not survive.
running_pid() {
    local pid
    [ -f "$PID_FILE" ] || return 1
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

do_start() {
    local pid
    if pid="$(running_pid)"; then
        say "already running (pid ${pid})"
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
    nohup python -m graph_context.orchestrator.serve >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    # A config error (bad spaces.toml, missing key, port in use) kills the
    # process in the first second or two. Surface it here instead of leaving a
    # stale pid file and a container that looks healthy.
    sleep 2
    if ! pid="$(running_pid)"; then
        warn "ERROR: exited immediately. Last 20 lines of ${LOG_FILE}:"
        tail -n 20 "$LOG_FILE" >&2
        rm -f "$PID_FILE"
        return 1
    fi
    say "running (pid ${pid}) -- logs: ${LOG_FILE}"
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
