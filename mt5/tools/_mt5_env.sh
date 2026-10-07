#!/usr/bin/env bash
# Shared environment discovery for the MT5 helper scripts (ALGODEV-62).
#
# Source it, do not run it:  . "$(dirname "$0")/_mt5_env.sh"
#
# Exports: MT5_PREFIX (wine prefix), MT5_TERMINAL (the terminal folder that is
# trading -- read from, never written to by the sandboxed helpers),
# MT5_WINE_BIN, MT5_COMMON (the Files folder FILE_COMMON maps to), MT5_REPO.
# Also defines mt5_stop_sandbox <name-fragment>.

MT5_TERMINAL_SUBDIR="drive_c/Program Files/MetaTrader 5"

mt5_resolve_env() {
  local here repo
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo="$(cd "$here/../.." && pwd)"

  local prefix="${MT5_WINEPREFIX:-$HOME/Library/Application Support/net.metaquotes.wine.metatrader5}"
  [[ -d "$prefix/$MT5_TERMINAL_SUBDIR" ]] || prefix="$HOME/.mt5"
  local terminal="$prefix/$MT5_TERMINAL_SUBDIR"

  local wine="${MT5_WINE:-}"
  if [[ -z "$wine" ]]; then
    wine="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64"
    [[ -x "$wine" ]] || wine="$(command -v wine64 || command -v wine || true)"
  fi
  if [[ ! -d "$terminal" || ! -x "$wine" ]]; then
    echo "no terminal ($terminal) or wine ($wine); set MT5_WINEPREFIX / MT5_WINE" >&2
    return 2
  fi

  # Wine maps FILE_COMMON to the Wine user's own AppData, and a prefix often
  # carries a stale "user" profile next to the real one -- prefer $USER's.
  local common="" candidate
  for candidate in "$prefix/drive_c/users/$USER/AppData/Roaming/MetaQuotes/Terminal/Common/Files" \
                   $(find "$prefix/drive_c/users" -maxdepth 8 -type d -path '*MetaQuotes/Terminal/Common/Files' 2>/dev/null); do
    [[ -d "$candidate" ]] && { common="$candidate"; break; }
  done

  MT5_REPO="$repo"; MT5_PREFIX="$prefix"; MT5_TERMINAL="$terminal"
  MT5_WINE_BIN="$wine"; MT5_COMMON="$common"
  export MT5_REPO MT5_PREFIX MT5_TERMINAL MT5_WINE_BIN MT5_COMMON
}

# Kill anything still holding a sandbox folder. The wine process reports its
# WINDOWS path in ps (C:\<name>\...), so match the folder name, not a unix path.
mt5_stop_sandbox() {
  local name="$1" attempt pids pid
  for attempt in 1 2 3; do
    pids="$(pgrep -f "$name" 2>/dev/null || true)"
    [[ -z "$pids" ]] && return 0
    for pid in $pids; do
      if [[ $attempt -eq 1 ]]; then kill "$pid" 2>/dev/null; else kill -9 "$pid" 2>/dev/null; fi
    done
    sleep 3
  done
  [[ -z "$(pgrep -f "$name" 2>/dev/null || true)" ]]
}

# Build one .mq5 inside a sandbox that already holds MetaEditor64.exe and the
# sources. Prints the compiler's own one-line verdict; returns non-zero on any
# error. MetaEditor's exit code is not reliable, and its log is UTF-16LE.
#   mt5_compile_in <sandbox-dir> <sandbox-name> <path under MQL5>
mt5_compile_in() {
  local sandbox="$1" name="$2" rel="$3"
  local log_name="compile_$(basename "$rel" .mq5).log"
  local log="$sandbox/$log_name"
  rm -f "$log"
  (cd "$sandbox" && WINEPREFIX="$MT5_PREFIX" WINEDEBUG=-all "$MT5_WINE_BIN" MetaEditor64.exe \
     /portable /compile:"MQL5\\${rel//\//\\}" /log:"C:\\$name\\$log_name" >/dev/null 2>&1) || true
  if [[ ! -f "$log" ]]; then
    echo "FAIL $rel -- MetaEditor wrote no log"
    return 1
  fi
  local text result
  text="$(iconv -f UTF-16LE -t UTF-8 "$log" | tr -d '\r')"
  result="$(grep -E '^Result:' <<<"$text" | tail -1)"
  if grep -qE '^Result: 0 error' <<<"$result"; then
    echo "ok   $rel -- ${result#Result: }"
    return 0
  fi
  echo "FAIL $rel -- ${result:-no Result line}"
  grep -E ' : (error|warning) ' <<<"$text" | head -20
  return 1
}

mt5_resolve_env || return 2
