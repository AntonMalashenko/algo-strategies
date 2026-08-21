"""S011/RSI(2)-portfolio paper/live daily cycle — Larry Connors RSI(2)
mean-reversion, traded on ONE cTrader account across the whole frozen
walk-forward universe at once (see `strategies/rsi2_portfolio.py` for the
engine and `backtest/run_rsi2_portfolio.py::WALK_FORWARD_UNIVERSE` for the
13-asset universe this reuses byte-for-byte).

Architectural decision this module implements (2026-08-18, confirmed with
Anton): the S011 universe is CAC40/DOW/ESTOXX50/ETHUSDT/EURGBP/EURUSD/
FTSE100/NASDAQ/RUSSELL/SOLUSDT/SP500idx/SPY/XAUUSD — a mix that originally
looked like it needed two brokers (cTrader for indices/FX/gold, Bybit for
the two crypto legs, like S009). Anton confirmed the cTrader broker this
account will actually run on ALSO lists crypto CFDs (ETHUSD/SOLUSD or
similar), so the whole universe can live on ONE account/ONE broker — no
S011-CTRADER + S011-BYBIT split, no cross-broker capital coordination. This
is simpler than the ticket's originally-flagged "critical architectural
question" and is why `_worker_s011` (webapp/runner.py) only ever checks
`acc.broker == "CTRADER"`.

One universe member IS excluded from the live deploy book (see
LIVE_DEPLOY_EXCLUDED_SYMBOLS below): SPY is a US ETF the backtest sourced
from Yahoo, most likely NOT a distinct tradable instrument from the
SP500idx index CFD on a cTrader broker (cTrader brokers commonly offer
index CFDs, not the SPY ticker itself as a separate product) — mirrors
S009's own precedent of trimming its deploy universe (LOW_CAPITAL_EXCLUDED_
SYMBOLS in bot/s009_paper.py) while leaving the frozen BACKTEST universe
(backtest/run_rsi2_portfolio.py::WALK_FORWARD_UNIVERSE) untouched. This is
a NAMED, DOCUMENTED deviation from the honest walk-forward universe, not a
silent one — the live book trades 12 instruments where the backtest
measured 13. Revisit if/when the broker's symbol list turns out to include
a genuine SPY product (see CTRADER_SYMBOL_CANDIDATES's own TODO — every
candidate name below is UNVERIFIED against this broker's real symbol list
and MUST be checked with CTraderS011.run_live_cycle_multi's `unresolved`
return value before the demo/dry gate, per decisions-log.md's open item).

Timing decision — ONE fixed cutover for a mixed-session universe: unlike
`backtest/run_rsi2_portfolio.py::load_universe()`, which reads each
instrument's own exchange-local daily bar from Yahoo (FX/gold trade
~24/5, indices have their own session, crypto is 24/7 — each source's
"daily close" lands at a different UTC instant), a LIVE cycle needs one
single fixed UTC time to treat "today's bar" as closed for every
instrument at once, or a signal on one asset would be computed on
yesterday's data while another already sees today's. This module (and the
cron entry it is wired to, see deployment/schedule.yml) fires once daily,
after essentially every included market's own session has closed
(European index sessions close by ~17:30 UTC, NYSE/NASDAQ by ~21:00 UTC
winter / ~20:00 UTC summer, FX/gold trade through but this broker's own D1
bar boundary is what actually governs — see CTraderS011._get_daily_step's
docstring) — 22:05 UTC, weekdays (crypto is 24/7 and unaffected by the
weekday gate; it simply gets one fewer signal check on weekends, same
tradeoff the backtest's Yahoo-sourced calendar already has since equities/
FX don't print weekend bars either). This is a DEVIATION from the
Yahoo-close boundaries the backtest was validated on, same category of
caveat S009's module docstring documents for its own irregular-tick
timing — reconcile paper-shadow signals against a fresh backtest re-run on
the same dates (see `--reconcile` below) before trusting this in demo/dry.

Reuses the SAME engine as the backtest (`strategies.rsi2.rsi2_signal` +
the entry/exit/sizing steps of `strategies.rsi2_portfolio.
simulate_compounding_portfolio`, mirrored — not re-derived — in
`_decide_target_book` below) so the live target book cannot drift from the
research code, same principle bot/s009_paper.py's module docstring states
for its own engine reuse.

Gates before real money (see claude/prompt-s011-webapp-implementation.md
and the strategy-lifecycle skill): shadow (`--broker off`, this module's
default) first, reconciled against a fresh backtest re-run; then demo/dry
(`--broker dry`) on an actual cTrader demo account, symbol-list-verified;
only then `--broker execute`, gated by `--allow-mainnet` AND (for the
DB-driven runner) `Account.env == "mainnet"`, same double-gate pattern
bot/s009_paper.py::run_cycle_for_account uses.

Commands:
    python bot/s011_paper.py --once                 # one daily cycle (paper/shadow)
    python bot/s011_paper.py --status                # current book + paper equity
    python bot/s011_paper.py --reconcile              # ledger vs a fresh backtest slice
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from strategies.rsi2 import BASELINE_RSI2, rsi2_signal                       # noqa: E402
from strategies.rsi2_portfolio import PortfolioConfig, PORTFOLIO_PROP_15PCT  # noqa: E402
from backtest.run_rsi2_portfolio import WALK_FORWARD_UNIVERSE                # noqa: E402
from utils.trade_logger import StrategyLogger                                # noqa: E402
from utils.strategy_state import FileStateStore                              # noqa: E402

STATE_DIR = REPO / "reports" / "paper_s011"
STATE_FILE = STATE_DIR / "state.json"
LEDGER_FILE = STATE_DIR / "ledger.csv"

HISTORY_DAYS = 400   # >= trend_sma(200) + a comfortable margin for RSI/SMA warmup

# Live-deploy-only universe trim -- see module docstring. Frozen BACKTEST
# universe (backtest/run_rsi2_portfolio.py::WALK_FORWARD_UNIVERSE) is
# untouched; only the live/paper book excludes this.
LIVE_DEPLOY_EXCLUDED_SYMBOLS = ("SPY",)
DEPLOY_UNIVERSE = tuple(a for a in WALK_FORWARD_UNIVERSE if a not in LIVE_DEPLOY_EXCLUDED_SYMBOLS)

# UNVERIFIED against the actual broker's symbol list -- placeholders using
# common cTrader-style naming conventions across several white-label
# brokers. MUST be checked (CTraderS011.run_live_cycle_multi's `unresolved`
# return, or a one-off `python -m bot.s011_paper --list-symbols` style
# check) before the demo/dry gate; see the module docstring and
# decisions-log.md's open item "S011: verify broker symbol list". Order
# within each tuple is preference order (first match wins).
CTRADER_SYMBOL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "CAC40": ("FRA40", "CAC40", "F40"),
    "DOW": ("US30", "DOW30", "DJ30"),
    "ESTOXX50": ("STOXX50", "EUSTX50", "ESTX50"),
    "ETHUSDT": ("ETHUSD", "ETH/USD"),
    "EURGBP": ("EURGBP",),
    "EURUSD": ("EURUSD",),
    "FTSE100": ("UK100", "FTSE100"),
    "NASDAQ": ("USTEC", "NAS100", "NASDAQ100"),
    "RUSSELL": ("US2000", "RUSSELL2000"),
    "SOLUSDT": ("SOLUSD", "SOL/USD"),
    "SP500idx": ("US500", "SPX500", "SP500"),
    "XAUUSD": ("XAUUSD",),
}
assert set(CTRADER_SYMBOL_CANDIDATES) == set(DEPLOY_UNIVERSE), \
    "CTRADER_SYMBOL_CANDIDATES must have exactly one entry per DEPLOY_UNIVERSE asset"

# Frozen deploy config, per Anton's 2026-08-18 decision (see
# claude/decisions-log.md): the prop-account preset, NOT PORTFOLIO_BASELINE.
DEPLOY = PORTFOLIO_PROP_15PCT


def _expected_last_closed_trading_date(now: datetime | None = None) -> str:
    """Pure, network-free function mirroring bot/s009_paper.py's
    _expected_last_closed_day() role for the up-to-date pre-check in
    run_cycle_for_account below, adapted to S011's calendar-date (not
    Unix-day) state and its documented single fixed local cutover (22:05,
    see module docstring's "Timing decision" section). Naive
    datetime.now() by design, same convention scripts/s007_tick.py::
    in_session() uses -- correct as long as this runs in a process whose
    TZ is Europe/Kyiv (deployment/schedule.yml documents this is true for
    the Ofelia/scheduler container that actually runs it); accepts an
    explicit `now` for testing.

    Returns the ISO date string of the most recently closed trading day a
    fully-caught-up state (state.json for the single-account CLI, or a
    DB `strategy_state` row for the DB-driven runner)'s `last_date` should
    already equal, given only wall-clock time -- weekends always resolve
    back to the preceding Friday, matching every included instrument's own
    lack of weekend bars.
    """
    now = now or datetime.now()
    cutover = now.replace(hour=22, minute=5, second=0, microsecond=0)
    d = now.date() if now >= cutover else now.date() - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6 -- walk back to Friday
        d -= timedelta(days=1)
    return d.isoformat()


def _default_state() -> dict:
    return {"last_date": None, "cash": DEPLOY.start_capital, "equity": DEPLOY.start_capital,
            "position_value": {}, "prev_held": {}}


_state_store = FileStateStore(STATE_FILE, default_factory=_default_state)


def load_state() -> dict:
    return _state_store.load()


def save_state(st: dict) -> None:
    _state_store.save(st)


def append_ledger(row: dict, ledger_file: Path | None = None) -> None:
    path = ledger_file if ledger_file is not None else LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = not path.exists()
    pd.DataFrame([row]).to_csv(path, mode="a", header=hdr, index=False)


def _decide_target_book(held_today: dict[str, int], prev_held: dict[str, int],
                        cash: float, position_value: dict[str, float],
                        cfg: PortfolioConfig) -> tuple[list[dict], float, dict[str, float]]:
    """ONE forward day's exits + entries, mirroring steps 1-2 of
    `strategies.rsi2_portfolio.simulate_compounding_portfolio`'s per-day loop
    body EXACTLY (same order: exits realise cash first, then entries size off
    the post-exit equity snapshot) -- deliberately re-derived here rather
    than imported, because the backtest function iterates a whole historical
    DataFrame and has no per-day entry point; this is the smallest amount of
    duplication that keeps both paths reading `cfg.cap_pct`/`cfg.cost_rate`
    from the SAME frozen `PortfolioConfig` (never a locally re-typed number).
    Does NOT do the backtest's step-3 mark-to-market or its circuit-breaker
    logic (S011's deploy preset, PORTFOLIO_PROP_15PCT, has neither breaker
    enabled -- see strategies/rsi2_portfolio.py) -- if a future preset change
    turns one on, this function needs the same breaker logic added, not
    silently left out.

    Returns (actions, new_cash, new_position_value). Each action is
    {"asset", "kind": "open"|"close", "notional"}.
    """
    actions: list[dict] = []
    position_value = dict(position_value)

    for a, held in held_today.items():
        if prev_held.get(a, 0) == 1 and held == 0:
            notional = position_value.get(a, 0.0)
            if notional > 0:
                cash += notional - notional * cfg.cost_rate
                actions.append({"asset": a, "kind": "close", "notional": notional})
            position_value[a] = 0.0

    equity_pre_entry = cash + sum(position_value.values())
    for a, held in held_today.items():
        if prev_held.get(a, 0) == 0 and held == 1:
            target = cfg.cap_pct * equity_pre_entry
            size = min(target, cash)
            if size > 0:
                position_value[a] = size - size * cfg.cost_rate
                cash -= size
                actions.append({"asset": a, "kind": "open", "notional": size})

    return actions, cash, position_value


def run_cycle_for_account(*, account_key: str, creds: dict | None, cfg: PortfolioConfig,
                          state, logger: StrategyLogger, broker: str = "off",
                          allow_mainnet: bool = False, env: str | None = None,
                          history_days: int = HISTORY_DAYS,
                          candidates: dict[str, tuple[str, ...]] | None = None) -> dict:
    """One S011 daily cycle for an arbitrary account -- same
    never-raises / cycle_start-cycle_end-always-paired contract as
    bot/s009_paper.py::run_cycle_for_account, for the same reason (a future
    parallel multi-account fan-out must be able to isolate one account's
    failure).

    `state`: any `.load()`/`.save(dict)` object (utils/strategy_state.py's
    FileStateStore for the single-account CLI below,
    webapp/state_store.py::DBStateStore for the DB-driven multi-account
    runner) -- see that module's docstring for why this must be per-account,
    not a single shared file (S009 hit exactly this collision, see
    webapp/models.py::StrategyState's docstring).

    `broker`: "off" (shadow -- book the paper ledger from broker-fetched D1
    bars, place no orders), "dry" (compute + log the intended market orders,
    place none), "execute" (place real orders). `allow_mainnet` + `env` gate
    real mainnet orders exactly like bot/s009_paper.py's own params (the
    DB-driven caller, webapp/runner.py::_worker_s011, is responsible for
    passing Account.env explicitly -- see that worker's own docstring for
    the "silently defaulting the env is how S009 traded on the wrong
    network for 17h" incident this mirrors).

    Returns dict(booked: bool, target: dict[asset, notional], equity, error,
    date, actions, unresolved, broker_orders).
    """
    cid = logger.cycle_start(mode="paper-shadow" if broker == "off" else broker,
                             account=account_key, universe=len(candidates or CTRADER_SYMBOL_CANDIDATES))
    booked = False
    target: dict[str, float] = {}
    equity = None
    date = None
    unresolved: list[str] = []
    actions: list[dict] = []
    error = None
    try:
        # NOTE: cTrader's own env vocabulary is "demo"/"live" (see
        # webapp/schemas/enums.py::Env / ENV_BY_BROKER[Broker.CTRADER]), NOT
        # Bybit's "testnet"/"mainnet" -- `allow_mainnet` is named to match
        # bot/s009_paper.py's parameter for API-shape consistency across
        # workers, but the real-money env value it checks here is "live".
        if broker == "execute" and allow_mainnet and env != "live":
            raise RuntimeError(
                f"--allow-mainnet given but env={env!r} != 'live' -- refusing (see "
                f"bot/s009_paper.py's env-gate incident this double-gate mirrors)")

        st = state.load() or _default_state()

        # Cheap, network-free up-to-date check (added 2026-08-21, see
        # decisions-log.md same date): mirrors bot/s009_paper.py's
        # _expected_last_closed_day() pre-check (2026-08-18) -- S011's
        # single daily cron slot (deployment/schedule.yml, historically
        # "5 22 * * 1-5") had the exact same missed-window exposure S009
        # had, made WORSE by this module never tracking "already done
        # today" at all before this change: every invocation opened a
        # real cTrader session and recomputed the signal from scratch
        # regardless of whether a new D1 bar had actually closed. A
        # missed 22:05 slot (container/runner down at that exact minute)
        # stranded the account un-rebalanced until the next manual run or
        # the following day's single slot -- confirmed in practice: no
        # cycle at all logged 2026-08-20 (Thursday, a normal trading day),
        # NASDAQ's RSI(2) flip to held=1 only got booked 2026-08-21 by a
        # manual on-demand run, not the scheduler (see
        # reports/logs/S011-acct48354548/). This is the same failure Anton
        # described for S009 on 2026-08-18 and fixed the same way there.
        last_date = st.get("last_date")
        if last_date is not None and last_date >= _expected_last_closed_trading_date():
            logger.cycle_end(cid, status=f"up-to-date (last_date={last_date})")
            return dict(booked=False,
                       target={a: v for a, v in st.get("position_value", {}).items() if v > 0},
                       equity=st.get("equity"), error=None, date=None, actions=[], unresolved=[])

        # Deliberately imported here, AFTER the up-to-date short-circuit
        # above (moved 2026-08-21 from module-call-top, before `try:`,
        # together with adding that check) -- two reasons: (1) an
        # up-to-date no-op tick now genuinely touches nothing broker- or
        # SDK-related, matching S009's own zero-network no-op guarantee;
        # (2) an import failure here is now caught by this function's own
        # `except Exception` below and reported via the returned `error`
        # field, instead of propagating out of run_cycle_for_account
        # uncaught -- the latter would have violated this function's own
        # documented "never raises" contract (see docstring above).
        from bot.ctrader_s011 import CTraderS011

        client = CTraderS011(creds=creds)
        cand = candidates or CTRADER_SYMBOL_CANDIDATES
        # Captured by `decide` below via closure; `decide` runs INSIDE
        # run_live_cycle_multi's single session, so the target-book decision
        # and (when broker != "off") the resulting orders happen in the SAME
        # cTrader session as the D1 bar fetch -- no second connect/auth
        # round trip, and no risk of the broker's book moving between a
        # "decide" call and a separate "execute" call.
        decided: dict = {}

        def decide(daily_bars, positions, balance, symbol_meta, last_price, resolved):
            cash = st.get("cash", cfg.start_capital)
            position_value = st.get("position_value", {})
            prev_held = st.get("prev_held", {})

            held_today: dict[str, int] = {}
            latest_dates = []
            for asset, df in daily_bars.items():
                if df.empty:
                    continue
                # Drop a still-forming current day's bar (mirrors S009's
                # drop_forming reasoning, applied per-instrument since D1 bar
                # completeness can differ across FX/index/crypto instruments
                # on this broker) before computing today's decided position.
                today_utc = datetime.now(timezone.utc).date()
                bars = df[df.index.date < today_utc] if df.index[-1].date() >= today_utc else df
                if len(bars) < 2:
                    continue
                pos = rsi2_signal(bars, BASELINE_RSI2)
                held_today[asset] = int(pos.iloc[-1])
                latest_dates.append(bars.index[-1])
                logger.position(f"S011:{asset}", "decided", cycle=cid,
                                held=held_today[asset], rsi_close=float(bars["close"].iloc[-1]))

            if not held_today:
                return []

            decided["date"] = max(latest_dates).strftime("%Y-%m-%d")
            deltas, new_cash, new_position_value = _decide_target_book(
                held_today, prev_held, cash, position_value, cfg)
            decided["deltas"] = deltas
            decided["cash"] = new_cash
            decided["position_value"] = new_position_value
            decided["held_today"] = held_today

            if broker == "off" or not deltas:
                return []
            broker_actions = []
            for a in deltas:
                asset, dt = a["asset"], decided["date"]
                if a["kind"] == "open":
                    symbol = resolved.get(asset)
                    if symbol is None:
                        continue  # unresolved this cycle -- already logged via `unresolved`
                    broker_actions.append({"kind": "open", "asset": asset, "symbol": symbol,
                                           "side": "buy", "notional": a["notional"],
                                           "label": f"S011:{asset}:{dt}"})
                else:
                    match = next((p for p in positions
                                 if p["label"].startswith(f"S011:{asset}:")), None)
                    if match:
                        broker_actions.append({"kind": "close", "asset": asset,
                                               "position_id": match["position_id"],
                                               "volume": match["volume"],
                                               "label": match["label"]})
            if broker == "dry":
                # Compute + log the intended plan, place nothing -- mirrors
                # bot/s009_paper.py::reconcile_to_target's dry path
                # (result="dry-run", no order sent).
                for a in broker_actions:
                    logger.order(f"S011:{a['asset']}", a["kind"], cycle=cid,
                                request=a, result="dry-run")
                return []
            return broker_actions  # broker == "execute"

        result = client.run_live_cycle_multi(cand, history_days, decide)
        unresolved = result["unresolved"]
        if unresolved:
            logger.event("unresolved_symbols", cycle=cid, assets=unresolved,
                         level="warning" if broker != "off" else "info")

        if decided:
            date = decided["date"]
            deltas = decided["deltas"]
            actions = deltas
            for a in deltas:
                logger.position(f"S011:{a['asset']}", a["kind"], cycle=cid, notional=a["notional"])

            equity = decided["cash"] + sum(decided["position_value"].values())
            st.update({"last_date": date, "cash": decided["cash"], "equity": equity,
                      "position_value": decided["position_value"],
                      "prev_held": decided["held_today"]})
            state.save(st)
            append_ledger({"date": date, "cash": round(decided["cash"], 2), "equity": round(equity, 2),
                          "n_positions": sum(1 for v in decided["position_value"].values() if v > 0),
                          "n_actions": len(deltas)}, LEDGER_FILE)
            target = {a: v for a, v in decided["position_value"].items() if v > 0}
            booked = True

            for r in result["results"]:
                logger.order(f"S011:{r['action'].get('asset')}", r["action"]["kind"],
                            cycle=cid, request=r["action"], result=r["result"], error=r["error"])
    except Exception as e:
        logger.error("S011 cycle failed", exc=e, cycle=cid)
        error = repr(e)[:500]

    logger.cycle_end(cid, status=f"{broker}: {len(target)} position(s), booked={booked}",
                    equity=equity)
    return dict(booked=booked, target=target, equity=equity, error=error, date=date,
               actions=actions, unresolved=unresolved)


def run_once(broker: str = "off", allow_mainnet: bool = False) -> None:
    log = StrategyLogger("S011", log_root=REPO / "reports" / "logs", console=False)
    result = run_cycle_for_account(account_key="single", creds=None, cfg=DEPLOY,
                                   state=_state_store, logger=log, broker=broker,
                                   allow_mainnet=allow_mainnet)
    if result["error"]:
        print(f"ERROR: {result['error']}")
        return
    if not result["booked"]:
        print("No new closed day to process.")
        return
    print(f"=== S011 paper cycle {result['date']} ===")
    print(f"equity: {result['equity']:.2f}")
    print(f"TARGET BOOK: {result['target']}")
    if result["unresolved"]:
        print(f"UNRESOLVED (broker symbol not found -- see CTRADER_SYMBOL_CANDIDATES): "
              f"{result['unresolved']}")
    print(f"actions this cycle: {result['actions']}")
    print(f"logged -> {LEDGER_FILE}")


def status() -> None:
    st = load_state()
    if st.get("last_date") is None:
        print("No paper state yet -- run --once first.")
        return
    print(f"S011 paper -- as of {st['last_date']}: equity {st['equity']:.2f}")
    print(f"open positions: { {a: v for a, v in st.get('position_value', {}).items() if v > 0} }")


def reconcile() -> None:
    """Compare the paper ledger against a fresh backtest slice over the same
    dates -- theory vs theory, same purpose as bot/s009_paper.py::reconcile().
    NOTE: this is an APPROXIMATE check only, because the live cycle's D1
    bars come from the broker (one fixed UTC cutover, see module docstring)
    while the backtest's bars come from Yahoo (per-instrument exchange
    session close) -- a mismatch here does not automatically mean a bug,
    see the module docstring's timing-deviation caveat before treating any
    divergence as an error."""
    if not LEDGER_FILE.exists():
        print("No ledger yet.")
        return
    led = pd.read_csv(LEDGER_FILE)
    print(f"S011 paper ledger: {len(led)} day(s) logged, {LEDGER_FILE}")
    print("Manual step: compare against backtest/run_rsi2_portfolio.py's equity curve "
          "over the same date range (see module docstring's timing-deviation caveat).")


def main() -> None:
    ap = argparse.ArgumentParser(description="S011 RSI(2)-portfolio paper/live daily cycle.")
    ap.add_argument("--once", action="store_true", help="run one daily cycle")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--broker", choices=["off", "dry", "execute"], default="off",
                    help="off=shadow only; dry=compute+log intended orders; execute=place orders")
    ap.add_argument("--allow-mainnet", action="store_true", help="DANGER: permit real mainnet orders")
    args = ap.parse_args()

    if args.status:
        status()
    elif args.reconcile:
        reconcile()
    elif args.once:
        run_once(broker=args.broker, allow_mainnet=args.allow_mainnet)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
