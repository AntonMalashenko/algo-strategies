"""Account-level loss guard for prop-firm style accounts (ALGODEV-55).

Every strategy already caps its OWN risk (risk_pct per trade, a per-strategy
daily budget -- see bot/s007_paper.py's DAILY_RISK_CAP_PCT logic). None of
that knows about the ACCOUNT: two strategies sharing one prop account (S007 +
S021 on FTMO) each respect their own budget, but nothing stops the account
as a whole from walking into the prop firm's hard limits (daily loss, max
loss) after, say, a slippage-heavy day. This module is that missing layer.

Policy ("variant A", agreed with Anton 2026-09-29): the guard only BLOCKS
NEW RISK. Positions already open are never force-closed by it -- each one
still carries its own server-side stop-loss, which is exactly the loss the
guard already budgets for (see `open_risk` below).

The check, before each new entry:

  daily:  (day_start_balance - balance)  +  open_risk  +  new_risk
            <= daily_loss_pct % of initial_balance
  max:    balance - open_risk - new_risk
            >= initial_balance - max_loss_pct % of initial_balance

i.e. "if every open position AND the new one got stopped out right now,
would we breach our own (tighter-than-the-firm's) limit?". Measured the way
FTMO measures it: daily loss against the balance at the firm's own day
boundary (00:00 CE(S)T for FTMO -> `day_reset_tz`), max loss against the
static initial balance. See https://ftmo.com/en/trading-objectives/.

`day_start_balance` is not stored anywhere: it is reconstructed as
`balance - realised P&L of every closing deal since the day boundary`, from
the broker's own deal history (all strategies on the account, not just the
caller's), so it survives restarts and needs no extra state.

KNOWN LIMITATION (documented, accepted): `open_risk` is the caller's OWN
open positions' distance-to-stop only. Other strategies' OPEN positions on
the same account are in another instrument whose $-per-point this module
does not know; their risk is bounded by their own per-strategy daily budget
instead, and everything they have already REALISED is counted via the deal
history above. With S007 2% + S021 1% budgets under a 4% guard, the sum of
per-strategy budgets alone already stays inside the guard.

Pure math, no broker I/O -- callers hand in what their cycle already fetched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PCT = 100.0                    # percent -> fraction divisor
MS_PER_S = 1000                # cTrader timestamps are epoch milliseconds
DEFAULT_DAY_RESET_TZ = "UTC"   # used when an account names no reset timezone

# Verdict reasons -- logged as-is, keep stable (log readers grep for them).
REASON_DAILY = "account_daily_loss_guard"
REASON_MAX = "account_max_loss_guard"


@dataclass(frozen=True)
class AccountLimits:
    """Our own (buffered) account-level limits, read from webapp Account.

    Either pct may be None = that check is off. `initial_balance` is the
    account's starting capital (Account.initial_balance) -- both limits are
    percentages OF IT, same convention as FTMO."""
    initial_balance: float
    daily_loss_pct: float | None = None
    max_loss_pct: float | None = None
    day_reset_tz: str = DEFAULT_DAY_RESET_TZ

    @property
    def active(self) -> bool:
        return bool(self.initial_balance) and (
            self.daily_loss_pct is not None or self.max_loss_pct is not None)


@dataclass
class GuardVerdict:
    allowed: bool
    reason: str | None = None
    details: dict = field(default_factory=dict)


def day_start_ms(tz_name: str, now: datetime | None = None) -> int:
    """Epoch ms of the most recent local midnight in `tz_name`."""
    tz = ZoneInfo(tz_name or DEFAULT_DAY_RESET_TZ)
    local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * MS_PER_S)


def realized_pnl_since(closed_deals, since_ms: int) -> float:
    """Sum of net P&L (gross + swap + commission) of closing deals executed at
    or after `since_ms`. Deal dicts are CTraderS007._parse_deals' shape."""
    return sum(float(d.get("pnl") or 0.0) for d in (closed_deals or [])
               if d.get("executed_ms") is not None and d["executed_ms"] >= since_ms)


def check_new_risk(limits: AccountLimits | None, *, balance: float, closed_deals,
                   open_risk: float, new_risk: float,
                   now: datetime | None = None) -> GuardVerdict:
    """May a new position risking `new_risk` (account currency, to its SL) be
    opened? See the module docstring for the exact formulas."""
    if limits is None or not limits.active:
        return GuardVerdict(True)
    initial = float(limits.initial_balance)
    realized_today = realized_pnl_since(closed_deals, day_start_ms(limits.day_reset_tz, now))
    day_start_balance = balance - realized_today
    details = dict(balance=balance, day_start_balance=day_start_balance,
                   realized_today=realized_today, open_risk=open_risk, new_risk=new_risk,
                   initial_balance=initial)

    if limits.daily_loss_pct is not None:
        daily_budget = initial * limits.daily_loss_pct / PCT
        worst_day_loss = (day_start_balance - balance) + open_risk + new_risk
        details.update(daily_budget=daily_budget, worst_day_loss=worst_day_loss)
        if worst_day_loss > daily_budget:
            return GuardVerdict(False, REASON_DAILY, details)

    if limits.max_loss_pct is not None:
        floor = initial - initial * limits.max_loss_pct / PCT
        worst_balance = balance - open_risk - new_risk
        details.update(max_loss_floor=floor, worst_balance=worst_balance)
        if worst_balance < floor:
            return GuardVerdict(False, REASON_MAX, details)

    return GuardVerdict(True, None, details)
