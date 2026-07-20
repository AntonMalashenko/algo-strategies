"""Entry detection: scenario A (0.5 mid-break) and B (boundary breakout).

Verbatim logic from run.py / pyramid_v2.find_setup. Scans the London window bar
by bar on closes:
  * close beyond a boundary            -> B, enter same bar
  * close crosses mid, next close      -> if it breaks a boundary -> B on next bar
    confirms same side                    else if same side confirms -> A on next bar
Returns (scenario, direction, entry_idx, entry_price) or (None, ...).
"""
from __future__ import annotations

import numpy as np


def find_setup(opens: np.ndarray, closes: np.ndarray,
               range_high: float, range_low: float, mid: float):
    n = len(closes)
    cur = "above" if opens[0] > mid else "below"
    i = 0
    while i < n:
        c = closes[i]
        if c > range_high:
            return "B", "up", i, c
        if c < range_low:
            return "B", "down", i, c
        cs = "above" if c > mid else ("below" if c < mid else cur)
        if cs != cur:
            if i + 1 < n:
                c2 = closes[i + 1]
                if c2 > range_high:
                    return "B", "up", i + 1, c2
                if c2 < range_low:
                    return "B", "down", i + 1, c2
                s2 = "above" if c2 > mid else ("below" if c2 < mid else cur)
                if s2 == cs:
                    return "A", ("up" if cs == "above" else "down"), i + 1, c2
                i += 1
                continue
            break
        i += 1
    return None, None, None, None
