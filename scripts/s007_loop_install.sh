#!/bin/bash
# Install/uninstall the launchd tick job for scripts/s007_tick.py.
#
# scripts/s007_tick.py is a stateless, one-shot check: launchd's
# StartCalendarInterval (an empty match dict = every wall-clock minute)
# invokes it, always -- it is NOT a long-running process, so there is
# nothing here for macOS to throttle or App Nap the way it did the old
# bash loop's ~17 hour sleep (see decisions-log.md 2026-07-23). This uses
# StartCalendarInterval rather than StartInterval on purpose: StartInterval
# schedules N seconds after the previous actual start, so any per-cycle
# overhead compounds into drift with no daily reset; StartCalendarInterval
# is pinned to the real wall clock every time, so it cannot drift (see
# decisions-log.md 2026-07-23, follow-up on interval drift). Because it
# exits every time, do NOT add KeepAlive to the plist -- that key is for a
# job meant to keep running.
#
# Usage:
#   scripts/s007_loop_install.sh install    # copy the plist in, load it
#   scripts/s007_loop_install.sh uninstall  # unload it, remove the plist
#   scripts/s007_loop_install.sh status     # launchctl list + tail launchd logs
set -euo pipefail

LABEL="com.anton.algo.s007bot"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO_DIR/scripts/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

case "${1:-}" in
  install)
    echo "Before installing: make sure the OLD bash loop is not still running --"
    echo "  pkill -f s007_loop.sh   (only if 'ps aux | grep s007_loop.sh' shows it)"
    echo "Running both at once would double-poll the broker."
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load "$PLIST_DST"
    echo "Installed and loaded $LABEL (ticks every wall-clock minute). Check with: $0 status"
    ;;
  uninstall)
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "Unloaded and removed $LABEL. The bot is no longer scheduled/auto-started."
    ;;
  status)
    launchctl list | grep "$LABEL" || echo "$LABEL is not loaded"
    echo "--- last 20 lines of launchd stdout ---"
    tail -n 20 "$REPO_DIR/reports/logs/S007/launchd.out.log" 2>/dev/null || echo "(no output yet)"
    echo "--- last 20 lines of launchd stderr ---"
    tail -n 20 "$REPO_DIR/reports/logs/S007/launchd.err.log" 2>/dev/null || echo "(no output yet)"
    echo "--- most recent loop_heartbeat / loop_settled events today ---"
    today_file="$REPO_DIR/reports/logs/S007/events-$(date +%Y-%m-%d).jsonl"
    grep -E '"kind": "loop_(heartbeat|settled)"' "$today_file" 2>/dev/null | tail -n 5 \
      || echo "(none yet today)"
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}"
    exit 1
    ;;
esac
