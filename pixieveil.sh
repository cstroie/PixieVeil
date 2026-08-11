#!/usr/bin/env bash
# Control script for the PixieVeil server: start/stop/restart/status.
#
# start/restart do NOT fork or background pixieveil.py themselves — the
# process stays in the foreground, same as Type=simple in pixieveil.service
# and supervise-daemon in pixieveil.openrc. Background it yourself if you
# want that (tmux/screen, `nohup ./pixieveil.sh start &`, a supervisor, ...).
#
# Usage:
#   ./pixieveil.sh start [extra args]     # foreground; refuses if already running
#   ./pixieveil.sh stop
#   ./pixieveil.sh restart [extra args]   # stop, then start (foreground)
#   ./pixieveil.sh status
#
#   PIDFILE=/run/pixieveil.pid ./pixieveil.sh status
#   ./pixieveil.sh start --log-level DEBUG   # extra args pass through to pixieveil.py
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

STOP_TIMEOUT="${STOP_TIMEOUT:-15}"

# Use the .python venv (if one exists) otherwise fall back to whatever
# python3 is on PATH.
if [[ -x .python/bin/python3 ]]; then
    PYTHON_BIN=.python/bin/python3
else
    PYTHON_BIN=python3
fi

# Default pidfile lives alongside PixieVeil's other runtime state (the
# "data" directory that also holds data/dicom, data/log, ...) rather than
# the checkout root. Derived from storage.base_path in config/settings.yaml
# (e.g. "./data/dicom" -> "./data"); falls back to "data" if settings can't
# be loaded (missing venv deps, no config yet, ...).
default_pid_dir() {
    local base_path
    base_path="$("$PYTHON_BIN" -c '
from pixieveil.config import Settings
try:
    print(Settings.load().storage.get("base_path", "./data/dicom"))
except Exception:
    pass
' 2>/dev/null)"
    if [[ -n "$base_path" ]]; then
        dirname -- "$base_path"
    else
        echo "data"
    fi
}

PIDFILE="${PIDFILE:-$(default_pid_dir)/pixieveil.pid}"

# Sets RUNNING_PID on a running process, STALE_PID on a dead one referenced
# by the pidfile. Returns 0 if running, 1 if stopped (stale or absent).
check_pid() {
    RUNNING_PID=""
    STALE_PID=""
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid="$(cat "$PIDFILE")"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            RUNNING_PID="$pid"
            return 0
        else
            STALE_PID="$pid"
        fi
    fi
    return 1
}

do_status() {
    if check_pid; then
        echo "PixieVeil is running (pid $RUNNING_PID)"
        return 0
    elif [[ -n "$STALE_PID" ]]; then
        echo "PixieVeil is not running (stale pidfile, pid $STALE_PID)"
    else
        echo "PixieVeil is not running"
    fi
    return 1
}

do_stop() {
    if check_pid; then
        echo "Stopping PixieVeil (pid $RUNNING_PID)..."
        kill -TERM "$RUNNING_PID"
        local waited=0
        while kill -0 "$RUNNING_PID" 2>/dev/null; do
            if (( waited >= STOP_TIMEOUT )); then
                echo "warning: pid $RUNNING_PID did not exit after ${STOP_TIMEOUT}s, sending SIGKILL" >&2
                kill -KILL "$RUNNING_PID" 2>/dev/null || true
                break
            fi
            sleep 1
            waited=$((waited + 1))
        done
        rm -f "$PIDFILE"
    elif [[ -n "$STALE_PID" ]]; then
        echo "Stale pidfile (pid $STALE_PID not running), removing"
        rm -f "$PIDFILE"
    else
        echo "PixieVeil is not running"
    fi
}

do_start() {
    if check_pid; then
        echo "error: PixieVeil is already running (pid $RUNNING_PID)" >&2
        exit 1
    elif [[ -n "$STALE_PID" ]]; then
        echo "Stale pidfile (pid $STALE_PID not running), removing"
        rm -f "$PIDFILE"
    fi
    mkdir -p -- "$(dirname -- "$PIDFILE")"
    echo "Starting PixieVeil..."
    exec "$PYTHON_BIN" pixieveil.py --pidfile "$PIDFILE" "$@"
}

cmd="${1:-}"
[[ $# -gt 0 ]] && shift

case "$cmd" in
    start)
        do_start "$@"
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_stop
        do_start "$@"
        ;;
    status)
        do_status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status} [extra args for start/restart]" >&2
        exit 2
        ;;
esac
