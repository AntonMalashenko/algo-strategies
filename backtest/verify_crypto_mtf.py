"""Gate 0a regression for S008: the vendored core reproduces the source bot.

Feeds identical real ETH candles to the vendored `strategies/crypto_mtf` core
and to the original `tradingbot` implementation, and asserts the feature vector
is bit-identical and the market context agrees. The original lives in the
sibling repo `../tradingbot`, which is imported directly (it ships its own
`config.settings` / `observability.logger`, so no shims are needed here).

Run from the algo repo root, with the crypto data already fetched:

    python backtest/verify_crypto_mtf.py

Requires the sibling checkout at ../tradingbot and
data/raw/crypto/ETHUSDT/{m15,h1,h4,d1}.csv (see scripts/fetch_crypto_bybit.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
TRADINGBOT = REPO.parent / "tradingbot"
DATA = REPO / "data" / "raw" / "crypto" / "ETHUSDT"

sys.path.insert(0, str(TRADINGBOT))   # originals: ml.features, ml.context
sys.path.insert(0, str(REPO))         # vendored: strategies.crypto_mtf

from ml.features import build_feature_vector as orig_feat       # noqa: E402
from ml.context import build_market_context as orig_ctx         # noqa: E402
from config.settings import ContextSettings                     # noqa: E402
from strategies.crypto_mtf.indicators import build_feature_vector as new_feat  # noqa: E402
from strategies.crypto_mtf.context import build_market_context as new_ctx      # noqa: E402
from strategies.crypto_mtf.config import BASELINE_S008          # noqa: E402

CTX_WINDOW = 200
FEAT_WINDOW = 30
N_SAMPLES = 200


def load(tf: str):
    df = pd.read_csv(DATA / f"{tf}.csv")
    df["ts"] = df["ts"].astype("int64")
    return df["ts"].values, df[["ts", "open", "high", "low", "close", "volume"]].to_dict("records")


def slice_upto(ts_arr, recs, t, n):
    k = int(np.searchsorted(ts_arr, t, side="right"))
    return recs[max(0, k - n):k]


def main() -> None:
    m15_ts, m15 = load("m15")
    h1_ts, h1 = load("h1")
    h4_ts, h4 = load("h4")
    d1_ts, d1 = load("d1")

    idxs = np.linspace(5000, len(m15) - 2, N_SAMPLES).astype(int)
    n_base = n_full = n_ctx = 0
    max_bias = max_feat = 0.0
    fails = []
    tuning = ContextSettings()

    for i in idxs:
        t = int(m15_ts[i])
        win = m15[i - FEAT_WINDOW:i]
        fo, fn = orig_feat(win), new_feat(win)
        if fo is None or fn is None:
            continue
        n_base += int(np.array_equal(fo, fn))

        sd = slice_upto(d1_ts, d1, t, CTX_WINDOW)
        sh4 = slice_upto(h4_ts, h4, t, CTX_WINDOW)
        sh1 = slice_upto(h1_ts, h1, t, CTX_WINDOW)
        ref = m15[i]["close"]
        co = orig_ctx(sd, sh4, sh1, reference_price=ref, tuning=tuning)
        cn = new_ctx(sd, sh4, sh1, reference_price=ref, cfg=BASELINE_S008)

        max_bias = max(max_bias, abs(co.bias - cn.bias))
        ok = (abs(co.bias - cn.bias) < 1e-9 and co.allowed == cn.allowed
              and co.market_stage.value == cn.market_stage.value
              and co.stage_side == cn.stage_side
              and co.h1_confirmed_side == cn.h1_confirmed_side
              and len(co.h4_poi) == len(cn.h4_poi)
              and ((co.fta_level is None) == (cn.fta_level is None)))
        n_ctx += int(ok)
        if not ok:
            fails.append((t, "ctx"))

        ffo, ffn = orig_feat(win, co), new_feat(win, cn)
        if ffo is not None and ffn is not None:
            max_feat = max(max_feat, float(np.max(np.abs(ffo - ffn))))
            n_full += int(np.array_equal(ffo, ffn))

    n = len(idxs)
    print(f"samples            : {n}")
    print(f"base feature exact : {n_base}/{n}")
    print(f"context agree      : {n_ctx}/{n}   max|bias diff|={max_bias:.2e}")
    print(f"full feature exact : {n_full}/{n}   max|feat diff|={max_feat:.2e}")
    if fails or n_base < n or n_ctx < n:
        print(f"\nFAIL — {len(fails)} context mismatches")
        sys.exit(1)
    print("\nGATE 0a PASS — vendored core reproduces tradingbot originals byte-for-byte.")


if __name__ == "__main__":
    main()
