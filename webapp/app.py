"""FastAPI web UI for the multi-account bot.

Session login (multi-user); each user manages only their own cTrader credentials
and accounts. The UI only edits DB state (enable/disable = start/stop, config); the
`webapp.runner` process does the trading. Run:

    pip install fastapi uvicorn jinja2 python-multipart itsdangerous
    APP_SECRET_KEY=... uvicorn webapp.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from webapp.db import SessionLocal, init_db
from webapp.models import User, Account, Position
from webapp.security import hash_password, verify_password
from bot import s007_config as SC

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
PRESETS = ["BASELINE_S007", "FILTERED_S007", "WORKING_S007", "WORKING_S007_V2"]

init_db()
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
    request.session.setdefault("_flash", []).append({"msg": msg, "kind": kind})


def pop_flash(request: Request):
    f = request.session.pop("_flash", [])
    return f


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
        open_pos[a.id] = [p for p in a.positions if p.status == "open"]
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "user": u, "accounts": accounts, "open_pos": open_pos,
        "has_token": bool(u._access_token_enc), "flash": pop_flash(request)})


@app.post("/accounts/{aid}/toggle")
def toggle(aid: int, request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    a = db.get(Account, aid)
    if a and a.user_id == u.id:
        a.enabled = not a.enabled
        if not a.enabled:
            a.status = "stopped"
        db.commit()
        flash(request, f"Account {a.ctid_trader_account_id} {'started' if a.enabled else 'stopped'}")
    return _redirect("/")


@app.get("/accounts/add", response_class=HTMLResponse)
def add_form(request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    return templates.TemplateResponse(request, "add_account.html", {
        "request": request, "user": u, "presets": PRESETS, "flash": pop_flash(request)})


@app.post("/accounts/add")
def add_account(request: Request, ctid: int = Form(...), label: str = Form(""),
                preset: str = Form("BASELINE_S007"), risk: float = Form(0.25),
                lot: float = Form(0.01), is_live: bool = Form(False), db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    dup = db.query(Account).filter_by(user_id=u.id, ctid_trader_account_id=ctid).first()
    if dup:
        flash(request, "That account id already exists", "err")
        return _redirect("/accounts/add")
    db.add(Account(user_id=u.id, ctid_trader_account_id=ctid, label=label, preset=preset,
                   risk_pct=risk, fixed_lot=lot, is_live=is_live, enabled=False))
    db.commit()
    flash(request, f"Account {ctid} added (disabled — start it from the dashboard)")
    return _redirect("/")


@app.post("/accounts/{aid}/save")
def save_account(aid: int, request: Request, preset: str = Form(...),
                 risk: float = Form(...), lot: float = Form(...), db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    a = db.get(Account, aid)
    if a and a.user_id == u.id:
        a.preset, a.risk_pct, a.fixed_lot = preset, risk, lot
        db.commit()
        flash(request, "Saved")
    return _redirect("/")


@app.post("/accounts/{aid}/delete")
def delete_account(aid: int, request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    a = db.get(Account, aid)
    if a and a.user_id == u.id:
        db.delete(a); db.commit()
        flash(request, "Account removed")
    return _redirect("/")


# ---------------- credentials ----------------

@app.get("/creds", response_class=HTMLResponse)
def creds_form(request: Request, db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    return templates.TemplateResponse(request, "creds.html", {
        "request": request, "user": u,
        "has_secret": bool(u._client_secret_enc), "has_token": bool(u._access_token_enc),
        "flash": pop_flash(request)})


@app.post("/creds")
def save_creds(request: Request, client_id: str = Form(""), client_secret: str = Form(""),
               access_token: str = Form(""), db=Depends(get_db)):
    u = current_user(request, db)
    if not u:
        return _redirect("/login")
    if client_id.strip():
        u.ctrader_client_id = client_id.strip()
    if client_secret.strip():
        u.client_secret = client_secret.strip()      # encrypted by the model setter
    if access_token.strip():
        u.access_token = access_token.strip()
    db.commit()
    flash(request, "Credentials saved (encrypted at rest)")
    return _redirect("/")


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
