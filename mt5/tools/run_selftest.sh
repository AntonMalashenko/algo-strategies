#!/usr/bin/env bash
# Run an MQL5 self-test script headlessly, without touching the terminal that
# is trading (ALGODEV-62 phase C).
#
#   mt5/tools/run_selftest.sh [S004_SelfTest|S021_SelfTest] [--keep]
#
# Why a sandbox: MT5 allows one instance per data folder, so a `/config:` run
# against the live data folder is silently dropped while the trading terminal
# holds it. This copies terminal64.exe, config/ and MQL5/{Include,Scripts} into
# a scratch folder, starts it with /portable, auto-trading and experts OFF and
# no profiles (so no chart EA can load and place an order), runs the script
# from [StartUp] and reads the JSON the script leaves in <Common>/Files.
#
# Compile first (mt5/tools/deploy.sh, or MetaEditor F7): this runs the .ex5
# already in the live data folder. Fixtures are taken from the repo.
#
# Env: MT5_WINE, MT5_WINEPREFIX -- same meaning as in deploy.sh.
set -uo pipefail

SCRIPT_NAME="${1:-S004_SelfTest}"
[[ "${1:-}" == --* ]] && SCRIPT_NAME="S004_SelfTest"
KEEP=0
for arg in "$@"; do [[ "$arg" == "--keep" ]] && KEEP=1; done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TERMINAL_SUBDIR="drive_c/Program Files/MetaTrader 5"
SANDBOX_SUBDIR="drive_c/algotrading_selftest"
RESULT_SUBPATH="AlgoTrading/selftest/${SCRIPT_NAME}.json"
TIMEOUT_SECONDS=300
POLL_SECONDS=3

prefix="${MT5_WINEPREFIX:-$HOME/Library/Application Support/net.metaquotes.wine.metatrader5}"
[[ -d "$prefix/$TERMINAL_SUBDIR" ]] || prefix="$HOME/.mt5"
terminal="$prefix/$TERMINAL_SUBDIR"
wine="${MT5_WINE:-}"
if [[ -z "$wine" ]]; then
  wine="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64"
  [[ -x "$wine" ]] || wine="$(command -v wine64 || command -v wine || true)"
fi
if [[ ! -d "$terminal" || ! -x "$wine" ]]; then
  echo "no terminal ($terminal) or wine ($wine); set MT5_WINEPREFIX / MT5_WINE" >&2
  exit 2
fi

# Wine maps FILE_COMMON to the Wine user's own AppData, and a prefix often
# carries a stale "user" profile next to the real one -- prefer $USER's.
common=""
for candidate in "$prefix/drive_c/users/$USER/AppData/Roaming/MetaQuotes/Terminal/Common/Files" \
                 $(find "$prefix/drive_c/users" -maxdepth 8 -type d -path '*MetaQuotes/Terminal/Common/Files' 2>/dev/null); do
  [[ -d "$candidate" ]] && { common="$candidate"; break; }
done
if [[ -z "$common" ]]; then
  echo "Common/Files not found under $prefix -- start the terminal once" >&2
  exit 2
fi

echo "terminal: $terminal"
echo "common:   $common"

# fixtures: repo is the source of truth, the terminal only reads them
if [[ -d "$REPO/mt5/MQL5/Files/AlgoTrading/fixtures" ]]; then
  mkdir -p "$common/AlgoTrading/fixtures"
  cp -R "$REPO/mt5/MQL5/Files/AlgoTrading/fixtures/." "$common/AlgoTrading/fixtures/"
fi

sandbox="$prefix/$SANDBOX_SUBDIR"
# a sandbox terminal left over from an interrupted run still holds the folder,
# and MT5 silently drops the second instance -- clear it before rebuilding
for pid in $(pgrep -f "$SANDBOX_SUBDIR" 2>/dev/null || true); do kill "$pid" 2>/dev/null; done
sleep 2
rm -rf "$sandbox"
mkdir -p "$sandbox/MQL5"
cp "$terminal/terminal64.exe" "$sandbox/"
cp -R "$terminal/config" "$sandbox/"
cp -R "$terminal/MQL5/Include" "$terminal/MQL5/Scripts" "$sandbox/MQL5/"
# no profiles/ and no Experts: nothing can attach to a chart and trade
printf '[Common]\nNewsEnable=false\n[Experts]\nAllowLiveTrading=false\nEnabled=false\nAccount=false\nProfile=false\n[StartUp]\nSymbol=EURUSD\nPeriod=M15\nScript=AlgoTrading\\%s\n' \
  "$SCRIPT_NAME" > "$sandbox/config/selftest.ini"

result="$common/$RESULT_SUBPATH"
rm -f "$result"
(cd "$sandbox" && WINEPREFIX="$prefix" WINEDEBUG=-all "$wine" terminal64.exe /portable \
   /config:"C:\\algotrading_selftest\\config\\selftest.ini" >/dev/null 2>&1 &)

status=1
for ((waited=0; waited<TIMEOUT_SECONDS; waited+=POLL_SECONDS)); do
  sleep "$POLL_SECONDS"
  [[ -f "$result" ]] || continue
  sleep "$POLL_SECONDS"                     # let the script finish writing
  echo "$SCRIPT_NAME: $(cat "$result")"
  grep -q '"failed":0' "$result" && status=0 || status=1
  break
done
[[ $status -eq 1 && ! -f "$result" ]] && echo "$SCRIPT_NAME: no result after ${TIMEOUT_SECONDS}s" >&2

for pid in $(pgrep -f "$SANDBOX_SUBDIR" 2>/dev/null || true); do kill "$pid" 2>/dev/null; done
[[ $KEEP -eq 1 ]] || rm -rf "$sandbox"
exit $status
