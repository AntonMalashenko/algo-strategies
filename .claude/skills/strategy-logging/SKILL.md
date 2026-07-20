---
name: strategy-logging
description: >-
  How to use and extend the global structured logging system for strategy bots
  (`utils/trade_logger.py`, `StrategyLogger`). Use this skill whenever the task involves
  logging, debugging a bot's behavior, tracing a trade, reading the log files under
  `reports/logs/`, adding logging to a new strategy or to the runner, or auditing what a
  position did — even if the request only says "add logging", "why did it open that trade",
  "debug the bot", "trace this cycle", or "where are the logs". Read this before writing any
  logging code so every strategy stays on the same per-strategy, per-position layout instead
  of inventing ad-hoc prints that can't be replayed later.
---

# strategy-logging — reusable structured logging for bots

`utils/trade_logger.py` defines `StrategyLogger`: one logger per strategy, designed so a
single trade can be replayed and debugged in isolation months later. It is **global and
strategy-agnostic** — S004, S007, and any future strategy instantiate their own
`StrategyLogger(name)` and get the same layout for free. Do not write bespoke logging per
strategy; route everything through this class so the on-disk format stays uniform and
tooling/audits work across strategies.

## On-disk layout

Under `log_root` (default `reports/logs/`), each strategy gets its own group:

```
<STRATEGY>/
    <STRATEGY>.log              # human-readable, rotating (5 MB × 10 backups), ALL cycles
    events-YYYY-MM-DD.jsonl     # every structured event, one JSON object per line
    cycles-YYYY-MM-DD.jsonl     # one record per reconcile cycle (start + end)
    positions/
        <label>.jsonl           # ONE FILE PER POSITION — its full lifecycle
```

The per-position file is the point: to see everything that happened to one trade
(desired → open → order request/result → add → close + reason), you read exactly one
file — `positions/<label>.jsonl` — instead of grepping a firehose. Every record carries
`ts` (ISO, local, ms), `strategy`, `cycle` (a correlation id), `kind`/`action`, plus the
caller's fields. Text and JSONL are written together: eyeball `<STRATEGY>.log`, or parse
the `.jsonl` streams programmatically.

## The cycle correlation id

A bot runs in **reconcile cycles** (the runner wakes, looks at desired vs broker state,
acts). `cycle_start(**fields)` returns a `cid` like `20260717-192607-0001`; pass that
`cid` into every `position(...)`, `order(...)`, `event(...)`, and `cycle_end(cid, ...)`
call in that cycle. That is what lets you reconstruct "in this one wake-up, the bot saw X
and did Y and Z" — never omit it.

## API (all thread-safe, each record flushed immediately)

```python
from utils.trade_logger import StrategyLogger

log = StrategyLogger("S007")                      # or f"S007-acct{ctid}" per account
cid = log.cycle_start(mode="live", account=555001, preset="BASELINE_S007",
                      in_window=True, direction="up",
                      context={"rh": 18763.8, "rl": 18721.7, "mid": 18742.7})

log.position("S007:2024-05-10:0", "open", cycle=cid, side="buy",
             entry=18768.8, sl=18742.7, tp=18805.9, is_add=False)

log.order("S007:2024-05-10:0", "place_market", cycle=cid,
          request={"side": "buy", "sl": 18742.7, "tp": 18805.9, "lot": 0.01},
          result={"order_id": 123})               # or error=exc on failure

log.event("state", cycle=cid, in_window=True, n_desired=3)   # any structured event
log.position("S007:2024-05-10:0", "close", cycle=cid, reason="target")
log.error("cycle failed", exc=some_exception, cycle=cid)     # ERROR + traceback
log.cycle_end(cid, status="live: 3 desired, 1 new")
```

- `event(kind, cycle=None, level=INFO, text=None, **fields)` → `events-<date>.jsonl` + text.
- `cycle_start(**fields) -> cid` / `cycle_end(cid, **fields)` → `cycles-<date>.jsonl`.
- `position(label, action, cycle=None, **fields)` → `positions/<label>.jsonl` + event log.
  `action` is free-form lifecycle: `desired`, `open`, `add`, `close`, `stop`, ...
- `order(label, op, cycle=None, request=?, result=?, error=?)` → per-position file + event.
  Always log the `request` alongside `result`/`error` so a broker call is fully auditable.
- `debug/info/warning` → text log only; `error(msg, exc=, cycle=)` → structured + traceback.

## Conventions to keep

- **One logger per strategy**; for multi-account runs, name it
  `f"{strategy}-acct{ctid}"` so each account gets its own group under `reports/logs/`
  (the runner already does this in `webapp/runner.py::_logger_for`).
- **Position labels are stable identifiers** (e.g. `S007:<date>:<bar_index>`). The same
  label across cycles appends to the same per-position file — that continuity is the
  feature; don't regenerate labels per cycle.
- **Log the intent and the outcome**: for every broker action log both the `request` you
  sent and the `result`/`error` you got back. When debugging "why did it do that", the
  `context=` snapshot on `cycle_start` (range high/low/mid, direction, scenario) is usually
  the answer.
- Records must stay JSON-serializable; non-serializable values fall back to `str` via
  `default=str`, so prefer plain dicts/numbers/strings in `**fields`.
- Never put secrets (tokens, client secrets, passwords) into any log field.

## Reading the logs when debugging

Start from `positions/<label>.jsonl` for a single suspect trade. Widen to
`cycles-<date>.jsonl` to see what each reconcile cycle decided, and `events-<date>.jsonl`
for the full ordered stream. Filter any of them by the `cycle` id to isolate one wake-up.
`<STRATEGY>.log` is the quick human scan. For a new strategy, just instantiate
`StrategyLogger("<NAME>")` and call the same methods — the layout appears automatically.
