#!/usr/bin/env bash
# Deploy the repo's MQL5 sources into a MetaTrader 5 terminal (macOS app or any
# Wine/Windows install) and refresh the self-test fixtures.
#
#   mt5/tools/install_mac.sh                      # autodetect the macOS MT5 app
#   mt5/tools/install_mac.sh --mql5 <dir> --common <dir> [--link]
#
# Default mode COPIES the repo folders into the terminal: the macOS (Wine) MT5
# app does not list Expert Advisors that live behind a symlink (scripts work,
# EAs do not show up in the Navigator / Strategy Tester). Rerun after every
# code update, then recompile in MetaEditor (F7) -- compiled .ex5 files are
# never copied and are kept in the terminal between runs.
# --link symlinks instead (Windows / terminals that follow symlinks).
#
# Also deploys the EA input presets (mt5/MQL5/Presets/AlgoTrading/*.set) into
# MQL5/Presets (the "Load" button of the EA inputs dialog) and
# MQL5/Profiles/Tester (the "Load" item of the Strategy Tester inputs tab).
#
# Only our own subfolders are touched -- never the terminal's stock files:
#   MQL5/Include/AlgoCore, MQL5/Include/Strategies,
#   MQL5/Experts/AlgoTrading, MQL5/Scripts/AlgoTrading,
#   <Common>/Files/AlgoTrading/fixtures, <Common>/Files/AlgoTrading/e2e
set -euo pipefail

REPO_MT5="$(cd "$(dirname "$0")/.." && pwd)"
MQL5_DIR=""
COMMON_DIR=""
MODE="copy"
MAC_PREFIX="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mql5)   MQL5_DIR="$2"; shift 2 ;;
    --common) COMMON_DIR="$2"; shift 2 ;;
    --copy)   MODE="copy"; shift ;;
    --link)   MODE="link"; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$MQL5_DIR" ]]; then
  MQL5_DIR="$(find "$MAC_PREFIX" -maxdepth 4 -type d -name MQL5 -path '*MetaTrader 5*' 2>/dev/null | head -1 || true)"
fi
if [[ -z "$COMMON_DIR" ]]; then
  COMMON_DIR="$(find "$MAC_PREFIX/users" -maxdepth 8 -type d -path '*MetaQuotes/Terminal/Common/Files' 2>/dev/null | head -1 || true)"
fi
if [[ -z "$MQL5_DIR" || ! -d "$MQL5_DIR" ]]; then
  echo "MQL5 data folder not found -- pass --mql5 (MetaEditor: File > Open Data Folder)." >&2
  exit 1
fi
echo "MQL5 folder:   $MQL5_DIR"
echo "Common/Files:  ${COMMON_DIR:-<not found, fixtures skipped>}"

# rsync when available (keeps compiled .ex5 in the terminal, removes deleted
# sources); plain cp otherwise (minimal Linux images)
sync_tree() {
  local source="$1" target="$2"
  mkdir -p "$target"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude '*.ex5' "$source/" "$target/"
  else
    cp -R "$source/." "$target/"
    find "$target" -name '*.ex5' -newer "$target" -path "$source*" -delete 2>/dev/null || true
  fi
}

deploy() {
  local source="$1" target="$2"
  mkdir -p "$(dirname "$target")"
  if [[ -L "$target" ]]; then rm "$target"; fi
  if [[ "$MODE" == "link" ]]; then
    if [[ -e "$target" ]]; then
      echo "refusing to replace a real folder with a link: $target (use --copy, or move it away)" >&2
      exit 1
    fi
    ln -s "$source" "$target"
  else
    sync_tree "$source" "$target"
  fi
  echo "  $MODE  $target"
}

deploy "$REPO_MT5/MQL5/Include/AlgoCore"         "$MQL5_DIR/Include/AlgoCore"
deploy "$REPO_MT5/MQL5/Include/Strategies"       "$MQL5_DIR/Include/Strategies"
deploy "$REPO_MT5/MQL5/Experts/AlgoTrading"      "$MQL5_DIR/Experts/AlgoTrading"
deploy "$REPO_MT5/MQL5/Scripts/AlgoTrading"      "$MQL5_DIR/Scripts/AlgoTrading"

PRESETS="$REPO_MT5/MQL5/Presets/AlgoTrading"
if [[ -d "$PRESETS" ]]; then
  for target in "$MQL5_DIR/Presets" "$MQL5_DIR/Profiles/Tester"; do
    mkdir -p "$target"
    cp "$PRESETS"/*.set "$target/"
  done
  echo "  copy  presets ($(ls "$PRESETS"/*.set | wc -l | tr -d ' ') .set) -> MQL5/Presets, MQL5/Profiles/Tester"
fi

FILES="$REPO_MT5/MQL5/Files/AlgoTrading"
if [[ -n "$COMMON_DIR" && -d "$FILES" ]]; then
  for sub in fixtures e2e; do
    if [[ -d "$FILES/$sub" ]]; then
      mkdir -p "$COMMON_DIR/AlgoTrading/$sub"
      cp -R "$FILES/$sub/." "$COMMON_DIR/AlgoTrading/$sub/"
      echo "  copy  $sub -> $COMMON_DIR/AlgoTrading/$sub"
    fi
  done
elif [[ ! -d "$FILES" ]]; then
  echo "  (no fixtures yet: python -m mt5.tools.s021_fixtures)"
fi
echo "Done. Compile in MetaEditor: Experts/AlgoTrading/S021_ORB.mq5, Scripts/AlgoTrading/*.mq5"
