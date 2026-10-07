#!/usr/bin/env bash
# One-command deploy of the MQL5 code into a MetaTrader 5 terminal (macOS / Linux):
#
#   1. regenerate the GENERATED headers from the Python source of truth (gen_params)
#   2. run the mt5 pytest suite                          (skip: --skip-tests)
#   3. refresh the self-test fixtures of the selected strategies if stale
#   4. copy sources + presets + fixtures into the terminal (mt5/tools/install_mac.sh)
#   5. compile the selected .mq5 with MetaEditor (CLI, through Wine) and fail on errors
#   6. --watch: repeat 1-5 whenever a source file changes (Ctrl+C to stop)
#
# Usage (repo root or anywhere):
#   mt5/tools/deploy.sh                 # every expert and script we own
#   mt5/tools/deploy.sh S004            # only what matches "S004"
#   mt5/tools/deploy.sh S004_FVG S004_SelfTest       # several, by name
#   mt5/tools/deploy.sh Experts/AlgoTrading/S021_ORB.mq5   # or by path under MQL5
#   mt5/tools/deploy.sh --watch         # auto-deploy on every change
#   mt5/tools/deploy.sh --skip-tests --no-compile
#   mt5/tools/deploy.sh --mql5 <MQL5 dir> --common <Common/Files dir>   # passed to install_mac.sh
#
# WHY THE SELECTOR EXISTS. Step 5 writes the .ex5 a running EA was loaded from.
# Rebuilding every program is right on a fresh machine and wrong on an account
# that is trading: it would replace the binary under the live expert with
# whatever the working tree currently holds. Name what you are deploying
# whenever another strategy is live on the same terminal. Steps 1-4 are safe
# either way -- install_mac.sh syncs sources with --exclude '*.ex5'.
#
# Environment overrides (autodetected when unset):
#   MT5_WINE        wine binary   (macOS: bundled in "MetaTrader 5.app"; Linux: `wine` on PATH)
#   MT5_WINEPREFIX  wine prefix   (macOS: ~/Library/Application Support/net.metaquotes.wine.metatrader5;
#                                  Linux: ~/.mt5, the MetaQuotes Linux installer default)
#   MT5_PYTHON      python to use (default: .venv/bin/python, else python3)
#
# Windows: use mt5/tools/deploy.ps1 instead.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
MT5="$REPO/mt5"
WATCH=0
RUN_TESTS=1
COMPILE=1
INSTALL_ARGS=()
SELECTORS=""   # newline-separated name fragments; empty means "everything"
WATCH_INTERVAL_SECONDS=3
COMPILE_TIMEOUT_SECONDS=180
TERMINAL_SUBDIR="drive_c/Program Files/MetaTrader 5"
OWN_SOURCES=("Experts/AlgoTrading" "Scripts/AlgoTrading")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --watch)      WATCH=1; shift ;;
    --skip-tests) RUN_TESTS=0; shift ;;
    --no-compile) COMPILE=0; shift ;;
    --mql5|--common) INSTALL_ARGS+=("$1" "$2"); shift 2 ;;
    --link)       INSTALL_ARGS+=("$1"); shift ;;
    --only)       SELECTORS+="$2"$'\n'; shift 2 ;;
    -h|--help)    sed -n '2,36p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) SELECTORS+="$1"$'\n'; shift ;;
  esac
done

# macOS has no `timeout` by default (brew coreutils installs it as gtimeout)
with_timeout() {
  local seconds="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$seconds" "$@";
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$seconds" "$@";
  else "$@"; fi
}

log()  { printf '\033[1m[deploy]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[deploy] FAILED:\033[0m %s\n' "$*" >&2; return 1; }

python_bin() {
  if [[ -n "${MT5_PYTHON:-}" ]]; then echo "$MT5_PYTHON";
  elif [[ -x "$REPO/.venv/bin/python" ]]; then echo "$REPO/.venv/bin/python";
  else echo python3; fi
}

# True when the string matches any selector (case-insensitive substring), or
# when nothing was selected at all. "S004", "s004_fvg" and the full
# "Experts/AlgoTrading/S004_FVG.mq5" are all valid ways to name one program.
matches_selection() {
  [[ -z "$SELECTORS" ]] && return 0
  local sel rc=1
  shopt -s nocasematch
  while IFS= read -r sel; do
    [[ -n "$sel" && "$1" == *"$sel"* ]] && { rc=0; break; }
  done <<<"$SELECTORS"
  shopt -u nocasematch
  return $rc
}

# Every .mq5 we own, as a path relative to MQL5/, narrowed to the selection.
selected_sources() {
  local dir file rel
  for dir in "${OWN_SOURCES[@]}"; do
    while IFS= read -r file; do
      rel="${file#"$MT5/MQL5/"}"
      matches_selection "$rel" && echo "$rel"
    done < <(find "$MT5/MQL5/$dir" -name '*.mq5' 2>/dev/null | sort)
  done
}

# True when the resolved set of programs contains one whose path mentions the
# given strategy. Asking selected_sources (and not the raw selectors) keeps it
# correct whichever way the user named the program -- "S004", "s004_fvg" or
# "Experts/AlgoTrading/S004_FVG.mq5".
selection_has() { grep -qi -- "$1" <<<"$(selected_sources)"; }

# A typo in a selector must not silently deploy nothing (or, worse, read as
# "no selection" somewhere downstream and deploy everything).
check_selection() {
  local sel
  [[ -z "$SELECTORS" ]] && return 0
  while IFS= read -r sel; do
    [[ -z "$sel" ]] && continue
    if [[ -z "$( SELECTORS="$sel"$'\n'; selected_sources )" ]]; then
      echo "no .mq5 matches '$sel'; available:" >&2
      ( SELECTORS=""; selected_sources ) | sed 's/^/  /' >&2
      exit 2
    fi
  done <<<"$SELECTORS"
  log "selection: $(selected_sources | tr '\n' ' ')"
}

detect_prefix() {
  if [[ -n "${MT5_WINEPREFIX:-}" ]]; then echo "$MT5_WINEPREFIX"; return; fi
  local mac="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5"
  if [[ -d "$mac/$TERMINAL_SUBDIR" ]]; then echo "$mac"; return; fi
  if [[ -d "$HOME/.mt5/$TERMINAL_SUBDIR" ]]; then echo "$HOME/.mt5"; return; fi
  echo ""
}

detect_wine() {
  if [[ -n "${MT5_WINE:-}" ]]; then echo "$MT5_WINE"; return; fi
  local app="/Applications/MetaTrader 5.app"
  if [[ -d "$app" ]]; then
    local found
    for name in wine64 wine wineloader; do
      found="$(find "$app" -type f -name "$name" -perm -u+x 2>/dev/null | head -1 || true)"
      if [[ -n "$found" ]]; then echo "$found"; return; fi
    done
  fi
  command -v wine64 2>/dev/null || command -v wine 2>/dev/null || echo ""
}

step_generate() {
  local py; py="$(python_bin)"
  log "1/5 generate headers ($py -m mt5.tools.gen_params)"
  (cd "$REPO" && "$py" -m mt5.tools.gen_params >/dev/null) || fail "gen_params"
}

step_tests() {
  [[ $RUN_TESTS -eq 1 ]] || { log "2/5 tests skipped"; return 0; }
  local py; py="$(python_bin)"
  log "2/5 pytest tests/mt5"
  (cd "$REPO" && "$py" -m pytest tests/mt5 -q -p no:cacheprovider) || fail "tests/mt5"
}

# True when target is missing, or when any source is newer than it.
is_stale() {
  local target="$1"; shift
  [[ -f "$target" ]] || return 0
  local src
  for src in "$@"; do
    [[ -f "$src" && "$src" -nt "$target" ]] && return 0
  done
  return 1
}

step_fixtures() {
  local py; py="$(python_bin)"
  local files="$MT5/MQL5/Files/AlgoTrading"
  log "3/5 self-test fixtures"
  # Expectations bake in the session clock and the strategy params, so a change to
  # either must invalidate them. A stale fixture is worse than a missing one: it keeps
  # asserting the previous behaviour and the self-test fails for no obvious reason.
  local s021_srcs=("$REPO/mt5/tools/s021_fixtures.py" "$REPO/mt5/tools/clock.py"
                   "$REPO/strategies/orb_intraday/config.py" "$REPO/strategies/orb_intraday/engine.py")
  local s004_srcs=("$REPO/mt5/tools/s004_fixtures.py" "$REPO/mt5/tools/clock.py"
                   "$REPO/strategies/s004_config.py" "$REPO/strategies/fvg_mtf.py"
                   "$REPO/backtest/s004_metalabel_data.py")

  if selection_has "S021"; then
    if is_stale "$files/fixtures/s021_m1.csv" "${s021_srcs[@]}"; then
      log "    S021 missing or stale -> generating"
      (cd "$REPO" && "$py" -m mt5.tools.s021_fixtures) || fail "s021_fixtures"
    else
      log "    S021 up to date"
    fi
    if is_stale "$files/e2e/s021_m1.csv" "${s021_srcs[@]}"; then
      log "    tester history missing or stale -> generating (2025-01-02..2026-09-30)"
      (cd "$REPO" && "$py" -m mt5.tools.s021_fixtures --start 2025-01-02 --end 2026-09-30 \
          --out "$files/e2e") || fail "e2e history"
      log "    NOTE: re-run ImportM1CustomSymbol in MT5, the offline tester symbol is now stale"
    fi
  fi

  if selection_has "S004"; then
    if is_stale "$files/fixtures/s004_m15.csv" "${s004_srcs[@]}"; then
      log "    S004 missing or stale -> generating"
      (cd "$REPO" && "$py" -m mt5.tools.s004_fixtures) || fail "s004_fixtures"
    else
      log "    S004 up to date"
    fi
  fi
}

step_install() {
  log "4/5 install into the terminal"
  bash "$MT5/tools/install_mac.sh" "${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}" || fail "install_mac.sh"
}

compile_one() {
  # $1 = path relative to MQL5 (forward slashes)
  local wine="$1" prefix="$2" rel="$3"
  local terminal="$prefix/$TERMINAL_SUBDIR"
  local name; name="$(basename "$rel" .mq5)"
  # path RELATIVE to the terminal folder: an absolute C:\... path or /inc: makes
  # MetaEditor skip the job or mis-resolve <AlgoCore/...> includes (verified)
  local win_src="MQL5\\${rel//\//\\}"
  local win_log="C:\\algotrading_compile_${name}.log"
  local unix_log="$prefix/drive_c/algotrading_compile_${name}.log"
  rm -f "$unix_log"
  (cd "$terminal" && WINEPREFIX="$prefix" WINEDEBUG=-all \
     with_timeout "$COMPILE_TIMEOUT_SECONDS" "$wine" MetaEditor64.exe /portable \
     /compile:"$win_src" /log:"$win_log" \
     >/dev/null 2>&1) || true
  if [[ ! -f "$unix_log" ]]; then
    echo "  ??   $rel: no compiler log (MetaEditor did not run)"; return 2
  fi
  local result
  result="$(iconv -f UTF-16LE -t UTF-8 "$unix_log" 2>/dev/null | tr -d '\r' | grep -E '^Result:' | tail -1)"
  if [[ "$result" == *" 0 errors"* ]]; then
    echo "  ok   $rel  ($result)"
  else
    echo "  FAIL $rel  (${result:-no result line})"
    iconv -f UTF-16LE -t UTF-8 "$unix_log" 2>/dev/null | tr -d '\r' | grep -E ' error | warning ' | head -20
    return 1
  fi
}

step_compile() {
  [[ $COMPILE -eq 1 ]] || { log "5/5 compile skipped"; return 0; }
  local prefix wine
  prefix="$(detect_prefix)"; wine="$(detect_wine)"
  if [[ -z "$prefix" || -z "$wine" ]]; then
    log "5/5 cannot compile from the command line (wine='$wine' prefix='$prefix')."
    log "    Set MT5_WINE / MT5_WINEPREFIX, or compile in MetaEditor with F7."
    return 0
  fi
  log "5/5 compile with MetaEditor (wine: $wine)"
  local status=0 rel
  while IFS= read -r rel; do
    compile_one "$wine" "$prefix" "$rel" || status=$?
  done < <(selected_sources)
  if [[ $status -eq 2 ]]; then
    log "    MetaEditor CLI did not produce logs on this machine -- compile in MetaEditor with F7."
    return 0
  fi
  [[ $status -eq 0 ]] || fail "compilation errors (see above)"
}

deploy_once() {
  local started; started="$(date +%s)"
  step_generate && step_tests && step_fixtures && step_install && step_compile || return 1
  log "done in $(( $(date +%s) - started ))s. Reload in MT5: Navigator -> right click -> Refresh."
}

watch_fingerprint() {
  # mtime+size of every input the deploy depends on
  find "$MT5/MQL5/Include" "$MT5/MQL5/Experts" "$MT5/MQL5/Scripts" "$MT5/MQL5/Presets" \
       "$MT5/tools" "$REPO/strategies/orb_intraday/config.py" "$REPO/bot/risk.py" \
       "$REPO/bot/account_guard.py" "$REPO/bot/orb_config.py" \
       "$REPO/strategies/s004_config.py" "$REPO/strategies/fvg_mtf.py" \
       -type f \( -name '*.mq5' -o -name '*.mqh' -o -name '*.set' -o -name '*.py' -o -name '*.sh' \) \
       ! -path '*/GeneratedCore.mqh' ! -path '*/Params.mqh' ! -path '*__pycache__*' \
       -exec ls -l {} + 2>/dev/null | awk '{print $5, $6, $7, $8, $NF}' | sort | cksum
}

check_selection

if [[ $WATCH -eq 0 ]]; then
  deploy_once
  exit $?
fi

log "watch mode: deploying now, then on every change (poll ${WATCH_INTERVAL_SECONDS}s, Ctrl+C to stop)"
deploy_once || true
last="$(watch_fingerprint)"
while true; do
  sleep "$WATCH_INTERVAL_SECONDS"
  current="$(watch_fingerprint)"
  if [[ "$current" != "$last" ]]; then
    log "change detected -> redeploy"
    deploy_once || log "deploy failed -- fix and save again"
    last="$(watch_fingerprint)"
  fi
done
