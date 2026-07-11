#!/usr/bin/env bash
# Download fresh M15 data (2022-03-05 .. now) from Dukascopy for the core pairs.
# Requires Node.js (npx). Run from the repo root:  bash scripts/fetch_fresh.sh
set -e
mkdir -p data/fresh
for sym in eurusd gbpusd usdjpy usdchf audusd eurjpy gbpjpy; do
  echo "=== $sym ==="
  npx -y dukascopy-node -i "$sym" -from 2022-03-05 -to now -t m15 -f csv -v true -dir data/fresh
done
echo "Done. Now run: python scripts/convert_dukascopy.py"
