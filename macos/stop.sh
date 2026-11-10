#!/bin/bash
# Stop the Work Sessions server started by "Work Sessions.app".
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$REPO/data/app.pid"

[ -f "$PID_FILE" ] || { echo "Work Sessions isn't running from this folder."; exit 0; }
PID="$(cat "$PID_FILE")"
case "$PID" in
  ''|*[!0-9]*) echo "Ignoring invalid PID file: $PID_FILE"; exit 1 ;;
esac

COMMAND="$(ps -p "$PID" -o command= 2>/dev/null || true)"
case "$COMMAND" in
  *"$REPO/app.py"*)
    kill "$PID"
    rm -f "$PID_FILE"
    echo "Stopped Work Sessions (pid $PID)."
    ;;
  '')
    rm -f "$PID_FILE"
    echo "Removed stale PID file."
    ;;
  *)
    echo "Refusing to stop pid $PID: it is not this Work Sessions server."
    exit 1
    ;;
esac
