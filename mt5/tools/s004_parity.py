"""S004 parity: the Python backtest engine vs the MQL5 EA, on the broker's own bars.

    python -m mt5.tools.s004_parity --bars EXPORTS/*_M1_<server>.csv \
        --ea-trades S004-mt5-acct<login>_trades.csv [--rule EET_US_DST] \
        [--since "2026.01.05 00:00"] [--out report.csv]

--bars        one Scripts/AlgoTrading/ExportM1.mq5 CSV per symbol (SERVER time),
              or a directory holding them. The symbol is read from the file name
              (`<SYMBOL>_M1_<server>.csv`). M1 is resampled to the M15 the
              engine runs on, after the server clock is converted to the
              strategy's session clock with the same two-hop rule the EA uses
              (Runtime.mqh::ServerToClock).
--ea-trades   the EA's own trade list, written by
              Strategies/S004_FVG/Runtime.mqh::FlushTradeRow. Produced by a
              Strategy Tester pass or by a live/shadow run. Its pip/cost columns
              re-configure the Python engine, so the comparison runs on the
              broker's real spread instead of S004's modelled 0.9 pips.
--since       ignore engine trades entered before this session-clock stamp.
              Default: the EA's first logged entry. The EA replays InpWarmupBars
              silently at start, so anything older is history it never reported.
--ea-start    the first bar of the EA's run (the Strategy Tester's "from" date).
              With --warmup-bars it trims the bars so the Python engine starts
              from the same bar the EA's warmup did. Without it the engine sees
              however much history happens to be exported, builds zones the EA
              never had, and reports trades the EA could not have taken.

Every trade is matched on (symbol, entry time) and checked on two levels:

  fields   entry / sl / tp / exit / exit_reason / r -- the engine port must
           reproduce the Python engine exactly on the same bars. Any miss is a
           FAILURE (that is what Engine.mqh is).
  status   whether the live layer TOOK the trade: the Asia-window filter and the
           portfolio-wide daily cap of backtest/run_s004_intraday.py, which the
           EA has to reproduce online, in symbol-name order on ties. Known,
           explained live-only outcomes are classified, not failed:
             ea_missed_fill   the engine's price was touched but no position
                              came back (a restart mid-bar, or a reject)
             ea_halted        the account guard had already stopped trading
           anything else is a FAILURE.

A shadow pass (InpTradeEnabled=false) is the intended input: the EA then still
counts the daily cap and writes status=shadow, so parity covers every decision
except the order round trip itself.

Exit code 0 = parity holds, 1 = at least one failure.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd

from mt5.tools import clock

SERVER_TIME_FORMAT = "%Y.%m.%d %H:%M"
EXPORT_SYMBOL_MARKER = "_M1_"
M15 = "15min"
PRICE_REL_TOLERANCE = 1e-6
R_ABS_TOLERANCE = 1e-5          # the CSV carries 6 decimals of R
PRICE_COLUMNS = ("entry", "sl", "tp", "exit")

# EA statuses, spelled exactly like Runtime.mqh's S004_STATUS_* constants.
STATUS_TAKEN = "taken"
STATUS_SHADOW = "shadow"
STATUS_MISSED_FILL = "missed_fill"
STATUS_VIRTUAL_HOUR = "virtual_hour"
STATUS_VIRTUAL_CAP = "virtual_cap"
STATUS_VIRTUAL_HALT = "virtual_halt"
# A trade the backtest counts: with orders on it is `taken`, in a shadow pass
# `shadow`. Both mean the daily cap was spent on it.
STATUSES_COUNTED = (STATUS_TAKEN, STATUS_SHADOW)

CATEGORY_MATCH = "match"
CATEGORY_MISSED_FILL = "ea_missed_fill"
CATEGORY_HALTED = "ea_halted"
CATEGORY_FAIL_MISSING = "FAIL_missing"      # the engine traded, the EA did not report it
CATEGORY_FAIL_EXTRA = "FAIL_extra"          # the EA reported a trade the engine never took
CATEGORY_FAIL_FIELDS = "FAIL_fields"
CATEGORY_FAIL_STATUS = "FAIL_status"
FAIL_CATEGORIES = (CATEGORY_FAIL_MISSING, CATEGORY_FAIL_EXTRA, CATEGORY_FAIL_FIELDS,
                   CATEGORY_FAIL_STATUS)


def _session_clock_rule() -> str:
    """The tz rule mirroring s004_config.SESSION_TZ -- the EA's S004_CLOCK_TZ_RULE."""
    from strategies.s004_config import SESSION_TZ

    if SESSION_TZ != "Europe/Bucharest":
        raise ValueError(f"no tz rule mirrors s004_config.SESSION_TZ {SESSION_TZ!r}")
    return clock.RULE_EET_EU_DST


def symbol_of(path: Path) -> str:
    """`GBPJPY_M1_FundingPips-Server.csv` -> `GBPJPY`."""
    stem = path.stem
    if EXPORT_SYMBOL_MARKER not in stem:
        raise ValueError(f"cannot read a symbol from {path.name!r}; expected "
                         f"<SYMBOL>{EXPORT_SYMBOL_MARKER}<server>.csv")
    return stem.split(EXPORT_SYMBOL_MARKER, 1)[0]


def expand_bar_paths(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.glob(f"*{EXPORT_SYMBOL_MARKER}*.csv")) if path.is_dir()
                     else [path])
    if not files:
        raise SystemExit(f"no ExportM1 CSV found in {[str(p) for p in paths]}")
    return files


def load_broker_bars(path: Path, rule: str, fixed_hours: int = 0) -> pd.DataFrame:
    """ExportM1 CSV (server-time M1) -> the M15 frame the engine runs on."""
    raw = pd.read_csv(path)
    server_index = pd.DatetimeIndex(pd.to_datetime(raw["time_server"], format=SERVER_TIME_FORMAT))
    utc_index = clock.index_local_to_utc(server_index, rule, fixed_hours)
    m1 = raw[["open", "high", "low", "close"]].astype(float)
    # fvg_mtf.resample_h4 sums a volume column; the engine never reads it.
    m1["volume"] = raw["tick_volume"].astype(float) if "tick_volume" in raw else 0.0
    m1.index = clock.index_utc_to_local(utc_index, _session_clock_rule())
    m1.index.name = "dt"
    m1 = m1[~m1.index.duplicated()].sort_index()
    m15 = m1.resample(M15).agg({"open": "first", "high": "max", "low": "min",
                               "close": "last", "volume": "sum"})
    return m15.dropna(subset=["close"])


def trim_to_ea_history(bars: dict[str, pd.DataFrame], ea_start: pd.Timestamp,
                      warmup_bars: int) -> dict[str, pd.DataFrame]:
    """Drop bars older than the EA's own warmup window.

    Zones survive for days, so an engine fed a deeper history than the EA was
    carries state the EA never had. Trimming is positional, not calendar-based:
    the EA replayed exactly `warmup_bars` CLOSED bars before its first one, and
    how many days that spans depends on the symbol's trading hours.
    """
    trimmed = {}
    for symbol, frame in bars.items():
        start = frame.index.searchsorted(ea_start)
        trimmed[symbol] = frame.iloc[max(start - warmup_bars, 0):]
    return trimmed


def engine_trades(bars: dict[str, pd.DataFrame], config, scales: dict[str, tuple]) -> pd.DataFrame:
    """Every pair through fvg_mtf.run_backtest, then the portfolio rules on top.

    `scales` maps symbol -> (pip, cost) as the EA's own engine was configured
    (the CSV's pip/cost columns). Both are re-used verbatim instead of the
    modelled 0.9-pip average: `pip` scales the stop's buffer and `cost` is the R
    denominator, so a modelled spread would show up as a fake parity failure.

    The hour filter and the daily cap are applied exactly as
    backtest/run_s004_intraday.py does -- including the ["time_in", "symbol"]
    sort, which is what decides who gets the last slot of a day.
    """
    from backtest.run_s004_intraday import apply_daily_cap
    from strategies.fvg_mtf import run_backtest

    frames = []
    for symbol, m15 in sorted(bars.items()):
        kwargs = config.engine_kwargs()
        pip, cost = scales.get(symbol, (kwargs["pip"], kwargs["pip"] * kwargs["spread_pips"]))
        kwargs.update(pip=pip, spread_pips=cost / pip)
        trades = run_backtest(m15, **kwargs)
        trades["symbol"] = symbol
        frames.append(trades)
    all_trades = pd.concat(frames, ignore_index=True).sort_values(["time_in", "symbol"])
    all_trades = all_trades.reset_index(drop=True)
    in_session = all_trades["hour"].isin(config.entry_hours)
    capped = apply_daily_cap(all_trades[in_session].reset_index(drop=True),
                             config.max_trades_per_day)
    # Out-of-window trades never reach the cap: they are virtual for a different
    # reason, and the EA must say so.
    keys = ["symbol", "time_in"]
    taken = capped.set_index(keys)["taken"]
    expected = all_trades.set_index(keys).index.map(taken)
    all_trades["expected_status"] = [
        STATUS_VIRTUAL_HOUR if pd.isna(flag) else (STATUS_TAKEN if flag else STATUS_VIRTUAL_CAP)
        for flag in expected]
    return all_trades


def load_ea_trades(path: Path) -> pd.DataFrame:
    return load_ea_trades_frame(pd.read_csv(path))


def load_ea_trades_frame(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in ("time_in", "time_out"):
        frame[column] = pd.to_datetime(frame[column], format=SERVER_TIME_FORMAT)
    return frame.sort_values(["time_in", "symbol"]).reset_index(drop=True)


def ea_scales(ea: pd.DataFrame) -> dict[str, tuple]:
    """symbol -> (pip, cost) the EA's engine ran with.

    Both are fixed at Init, so every row of a symbol normally carries the same
    pair; a restart that saw a different spread makes the file carry two, and
    the most frequent one is the one the bulk of the trades were priced with.
    """
    scales = {}
    for symbol, rows in ea.groupby("symbol"):
        pair = rows.groupby(["pip", "cost"]).size().idxmax()
        scales[symbol] = (float(pair[0]), float(pair[1]))
    return scales


def _near(actual: float, expected: float) -> bool:
    if pd.isna(actual) or pd.isna(expected):
        return bool(pd.isna(actual) and pd.isna(expected))
    return math.isclose(float(actual), float(expected), rel_tol=PRICE_REL_TOLERANCE,
                        abs_tol=1e-9)


def _field_misses(ea: pd.Series, engine: pd.Series) -> list[str]:
    misses = [f"{column}: ea={ea[column]} engine={engine[column]}"
              for column in PRICE_COLUMNS if not _near(ea[column], engine[column])]
    if abs(float(ea["r"]) - float(engine["r"])) > R_ABS_TOLERANCE:
        misses.append(f"r: ea={ea['r']} engine={engine['r']}")
    if str(ea["exit_reason"]) != str(engine["exit_reason"]):
        misses.append(f"exit_reason: ea={ea['exit_reason']!r} engine={engine['exit_reason']!r}")
    if int(ea["dir"]) != int(engine["dir"]):
        misses.append(f"dir: ea={ea['dir']} engine={engine['dir']}")
    return misses


def _status_category(ea_status: str, expected_status: str) -> str:
    if ea_status == STATUS_MISSED_FILL:
        return CATEGORY_MISSED_FILL if expected_status == STATUS_TAKEN else CATEGORY_FAIL_STATUS
    if ea_status == STATUS_VIRTUAL_HALT:
        return CATEGORY_HALTED
    if expected_status == STATUS_TAKEN:
        return CATEGORY_MATCH if ea_status in STATUSES_COUNTED else CATEGORY_FAIL_STATUS
    return CATEGORY_MATCH if ea_status == expected_status else CATEGORY_FAIL_STATUS


def compare(engine: pd.DataFrame, ea: pd.DataFrame, since: pd.Timestamp | None,
            until: pd.Timestamp | None) -> pd.DataFrame:
    """One row per trade either side reported inside the compared window."""
    if since is None:
        since = ea["time_in"].min() if not ea.empty else engine["time_in"].min()
    if until is None:
        until = ea["time_in"].max() if not ea.empty else engine["time_in"].max()
    window = engine[(engine["time_in"] >= since) & (engine["time_in"] <= until)]
    keys = ["symbol", "time_in"]
    ea_indexed = ea.set_index(keys)
    seen: set[tuple] = set()
    rows = []
    for _index, trade in window.iterrows():
        key = (trade["symbol"], trade["time_in"])
        row = dict(symbol=trade["symbol"], time_in=trade["time_in"],
                   expected_status=trade["expected_status"], ea_status="",
                   category=CATEGORY_MATCH, detail="")
        if key not in ea_indexed.index:
            row.update(category=CATEGORY_FAIL_MISSING,
                       detail=f"the engine took this trade ({trade['exit_reason']}, "
                              f"{trade['r']:+.3f}R), the EA never reported it")
            rows.append(row)
            continue
        seen.add(key)
        ea_row = ea_indexed.loc[key]
        if isinstance(ea_row, pd.DataFrame):          # a duplicated row in the CSV
            row.update(category=CATEGORY_FAIL_EXTRA,
                       detail=f"{len(ea_row)} EA rows share this entry")
            rows.append(row)
            continue
        row["ea_status"] = ea_row["status"]
        misses = _field_misses(ea_row, trade)
        if misses:
            row.update(category=CATEGORY_FAIL_FIELDS, detail="; ".join(misses))
            rows.append(row)
            continue
        row["category"] = _status_category(str(ea_row["status"]), trade["expected_status"])
        if row["category"] == CATEGORY_FAIL_STATUS:
            row["detail"] = (f"ea={ea_row['status']} expected={trade['expected_status']} "
                             f"(hour {trade['hour']})")
        rows.append(row)
    for _index, ea_row in ea.iterrows():
        key = (ea_row["symbol"], ea_row["time_in"])
        if key in seen or not (since <= ea_row["time_in"] <= until):
            continue
        rows.append(dict(symbol=ea_row["symbol"], time_in=ea_row["time_in"], expected_status="",
                         ea_status=ea_row["status"], category=CATEGORY_FAIL_EXTRA,
                         detail="the EA reported a trade the engine never took"))
    report = pd.DataFrame(rows)
    return report.sort_values(["time_in", "symbol"]).reset_index(drop=True) \
        if not report.empty else report


def slippage(ea: pd.DataFrame) -> dict:
    """What the real fills cost against the engine's modelled entry, in price units."""
    filled = ea[(ea["status"] == STATUS_TAKEN) & (ea["fill"] > 0.0)]
    if filled.empty:
        return {}
    # Positive = the fill was worse than the engine's price for that direction.
    delta = (filled["fill"] - filled["entry"]) * filled["dir"]
    return dict(n=len(filled), mean=float(delta.mean()), worst=float(delta.max()))


def main(argv: list[str] | None = None) -> int:
    from strategies.s004_config import S004_INTRADAY

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bars", type=Path, nargs="+", required=True)
    parser.add_argument("--ea-trades", type=Path, required=True)
    parser.add_argument("--rule", default=clock.RULE_EET_US_DST, choices=clock.RULES)
    parser.add_argument("--fixed-hours", type=int, default=0)
    parser.add_argument("--since", default=None,
                        help=f"session-clock stamp, {SERVER_TIME_FORMAT}")
    parser.add_argument("--until", default=None)
    parser.add_argument("--ea-start", default=None,
                        help=f"the EA run's first bar, {SERVER_TIME_FORMAT}")
    parser.add_argument("--warmup-bars", type=int, default=1000,
                        help="InpWarmupBars of the run being compared")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    bars = {}
    for path in expand_bar_paths(args.bars):
        bars[symbol_of(path)] = load_broker_bars(path, args.rule, args.fixed_hours)
    ea = load_ea_trades(args.ea_trades)
    # The exports folder is shared with the other strategies, so it routinely
    # holds symbols S004 does not trade. Feeding one to the engine invents
    # trades that compete for the daily cap and turns every later entry of that
    # day into a false divergence, so narrow to the configured pairs.
    foreign = sorted(set(bars) - set(S004_INTRADAY.pairs))
    if foreign:
        print(f"ignoring bars not in the config: {', '.join(foreign)}")
        bars = {symbol: frame for symbol, frame in bars.items() if symbol not in foreign}
    # A pair the EA traded but we have no bars for is fatal for the same reason,
    # only backwards: the engine would be one competitor short of the EA.
    unexported = sorted(set(ea["symbol"].unique()) - set(bars))
    if unexported:
        print(f"the EA traded {', '.join(unexported)} but no bars were exported for them"
              " -- re-run ExportM1 with every pair")
        return 2
    idle = sorted(set(S004_INTRADAY.pairs) - set(bars))
    if idle:
        print(f"warning: no bars for {', '.join(idle)}; the daily cap is compared with"
              " fewer competing pairs than the strategy trades")
    if args.ea_start:
        bars = trim_to_ea_history(bars, pd.Timestamp(args.ea_start), args.warmup_bars)
    engine = engine_trades(bars, S004_INTRADAY, ea_scales(ea))
    since = pd.Timestamp(args.since) if args.since else None
    until = pd.Timestamp(args.until) if args.until else None
    report = compare(engine, ea, since, until)

    if args.out:
        report.to_csv(args.out, index=False)
    counts = report["category"].value_counts().to_dict() if not report.empty else {}
    bar_counts = {symbol: len(frame) for symbol, frame in sorted(bars.items())}
    print(f"symbols={bar_counts} engine_trades={len(engine)} ea_trades={len(ea)} "
          f"categories={counts}")
    costs = slippage(ea)
    if costs:
        print(f"fill vs engine entry: n={costs['n']} mean={costs['mean']:+.6f} "
              f"worst={costs['worst']:+.6f} price units")
    failures = report[report["category"].isin(FAIL_CATEGORIES)] if not report.empty else report
    for _index, failure in failures.head(20).iterrows():
        print(f"  {failure['time_in']} {failure['symbol']} {failure['category']}: "
              f"{failure['detail']}")
    return 1 if len(failures) else 0


if __name__ == "__main__":
    sys.exit(main())
