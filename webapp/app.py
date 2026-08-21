"""FastAPI web UI for the multi-user / multi-strategy / multi-broker bot control panel.

Session login (multi-user); each user manages only their own broker accounts and the
strategies linked to them. The UI only edits DB state (add/enable/disable/config); the
separate `webapp.runner` process (one per strategy per tick) does the actual trading --
see the "UI <-> runner decoupling" rule in the webapp skill. This file must never import
anything from bot/ that places or closes an order.

    pip install fastapi uvicorn jinja2 python-multipart itsdangerous
    APP_SECRET_KEY=... uvicorn webapp.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

from webapp.db import SessionLocal
from webapp.models import Account, AccountStrategy, Broker as BrokerRow, LogEntry, Position, Strategy, User
from webapp.schemas import (
    AccountCreate, AccountStrategyCreate, Broker, CREDENTIALS_BY_BROKER, ENV_BY_BROKER,
)
from webapp.schemas.enums import LogKind, LogLevel
from webapp.security import hash_password, verify_password

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

# Schema is Alembic-managed (webapp/migrations/), not auto-created here: the
# web process must never silently create/alter tables behind migration
# history's back. Bootstrap a fresh dev DB explicitly with
# `python -m webapp.cli init-db` (dev/test convenience, see cli.py) or
# `alembic -c webapp/alembic.ini upgrade head` (the real path, also used on
# every deploy). See webapp/db.py::init_db.
app = FastAPI(title="Algo bot control")
app.add_middleware(SessionMiddleware,
                   secret_key=os.environ.get("APP_SECRET_KEY", "dev-insecure-key"),
                   same_site="lax", https_only=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def flash(request: Request, msg: str, kind: str = "ok"):
    # Reassign, never mutate in place: Starlette's Session only sets `modified`
    # (and therefore only re-sends the cookie) on __setitem__/__delitem__. The
    # old `session.setdefault("_flash", []).append(...)` mutated an existing
    # list, so every flash after the first one in a session was silently lost.
    msgs = list(request.session.get("_flash", []))
    msgs.append({"msg": msg, "kind": kind})
    request.session["_flash"] = msgs


def pop_flash(request: Request):
    return request.session.pop("_flash", [])


def current_user(request: Request, db) -> User | None:
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


def _redirect(url="/login"):
    return RedirectResponse(url, status_code=303)


# ---------------- auth ----------------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "flash": pop_flash(request)})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    u = db.query(User).filter_by(username=username).one_or_none()
    if not u or not verify_password(password, u.password_hash):
        flash(request, "Invalid username or password", "err")
        return _redirect("/login")
    request.session["user_id"] = u.id
    return _redirect("/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return _redirect("/login")


# ---------------- dashboard ----------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    accounts = db.query(Account).filter_by(user_id=u.id).order_by(Account.id).all()
    open_pos = {}
    for a in accounts:
        for link in a.strategy_links:
            open_pos[link.id] = (
                db.query(Position)
                .filter_by(account_id=a.id, strategy_id=link.strategy_id, status="open")
                .all())
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "user": u, "accounts": accounts, "open_pos": open_pos,
        "flash": pop_flash(request)})


# ---------------- accounts ----------------

@app.get("/accounts/add", response_class=HTMLResponse)
def add_account_form(request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    return templates.TemplateResponse(request, "add_account.html", {
        "request": request, "user": u,
        "env_by_broker": {b.value: sorted(e.value for e in envs) for b, envs in ENV_BY_BROKER.items()},
        "cred_fields": {b.value: list(cls.model_fields.keys())
                        for b, cls in CREDENTIALS_BY_BROKER.items()},
        "flash": pop_flash(request)})


@app.post("/accounts/add")
def add_account(request: Request, broker: str = Form(...), env: str = Form(...),
                external_account_id: str = Form(""), label: str = Form(""),
                broker_host: str = Form(""),
                client_id: str = Form(""), client_secret: str = Form(""), access_token: str = Form(""),
                api_key: str = Form(""), api_secret: str = Form(""),
                db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    creds = (dict(client_id=client_id, client_secret=client_secret, access_token=access_token)
             if broker == Broker.CTRADER.value
             else dict(api_key=api_key, api_secret=api_secret))
    try:
        payload = AccountCreate(
            user_id=u.id, broker=broker, env=env,
            external_account_id=external_account_id or None, label=label or "",
            broker_host=broker_host or None, credentials=creds)
    except ValidationError as e:
        flash(request, f"Could not add account: {e.errors()[0]['msg']}", "err")
        return _redirect("/accounts/add")
    dup = db.query(Account).filter_by(
        user_id=u.id, broker=payload.broker.value,
        external_account_id=payload.external_account_id).first()
    if dup:
        flash(request, "An account with this broker/id already exists", "err")
        return _redirect("/accounts/add")
    # ALGODEV-30: Account.broker_id is NOT NULL. No broker-entity picker in
    # this form yet (that's future UI work, not this schema ticket) --
    # resolve the platform's one seeded broker row (IC Markets/CTRADER,
    # Bybit/BYBIT). Errors loudly rather than guessing if that ever stops
    # being a 1:1 mapping (a second broker on the same platform gets added).
    broker_row = db.query(BrokerRow).filter_by(platforms=payload.broker.value).first()
    if broker_row is None:
        flash(request, f"No `brokers` row configured for platform {payload.broker.value} -- "
                        f"run 'webapp.cli list-brokers' / seed one first", "err")
        return _redirect("/accounts/add")
    acc = Account(user_id=payload.user_id, broker=payload.broker.value, broker_id=broker_row.id,
                  env=payload.env.value,
                  external_account_id=payload.external_account_id, label=payload.label,
                  broker_host=payload.broker_host)
    acc.credentials = payload.credentials   # encrypted by the model property setter
    db.add(acc)
    db.commit()
    flash(request, f"Account '{acc.label or acc.external_account_id}' added -- "
                    f"link a strategy to it to start trading")
    return _redirect("/")


@app.post("/accounts/{aid}/delete")
def delete_account(aid: int, request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    a = db.get(Account, aid)
    if a and a.user_id == u.id:
        db.delete(a)
        db.commit()
        flash(request, "Account removed")
    return _redirect("/")


# ---------------- account <-> strategy links ----------------

@app.get("/accounts/{aid}/link-strategy", response_class=HTMLResponse)
def link_strategy_form(aid: int, request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    acc = db.get(Account, aid)
    if not acc or acc.user_id != u.id:
        return _redirect("/")
    linked_ids = {link.strategy_id for link in acc.strategy_links}
    strategies = [s for s in db.query(Strategy).filter_by(broker=acc.broker).all()
                  if s.id not in linked_ids]
    return templates.TemplateResponse(request, "link_strategy.html", {
        "request": request, "user": u, "account": acc, "strategies": strategies,
        "flash": pop_flash(request)})


@app.post("/accounts/{aid}/link-strategy")
def link_strategy(aid: int, request: Request, strategy_id: int = Form(...),
                  preset: str = Form(""), symbol: str = Form(""),
                  risk_pct: float = Form(0.25), fixed_lot: float = Form(0.01),
                  use_fixed_lot: bool = Form(False), initial_balance: str = Form(""),
                  enabled: bool = Form(False), db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    acc = db.get(Account, aid)
    if not acc or acc.user_id != u.id:
        return _redirect("/")
    strat = db.get(Strategy, strategy_id)
    if not strat or strat.broker != acc.broker:
        flash(request, "That strategy isn't available for this account's broker", "err")
        return _redirect(f"/accounts/{aid}/link-strategy")
    if db.query(AccountStrategy).filter_by(account_id=acc.id, strategy_id=strat.id).first():
        flash(request, f"{strat.name} is already linked to this account", "err")
        return _redirect("/")
    try:
        payload = AccountStrategyCreate(
            account_id=acc.id, strategy_id=strat.id, enabled=enabled,
            preset=preset or strat.default_preset, symbol=symbol or None,
            risk_pct=risk_pct, fixed_lot=fixed_lot, use_fixed_lot=use_fixed_lot,
            initial_balance=float(initial_balance) if initial_balance else None)
    except ValidationError as e:
        flash(request, f"Could not link strategy: {e.errors()[0]['msg']}", "err")
        return _redirect(f"/accounts/{aid}/link-strategy")
    link = AccountStrategy(**payload.model_dump())
    db.add(link)
    db.commit()
    flash(request, f"{strat.name} linked to '{acc.label or acc.external_account_id}'"
                    f"{' and started' if enabled else ' (stopped -- start it below)'}")
    return _redirect("/")


@app.post("/account-strategies/{lid}/toggle")
def toggle_link(lid: int, request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    link = db.get(AccountStrategy, lid)
    if link and link.account.user_id == u.id:
        link.enabled = not link.enabled
        if not link.enabled:
            link.status = "stopped"
        db.commit()
        flash(request, f"{link.strategy.name} on '{link.account.label}' "
                        f"{'started' if link.enabled else 'stopped'}")
    return _redirect("/")


@app.post("/account-strategies/{lid}/save")
def save_link(lid: int, request: Request, preset: str = Form(""), symbol: str = Form(""),
             risk_pct: float = Form(...), fixed_lot: float = Form(...),
             use_fixed_lot: bool = Form(False), db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    link = db.get(AccountStrategy, lid)
    if link and link.account.user_id == u.id:
        link.preset = preset or None
        link.symbol = symbol or None
        link.risk_pct = risk_pct
        link.fixed_lot = fixed_lot
        link.use_fixed_lot = use_fixed_lot
        db.commit()
        flash(request, "Saved")
    return _redirect("/")


@app.post("/account-strategies/{lid}/unlink")
def unlink(lid: int, request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    link = db.get(AccountStrategy, lid)
    if link and link.account.user_id == u.id:
        db.delete(link)
        db.commit()
        flash(request, "Strategy unlinked")
    return _redirect("/")


# ---------------- journal ----------------

PAGE_SIZE = 50


@app.get("/journal", response_class=HTMLResponse)
def journal(request: Request, tab: str = "events", account_id: int = 0,
            strategy_id: int = 0, level: str = "", kind: str = "",
            status: str = "", page: int = 1, db=Depends(get_db)):
    """Event log + full position history for everything this user owns.

    Read-only view over DB state written by the runner -- no broker calls
    (see the UI <-> runner decoupling rule)."""
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    if tab not in ("events", "positions"):
        tab = "events"
    page = max(1, page)

    accounts = db.query(Account).filter_by(user_id=u.id).order_by(Account.id).all()
    my_acc_ids = [a.id for a in accounts]
    # strategies actually linked to this user's accounts -- the filter dropdown
    # should not offer strategies that can never appear in these rows
    strategies = (db.query(Strategy)
                  .join(AccountStrategy, AccountStrategy.strategy_id == Strategy.id)
                  .filter(AccountStrategy.account_id.in_(my_acc_ids or [0]))
                  .distinct().order_by(Strategy.name).all()) if my_acc_ids else []

    if account_id and account_id not in my_acc_ids:
        account_id = 0          # silently ignore someone else's account id

    if tab == "events":
        q = db.query(LogEntry).filter(
            (LogEntry.user_id == u.id) | (LogEntry.account_id.in_(my_acc_ids or [0])))
        if account_id:
            q = q.filter(LogEntry.account_id == account_id)
        if strategy_id:
            q = q.filter(LogEntry.strategy_id == strategy_id)
        if level:
            q = q.filter(LogEntry.level == level)
        if kind:
            q = q.filter(LogEntry.kind == kind)
        total = q.count()
        rows = (q.order_by(LogEntry.ts.desc(), LogEntry.id.desc())
                .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all())
    else:
        q = db.query(Position).filter(Position.account_id.in_(my_acc_ids or [0]))
        if account_id:
            q = q.filter(Position.account_id == account_id)
        if strategy_id:
            q = q.filter(Position.strategy_id == strategy_id)
        if status in ("open", "closed"):
            q = q.filter(Position.status == status)
        total = q.count()
        rows = (q.order_by(Position.opened_at.desc(), Position.id.desc())
                .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all())

    return templates.TemplateResponse(request, "journal.html", {
        "request": request, "user": u, "tab": tab, "rows": rows, "total": total,
        "page": page, "pages": max(1, -(-total // PAGE_SIZE)), "page_size": PAGE_SIZE,
        "accounts": accounts, "strategies": strategies,
        "levels": [l.value for l in LogLevel], "kinds": [k.value for k in LogKind],
        "f": {"account_id": account_id, "strategy_id": strategy_id,
              "level": level, "kind": kind, "status": status},
        "flash": pop_flash(request)})


# ---------------- per (account, strategy) trading dashboard ----------------

def _link_stats(db, link) -> dict:
    """Everything the detail page shows, computed in Python over the rows we
    already fetch.

    PnL comes from webapp/sync_positions.py, which fills exit_price/pnl from
    the broker's closing deal. It is nullable on purpose: NULL means "not
    synced / the broker cannot report it", which is NOT 0.0. Every money
    aggregate below therefore runs over `priced` (rows with a non-NULL pnl)
    and reports how many rows that was, rather than coercing NULL to zero and
    quietly reporting an unsynced trade as a break-even one.
    """
    pos = (db.query(Position)
           .filter_by(account_id=link.account_id, strategy_id=link.strategy_id)
           .order_by(Position.opened_at.desc(), Position.id.desc()).all())
    closed = [p for p in pos if p.status == "closed"]
    opened = [p for p in pos if p.status == "open"]

    # Hold-time stats cover bot trades only: an adopted row's opened_at is the
    # broker's, and whatever a human did with it says nothing about how long
    # this strategy holds a position.
    holds = [(p.closed_at - p.opened_at).total_seconds() / 60.0
             for p in closed
             if p.closed_at and p.opened_at and p.origin != "adopted"]
    holds_sorted = sorted(holds)
    median = (holds_sorted[len(holds_sorted) // 2] if len(holds_sorted) % 2
              else (holds_sorted[len(holds_sorted) // 2 - 1]
                    + holds_sorted[len(holds_sorted) // 2]) / 2) if holds_sorted else None

    reasons = Counter(p.reason or "—" for p in closed).most_common()
    sides = Counter(p.side for p in pos)
    adds = sum(1 for p in pos if p.is_add)

    today = datetime.utcnow().date()
    per_day_c = Counter(p.opened_at.date() for p in pos if p.opened_at)
    per_day = [(today - timedelta(days=i), per_day_c.get(today - timedelta(days=i), 0))
               for i in range(29, -1, -1)]
    day_max = max([n for _, n in per_day] or [0])

    since = datetime.utcnow() - timedelta(hours=24)
    logs = (db.query(LogEntry)
            .filter_by(account_id=link.account_id, strategy_id=link.strategy_id)
            .order_by(LogEntry.ts.desc(), LogEntry.id.desc()).limit(20).all())
    errors_24h = (db.query(LogEntry)
                  .filter_by(account_id=link.account_id, strategy_id=link.strategy_id)
                  .filter(LogEntry.level == LogLevel.ERROR.value, LogEntry.ts >= since)
                  .count())
    priced = [p for p in closed if p.pnl is not None]
    wins = [p for p in priced if p.pnl > 0]
    losses = [p for p in priced if p.pnl < 0]
    gross_win = sum(p.pnl for p in wins)
    gross_loss = sum(p.pnl for p in losses)      # already negative
    # Equity curve over priced trades, oldest first (`pos` is newest-first).
    eq, run = [], 0.0
    for p in sorted(priced, key=lambda x: (x.closed_at or x.opened_at, x.id)):
        run += p.pnl
        eq.append(run)
    peak, max_dd = 0.0, 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = min(max_dd, v - peak)

    return {
        "total": len(pos), "open": len(opened), "closed": len(closed),
        # --- money (see the docstring: NULL pnl is excluded, not zeroed) ---
        "priced_n": len(priced),
        # closed rows we have no money for -- shown so a small `priced_n` is
        # read as "not synced yet", not as "this strategy barely trades"
        "unpriced_n": len(closed) - len(priced),
        "pnl_total": sum(p.pnl for p in priced) if priced else None,
        "wins": len(wins), "losses": len(losses),
        "win_rate": (100.0 * len(wins) / len(priced)) if priced else None,
        "avg_win": (gross_win / len(wins)) if wins else None,
        "avg_loss": (gross_loss / len(losses)) if losses else None,
        "best": max((p.pnl for p in priced), default=None),
        "worst": min((p.pnl for p in priced), default=None),
        # profit factor is undefined with no losing trade (division by zero),
        # so it stays None rather than becoming a misleading "infinite edge"
        "profit_factor": (gross_win / abs(gross_loss)) if gross_loss else None,
        "max_dd": max_dd if eq else None,
        "equity": eq,
        "adopted_n": sum(1 for p in pos if p.origin == "adopted"),
        "adds": adds,
        "adds_pct": (100.0 * adds / len(pos)) if pos else 0.0,
        "buys": sides.get("buy", 0), "sells": sides.get("sell", 0),
        "reasons": reasons, "closed_n": len(closed),
        "avg_hold": (sum(holds) / len(holds)) if holds else None,
        "median_hold": median,
        "per_day": per_day, "day_max": day_max,
        "recent": pos[:20], "logs": logs, "errors_24h": errors_24h,
    }


@app.get("/account-strategies/{lid}", response_class=HTMLResponse)
def link_detail(lid: int, request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    link = db.get(AccountStrategy, lid)
    if not link or link.account.user_id != u.id:
        return _redirect("/")
    return templates.TemplateResponse(request, "strategy_detail.html", {
        "request": request, "user": u, "link": link, "s": _link_stats(db, link),
        "flash": pop_flash(request)})


# ---------------- admin: user management ----------------

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u or not u.is_admin:
        return _redirect("/")
    return templates.TemplateResponse(request, "users.html", {
        "request": request, "user": u, "users": db.query(User).all(),
        "flash": pop_flash(request)})


@app.post("/users/create")
def create_user(request: Request, username: str = Form(...), password: str = Form(...),
                is_admin: bool = Form(False), db=Depends(get_db)):
    u = current_user(request, db)
    if not u or not u.is_admin:
        return _redirect("/")
    if db.query(User).filter_by(username=username).first():
        flash(request, "Username exists", "err")
        return _redirect("/users")
    db.add(User(username=username, password_hash=hash_password(password), is_admin=is_admin))
    db.commit()
    flash(request, f"User '{username}' created")
    return _redirect("/users")
