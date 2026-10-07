#!/usr/bin/env bash
# Compile MQL5 sources from the REPO, in a scratch copy of the terminal
# (ALGODEV-62 phase C).
#
#   mt5/tools/compile.sh [Experts/AlgoTrading/S021_ORB.mq5 ...]
#
# With no arguments it compiles every .mq5 under mt5/MQL5/{Experts,Scripts},
# which is the check that matters after touching a shared AlgoCore header: a
# change that only S004 needs must still leave S021 compiling.
#
# Why a scratch copy: the live terminal is trading. Compiling there would
# overwrite the .ex5 the running EA was loaded from, so this never writes into
# it -- it copies MetaEditor and the standard library out, overlays the repo's
# own Include/Experts/Scripts on top, and builds there. Nothing it produces is
# deployed; use mt5/tools/deploy.sh for that.
#
# Env: MT5_WINE, MT5_WINEPREFIX -- same meaning as in deploy.sh.
set -uo pipefail
set +m

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=mt5/tools/_mt5_env.sh
. "$HERE/_mt5_env.sh" || exit 2

SANDBOX_NAME="algotrading_compile"
sandbox="$MT5_PREFIX/drive_c/$SANDBOX_NAME"
src="$MT5_REPO/mt5/MQL5"

sources=("$@")
if [[ ${#sources[@]} -eq 0 ]]; then
  while IFS= read -r path; do sources+=("${path#"$src/"}"); done \
    < <(find "$src/Experts" "$src/Scripts" -name '*.mq5' 2>/dev/null | sort)
fi
[[ ${#sources[@]} -eq 0 ]] && { echo "nothing to compile" >&2; exit 2; }

mt5_stop_sandbox "$SANDBOX_NAME" || { echo "a stale compile sandbox will not die" >&2; exit 2; }
rm -rf "$sandbox"
mkdir -p "$sandbox/MQL5"
cp "$MT5_TERMINAL/MetaEditor64.exe" "$sandbox/"
# the standard library (<Trade/Trade.mqh> and friends) ships with the terminal
cp -R "$MT5_TERMINAL/MQL5/Include" "$sandbox/MQL5/"
# repo wins over whatever the live terminal happens to have deployed
cp -R "$src/Include/." "$sandbox/MQL5/Include/"
for dir in Experts Scripts Files; do
  [[ -d "$src/$dir" ]] && { mkdir -p "$sandbox/MQL5/$dir"; cp -R "$src/$dir/." "$sandbox/MQL5/$dir/"; }
done

status=0
for rel in "${sources[@]}"; do
  mt5_compile_in "$sandbox" "$SANDBOX_NAME" "$rel" || status=1
done

mt5_stop_sandbox "$SANDBOX_NAME" || echo "warning: a compile sandbox is still running" >&2
rm -rf "$sandbox"
exit $status
