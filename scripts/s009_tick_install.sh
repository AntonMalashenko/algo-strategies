#!/bin/bash
# Install/uninstall the launchd tick job for scripts/s009_tick.py.
#
# scripts/s009_tick.py is a stateless, one-shot check: launchd's
# StartCalendarInterval (an empty match dict = every wall-clock minute)
# invokes it, always -- it is NOT a long-running process, so there is
# nothing here for macOS to throttle or App Nap the way it did S007's old
# bash loop's ~17 hour sleep (see decisions-log.md 2026-07-23, and
# scripts/s009_tick.py's own docstring for why this applies to S009 too).
# This uses StartCalendarInterval rather than StartInterval on purpose:
# StartInterval schedules N seconds after the previous actual start, so any
# per-cycle overhead compounds into drift with no daily reset;
# StartCalendarInterval is pinned to the real wall clock every time, so it
# cannot drift. Because it exits every time, do NOT add KeepAlive to the
# plist -- that key is for a job meant to keep running.
#
# Usage:
#   scripts/s009_tick_install.sh install    # copy the plist in, load it
#   scripts/s009_tick_install.sh uninstall  # unload it, remove the plist
#   scripts/s009_tick_install.sh status     # launchctl list + tail launchd logs
set -euo pipefail

LABEL="com.algo.s009-paper"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO_DIR/deployment/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

case "${1:-}" in
  install)
    echo "Before installing: make sure 'bot/s009_paper.py --loop' is not still"
    echo "running in a terminal (ps aux | grep s009_paper) -- running both at"
    echo "once would double-process the same day."
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
    tail -n 20 "$REPO_DIR/reports/logs/S009/launchd.out.log" 2>/dev/null || echo "(no output yet)"
    echo "--- last 20 lines of launchd stderr ---"
    tail -n 20 "$REPO_DIR/reports/logs/S009/launchd.err.log" 2>/dev/null || echo "(no output yet)"
    echo "--- most recent loop_heartbeat / loop_settled events today ---"
    today_file="$REPO_DIR/reports/logs/S009/events-$(date +%Y-%m-%d).jsonl"
    grep -E '"kind": "loop_(heartbeat|settled)"' "$today_file" 2>/dev/null | tail -n 5 \
      || echo "(none yet today)"
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}"
    exit 1
    ;;
esac
