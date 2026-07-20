---
name: webapp
description: >-
  Architecture, run instructions, and conventions for the multi-user / multi-account
  web control panel (`webapp/`) that manages the trading bots. Use this skill whenever
  the task touches the web UI, login/session auth, users or accounts management, cTrader
  credential storage, the SQLite database (User/Account/Position models), credential
  encryption, or the decoupled multi-account runner — even if the request only mentions
  "the dashboard", "the login page", "add an account", "store the API keys", "the runner",
  or "the database". Read this before editing anything under `webapp/` so you preserve the
  UI↔runner decoupling and the security model instead of accidentally coupling trading to
  the web process or leaking secrets.
---

# webapp — multi-user / multi-account bot control panel

`webapp/` turns the single-account strategy bot into a multi-user, multi-account system.
A database holds **users** (login + cTrader credentials) and **accounts** (one user →
many accounts, each with its own strategy/preset/risk). Two processes share that DB:

- the **web UI** (`app.py`) — only *edits DB state*: enable/disable an account
  (= Start/Stop), change preset/risk/lot, store credentials, manage users.
- the **runner** (`runner.py`) — the *trading* process. Reads enabled accounts and runs
  one reconcile cycle each against cTrader, writing status/positions back to the DB.

## The one rule that must not be broken: UI ↔ runner decoupling

The UI never places or closes a trade, and the runner never serves HTTP. They communicate
**only through the database**. This is deliberate: the UI can be restarted, redeployed, or
crash without touching live trading, and a runner cycle failure never takes the UI down.
When adding a feature, keep this seam intact — if a UI action needs to affect trading, it
does so by writing DB state that the next runner cycle reads, not by calling broker code
from a request handler. Trading logic belongs in `runner.py` / `bot/`, never in `app.py`.

## Files

- `db.py` — SQLAlchemy engine + session. `DB_URL = APP_DB_URL` env or
  `sqlite:///data/app.db`. `init_db()` creates tables; `SessionLocal` / `get_session()`.
  Swap `APP_DB_URL` to a Postgres URL to scale beyond SQLite with no code change.
- `models.py` — three tables:
  - `User` — `username`, `password_hash` (PBKDF2), `is_admin`, `ctrader_client_id`, and
    **encrypted** `client_secret` / `access_token`. Secrets are exposed as Python
    properties whose *setters encrypt* and whose *getters decrypt*; the raw ciphertext
    lives in columns `_client_secret_enc` / `_access_token_enc`. Never add a plaintext
    secret column and never log the decrypted value.
  - `Account` — `user_id` FK, `ctid_trader_account_id`, `enabled` (Start/Stop),
    `strategy`, `preset`, `symbol`, `risk_pct`, `fixed_lot`, `use_fixed_lot`, `is_live`,
    `host`, and runner-written `status` / `last_cycle_at` / `last_error`.
  - `Position` — `account_id` FK, `label`, `side`, `entry`, `sl`, `tp`, `is_add`,
    `broker_position_id`, `status` (open/closed), `reason`, `opened_at`, `closed_at`.
    The runner mirrors broker/desired positions here so the UI can show them.
- `crypto.py` — Fernet symmetric encryption keyed by `SHA256(APP_SECRET_KEY)`.
  `encrypt_secret` / `decrypt_secret`. Rotating `APP_SECRET_KEY` invalidates every stored
  secret by design (they must be re-entered).
- `security.py` — `hash_password` / `verify_password`, PBKDF2-HMAC-SHA256 with a per-user
  salt (format `pbkdf2_sha256$iters$salt$hash`). Used for the login only.
- `app.py` — FastAPI + Starlette `SessionMiddleware` (cookie session) + Jinja2. Routes:
  `/login` `/logout`, `/` dashboard, `/accounts/add`, `/accounts/{id}/toggle|save|delete`,
  `/creds`, `/users` + `/users/create` (admin only). Each user sees only their own accounts
  (every handler filters by `user_id`).
- `runner.py` — `run_all(dry, at)` → `run_account(...)` per enabled account. Builds a
  `CTraderS007` from the owner's decrypted creds, calls `plan_now(preset=acc.preset)`,
  reconciles broker vs desired, updates DB + logs via `StrategyLogger`. **Error-isolated:**
  one account raising never stops the others. `--dry` runs the whole DB→plan→status→log
  pipeline from local M1 CSV with no broker/SDK.
- `cli.py` — admin CLI for before/without the UI: `init-db`, `create-user`, `set-creds`,
  `add-account`, `enable`/`disable`, `list`.
- `templates/` — server-rendered Jinja2 (`base.html` dark theme, `login`, `dashboard`,
  `add_account`, `creds`, `users`). No JS build step; dashboard auto-refreshes every 30s.

## Security model (do not regress)

- cTrader `client_secret` and `access_token` are **encrypted at rest** (Fernet). The DB
  stores only ciphertext. Set/read them exclusively through the `User` model properties.
- Credential form fields are **write-only**: templates show a "set" badge, never the value.
  Empty submitted fields leave the stored secret unchanged.
- Passwords are PBKDF2-hashed, never stored or logged in plaintext.
- `APP_SECRET_KEY` (encryption) and the DB file are secrets — keep them out of git.
- Never read or echo `.env` secret *values*; refer to them by variable name only.

## Running it

```bash
pip install fastapi uvicorn jinja2 python-multipart itsdangerous sqlalchemy cryptography

# 1) init + first admin (CLI), or do it from the UI once one admin exists
python -m webapp.cli init-db
python -m webapp.cli create-user --username anton --password *** --admin --access-token <tok>
python -m webapp.cli add-account --username anton --ctid <ctid> --preset BASELINE_S007 --risk 0.25 --enable

# 2) the UI (edits DB only — safe to restart)
APP_SECRET_KEY=... uvicorn webapp.app:app --host 0.0.0.0 --port 8000
# put behind a TLS reverse proxy for anything but localhost

# 3) the runner (trades) — schedule every minute during the session, separate process
* 10-16 * * 1-5  cd /path/to/algo && APP_SECRET_KEY=... python -m webapp.runner
# offline pipeline test, no broker:
python -m webapp.runner --dry --at "2024-05-10 10:45"
```

## Gotchas learned the hard way

- **Starlette `TemplateResponse` signature**: use `templates.TemplateResponse(request,
  "name.html", {...})` — request first. The old `(name, context)` form silently passes the
  dict as the template name and fails with "unhashable type: 'dict'".
- `PRESETS` in `app.py` must stay in sync with the presets defined in the strategy config
  (`bot/s007_config.py` / `ger40_lonfra/config.py`).
- The runner's local-M1 dry path slices `df.loc[:at]` **before** `.tail(...)`; reversing
  them yields an empty frame.
- New strategies plug in by giving `Account.strategy` a value the runner can dispatch on;
  keep per-strategy planning in the strategy module, not in the runner's control flow.

## Extending

Adding a UI feature: add the route to `app.py` (filter by `current_user`), a template,
and — if it changes trading behavior — a DB field the runner reads. Adding a strategy to
the multi-account system: expose a `plan_now`-style planner for it and branch the runner
on `acc.strategy`. Scaling past SQLite: point `APP_DB_URL` at Postgres. Always keep the
UI free of broker calls.
