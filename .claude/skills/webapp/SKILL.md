---
name: webapp
description: >-
  Architecture, run instructions, and conventions for the multi-user / multi-account /
  multi-strategy web control panel (`webapp/`) that manages the trading bots. Use this
  skill whenever the task touches the web UI, login/session auth, users or accounts
  management, broker credential storage, the SQLite/Alembic database (User / Account /
  AccountStrategy / Strategy / Position / LogEntry models), credential encryption, the
  DB-driven multi-account runner, position sync, or the scheduler — even if the request
  only mentions "the dashboard", "the login page", "add an account", "store the API keys",
  "the runner", or "the database". Read this before editing anything under `webapp/` so
  you preserve the UI↔runner decoupling and the security model instead of accidentally
  coupling trading to the web process or leaking secrets.
---

# webapp — multi-user / multi-account / multi-strategy bot control panel

**`webapp/README.md` is the detailed, maintained reference for this package** — module
map, schema, runner internals, scheduling, migrations, quick start. Read it before any
`webapp/` work; this skill records only the invariants that must never regress and the
mental model.

## Mental model

DB schema: `User` (login only) → `Account` (broker/env + **one Fernet-encrypted JSON
credentials blob**, shape depends on `broker`) → `AccountStrategy` (join row: one account
running one strategy, with its own `enabled`/preset/risk/lot/`broker_mode`/status) →
`Strategy` (lookup: name + broker). Plus `Position` (bot rows + broker-synced fill data),
`LogEntry` (curated business events), `Broker`/`Asset` lookups, and `strategy_state`
(per-link cross-cycle state). Schema is owned by **Alembic** (`webapp/migrations/`);
`init_db()` is dev/test-only.

Two processes share the DB:
- **UI** (`app.py`, FastAPI + Jinja2) — only edits DB state.
- **Runner** (`runner.py`) — the trading process: a coordinator per strategy per tick
  spawns **one worker subprocess per account** (required: one Twisted reactor per
  process), dispatched via the `STRATEGY_WORKERS` registry. Adding a strategy = one
  worker function + one registry entry. Scheduling lives in `deployment/schedule.yml`
  read by `scripts/scheduler_tick.py` (Docker/Ofelia), not in per-strategy infra.

## The one rule that must not be broken: UI ↔ runner decoupling

The UI never places or closes a trade, and the runner never serves HTTP. They communicate
**only through the database**. If a UI action needs to affect trading, it writes DB state
that the next runner cycle reads — `app.py` must never import anything from `bot/` that
places or closes an order. Trading logic belongs in `runner.py` / `bot/`, never in
`app.py`.

## Security model (do not regress)

- Broker credentials are **encrypted at rest** (Fernet keyed by `SHA256(APP_SECRET_KEY)`)
  as one JSON blob per `Account`; access exclusively via the `Account.credentials`
  property, never the raw `credentials_enc` column. Never add a plaintext secret column
  and never log a decrypted value.
- Credential form fields are **write-only** in the UI: show a "set" badge, never the
  value; an empty submitted field leaves the stored secret unchanged.
- Passwords are PBKDF2-HMAC-SHA256 hashed (`security.py`), never stored/logged plaintext.
- `APP_SECRET_KEY` and the DB file are secrets — out of git. Rotating `APP_SECRET_KEY`
  invalidates every stored credential by design. Never read or echo `.env` secret
  *values*; refer to them by variable name only.
- Every UI handler filters by the logged-in user — a user sees only their own accounts.
- **Real-order gating is per `AccountStrategy` row** (`broker_mode`: off/dry/execute,
  default off), further gated by `Account.env` — never a global switch, and never a side
  effect of merely registering a worker.

## Gotchas that keep biting

- Starlette `TemplateResponse`: request-first signature —
  `templates.TemplateResponse(request, "name.html", {...})`.
- All writes go through `webapp/schemas/` validation (the CLI does too); `models.py`
  does not re-validate broker/env/credential shape itself.
- `webapp/` does **not** auto-load `.env` — export `APP_SECRET_KEY`/`APP_DB_URL` before
  running any `webapp.*` command.
- Runtime deps for Docker are pinned in `requirements-docker.txt`, not
  `requirements.txt` — keep the former in sync when adding a dependency.
- One account failing/timing out never stops the others (worker subprocess isolation) —
  preserve that when touching the coordinator.

## Extending

UI feature: route in `app.py` (filtered by current user) + template + (if it affects
trading) a DB field the runner reads. New strategy in the runner: one worker function +
`STRATEGY_WORKERS` entry + `add-strategy`/`link-strategy` rows; cadence in
`deployment/schedule.yml`. Scaling past SQLite: point `APP_DB_URL` at Postgres. Always
keep the UI free of broker calls. When code here changes in ways this skill or
`webapp/README.md` describe, update `webapp/README.md` in the same task (skill edits need
Anton's consent per `code-architecture`).
