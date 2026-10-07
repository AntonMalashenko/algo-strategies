"""The S021 manual-plan geometry and formatting -- shared by every way of getting an
ADR14 (an MT5 EA's own log, or a broker-neutral REST feed). No MT5 dependency: this is
pure math plus string formatting, so both mt5/tools/s021_manual.py (reads the EA's log)
and tools/s021_capital.py (reads Capital.com's feed) depend on this, not on each other.
"""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from strategies.orb_intraday.config import ORB_BASE

NY = ZoneInfo("America/New_York")


def levels_from_adr(open_price: float, adr: float, cfg=ORB_BASE) -> dict:
    """The frozen ORB_BASE geometry around one session open."""
    half = cfg.k_range * adr
    stop_distance = cfg.stop_adr_mult * adr
    upper = open_price + half
    lower = open_price - half
    return {
        "open": open_price,
        "adr": adr,
        "upper": upper,
        "lower": lower,
        "stop_distance": stop_distance,
        "long_stop_loss": upper - stop_distance,
        "short_stop_loss": lower + stop_distance,
    }


def both_clocks(ny_date, clock_time) -> str:
    ny = datetime.combine(ny_date, clock_time, tzinfo=NY)
    local = ny.astimezone()
    return f"{local:%H:%M} local / {ny:%H:%M} New York"


def telegram_message(levels: dict, ny_date, source: str) -> str:
    """The same plan as print_plan, shaped for a phone: every number is tap-to-copy and
    sits on its own line, so a tap grabs the number and nothing else."""
    from utils.telegram import code

    buy = code(f"{levels['upper']:.2f}")
    buy_sl = code(f"{levels['long_stop_loss']:.2f}")
    sell = code(f"{levels['lower']:.2f}")
    sell_sl = code(f"{levels['short_stop_loss']:.2f}")
    distance = code(f"{levels['stop_distance']:.2f}")
    open_price = code(f"{levels['open']:.2f}")
    place = html.escape(both_clocks(ny_date, ORB_BASE.session_open))
    cutoff = html.escape(both_clocks(ny_date, ORB_BASE.entry_cutoff))
    close = html.escape(both_clocks(ny_date, ORB_BASE.session_close))

    return "\n".join([
        f"<b>S021 — {ny_date}</b>",
        f"<i>{html.escape(source)}</i>",
        "",
        "🟢 <b>BUY STOP</b>",
        buy,
        f"stop loss {buy_sl}",
        "",
        "🔴 <b>SELL STOP</b>",
        sell,
        f"stop loss {sell_sl}",
        "",
        f"open {open_price} · stop distance {distance}",
        f"ADR14 {levels['adr']:.2f}",
        "",
        f"⏰ {place} — place both",
        "⚠️ one fills → <b>cancel the other</b>",
        f"⏱ {cutoff} — cancel if unfilled",
        f"🏁 {close} — close by the clock",
    ])


def print_plan(levels: dict, ny_date, source: str) -> None:
    """The levels block and the by-hand checklist, shared by every S021 manual tool."""
    print(f"S021 manual levels -- {ny_date} New York")
    print(f"ADR14 {levels['adr']:.3f}   ({source})")
    print()
    print(f"  your open  O   {levels['open']:>12.2f}")
    print(f"  buy stop   U   {levels['upper']:>12.2f}   stop loss {levels['long_stop_loss']:.2f}")
    print(f"  sell stop  L   {levels['lower']:>12.2f}   stop loss {levels['short_stop_loss']:.2f}")
    print(f"  stop distance  {levels['stop_distance']:>12.2f}")
    print()
    print("  the EA does none of this for you here -- by hand:")
    print(f"    1. at {both_clocks(ny_date, ORB_BASE.session_open)}, place both stop orders "
          "with their stop losses")
    print("    2. when one fills, CANCEL THE OTHER (no OCO: both filling doubles your risk)")
    print(f"    3. cancel both if unfilled by {both_clocks(ny_date, ORB_BASE.entry_cutoff)}")
    print(f"    4. close whatever is open at {both_clocks(ny_date, ORB_BASE.session_close)}")


def deliver(levels: dict, ny_date, source: str, to_telegram: bool) -> None:
    """Print the plan, and optionally push the phone-shaped copy to Telegram."""
    print_plan(levels, ny_date, source)
    if not to_telegram:
        return
    from utils import telegram

    if telegram.send(telegram_message(levels, ny_date, source)):
        print("\nsent to Telegram")
    else:
        print("\nTelegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
