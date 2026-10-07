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
# The sandbox is built from the REPO: sources are copied in and compiled
# there (mt5/tools/compile.sh shares the primitive), so what runs is what is
# checked in, and no deploy to the trading terminal is needed first. Fixtures
# come from the repo too.
#
# Env: MT5_WINE, MT5_WINEPREFIX -- same meaning as in deploy.sh.
set -uo pipefail
set +m          # no "Terminated" job notices when the sandbox is stopped

SCRIPT_NAME="${1:-S004_SelfTest}"
[[ "${1:-}" == --* ]] && SCRIPT_NAME="S004_SelfTest"
KEEP=0
for arg in "$@"; do [[ "$arg" == "--keep" ]] && KEEP=1; done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=mt5/tools/_mt5_env.sh
. "$HERE/_mt5_env.sh" || exit 2
REPO="$MT5_REPO"
terminal="$MT5_TERMINAL"
prefix="$MT5_PREFIX"
wine="$MT5_WINE_BIN"
common="$MT5_COMMON"
SANDBOX_NAME="algotrading_selftest"
SANDBOX_SUBDIR="drive_c/$SANDBOX_NAME"
RESULT_SUBPATH="AlgoTrading/selftest/${SCRIPT_NAME}.json"
TIMEOUT_SECONDS=600
POLL_SECONDS=3

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
# A sandbox terminal left over from an interrupted run still holds the folder,
# and MT5 silently drops the second instance -- so the run would just time out.
# Wine's preloader ignores the first TERM often enough to need the follow-up.
if ! mt5_stop_sandbox "$SANDBOX_NAME"; then
  echo "a sandbox terminal from an earlier run will not die -- kill it by hand" >&2
  exit 2
fi
rm -rf "$sandbox"
mkdir -p "$sandbox/MQL5"
cp "$terminal/terminal64.exe" "$terminal/MetaEditor64.exe" "$sandbox/"
cp -R "$terminal/config" "$sandbox/"
cp -R "$terminal/MQL5/Include" "$sandbox/MQL5/"      # the standard library
cp -R "$REPO/mt5/MQL5/Include/." "$sandbox/MQL5/Include/"
mkdir -p "$sandbox/MQL5/Scripts"
cp -R "$REPO/mt5/MQL5/Scripts/." "$sandbox/MQL5/Scripts/"
# build the self-test from the repo, so a stale .ex5 can never be what passes
mt5_compile_in "$sandbox" "$SANDBOX_NAME" "Scripts/AlgoTrading/${SCRIPT_NAME}.mq5" || {
  mt5_stop_sandbox "$SANDBOX_NAME"; rm -rf "$sandbox"; exit 1; }
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

for pid in $(pgrep -f "$SANDBOX_NAME" 2>/dev/null || true); do kill "$pid" 2>/dev/null; done
mt5_stop_sandbox "$SANDBOX_NAME" || echo "warning: a sandbox terminal is still running" >&2
[[ $KEEP -eq 1 ]] || rm -rf "$sandbox"
exit $status
