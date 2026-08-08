"""plan_now's `filtered` field (added 2026-07-23, see decisions-log.md and
scripts/s007_loop.py): must be True ONLY when the Frankfurt-range height
filter has DEFINITIVELY ruled out today (a verdict that cannot change later
in the same session, since the 09:00-09:59 range is already closed by the
time it's computed) -- never for the "not enough bars yet" case, which is
still pending and must keep being polled.
"""
from __future__ import annotations

import pandas as pd

from bot.s007_signals import plan_now


def _frankfurt_bars(date: str, n: int, low: float, high: float) -> pd.DataFrame:
    """n one-minute bars starting 09:00 on `date`, oscillating between
    `low` and `high` so the resulting Frankfurt range height is `high - low`."""
    idx = pd.date_range(f"{date} 09:00", periods=n, freq="1min")
    mid = (low + high) / 2
    rows = []
    for i in range(n):
        px = low if i % 2 == 0 else high
        rows.append(dict(open=mid, high=high if i % 2 else mid,
                          low=low if i % 2 == 0 else mid, close=px))
    return pd.DataFrame(rows, index=idx)


def test_wide_frankfurt_range_sets_filtered_true():
    # WORKING_S007 caps max_height at 100pt; 200pt range must trip it.
    bars = _frankfurt_bars("2026-01-05", n=60, low=25000.0, high=25200.0)
    res = plan_now(bars, now=pd.Timestamp("2026-01-05 10:05:00"), preset="WORKING_S007")
    assert res["filtered"] is True
    assert res["direction"] is None
    assert res["context"] is None
    assert res["in_window"] is True   # still a real trading-hours cycle, just a no-trade day


def test_narrow_frankfurt_range_does_not_set_filtered():
    bars = _frankfurt_bars("2026-01-05", n=60, low=25000.0, high=25010.0)  # 10pt, well under cap
    res = plan_now(bars, now=pd.Timestamp("2026-01-05 10:05:00"), preset="WORKING_S007")
    assert res.get("filtered", False) is False


def test_too_few_frankfurt_bars_does_not_set_filtered():
    # Only 10 of the required 45 min_fr_bars -- "not enough data yet", which
    # CAN resolve later (more bars may still arrive), unlike the height case.
    bars = _frankfurt_bars("2026-01-05", n=10, low=25000.0, high=25200.0)
    res = plan_now(bars, now=pd.Timestamp("2026-01-05 09:15:00"), preset="WORKING_S007")
    assert res.get("filtered", False) is False
    assert res["context"] is None


def test_resolved_positions_and_reversal_context_surface_separately(monkeypatch):
    """A position the engine entered AND resolved (stop/tp/daycap) within the
    same replayed bars never becomes a live order -- bot/s007_paper.py::decide
    only ever places orders for `positions` (status 'eod', still open as of
    "now"). plan_now must still surface it via `resolved` so it doesn't
    vanish without a trace, and the b-reversal diagnostics (reached_tp/
    n_recovery) must reach `context` -- neither existed before this test was
    added (found live 2026-08-05 investigating a B setup that never produced
    an order, see decisions-log.md)."""
    import bot.s007_signals as sig

    # A real B(up)-stopped-at-mid -> A(down)-reversal-hit-tp pair, taken from
    # a historical replay (2026-06-17) via the same engine.simulate_day this
    # monkeypatch stands in for -- reproduced verbatim rather than invented,
    # so the shape matches exactly what engine.py actually returns.
    fake_r = dict(
        scenario="B", direction="up", tp=25110.899, reached_tp=False, n_recovery=1, n_pos=2,
        positions=[
            dict(idx=3, entry=24855.655, stop=24833.2325, status="stop", exit=24833.2325,
                up=True, is_add=False, R=-1.0),
            dict(idx=32, entry=24833.2325, stop=24847.799, status="tp", exit=24818.666,
                up=False, is_add=False, is_recovery=True, R=0.9999999999997502),
        ],
    )
    monkeypatch.setattr(sig, "simulate_day", lambda *a, **k: fake_r)

    fr = _frankfurt_bars("2026-01-05", n=45, low=24818.666, high=24847.799)
    ld_idx = pd.date_range("2026-01-05 10:00", periods=5, freq="1min")
    ld = pd.DataFrame(dict(open=24830.0, high=24835.0, low=24825.0, close=24830.0), index=ld_idx)
    bars = pd.concat([fr, ld])

    res = plan_now(bars, now=pd.Timestamp("2026-01-05 11:00:00"), preset="WORKING_S007")

    assert res["positions"] == []   # nothing still open -> nothing to place live
    assert res["context"]["reached_tp"] is False
    assert res["context"]["n_recovery"] == 1

    by_label = {p["label"]: p for p in res["resolved"]}
    assert by_label["S007:2026-01-05:3"]["status"] == "stop"
    assert by_label["S007:2026-01-05:3"]["is_recovery"] is False
    assert by_label["S007:2026-01-05:32"]["status"] == "tp"
    assert by_label["S007:2026-01-05:32"]["is_recovery"] is True


def test_a_wanted_position_uses_its_own_tp_not_the_primary_legs(monkeypatch):
    """A b-reversal leg is simulated against its own target (tp_A, the
    opposite range boundary) -- a DIFFERENT value than the primary B leg's
    day-level tp. plan_now used to attach the single day-level `tp` to every
    wanted position regardless of which leg it belonged to: a live BUY
    reversal went out with the primary SELL leg's (lower) target, and
    cTrader rejected it outright (TRADING_BAD_STOPS: TP below entry on a
    BUY) -- found live 2026-08-06 while a reversal leg was still open as of
    "now" (status 'eod'), see decisions-log.md."""
    import bot.s007_signals as sig

    fake_r = dict(
        scenario="B", direction="up", tp=25110.899, reached_tp=False, n_recovery=1, n_pos=2,
        positions=[
            dict(idx=3, entry=24855.655, stop=24833.2325, status="stop", exit=24833.2325,
                up=True, is_add=False, R=-1.0, tp=25110.899),
            # still open ('eod') as of "now" -- this is what becomes a live order
            dict(idx=32, entry=24833.2325, stop=24847.799, status="eod", exit=None,
                up=False, is_add=False, is_recovery=True, tp=24818.666),
        ],
    )
    monkeypatch.setattr(sig, "simulate_day", lambda *a, **k: fake_r)

    fr = _frankfurt_bars("2026-01-05", n=45, low=24818.666, high=24847.799)
    ld_idx = pd.date_range("2026-01-05 10:00", periods=5, freq="1min")
    ld = pd.DataFrame(dict(open=24830.0, high=24835.0, low=24825.0, close=24830.0), index=ld_idx)
    bars = pd.concat([fr, ld])

    res = plan_now(bars, now=pd.Timestamp("2026-01-05 11:00:00"), preset="WORKING_S007")

    assert len(res["positions"]) == 1
    wanted = res["positions"][0]
    assert wanted["side"] == "sell"
    assert wanted["tp"] == 24818.666          # the recovery leg's OWN target
    assert wanted["tp"] != res["context"]["tp"]  # not the primary leg's day-level tp
    assert wanted["tp"] < wanted["entry"]     # valid for a sell (would be invalid for the primary leg's tp)
