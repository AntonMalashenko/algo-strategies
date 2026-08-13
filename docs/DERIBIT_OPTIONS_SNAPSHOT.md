# Deribit option-board snapshot -- data schema (v1)

Produced by `scripts/fetch_deribit_snapshot.py` (ALGODEV-9). Companion to
`claude/infra-scheduled-jobs-spec.md` rev.2 (Claude Project) SS3, which this
file implements the "documented in `docs/`" half of.

## Why this exists

Deribit's public API only exposes the live option chain -- expired options
disappear from it permanently. This collector is the project's one source
of this data; a day it misses cannot be recovered by any later backfill
(see the spec's SS2 "capture-now" contract: `--since`/`--until` are not
supported, on purpose).

## Layout

```
data/raw/deribit_options/date=<YYYY-MM-DD>/<CURRENCY>.parquet
```

One partition directory per UTC calendar date the snapshot was captured
for, one parquet file per currency (`BTC.parquet`, `ETH.parquet`). Written
atomically (temp file in the same directory, then `os.replace()`) and
idempotently (rerunning `--force` for a date overwrites that file with the
same content, never appends a duplicate).

## Row grain

One row = one option instrument, as reported by Deribit's public API at
`snapshot_ts_utc`.

## Columns (v1)

| Column | Source | Type | Notes |
|---|---|---|---|
| `snapshot_ts_utc` | derived | string (ISO 8601, UTC) | Same value for every row in a partition -- the moment the collector ran, not per-field. |
| `currency` | derived | string | `"BTC"` or `"ETH"`. |
| `instrument_name` | both endpoints | string | Join key between `get_book_summary_by_currency` and `get_instruments`. |
| `bid_price` | book summary | float | |
| `ask_price` | book summary | float | |
| `mid_price` | book summary | float | |
| `mark_price` | book summary | float | |
| `last` | book summary | float | Last traded price. |
| `mark_iv` | book summary | float | **Only** IV column in the default (non-`--with-greeks`) snapshot -- Deribit's book-summary endpoint has no bid/ask IV. |
| `open_interest` | book summary | float | |
| `volume` | book summary | float | |
| `volume_usd` | book summary | float | |
| `underlying_price` | book summary | float | |
| `underlying_index` | book summary | string | |
| `interest_rate` | book summary | float | |
| `estimated_delivery_price` | book summary | float | |
| `creation_timestamp` | book summary | int (epoch ms) | |
| `strike` | instruments | float | |
| `option_type` | instruments | string | `"call"` / `"put"`. |
| `expiration_timestamp` | instruments | int (epoch ms) | |
| `tick_size` | instruments | float | |
| `min_trade_amount` | instruments | float | |
| `maker_commission` | instruments | float | For a cost model -- no need to guess it later. |
| `taker_commission` | instruments | float | |

### `--with-greeks` (opt-in, off by default)

Adds, via one extra `public/ticker` call per instrument:

| Column | Notes |
|---|---|
| `bid_iv` | Not available anywhere else in the API. |
| `ask_iv` | Not available anywhere else in the API. |
| `delta` | Deribit's own greeks model -- an opinion, not ground truth; kept clearly separate from the raw columns above so it's never mistaken for one. |

## What is deliberately NOT stored

Delta (unless `--with-greeks`), bid/ask IV (unless `--with-greeks`),
moneyness, time-to-expiry. All are recomputable later from `strike` +
`expiration_timestamp` + `option_type` + `underlying_price` + `mark_iv`,
which are already in every row. Storing a derived value bakes in whatever
model computed it, permanently, in a way a raw field never does -- see the
spec's SS3.2 for the full reasoning.

## Volume (to be measured, not assumed)

Spec estimate: ~1.5-1.7k rows/day across BTC+ETH, single-digit MB/month
compressed. **Not yet confirmed against a real run** -- fill in the actual
figures here after the first production capture.

## Join semantics

`get_book_summary_by_currency` and `get_instruments` are two separate API
calls that can race with listing/delisting. The collector does an
**inner** join on `instrument_name`: an instrument present in only one
response is silently dropped from that day's snapshot rather than treated
as an error -- see `build_snapshot()` in `scripts/fetch_deribit_snapshot.py`.

## Schema versioning

This is v1. Any column added, renamed, or reinterpreted must bump the
version at the top of this file and note what changed and from which
partition date onward -- readers of older partitions need to know they
predate the change.
