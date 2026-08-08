# Development plans

Infrastructure/engineering initiatives that are decided but not (fully) built
yet — distinct from strategy passports, experiment logs, and decision logs,
which live in the AlgoTrading Claude Project (see `.claude/skills/strategy-lifecycle/SKILL.md`).
This file tracks cross-cutting engineering work: architecture decisions,
their rationale, and what's actually implemented vs still pending. Update the
"Status" section of a plan as work lands; don't let it drift out of sync with
the code, and log every change to this file via `.claude/skills/code-change-log`.

## Multi-account Docker migration

Started 2026-07-25: migrating the bots (S007/cTrader, S009/Bybit, more later)
from single-account launchd-scheduled processes to Docker, supporting many
users, each with multiple broker accounts.

### Decisions

- **Scheduling model = "B": stateless tick + external scheduler**, not a
  persistent long-lived worker process. Locally: Docker Compose + Ofelia
  (label-based cron for containers). In the cloud: swap the trigger for a
  native primitive (k8s CronJob / Cloud Run Jobs + Cloud Scheduler / ECS
  Scheduled Tasks) with zero change to the worker image/code — the contract
  is "one process, one exit code, no persistent state," same as
  `scripts/s007_tick.py` already implements for the single-account launchd
  setup.
  Why: (1) cost — persistent workers idle most of the day for both S007
  (outside 10:00-16:59 session, weekends) and S009 (23h59m/day) and a cloud
  host bills for that idle time; (2) already proven — direct continuation of
  the stateless-tick redesign done after a persistent bash loop silently
  hung for ~17h (macOS App-Nap-style throttling); (3) concrete technical
  blocker for a persistent-worker design — cTrader's Twisted reactor cannot
  be restarted in-process (`ReactorNotRestartable`), so even a "persistent"
  worker would have to spawn a fresh subprocess per account per cycle
  anyway, meaning it degrades to the stateless-tick model with extra layers,
  not a real alternative.
- **Granularity = one job per STRATEGY, not per account or per user.** Each
  scheduled tick queries the DB fresh for all `enabled=true` accounts
  belonging to that strategy and fans them out **in parallel** (concurrent
  subprocesses/async, not sequential) within the single tick invocation —
  sequential would blow the 60s budget past a handful of accounts for
  minute-cadence strategies like S007 (~7-10s per account cycle observed
  live). Adding/removing a user takes effect on the next tick automatically,
  zero redeploy. Add a per-account timeout (like the existing
  `CYCLE_TIMEOUT_SECONDS` pattern) so one stuck account can't block the
  whole tick; shard by account-id hash across parallel job instances if the
  account count ever gets large enough to need it (not needed yet).
- **DB (not `configs/accounts.yml`) is the source of truth** for users and
  accounts going forward. `accounts.yml` becomes an import/bootstrap
  convenience only, not the production credential store (it stays wired as
  a fallback in `bot/accounts_config.py` / `webapp/runner.py`'s
  `_creds_for()`, but the DB is primary).
- **DB schema** (superseding the old cTrader-only shape): broker credentials
  live on `Account` (not `User`), as a single encrypted JSON blob
  (`credentials_enc`) whose shape depends on `Account.broker` — no
  per-broker credential table, no migration needed to add a new broker's
  field set. `Account.env` is a plain string (`"demo"`/`"live"`/`"testnet"`/
  `"mainnet"`) to cover Bybit's 3-way env split that a bool can't express.
  Enum-like columns follow the project convention: plain string column +
  parallel code `enum.Enum` + Pydantic validation at the write boundary, not
  a DB enum type. Domain objects and request/response schemas live together
  in `webapp/schemas/`, shared by DB validation and any future API.
- **Deferred, explicitly parked for later:** a local market-data cache (e.g.
  a shared M1-bar cache per tick, since accounts trading the same symbol
  currently each independently re-fetch identical bars from the broker —
  the dominant cost in the observed ~7-10s per-account cycle time). Revisit
  when implementing the parallel fan-out.
- **Logging split:** a DB `logs` table (FKs to user/account/strategy,
  optional position; `level`/`kind`/`payload`/`cycle_id`) holds only
  curated business events (position open/close, errors, cycle summaries,
  skip_* decisions) — powers the future API/UI. Everything else (full
  per-tick debug volume, system/scheduler noise) goes to the container's
  stdout/stderr as JSON lines, not files — 12-factor style, captured by
  whatever the platform already provides (`docker logs`/Ofelia locally;
  CloudWatch/Cloud Logging/Loki in the cloud) instead of a bespoke
  storage/retention system, and sidesteps ephemeral-container filesystems
  (a k8s CronJob/Cloud Run Job pod may have no persistent local disk, or
  loses it on exit). `utils/trade_logger.StrategyLogger` (currently
  file-only) will need a stdout-JSON-lines path added when the runner
  actually gets containerized.
  Concrete debugging plan: our job containers will run with `--rm` under
  Ofelia (fresh container per tick, self-deletes on exit), so plain
  `docker logs` only works while the container still exists. Plan: add
  **Grafana Loki** (+ the `grafana/loki-docker-driver` Docker logging
  driver) as a service in the compose stack — set `logging: driver: loki`
  on each worker service so stdout ships to Loki before the container is
  removed, queryable after the fact via `logcli`/Grafana UI, filterable by
  label/JSON field (`cycle_id`, `account_id`, ...) across all accounts at
  once. Cheaper fallback if Loki is overkill: don't `--rm` job containers
  immediately (let them sit, `docker system prune` on a schedule) so plain
  `docker logs <id>` still works until pruned.
- **Schema migrations go through Alembic** (`webapp/migrations/`, config at
  `webapp/alembic.ini`), not `webapp.db.init_db()`'s `create_all()` (that's
  test/scratch-DB only). Revision IDs are sequential (`001`, `002`, ...)
  passed explicitly via `--rev-id` on `alembic revision --autogenerate
  --rev-id 00N -m "..."` — Alembic's default is a random hex ID, this
  project's convention overrides that.
- **`configs/accounts.yml` -> DB import**: `scripts/migrate_accounts_yml.py`
  (one-off, idempotent — re-running updates existing rows rather than
  duplicating). Maps CTRADER entries to Strategy "S007", BYBIT to "S009";
  `active` -> `AccountStrategy.enabled`; `initial_balance` -> the
  `AccountStrategy.initial_balance` column (seed for a paper/shadow ledger,
  not live balance). Requires an `env:` field in `accounts.yml` for CTRADER
  entries (no default guessed); for BYBIT, an explicit `env:` is preferred,
  falling back to `TESTNET: true/false` -> `testnet`/`mainnet` (Bybit's
  separate "demo" env isn't expressible by that boolean alone). One `User`
  row per unique `username`, password supplied at migration time
  (`accounts.yml` carries no password).

### Status (2026-07-31)

- Done: DB schema (users/accounts/strategies/account_strategies/positions/
  logs) implemented and migrated via Alembic (`001_initial_schema`);
  `configs/accounts.yml` import script (`scripts/migrate_accounts_yml.py`)
  written.
- Not started: `Dockerfile`/`docker-compose.yml`/Ofelia scheduler config;
  the Loki logging driver setup (plan above, no code yet).
- Stale, needs updating to the new multi-broker schema: `webapp/cli.py`,
  `webapp/app.py`, `webapp/runner.py`, `webapp/README.md` (still reference
  the old pre-multi-broker shape — `ctid_trader_account_id`, cTrader creds
  on `User`, a single `Account.strategy`).
- Blocked: running `scripts/migrate_accounts_yml.py` against the real
  `configs/accounts.yml` needs a plaintext password for the new `User` row,
  not yet supplied.
