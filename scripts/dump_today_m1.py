"""One-off diagnostic: dump today's M1 bars for GER40/DE40 to a CSV so we can
verify the 0.5-level cross / stop placement for a specific live day by eye.
Does NOT touch any live-trading code path -- read-only broker call, output
goes to reports/, not into any bot decision. Safe to delete after use.

Fix vs. first version: resolve_symbol() and get_m1() each open their own
Twisted reactor session (bot/ctrader.py::_run() calls reactor.run(), which
can only run ONCE per process -- see scripts/s007_tick.py's docstring and
ctrader_s007.py::run_live_cycle for the same constraint). Calling both in one
script triggers ReactorNotRestartable on the second call. Fix: skip
resolve_symbol() -- we already know today's resolved symbol from the bot's
own event log ("symbol": "DE40") -- so this script makes exactly one _run().

Usage (run in your own terminal, needs network + venv):
    python3 scripts/dump_today_m1.py
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.ctrader_s007 import CTraderS007

SYMBOL = "DE40"  # from today's events-2026-07-30.jsonl state events; skip resolve_symbol()

api = CTraderS007()
m1 = api.get_m1(SYMBOL, days=2)
out = ROOT / "reports" / "tmp_m1_today.csv"
out.parent.mkdir(parents=True, exist_ok=True)
m1.to_csv(out)
print(f"symbol={SYMBOL}  bars={len(m1)}  saved -> {out}")
print(m1.tail(5))
