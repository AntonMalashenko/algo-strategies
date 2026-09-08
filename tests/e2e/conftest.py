"""Harness for S007's sequential pipeline (e2e) tests.

Drives the REAL code (strategies.ger40_lonfra.simulate_day via
bot.s007_signals.plan_now via bot.s007_paper.decide()/run_cycle_for_account)
across a full simulated trading day, cycle by cycle, exactly like the live
scheduler would -- the only thing replaced is the network boundary
(bot.ctrader_s007.CTraderS007), by a STATEFUL fake broker that behaves like a
real server (fills orders, applies server-side SL/TP execution, keeps a deal
history) instead of answering one canned cycle. See
.claude/plans/sequential-marinating-frog.md for the full case catalog this
harness supports.
"""
from __future__ import annotations

import sys
import types

import numpy as np
import pandas as pd
import pytest

from bot import s007_config as C
from bot import s007_paper
from strategies.ger40_lonfra import simulate_day
from strategies.ger40_lonfra.config import StrategyConfig
from utils.trade_logger import StrategyLogger

MAGIC = "S007"


class FakeCTraderS007E2E:
    """Stateful double for bot.ctrader_s007.CTraderS007.

    Holds an open-position book and a deal history exactly like a real
    broker session would, across many `run_live_cycle` calls (one per
    simulated minute):
      - `run_live_cycle` calls the REAL decide() closure with the broker's
        current state, then applies the returned actions (place/close/amend)
        to that state -- decide() itself is oblivious to the fact its
        "broker" is fake.
      - `apply_bar` simulates the broker's OWN server-side SL/TP execution
        against the next bar's high/low -- decide() never triggers this
        itself, it only discovers a position gone on the following cycle's
        reconcile (`broker_positions` no longer contains it).
      - `slippage_points`: fills a fresh `place` worse by this many points
        (away from the position's stop), and a stop-out worse by this many
        points, on the side that makes both realistic -- 0.0 (default) means
        no slippage, exact fills at the engine's own price levels.
    """

    def __init__(self, creds=None, require_account=True, *,
                 balance: float = 10_000.0, money_per_point_per_lot: float = 114.3,
                 slippage_points: float = 0.0):
        self.creds = creds
        self.balance = balance
        self.money_per_point_per_lot = money_per_point_per_lot
        self.slippage_points = slippage_points
        self.positions: dict[str, dict] = {}
        self.deal_history: list[dict] = []
        self.error_labels: set[str] = set()
        self.raise_next_cycle = False
        self._next_pid = 1000
        self.m1: pd.DataFrame | None = None  # set by _run_day before every cycle
        self.cycles: list[dict] = []  # every run_live_cycle's return value, for inspection

    def resolve_symbol(self, candidates):
        return candidates[0] if candidates else "GER40"

    def _fresh_pid(self) -> int:
        pid = self._next_pid
        self._next_pid += 1
        return pid

    def run_live_cycle(self, symbol_candidates, history_days, decide):
        if self.raise_next_cycle:
            self.raise_next_cycle = False
            raise RuntimeError("simulated network blip (B18)")

        symbol = symbol_candidates[0] if symbol_candidates else "GER40"
        broker_positions = list(self.positions.values())
        closed_deals = list(self.deal_history)
        actions = decide(symbol, self.m1, broker_positions, self.balance,
                         self.money_per_point_per_lot, closed_deals=closed_deals)

        results = []
        for a in actions:
            lab = a["label"]
            if lab in self.error_labels:
                results.append(dict(action=a, result=None,
                                    error=f"simulated broker error for {lab}"))
                continue
            if a["kind"] == "place":
                pid = self._fresh_pid()
                side_sign = 1.0 if a["side"] == "buy" else -1.0
                fill = a["entry"] + side_sign * self.slippage_points
                self.positions[lab] = dict(
                    label=lab, position_id=pid, volume=round(a["volume_lots"] * 100.0),
                    price=fill, side=a["side"], stop_loss=a["sl"], take_profit=a["tp"])
                res = types.SimpleNamespace(position=types.SimpleNamespace(positionId=pid))
                results.append(dict(action=a, result=res, error=None))
            elif a["kind"] == "close":
                p = self.positions.pop(lab, None)
                if p is not None:
                    self.deal_history.append(dict(position_id=p["position_id"],
                                                  entry_price=p["price"]))
                results.append(dict(action=a, result={"ok": True}, error=None))
            elif a["kind"] == "amend":
                p = self.positions.get(lab)
                if p is not None:
                    p["stop_loss"] = a["sl"]
                    if a.get("tp") is not None:
                        p["take_profit"] = a["tp"]
                results.append(dict(action=a, result={"ok": True}, error=None))
            else:  # pragma: no cover - decide() never emits anything else today
                raise AssertionError(f"unexpected action kind {a['kind']!r}")

        cyc = dict(symbol=symbol, m1=self.m1, positions=broker_positions, actions=actions,
                  results=results, balance=self.balance,
                  money_per_point_per_lot=self.money_per_point_per_lot)
        self.cycles.append(cyc)
        return cyc

    def apply_bar(self, bar) -> list[str]:
        """Simulate the broker's own server-side SL/TP execution against the
        NEXT bar's high/low. Returns the labels closed this way."""
        hi, lo = float(bar["high"]), float(bar["low"])
        closed = []
        for lab in list(self.positions.keys()):
            p = self.positions[lab]
            sl, tp = p["stop_loss"], p["take_profit"]
            hit = False
            if p["side"] == "buy":
                if sl is not None and lo <= sl:
                    hit = True
                elif tp is not None and hi >= tp:
                    hit = True
            else:
                if sl is not None and hi >= sl:
                    hit = True
                elif tp is not None and lo <= tp:
                    hit = True
            if hit:
                self.deal_history.append(dict(position_id=p["position_id"],
                                              entry_price=p["price"]))
                del self.positions[lab]
                closed.append(lab)
        return closed


@pytest.fixture
def logger(tmp_path):
    return StrategyLogger("S007E2E", log_root=str(tmp_path), console=False)


@pytest.fixture
def fake(monkeypatch):
    """A fresh FakeCTraderS007E2E wired in as bot.ctrader_s007.CTraderS007 for
    the duration of one test. Tests that need a second, independent broker
    (e.g. C5's two-accounts case) construct their own FakeCTraderS007E2E
    directly and DON'T use this fixture."""
    f = FakeCTraderS007E2E()
    fake_mod = types.SimpleNamespace(CTraderS007=lambda creds=None, require_account=True: f)
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007", fake_mod)
    return f


def install_fake(monkeypatch, fake_broker: FakeCTraderS007E2E) -> None:
    """Wire an already-constructed fake broker in as CTraderS007 -- for tests
    (C-layer, multi-account) that build their own fake instance(s) instead of
    using the `fake` fixture."""
    fake_mod = types.SimpleNamespace(
        CTraderS007=lambda creds=None, require_account=True: fake_broker)
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007", fake_mod)


def run_day(m1_day: pd.DataFrame, fake_broker: FakeCTraderS007E2E, logger: StrategyLogger,
           *, preset: str | None = None, risk_pct: float = C.RISK_PCT,
           fixed_lot: float = C.FIXED_LOT, use_fixed_lot: bool = C.USE_FIXED_LOT,
           daily_risk_cap_pct: float = C.DAILY_RISK_CAP_PCT, fx_rate: float = 1.0,
           initial_balance: float | None = None, stop_flag_active=None,
           magic: str = MAGIC, cycle_from: str = "10:00", cycle_to: str = "14:30",
           run_cycle=None) -> list[dict]:
    """Drives `run_cycle_for_account` once per minute across `m1_day`'s
    session window (default: the live TRADE_START..a few minutes past
    EXIT_END, matching what the real scheduler ticks during -- pre-10:00
    Frankfurt-only bars carry no live decision and are skipped for speed, but
    are still present in `m1_day` so plan_now()'s FR-range calc sees them).

    Between cycles, feeds the JUST-ELAPSED bar to `fake_broker.apply_bar` so a
    server-side stop/tp touch is discovered by the NEXT cycle's reconcile,
    exactly like a real broker would report it. Returns the full per-cycle
    trace: [{ts, result, closed_by_broker}, ...].
    """
    run_cycle = run_cycle or s007_paper.run_cycle_for_account
    time_only = m1_day.index.strftime("%H:%M")
    mask = (time_only >= cycle_from) & (time_only <= cycle_to)
    cycle_ts = m1_day.index[mask]
    trace = []
    prev_ts = None
    for ts in cycle_ts:
        if prev_ts is not None:
            closed = fake_broker.apply_bar(m1_day.loc[ts])
        else:
            closed = []
        fake_broker.m1 = m1_day.loc[:ts]
        result = run_cycle(
            None, preset=preset, risk_pct=risk_pct, fixed_lot=fixed_lot,
            use_fixed_lot=use_fixed_lot, magic=magic, logger=logger,
            symbol_candidates=["GER40"], history_days=C.HISTORY_DAYS,
            daily_risk_cap_pct=daily_risk_cap_pct, fx_rate=fx_rate,
            stop_flag_active=stop_flag_active, initial_balance=initial_balance)
        trace.append(dict(ts=ts, result=result, closed_by_broker=closed))
        prev_ts = ts
    return trace


# ---------------------------------------------------------------------------
# Synthetic day-bar construction
# ---------------------------------------------------------------------------

FR_BARS = 60          # 09:00-09:59
LONDON_BARS = 265     # 10:00-14:24 inclusive, plus a little past for the flat-close check


def make_day(date: str, fr_low: float, fr_high: float, *,
            price: dict[int, float] | None = None,
            close: dict[int, float] | None = None,
            high: dict[int, float] | None = None,
            low: dict[int, float] | None = None,
            open_: dict[int, float] | None = None,
            n_london: int = LONDON_BARS) -> pd.DataFrame:
    """One full day's flat M1 OHLC frame: Frankfurt range (09:00-09:59)
    oscillating between fr_low/fr_high (so plan_now's own FR-range calc sees
    exactly rh=fr_high, rl=fr_low), then a flat London session at the range
    midpoint from 10:00 -- overrides apply to specific LONDON-relative bar
    indices (0 == the 10:00 bar):

      price  -- sets BOTH open AND close to the same value, i.e. "this bar
                sits flat at X" -- the usual way to move/hold a setup, since
                a bar with open left at the old default (mid) but only close
                overridden would still WICK back down/up to that stale
                default once high/low are bracketed (a real bug this harness
                hit while being built: a scenario-B "holding" bar at the
                default mid is simultaneously sitting exactly ON that
                scenario's own stop, mid -- so half-overriding it silently
                stopped the position out one bar early).
      close/open_ -- override just one side, when a bar genuinely needs
                open != close (rare -- most scenarios only need `price`).
      high/low -- widen a bar's wick beyond its (possibly just-set) open/
                close, e.g. to touch a stop/tp that sits beyond both.

    Naive Kyiv-local index, 1-minute bars -- matches what the cTrader adapter
    hands bot.s007_signals.plan_now() live (see plan_now's own docstring).
    """
    mid = (fr_low + fr_high) / 2.0
    fr_idx = pd.date_range(f"{date} 09:00", periods=FR_BARS, freq="1min")
    ld_idx = pd.date_range(f"{date} 10:00", periods=n_london, freq="1min")
    idx = fr_idx.append(ld_idx)
    n = len(idx)

    o = np.full(n, mid)
    h = np.full(n, mid)
    lo = np.full(n, mid)
    c = np.full(n, mid)
    for i in range(FR_BARS):
        px = fr_low if i % 2 == 0 else fr_high
        c[i] = px
        h[i] = fr_high if i % 2 == 0 else mid
        lo[i] = fr_low if i % 2 == 0 else mid
        o[i] = mid

    def _apply(arr, overrides):
        if not overrides:
            return
        for rel_idx, val in overrides.items():
            arr[FR_BARS + rel_idx] = val

    _apply(o, price)
    _apply(c, price)
    _apply(o, open_)
    _apply(c, close)
    # LONDON bars' high/low default to bracketing their OWN (possibly just-
    # overridden) open/close -- a caller that only overrode `close` gets a
    # flat bar at the new level, not a bar that still wicks back down/up to
    # the old pre-override default (mid). Frankfurt bars keep the deliberate
    # alternating shape set above; explicit high/low overrides (below) widen
    # a bar's wick beyond its open/close on top of this default.
    h[FR_BARS:] = np.maximum(o[FR_BARS:], c[FR_BARS:])
    lo[FR_BARS:] = np.minimum(o[FR_BARS:], c[FR_BARS:])
    if high:
        for rel_idx, val in high.items():
            h[FR_BARS + rel_idx] = max(h[FR_BARS + rel_idx], val)
    if low:
        for rel_idx, val in low.items():
            lo[FR_BARS + rel_idx] = min(lo[FR_BARS + rel_idx], val)

    return pd.DataFrame(dict(open=o, high=h, low=lo, close=c), index=idx)


def oracle(m1_day: pd.DataFrame, fr_low: float, fr_high: float,
          cfg: StrategyConfig) -> dict:
    """Single-shot ground truth: run the real engine directly on the day's
    London-session bars (no cycle-by-cycle polling). The live cycle-by-cycle
    trace (via run_day) must agree with this by construction -- that
    agreement IS the invariant this whole suite exists to check."""
    mid = (fr_low + fr_high) / 2.0
    height = fr_high - fr_low
    london = m1_day.iloc[FR_BARS:].reset_index(drop=True)
    return simulate_day(london, fr_high, fr_low, mid, height, {}, cfg)


def live_preset(**overrides) -> str | None:
    """None -> plan_now() resolves bot.s007_config.PRESET (the actual live
    preset, WORKING_S007_NEWSSAFE_MAX8_BE05) with no overrides; kwargs name a
    StrategyConfig field to override on top of the live preset, returned as
    an ad-hoc name registered into strategies.ger40_lonfra.config's module
    namespace so plan_now()'s `_preset(name)` (a plain getattr) can find it.
    """
    if not overrides:
        return None
    from strategies.ger40_lonfra import config as GC
    base = getattr(GC, C.PRESET)
    cfg = base.with_(**overrides)
    name = "_E2E_" + "_".join(f"{k}{v}" for k, v in overrides.items())
    name = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    setattr(GC, name, cfg)
    return name


def resolved_cfg(preset_name: str | None) -> StrategyConfig:
    """The actual StrategyConfig plan_now() will use for `preset_name` (None
    == the live C.PRESET), with the same trade_start/exit_end/fr_* override
    plan_now() itself applies -- so a test's `oracle()` call matches exactly."""
    from strategies.ger40_lonfra import config as GC
    base = getattr(GC, preset_name or C.PRESET)
    return base.with_(trade_start=C.TRADE_START, exit_end=C.EXIT_END,
                      fr_start=C.FR_START, fr_end=C.FR_END)


def all_actions(trace: list[dict], kind: str | None = None) -> list[dict]:
    out = []
    for row in trace:
        for a in row["result"]["actions"]:
            if kind is None or a["kind"] == kind:
                out.append(a)
    return out


def all_events(logger: StrategyLogger, kind: str | None = None) -> list[dict]:
    """Every event this logger has written so far. StrategyLogger partitions
    events-<date>.jsonl by REAL wall-clock date (datetime.now()), not by the
    simulated trading day encoded in a test's bar index -- a single test run
    only ever spans one real day, so today's file is always the right (and
    only) one to read."""
    import json
    from datetime import datetime
    day = datetime.now().strftime("%Y-%m-%d")
    path = logger.dir / f"events-{day}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if kind is None or rec.get("kind") == kind:
            out.append(rec)
    return out
