"""Price data loading helpers.

Available sources:
  - load_yf():        download via yfinance and cache under ``data/raw/<instrument>/``.
  - load_csv():       read a local CSV file by explicit path or by filename from ``data/raw``.
  - synthetic_ohlc(): generate synthetic OHLC data for offline tests.

All loaders return a DataFrame with ``['open', 'high', 'low', 'close', 'volume']``
columns and a ``DatetimeIndex``.
"""
from __future__ import annotations

from pathlib import Path
import re

import numpy as np
import pandas as pd

DATA_RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
_COLS = ["open", "high", "low", "close", "volume"]
_TIMEFRAME_SUFFIX_RE = re.compile(r"^(?P<instrument>[A-Z0-9]+?)(?P<timeframe>[dmhw]\d+)$")


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: c.lower() for c in df.columns})
    for c in _COLS:
        if c not in df.columns:
            df[c] = np.nan
    df = df[_COLS].astype(float)
    df.index = pd.to_datetime(df.index)
    return df.dropna(subset=["close"]).sort_index()


def _instrument_folder_name(ticker: str) -> str:
    base = ticker.removesuffix("=X").replace("/", "")
    cleaned = "".join(ch for ch in base if ch.isalnum() or ch in {"-", "_"})
    return cleaned or "misc"


def _detect_instrument_from_stem(stem: str) -> str | None:
    if "_" in stem:
        prefix, suffix = stem.rsplit("_", 1)
        if prefix and any(ch.isdigit() for ch in suffix):
            return prefix

    match = _TIMEFRAME_SUFFIX_RE.fullmatch(stem)
    if match:
        return match.group("instrument")

    return None


def _build_cache_path(ticker: str, interval: str) -> Path:
    instrument = _instrument_folder_name(ticker)
    safe = ticker.replace("=", "").replace("/", "")
    return DATA_RAW / instrument / f"{safe}_{interval}.csv"


def resolve_raw_path(path: str | Path) -> Path:
    """Resolve a CSV path from an explicit path or a filename inside ``data/raw``."""
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate

    if candidate.exists():
        return candidate

    raw_candidate = DATA_RAW / candidate
    if raw_candidate.exists():
        return raw_candidate

    instrument = _detect_instrument_from_stem(candidate.stem)
    if instrument:
        nested_candidate = DATA_RAW / instrument / candidate.name
        if nested_candidate.exists():
            return nested_candidate

    matches = sorted(DATA_RAW.rglob(candidate.name))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        joined = ", ".join(str(match.relative_to(DATA_RAW)) for match in matches)
        raise FileNotFoundError(
            f"Ambiguous raw data reference '{candidate}'; matches: {joined}"
        )

    raise FileNotFoundError(f"Raw data file not found: {candidate}")


def load_yf(ticker: str = "EURUSD=X", start: str = "2005-01-01",
            end: str | None = None, interval: str = "1d",
            cache: bool = True) -> pd.DataFrame:
    """Download data via yfinance and optionally cache it under ``data/raw/<instrument>/``."""
    import yfinance as yf  # local import: only needed for live downloads

    df = yf.download(ticker, start=start, end=end, interval=interval,
                     auto_adjust=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = _normalize(df)
    if df.empty:
        raise RuntimeError(
            f"yfinance returned no data for {ticker}. Check the ticker, date range, or network."
        )
    if cache:
        out = _build_cache_path(ticker, interval)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out)
        print(f"Saved: {out} ({len(df)} bars, {df.index.min().date()}..{df.index.max().date()})")
    return df


def load_csv(path: str | Path) -> pd.DataFrame:
    """Read a local CSV file where the first column is the date/index."""
    path = resolve_raw_path(path)
    df = pd.read_csv(path, index_col=0)
    return _normalize(df)


def synthetic_ohlc(n: int = 252 * 8, seed: int = 7, start: str = "2015-01-01",
                   freq: str = "B") -> pd.DataFrame:
    """Generate synthetic OHLC data with alternating trend and range regimes."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=freq)
    # Regime drift: switch the sign and strength of the trend in blocks.
    drift = np.zeros(n)
    i = 0
    while i < n:
        block = rng.integers(40, 160)
        mu = rng.choice([0.0008, -0.0008, 0.0]) * rng.uniform(0.5, 1.5)
        drift[i:i + block] = mu
        i += block
    rets = drift + rng.normal(0, 0.006, n)
    close = 1.10 * np.exp(np.cumsum(rets))  # Start near the EUR/USD spot level.
    close = pd.Series(close, index=idx)
    # Reconstruct OHLC from close.
    noise = np.abs(rng.normal(0, 0.002, n))
    high = close * (1 + noise)
    low = close * (1 - noise)
    open_ = close.shift(1).fillna(close.iloc[0])
    vol = pd.Series(rng.integers(1000, 5000, n), index=idx)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol})


def load_fred(path: str | Path = None) -> pd.DataFrame:
    """Load FRED DEXUSEU (EUR/USD close-only) and expand it to an OHLC frame.

    Expected CSV format: ``observation_date,DEXUSEU`` with holiday gaps marked as ``.``.
    """
    path = resolve_raw_path(path or "DEXUSEU.csv")
    raw = pd.read_csv(path)
    raw.columns = ["date", "close"]
    raw["date"] = pd.to_datetime(raw["date"])
    raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
    raw = raw.dropna(subset=["close"]).set_index("date").sort_index()
    return pd.DataFrame({"open": raw["close"], "high": raw["close"],
                         "low": raw["close"], "close": raw["close"],
                         "volume": np.nan})
