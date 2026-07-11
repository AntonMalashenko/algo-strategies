"""Paper-trading runner (S004 v1.0).

Dry-run mode works entirely from local CSVs and prints the desired order
set — use it to eyeball signals before wiring the broker:

    python -m bot.paper --dry-run                # latest local fresh data
    python -m bot.paper --dry-run --at "2026-06-25 03:00"

Live-paper mode (after bot/ctrader.py is implemented) reconciles the
broker state to the desired state every run; schedule it every 15 minutes:

    python -m bot.paper --live
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from bot import config as C
from bot.signals import desired_orders

ROOT = Path(__file__).resolve().parent.parent


def load_local_m15(sym: str) -> pd.DataFrame:
    """Latest local data (fresh file preferred) in raw points."""
    base = ROOT / "data" / "raw" / sym
    path = base / f"{sym}m15fresh.csv"
    if not path.exists():
        path = base / f"{sym}m15.csv"
    d = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
    d = d.rename(columns={"tick_volume": "volume"})
    for c in ["open", "high", "low", "close"]:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d.dropna(subset=["close"])[["open", "high", "low", "close", "volume"]]


def dry_run(at: str | None):
    out = []
    for sym in C.PAIRS:
        m15 = load_local_m15(sym)
        if at:
            m15 = m15.loc[:pd.Timestamp(at)]
        m15 = m15.tail(C.HISTORY_DAYS * 96)
        res = desired_orders(m15, sym)
        out.append(dict(symbol=sym, in_window=res["in_window"],
                        has_position=res["position"] is not None,
                        orders=res["orders"]))
    n_orders = sum(len(x["orders"]) for x in out)
    print(f"as-of: {at or 'latest local bar'}  |  desired orders: {n_orders}")
    for x in out:
        for o in x["orders"]:
            print(f"  {o['symbol']:7} {o['side']:4} limit @{o['price']:.0f} "
                  f"SL {o['sl']:.0f} TP {o['tp']:.0f} risk {o['risk_points']:.0f}pt")
        if x["has_position"]:
            print(f"  {x['symbol']:7} [open position in engine state]")
    log_dir = ROOT / C.PAPER_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (log_dir / f"dryrun_{stamp}.json").write_text(json.dumps(out, default=str, indent=1))


def live():
    from bot.ctrader import CTraderAdapter   # noqa: F401
    raise SystemExit("live-paper loop: implement after cTrader adapter is wired")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--at", default=None, help="simulate 'now' at this timestamp")
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    if a.live:
        live()
    else:
        dry_run(a.at)
