"""Convert Dukascopy M15 CSVs (UTC, real prices) to the repo's ejtrader-style
format (broker/EET time, MT points) so the FVG engine and the Asia-session
definition stay consistent with the 2012-2022 research data.

Input:  data/fresh/<sym>-m15-*.csv  (from scripts/fetch_fresh.sh)
Output: data/raw/<SYM>/<SYM>m15fresh.csv  (Date,open,high,low,close,tick_volume)

Scaling: non-JPY pairs x1e5, JPY pairs x1e3 (=> 1 pip == 10 raw units, as in
the ejtrader files). Timezone: UTC -> Europe/Bucharest (EET/EEST) to
approximate MT broker server time; the DST edges may drift ~1h for a few
weeks a year -- acceptable for session bucketing.
"""
from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
JPY = {"USDJPY", "EURJPY", "GBPJPY", "AUDJPY"}


def convert(path: Path) -> None:
    sym = path.name.split("-")[0].upper()
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = "timestamp" if "timestamp" in df.columns else df.columns[0]
    ts = df[tcol]
    if pd.api.types.is_numeric_dtype(ts):
        idx = pd.to_datetime(ts, unit="ms", utc=True)
    else:
        idx = pd.to_datetime(ts, utc=True)
    df.index = idx.dt.tz_convert("Europe/Bucharest").dt.tz_localize(None)
    df.index.name = "Date"
    scale = 1e3 if sym in JPY else 1e5
    out = pd.DataFrame({
        "open": df["open"] * scale, "high": df["high"] * scale,
        "low": df["low"] * scale, "close": df["close"] * scale,
        "tick_volume": df.get("volume", 0),
    }).dropna(subset=["close"]).sort_index()
    dest = ROOT / "data" / "raw" / sym
    dest.mkdir(parents=True, exist_ok=True)
    out_path = dest / f"{sym}m15fresh.csv"
    out.to_csv(out_path)
    print(f"OK {sym}: {len(out)} bars {out.index.min()}..{out.index.max()} -> {out_path}")


if __name__ == "__main__":
    files = sorted(glob.glob(str(ROOT / "data" / "fresh" / "*m15*.csv")))
    if not files:
        print("No files in data/fresh. Run scripts/fetch_fresh.sh first.")
    for f in files:
        convert(Path(f))
