---
name: trading-analysis
description: >-
  On-demand diagnostic that inspects a strategy's live/paper trading logs (and,
  read-only, the broker/exchange) and answers "is the bot behaving correctly right
  now, and if it has no open positions, is that a bug or expected behavior". Works
  for ANY strategy (S007, S009, and future ones) -- it is not tied to one strategy.
  Trigger phrases: "проанализируй торговлю <NNN/name>" (e.g. "проанализируй торговлю
  007", "...торговлю 009", "...торговлю funding_carry"), "analyze S0XX trading",
  "check the bot's behavior", "why are there no positions", "is this a bug or normal",
  "audit today's trades". ALWAYS run this full procedure (not a partial ad-hoc log
  read) whenever a request matches one of these phrases, for whichever strategy is
  named -- this is a standing instruction, not a one-off. Use this INSTEAD OF
  `strategy-logging` when the goal is "diagnose current behavior" rather than "how do
  I write to/read the log format" (read `strategy-logging` first if the on-disk layout
  itself is unfamiliar -- this skill assumes it).
---

# trading-analysis — diagnose a strategy's live behavior on demand

Answers one question, honestly and with evidence: **is the bot doing the right thing
right now, and if it isn't in a position, is that a bug or is that exactly what the
strategy is supposed to do here?** Never guess or pattern-match from memory of a past
conversation — every run re-reads the actual current logs/code/broker state, because
the answer changes every cycle.

This is strategy-agnostic by design: run it for S007, S009, or any strategy added
later. The specifics (session windows, signal fields, what "no position" can
legitimately mean) come from that strategy's own code, read fresh each time — never
hardcode one strategy's rules into your reasoning about another.

## Step -1 — pick the scope: this session, or "in principle"

The request is usually one of two different questions — decide which before reading
logs, since it changes how far back you look:

- **"Right now / this session"** (the default when nothing else is said, e.g. plain
  "проанализируй торговлю 007"): today's log only, per Steps 0-6 below. Answers
  "is today's behavior correct so far."
- **"In principle" / "in general" / "over time"** (e.g. "работает ли бот корректно в
  принципе", "за неделю", explicit date range): the question is whether the bot
  *reliably* executes the validated strategy, not just today. Widen Step 1 to the
  last N days of `events-<date>.jsonl` (or `cycles-<date>.jsonl` for a fast per-day
  summary) and Step 0's change-log read to the full tail, not just the most recent
  entries — the goal is spotting a **recurring** pattern (the same error shape on
  multiple days, a scheduler gap that keeps happening, ghost-trade frequency, desired-
  vs-broker mismatches) rather than judging one cycle. State the date range you
  actually covered in the answer, and call out explicitly if a pattern found today
  also shows up on earlier days (or doesn't) — that recurrence is exactly what turns
  "one weird cycle" into "a systemic issue worth fixing."

If genuinely ambiguous which scope is meant, default to session-level but say what a
wider check would additionally look for, rather than silently picking one.

## Step 0 — resolve which strategy

Map what the user said (a number like "007"/"009", or a name) to:

- **Log directory**: `reports/logs/<STRATEGY>/` (`strategy-logging`'s layout —
  `<STRATEGY>.log`, `events-<date>.jsonl`, `cycles-<date>.jsonl`, `positions/`).
  Strategy names in this repo are uppercase (`S007`, `S009`). A per-account runner
  may log under `<STRATEGY>-acct<id>/` instead/also (see Step 2) — check both.
- **Signal/engine code**: `bot/s0XX_*.py` and/or `strategies/<name>/` — this is
  where "what does a normal no-signal state look like" is actually defined; read it,
  don't assume it matches another strategy.
- **Scheduler**: `scripts/s0XX_tick.py` + a `deployment/*.plist` /
  `~/Library/LaunchAgents/*.plist`, and/or `webapp/runner.py` (DB-driven,
  multi-account). More than one implementation can exist at once during a migration
  (it does, as of 2026-08: S007 has both a legacy single-account launchd path and an
  unscheduled `webapp/runner.py` path) — Step 2 figures out which one is actually
  live, don't assume from the filename alone.
- **Known history**: read the tail of `.claude/change-log/<component>.jsonl` for the
  strategy's package (`bot`, `scripts`, ...) and its strategy component (see
  `code-change-log`'s resolution table). This tells you what's *already* diagnosed
  and fixed/accepted — don't re-report a known, already-explained pattern as a fresh
  finding.

## Step 1 — pull the facts, in this order

1. `tail -n 40` (or more if the last cycle is far back) of `<STRATEGY>.log` — the
   human-readable stream, fastest way to see the last several cycles and any `ERROR`.
2. Today's `events-<date>.jsonl` — the full structured stream for the day: every
   `cycle_start`/`state`/`position`/`order`/`loop_heartbeat`/`loop_settled`/`error`
   record. Use this (not just the tail of the text log) to see the whole day's shape,
   not just the last cycle.
3. If a specific position/label is in question, its own
   `positions/<label>.jsonl` — full lifecycle in one file.
4. If the strategy runs through a DB-backed coordinator (`webapp/runner.py`), also
   query `AccountStrategy.status` / `last_cycle_at` / `last_error` and `Position` rows
   — but verify freshness first (Step 2); a stale DB row from an old backfill/manual
   test run must not be read as live status.

## Step 2 — confirm the runner is actually the one you think it is, and that it's alive

Don't assume the plist/cron entry you find is the one producing the log lines you're
reading. Check, in this order:

- `launchctl list | grep <label>` and `ls ~/Library/LaunchAgents/ | grep <label>` —
  is a job actually loaded?
- `crontab -l | grep <strategy>` — anything scheduled there (e.g. for
  `webapp/runner.py`, which this repo schedules via cron, not launchd)?
- `ps aux | grep <strategy>` — any stray long-running process (there shouldn't be;
  every scheduler here uses a stateless tick, not a persistent loop — a persistent
  process is itself worth flagging).
- Cross-check the log content itself: a legacy single-account run's `state` lines
  show one fixed account's balance; a `webapp/runner.py` worker's lines show a
  `<STRATEGY>-acct<id>` logger name and DB-sourced config. Whichever one has *recent*
  timestamps is the one actually live — a plist existing doesn't mean it's loaded, a
  DB row existing doesn't mean anything currently updates it, and a log directory
  existing (e.g. `S007-acct47939312/`, last written 2026-08-01) can be a one-off
  manual test run, not a running scheduler. Say which is which.

## Step 3 — filter out noise before calling anything a bug

- **A traceback whose frames include a path under `tests/`** was written by a test
  run that hit a module-level logger pointing at the real `reports/logs/<STRATEGY>/`
  directory (a known gap: some `run_cycle()`-style helpers don't accept an injectable
  log the way `tick()` does). That is test pollution, not a production incident —
  say so plainly and don't let it anchor the rest of the analysis. This has
  concretely happened before (`scripts/s007_tick.py`'s `run_cycle()`,
  2026-08-07) and is worth a quick `grep` for `tests/` in any traceback before
  treating it as real.
- A single `loop_heartbeat`/no-op tick is not evidence of anything; look at the
  *pattern* (gaps during session hours, repeated identical errors, a desired position
  that never converts to a broker order across several consecutive cycles).

## Step 4 — the actual verdict: is "no position" a bug?

Read the strategy's own signal function output for the most recent cycle(s) and
reason from ITS rules, not general intuition. Concretely, for S007
(`bot/s007_signals.py::plan_now`) the relevant fields are `in_window`, `flat`,
`filtered`, `direction`, `n_desired`, `day_done`, `resolved` (ghost trades — entered
**and** resolved within bars already elapsed before any live cycle could see the
position as still-open, so no broker order was ever possible), and
`broker_positions` vs `ours_open` from the cycle's `state` log line. For S009
(`bot/s009_paper.py`) it's the target book (`forward_target_book`) vs
`BybitExec.positions()` (read-only) and the funding-carry signal ranking. Every
strategy will have its own equivalent — find it in that strategy's code, don't
transplant S007's field names onto a different strategy.

**Normal, not a bug** (say so and explain briefly *why*, in plain language):
- Outside the strategy's trading window/session.
- A day-quality filter rejected today's setup and that verdict is final for the day
  (e.g. S007's `filtered=True` — Frankfurt range height out of bounds).
- No qualifying setup has formed yet, and the window is still open (still-polling
  state, not stuck).
- The day's target was already reached (`day_done`/`reached_tp`), including via a
  same-bar "ghost" resolution — quote the `resolved`/ghost record so the user sees
  entry/exit/R, and explain that further upside after the target is out of scope by
  design (the live code intentionally mirrors the backtest's day-level stop rule, not
  a bug in the polling).
- For a non-directional/portfolio strategy like S009: today's target book already
  matches the live broker book (no rebalance was needed), or the scheduler is
  correctly waiting for a day to close.

**Not normal — a real defect, report it as such:**
- A desired position/target leg exists but never reached the broker across multiple
  consecutive cycles with no explanatory `ghost`/`resolved` record and no filter
  verdict.
- A broker order was attempted and rejected — quote the exact error string (e.g. a
  past real one: `INVALID_REQUEST: Order price ... has more digits than symbol
  allows`) and check whether it's already fixed in the working tree (`git diff` on
  the relevant broker adapter) or still live.
- The scheduler itself isn't ticking during session hours (no recent heartbeat/cycle,
  no loaded launchd job, no cron entry) — an operational gap, not a strategy question.
- A subprocess crash/timeout/non-zero exit that is NOT test pollution (Step 3).
- Broker reality (Step 5) disagrees with what the strategy's own state says it should
  hold.

## Step 5 — cross-check the broker, read-only, only when it adds signal

For a strategy trading real/demo money, a quick read-only broker check strengthens
the verdict — but never place, cancel, or modify an order as part of an analysis
request, and never open a fresh broker session that could race a live scheduler tick
using the same session-per-process model (cTrader/Twisted: a reactor can only
`run()` once per process — see `bot/ctrader_s007.py`'s docstrings; don't spin up a
second one concurrently with a live tick). Prefer reading the most recent cycle's own
`broker_positions=` field over opening a new connection. For REST-based brokers
(Bybit) a plain read-only GET (`BybitExec.positions()`/`wallet_equity()`) is cheap
and safe to call directly.

## Step 6 — answer

Follow `orchestrator`'s two-register format: the verdict and what it means for money/
risk in plain language first, then the technical trace (exact log lines, `file:line`,
quoted error strings) backing it up. Always state explicitly which case from Step 4
applies and why — "no position because X (normal)" or "no position and that's a bug:
Y (evidence)". If genuinely uncertain, say so rather than picking the reassuring
answer — this skill exists specifically so "everything's fine" is a checked
conclusion, not a default.
