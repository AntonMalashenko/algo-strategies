"""Deribit option-board snapshot collector -- ALGODEV-9.

Captures the live Deribit option chain (BTC + ETH) once a day and appends it
to a versioned, dated parquet archive. This is the project's one genuinely
irreversible data feed: expired options vanish from Deribit's API for good,
so a day missed here can never be backfilled (see
claude/infra-scheduled-jobs-spec.md rev.2 SS3, the Claude Project doc this
script implements).

Two public (unauthenticated) Deribit endpoints, joined by `instrument_name`:

  public/get_book_summary_by_currency?currency={BTC,ETH}&kind=option
      One call, whole chain. Gives bid/ask/mid/mark price, mark_iv,
      open_interest, volume, underlying_price, ... (see BOOK_SUMMARY_FIELDS).
  public/get_instruments?currency={BTC,ETH}&kind=option&expired=false
      Gives what the book-summary call omits: strike, option_type,
      expiration_timestamp, tick_size, maker/taker commission.

Deliberately NOT stored: delta, bid_iv, ask_iv, moneyness, time-to-expiry.
All of these are derivable later from strike + expiry + type + underlying
price + mark_iv, which the raw snapshot already has -- baking a derived
value (someone's model) into the archive freezes that model's assumptions
forever, and if it turns out wrong there is nothing to recompute from. A raw
field can always be recomputed; see spec SS3.2. `--with-greeks` is the
explicit, opt-in escape hatch for when a live comparison against Deribit's
own greeks is actually wanted.

Watermark-driven, not tick-driven (spec SS2): this script looks at what it
already has and decides whether today's slot is still open, rather than
trusting that it was invoked at the right time. A run that finds "already
captured" is a fast, network-free no-op -- exit 0, nothing written, nothing
logged beyond a skip event.

CLI:
    python -m scripts.fetch_deribit_snapshot [--currencies BTC ETH]
        [--with-greeks] [--dry-run] [--force]

Deliberately absent: --since/--until. This is a capture-now task -- a
snapshot of a live order book at a past moment does not exist anywhere to
backfill (spec SS2's "for capture-now tasks backfill is impossible in
principle"). Passing an unrecognized flag is argparse's own job (exit
code 2), which is the correct failure mode here -- no bespoke rejection
code needed.

Exit codes (spec SS2): 0 success (including "nothing to do" -- already
captured, or --dry-run); 1 transient failure (network, rate limit -- the
next invocation will retry, watermark untouched); 2 fatal (the API
response is missing an expected field, i.e. the contract changed under us).
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.strategy_state import FileStateStore  # noqa: E402
from utils.trade_logger import StrategyLogger  # noqa: E402

LOG_GROUP = "DERIBIT_SNAPSHOT"
LOG_ROOT = ROOT / "reports" / "logs"

DERIBIT_API_BASE = "https://www.deribit.com/api/v2"
DEFAULT_CURRENCIES = ("BTC", "ETH")

# Fields kept verbatim from get_book_summary_by_currency -- see spec SS3.1,
# checked against the live API 2026-08-08. `mark_iv` is deliberately the
# only IV column: bid_iv/ask_iv are not in this endpoint at all (only in
# per-instrument public/ticker, see --with-greeks).
BOOK_SUMMARY_FIELDS = (
    "instrument_name", "bid_price", "ask_price", "mid_price", "mark_price",
    "last", "mark_iv", "open_interest", "volume", "volume_usd",
    "underlying_price", "underlying_index", "interest_rate",
    "estimated_delivery_price", "creation_timestamp",
)
# Fields kept verbatim from get_instruments -- the strike/expiry/type that
# book-summary omits, plus the commission schedule (spec SS3.1: "пригодятся
# для модели издержек -- их не надо будет угадывать").
INSTRUMENT_FIELDS = (
    "instrument_name", "strike", "option_type", "expiration_timestamp",
    "tick_size", "min_trade_amount", "maker_commission", "taker_commission",
)
# public/ticker fields pulled only when --with-greeks is passed.
GREEKS_TICKER_FIELDS = ("instrument_name", "bid_iv", "ask_iv")

REQUEST_TIMEOUT_SECONDS = 30
# Between per-instrument public/ticker calls only (--with-greeks path,
# ~1650 calls/day across both currencies per spec SS3.1) -- the two
# whole-chain calls (book-summary, instruments) need no pacing at all.
GREEKS_REQUEST_SLEEP_SECONDS = 0.1

# Deribit's daily snapshot window per spec SS3.4: 16:00 UTC covers the
# Friday-16:00-UTC entry of the reference weekly short-strangle construction
# this feeds (S012 candidate). A run is "for today" if it happens at or
# after this UTC hour; a run earlier in the day is still today's window
# but hasn't opened yet is not a case this script special-cases -- it is
# watermark-driven (SS2), not clock-gated: an early manual run just captures
# early, same as any other successful capture.
SNAPSHOT_HOUR_UTC = 16

STATE_PATH = ROOT / "data" / "state" / "deribit_snapshot.json"
OUT_ROOT = ROOT / "data" / "raw" / "deribit_options"

EXIT_OK = 0
EXIT_TRANSIENT = 1
EXIT_FATAL = 2


class TransientFetchError(RuntimeError):
    """Network/rate-limit class of failure -- caller should exit 1, retry later."""


class FatalContractError(RuntimeError):
    """API response missing/reshaping an expected field -- caller should exit 2."""


def _default_state() -> dict:
    # last_attempt_ts: every real attempt, success or not (SS2's "когда
    # задача успешно отработала" -- attempted, not necessarily productive).
    # last_snapshot_date: the calendar date (UTC) actually closed and
    # written. These are kept separate on purpose -- SS2 forbids conflating
    # "ran" with "actually captured data".
    return {"last_attempt_ts": None, "last_snapshot_date": None}


def _state_store() -> FileStateStore:
    return FileStateStore(STATE_PATH, _default_state)


def _today_utc(now: datetime.datetime | None = None) -> datetime.date:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).date()


def _deribit_get(method: str, params: dict) -> dict:
    """One Deribit public JSON-RPC-over-HTTP call. Raises TransientFetchError
    for network/HTTP failures (retry next invocation) and FatalContractError
    if the envelope itself is malformed (missing "result" -- the API
    contract changed, not a blip)."""
    url = f"{DERIBIT_API_BASE}/{method}"
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise TransientFetchError(f"{method} request failed: {exc}") from exc
    except ValueError as exc:  # non-JSON body
        raise TransientFetchError(f"{method} returned non-JSON body: {exc}") from exc
    if "result" not in payload:
        raise FatalContractError(f"{method} response missing 'result': {payload!r}")
    return payload["result"]


def fetch_book_summary(currency: str) -> pd.DataFrame:
    rows = _deribit_get("public/get_book_summary_by_currency",
                        {"currency": currency, "kind": "option"})
    if not rows:
        return pd.DataFrame(columns=BOOK_SUMMARY_FIELDS)
    try:
        df = pd.DataFrame(rows)[list(BOOK_SUMMARY_FIELDS)]
    except KeyError as exc:
        raise FatalContractError(
            f"get_book_summary_by_currency({currency}) missing expected field: {exc}") from exc
    return df


def fetch_instruments(currency: str) -> pd.DataFrame:
    rows = _deribit_get("public/get_instruments",
                        {"currency": currency, "kind": "option", "expired": False})
    if not rows:
        return pd.DataFrame(columns=INSTRUMENT_FIELDS)
    try:
        df = pd.DataFrame(rows)[list(INSTRUMENT_FIELDS)]
    except KeyError as exc:
        raise FatalContractError(
            f"get_instruments({currency}) missing expected field: {exc}") from exc
    return df


def fetch_greeks(instrument_names: list[str]) -> pd.DataFrame:
    """--with-greeks only: one public/ticker call per instrument. Off by
    default (spec SS3.2) -- expensive (~1650 calls/day across BTC+ETH) and
    not needed for the raw archive; kept as a separate, clearly-labeled set
    of columns so raw fields are never silently mixed with someone's model
    output."""
    import time

    rows = []
    for name in instrument_names:
        result = _deribit_get("public/ticker", {"instrument_name": name})
        greeks = result.get("greeks") or {}
        rows.append({
            "instrument_name": name,
            "bid_iv": result.get("bid_iv"),
            "ask_iv": result.get("ask_iv"),
            "delta": greeks.get("delta"),
        })
        time.sleep(GREEKS_REQUEST_SLEEP_SECONDS)
    return pd.DataFrame(rows)


def build_snapshot(currency: str, snapshot_ts_utc: str,
                    with_greeks: bool) -> pd.DataFrame:
    """One currency's snapshot: book-summary INNER JOINed with instruments
    on instrument_name (an instrument absent from either side is dropped,
    not fatal -- listing/delisting races between the two calls are expected,
    not a contract break)."""
    book = fetch_book_summary(currency)
    instr = fetch_instruments(currency)
    try:
        df = book.merge(instr, on="instrument_name", how="inner", validate="one_to_one")
    except pd.errors.MergeError as exc:
        # Deribit returning a duplicate instrument_name would mean the API
        # contract itself broke (uniqueness assumed throughout this module,
        # incl. the --with-greeks left-join) -- surface it as fatal rather
        # than silently duplicating or dropping rows.
        raise FatalContractError(
            f"get_book_summary/get_instruments({currency}) join found duplicate "
            f"instrument_name: {exc}") from exc
    df.insert(0, "snapshot_ts_utc", snapshot_ts_utc)
    df.insert(1, "currency", currency)
    if with_greeks and not df.empty:
        greeks = fetch_greeks(df["instrument_name"].tolist())
        df = df.merge(greeks, on="instrument_name", how="left")
    return df


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write via a temp file in the SAME directory, then os.replace() -- an
    interrupted write leaves no readable half-file, and a rerun for the same
    partition overwrites cleanly (idempotent per spec SS2)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".parquet.tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        df.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def run(currencies: tuple[str, ...] = DEFAULT_CURRENCIES, with_greeks: bool = False,
        dry_run: bool = False, force: bool = False,
        now: datetime.datetime | None = None, log: StrategyLogger | None = None,
        state_store: FileStateStore | None = None, out_root: Path | None = None) -> int:
    """One capture attempt. Returns the process exit code (spec SS2: 0 ok/
    nothing-to-do, 1 transient, 2 fatal) -- main() just forwards it via
    sys.exit(). `now`/`log`/`state_store`/`out_root` are overridable for
    tests; production supplies none and gets the real clock, log, watermark
    file and output tree."""
    log = log or StrategyLogger(LOG_GROUP, log_root=str(LOG_ROOT))
    store = state_store or _state_store()
    out_root = out_root or OUT_ROOT
    today = _today_utc(now)

    state = store.load()
    if not force and state.get("last_snapshot_date") == today.isoformat():
        log.event("skip_already_captured", date=today.isoformat())
        return EXIT_OK

    snapshot_ts_utc = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(
        datetime.timezone.utc).isoformat(timespec="seconds")
    state["last_attempt_ts"] = snapshot_ts_utc

    frames: dict[str, pd.DataFrame] = {}
    try:
        for currency in currencies:
            frames[currency] = build_snapshot(currency, snapshot_ts_utc, with_greeks)
    except TransientFetchError as exc:
        log.error(f"transient fetch failure for {today.isoformat()}", exc=exc)
        store.save(state)  # last_attempt_ts moves; last_snapshot_date does not
        return EXIT_TRANSIENT
    except FatalContractError as exc:
        log.error(f"Deribit API contract mismatch on {today.isoformat()}", exc=exc)
        store.save(state)
        return EXIT_FATAL

    total_rows = sum(len(df) for df in frames.values())
    if dry_run:
        for currency, df in frames.items():
            log.event("dry_run_snapshot", currency=currency, rows=len(df),
                      date=today.isoformat())
        return EXIT_OK

    partition_dir = out_root / f"date={today.isoformat()}"
    for currency, df in frames.items():
        _atomic_write_parquet(df, partition_dir / f"{currency}.parquet")
        log.event("snapshot_written", currency=currency, rows=len(df),
                  path=str(partition_dir / f"{currency}.parquet"))

    state["last_snapshot_date"] = today.isoformat()
    store.save(state)
    log.event("snapshot_complete", date=today.isoformat(), total_rows=total_rows,
              currencies=list(currencies), with_greeks=with_greeks)
    return EXIT_OK


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Deribit option-board daily snapshot collector (capture-now, "
                    "watermark-driven -- no --since/--until, see module docstring).")
    ap.add_argument("--currencies", nargs="+", default=list(DEFAULT_CURRENCIES))
    ap.add_argument("--with-greeks", action="store_true",
                    help="also pull bid_iv/ask_iv/delta via public/ticker "
                         "(~1650 extra requests/day across both currencies)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report row counts; do not write or move the watermark")
    ap.add_argument("--force", action="store_true",
                    help="ignore the watermark and capture again even if today's slot "
                         "is already recorded (idempotent overwrite, not a duplicate)")
    args = ap.parse_args()
    exit_code = run(currencies=tuple(args.currencies), with_greeks=args.with_greeks,
                    dry_run=args.dry_run, force=args.force)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
