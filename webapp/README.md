# webapp — multi-user / multi-account control (foundation)

Turns the single-account S007 bot into a multi-user, multi-account system: a DB of
users (login + cTrader creds) and accounts (1 user → many), with a runner that
trades every enabled account. **The UI is decoupled**: it only edits DB state
(enable/disable, config); a separate runner process does the trading and writes
status back. So the UI going down never touches live trading, and vice-versa.

Status: **foundation done** (DB + encryption + auth hashing + multi-account runner).
Web UI = next step.

## Pieces
- `db.py` — SQLAlchemy engine/session (SQLite `data/app.db`; swap `APP_DB_URL` for Postgres).
- `models.py` — `User` (login + cTrader creds), `Account` (per-user, enable/config/status),
  `Position` (bot-tracked, for the UI). Secrets stored **encrypted**.
- `crypto.py` — Fernet encryption of tokens/secrets, keyed by `APP_SECRET_KEY` (env).
- `security.py` — PBKDF2 password hashing for the multi-user login.
- `runner.py` — reads ENABLED accounts, runs one S007 cycle each (per-account creds +
  config), writes status/positions back, logs per account. Isolated per account
  (one failure never stops the others).
- `cli.py` — admin CLI until the UI exists.

## Environment
```
APP_SECRET_KEY=<long random string>     # REQUIRED — encrypts secrets at rest
APP_DB_URL=sqlite:///data/app.db        # optional (default)
# global cTrader Open API app (used when a user has no own client_id/secret):
CTRADER_CLIENT_ID=... / CTRADER_CLIENT_SECRET=...
```
Extra deps: `pip install sqlalchemy cryptography` (+ `fastapi uvicorn jinja2` for the UI later).

## Quick start
```
python -m webapp.cli init-db
python -m webapp.cli create-user --username anton --password *** --admin \
    --access-token <ctrader_oauth_token>        # token encrypted in the DB
python -m webapp.cli add-account --username anton --ctid <ctidTraderAccountId> \
    --label demo1 --preset BASELINE_S007 --risk 0.25 --enable
python -m webapp.cli list

# offline test of the whole DB->runner->status->log pipeline (no broker):
python -m webapp.runner --dry --at "2024-05-10 10:45"

# live: schedule every minute in the session (runs all enabled accounts):
* 10-16 * * 1-5  cd /path/to/algo && APP_SECRET_KEY=... python -m webapp.runner
```

## Security notes
- Access tokens / client secrets are AES-encrypted (Fernet) — the DB holds only
  ciphertext (verified). Rotating `APP_SECRET_KEY` invalidates stored secrets by design.
- Passwords are PBKDF2-HMAC-SHA256 with per-user salt.
- Keep `.env` / `APP_SECRET_KEY` out of git (already gitignored) and the DB file private.

## Web UI (`webapp/app.py`, done)

FastAPI + session login + server-rendered dashboard (no JS build). Multi-user:
each user sees only their own accounts.

- `/login` `/logout` — session auth (PBKDF2 passwords).
- `/` dashboard — accounts table: type (demo/live), preset, risk/lot, status,
  open positions, last cycle, last error; **Start/Stop** = toggle `enabled`;
  inline edit (preset/risk/lot); delete. Auto-refreshes every 30s.
- `/creds` — set cTrader client id / secret / access token (secrets write-only,
  encrypted at rest; "set" badges).
- `/accounts/add` — add an account.
- `/users` (admin only) — list + create users.

Run:
```
pip install fastapi uvicorn jinja2 python-multipart itsdangerous
APP_SECRET_KEY=... uvicorn webapp.app:app --host 0.0.0.0 --port 8000
```
Put it behind a reverse proxy with TLS for anything but localhost. The UI and the
`webapp.runner` (cron) share the DB but run as separate processes — the UI never
trades, so restarting/redeploying it is safe. Verified end-to-end (TestClient):
login, dashboard, start/stop, credential save+encrypt, add account, admin users.
