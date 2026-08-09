# webapp — multi-user / multi-account / multi-strategy control panel

Turns single-account, single-broker bots (S007/cTrader, S009/Bybit, ...) into a
multi-user, multi-broker, multi-strategy system. Schema: `User` → `Account`
(broker/env/encrypted credentials, one user can hold several) → `AccountStrategy`
(join row: one account running one strategy, its own preset/risk/lot/enabled flag —
one account can run several strategies at once) → `Strategy` (lookup table, e.g.
S007/CTRADER, S009/BYBIT).

**The UI is decoupled from trading — this is the one rule that must never break**:
`webapp/app.py` only ever reads/writes DB state (accounts, links, enable/disable,
config, journal). It must never import anything from `bot/` that places or closes
an order. All trading lives in `webapp/runner.py`, a separate process the UI does
not control except by flipping `AccountStrategy.enabled` in the DB — the runner's
own next cycle picks that up. So the UI going down never touches live trading, and
a stuck/crashed runner never takes the dashboard down with it.

Status: **UI + runner + position sync + scheduling all in place** (dashboard,
journal, per-link detail pages, DB-driven multi-account runner registered for both
S007 and S009 — S009 shadow-only — broker position sync for cTrader, a generic
Docker/Ofelia scheduler). Not yet done: S009 real/demo order execution through the
DB-driven path (explicit future decision), cutover from the legacy single-account
launchd S007 job to the Docker/Ofelia path.

## Pieces
- `db.py` — SQLAlchemy engine/session. SQLite `data/app.db` by default (`APP_DB_URL`
  env var swaps in Postgres later with no model changes). `DB_URL` is resolved
  against the repo root, not the process cwd (see the module docstring for the
  2026-07-28 "silently wrote to `webapp/data/app.db`" incident this fixed).
  `init_db()` (`create_all()`) is a **dev/test-only convenience** — the running app
  no longer calls it automatically; schema is owned by Alembic (see below).
- `models.py` — `User` (login only), `Account` (broker/env/label + one Fernet-encrypted
  JSON credentials blob, shape depends on `broker` — see `crypto.py`/`Account.credentials`),
  `Strategy` (lookup: name + broker), `AccountStrategy` (the account↔strategy join —
  preset/risk/lot/enabled/status/last_cycle_at), `Position` (bot-tracked; entry/sl/tp
  plus, since migration 002, broker-synced fill data: `origin`, `exit_price`,
  `volume_lots`, `gross_profit`, `swap`, `commission`, `pnl`, `broker_deal_id`,
  `synced_at` — `pnl IS NULL` means "not yet synced", never 0.0), `LogEntry` (curated
  business events: cycle_start/cycle_end/position_open/position_close/error/sync/...).
- `crypto.py` — Fernet encryption of the credentials blob, keyed by
  `SHA256(APP_SECRET_KEY)`. Access only via `Account.credentials` (get/set property);
  never touch `credentials_enc` directly.
- `security.py` — PBKDF2-HMAC-SHA256 password hashing for the multi-user login.
- `runner.py` — `python -m webapp.runner --strategy S007` (or `S009`): a
  coordinator+worker pair. The coordinator loads enabled `AccountStrategy` rows for
  that strategy and spawns **one worker subprocess per account** — required, not
  just convenient, because `CTraderAdapter` drives its session through a single
  Twisted `reactor.run()` call and a Twisted reactor cannot be restarted in the same
  process, so parallel accounts within one tick need one OS process each. Each
  worker runs exactly one cycle for one `account_strategy_id`, writes curated events
  to both the DB `logs` table and the per-account `utils.trade_logger.StrategyLogger`
  JSONL files under `reports/logs/` (full per-cycle debug detail plus the
  open/closed-dedup state `decide()` depends on still lives only in those files —
  not yet safe to drop), and updates `account_strategies.status`/`last_cycle_at`.
  After a cTrader cycle, the runner spends any leftover tick budget on
  `sync_positions` in a subprocess (same one-reactor-per-process constraint). The
  S007↔S009 dispatch is a `STRATEGY_WORKERS = {"S007": _worker_s007, "S009":
  _worker_s009}` registry, not a hardcoded check — adding a new strategy means
  writing one worker function and registering it here, nothing else in this module
  changes. **S009's worker is deliberately shadow-only** (`broker="off"` hardcoded,
  not read from any config) — enabling real/demo orders through this DB-driven path
  is a separate, explicit decision, not a side effect of registering it. Its
  cross-cycle state (target book/equity/last-booked-day) lives in the `strategy_state`
  DB table (`webapp/state_store.py::DBStateStore`, one row per
  `account_strategy_id`) instead of a shared file, so several Bybit accounts can run
  through this path without clobbering each other — `utils/strategy_state.py`
  defines the same `.load()`/`.save(dict)` shape as a plain `FileStateStore`, which
  is what `bot/s009_paper.py`'s original single-account CLI (`--once`/`--loop`) still
  uses, unchanged.
- **Scheduling** — *what* runs *when* lives in `deployment/schedule.yml` (cron
  strings per strategy, plus arbitrary background `tasks`), read by
  `scripts/scheduler_tick.py`, not in any per-strategy infra config. One static
  Ofelia job (`docker-compose.yml`, image built from the repo-root `Dockerfile`)
  invokes that dispatcher every minute; it decides which strategy/task is actually
  due and subprocess-invokes `webapp.runner --strategy <name>` (or a task's
  command) only then. Adding a strategy's cadence or a new background job is a
  `schedule.yml` edit + code review — `docker-compose.yml` never needs to change
  again. S007 runs 10:00–16:59 Kyiv weekdays (`bot/s007_config.py`'s
  `TRADE_START`/`EXIT_END`); S009 runs once daily shortly after UTC midnight, not
  every minute like S007 — its cycle has no cheap "already up to date" check before
  hitting Bybit's public data API. See `docker-compose.yml`'s comments for
  Podman-machine-specific socket/SELinux notes if reproducing this locally.
- `sync_positions.py` — `python -m webapp.sync_positions --account-strategy-id N`.
  Refreshes `positions` from broker reality for **cTrader accounts only** (Bybit's
  API returns only `{symbol: net qty}` with no label/id, so its rows can't be
  matched to specific `Position` records — deliberately out of scope). Two-tier
  matching (`broker_position_id` first, label second): open rows get real
  entry/SL/TP/volume; rows gone from the broker are closed with
  `reason='broker_closed'`; unknown broker positions are adopted as new rows with
  `origin='adopted'`.
- `migrations/` — Alembic. **Single source of truth for schema** (see `db.py` above).
  Run with `python -m alembic -c webapp/alembic.ini upgrade head` from the repo
  root — `alembic.ini`'s `sqlalchemy.url` is overridden at runtime by `env.py` from
  the same `APP_DB_URL` env var `db.py` uses, so there is exactly one place the
  connection string is decided, not two that can drift.
- `cli.py` — admin CLI: `init-db` (dev/test only, see `db.py`), `create-user`,
  `add-account`, `add-strategy`, `link-strategy`, `enable`/`disable`
  (`--account-strategy-id`), `list`.
- `app.py` — the FastAPI dashboard, see "Web UI" below.

## Environment
```
APP_SECRET_KEY=<long random string>     # REQUIRED — derives the Fernet key for credentials_enc
APP_DB_URL=sqlite:///data/app.db        # optional (default; resolved against repo root)
```
`.env` is not auto-loaded by `webapp/` — export the vars you need before running any
`webapp.*` command (`cli.py`, `app.py`, `runner.py`, `sync_positions.py` all read
from `os.environ`, none of them call `python-dotenv`).

Extra deps beyond `requirements.txt` (the research/dev stack — not yet updated for
`webapp`/`bot`, install manually for a local venv): `pip install sqlalchemy alembic
fastapi uvicorn jinja2 python-multipart itsdangerous pydantic cryptography`. The
Docker image (`Dockerfile`) instead installs from `requirements-docker.txt`, the
actual pinned runtime list for `bot/`+`webapp/`+`utils/` — keep that file in sync
if you add a dependency, `requirements.txt` is not it.

## Quick start
```
python -m alembic -c webapp/alembic.ini upgrade head    # create/upgrade schema
python -m webapp.cli create-user --username anton --password ***
python -m webapp.cli add-account --username anton --broker CTRADER --env demo \
    --label ctrader-demo1 --client-id ... --client-secret ... --access-token ...
python -m webapp.cli add-strategy --name S007 --broker CTRADER
python -m webapp.cli link-strategy --account-id 1 --strategy S007 --preset BASELINE \
    --risk 0.25 --enable
python -m webapp.cli list

# one cycle for every enabled S007 AccountStrategy (spawns one worker per account):
python -m webapp.runner --strategy S007

# pull real broker fill data into one account's position history:
python -m webapp.sync_positions --account-strategy-id 1
```

## Security notes
- Credentials are one Fernet-encrypted JSON blob per account (`credentials_enc`),
  key = `SHA256(APP_SECRET_KEY)`. Always go through `Account.credentials`
  (get/set) — never read/write `credentials_enc` directly, and never let a
  decrypted value reach a log line (identify credentials by a truncated SHA-256
  fingerprint instead, same convention as `.env`/`configs/accounts.yml`).
- Passwords are PBKDF2-HMAC-SHA256 with a per-user salt (`security.py`).
- `.env` and `configs/accounts.yml` are gitignored and must stay that way; treat
  both as write-only from an assistant's perspective — read field *names* to
  reason about structure, never values.
- Rotating `APP_SECRET_KEY` invalidates every stored credential by design (Fernet
  decrypt fails) — there is no re-encryption path yet.

## Web UI (`webapp/app.py`)

FastAPI + session login (Starlette `SessionMiddleware`) + server-rendered
Jinja2 templates, no JS build. Multi-user: each user sees and edits only their own
accounts/links; a foreign `account_id`/`aid` in a URL is ignored rather than honoured.

- `/login`, `/logout` — session auth (PBKDF2 passwords).
- `/` dashboard — this user's accounts, each with its linked strategies
  (preset/risk/lot/status/enabled). **Start/Stop = toggle `AccountStrategy.enabled`
  in the DB only** — the next runner cycle for that strategy is what actually
  reacts; the route never touches a broker.
- `/accounts/add` — add an account (broker-conditional credential form, validated
  via `webapp/schemas/accounts.py`).
- `/accounts/{aid}/link-strategy` — attach a `Strategy` to an account with its own
  preset/risk/lot.
- `/account-strategies/{lid}/toggle|save|unlink` — per link, not per account (one
  account can run several strategies independently).
- `/journal` — two tabs: **events** (the `logs` table; filters: account, strategy,
  level, kind) and **positions** (full `Position` history including closed;
  filters: account, strategy, status).
- `/account-strategies/{lid}` — detail page for one (account, strategy) pair:
  counts, buy/sell split, pyramid-add share, hold-time stats, close-reason
  breakdown, a 30-day opened-per-day sparkline, and — for rows with a synced,
  non-NULL `pnl` — realised PnL/win-rate/profit-factor/drawdown. Rows still
  showing `pnl IS NULL` render as `--`, not `0.00`; unpriced money is never
  invented (run `sync_positions` to fill it in for cTrader).
- `/users` (admin only) — list + create users.

Run:
```
APP_SECRET_KEY=... uvicorn webapp.app:app --host 0.0.0.0 --port 8000
```
Put it behind a reverse proxy with TLS for anything but localhost. The UI and
`webapp.runner` share the DB but run as separate processes — restarting/redeploying
the UI never touches an open position. Test coverage: `tests/webapp/` (TestClient
against a throwaway seeded SQLite — pagination, every journal filter, cross-user
access, credential validation, flash-message persistence) and
`tests/webapp/test_sync_positions.py` (position-sync matching/adoption/closing, no
network).
