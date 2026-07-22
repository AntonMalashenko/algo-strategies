"""utils/trade_logger.py — reusable structured logging for strategy bots.

Global and strategy-agnostic: any strategy (S004, S007, ...) instantiates its own
`StrategyLogger(name)`. Logs are grouped per strategy, and **every position is
written to its own file** so a single trade can be replayed / debugged in isolation.

Directory layout (under `log_root`, default `reports/logs`):

    <STRATEGY>/
        <STRATEGY>.log                 # human-readable text (rotating, all cycles)
        events-YYYY-MM-DD.jsonl        # every structured event, one JSON per line
        cycles-YYYY-MM-DD.jsonl        # one snapshot per reconcile cycle
        positions/
            <label>.jsonl              # per-position lifecycle: open/add/stop/close/...

Every record carries: ts (ISO, local), strategy, cycle (correlation id), kind, and
the caller's fields. Text + JSONL are written together so you can eyeball the log or
parse it programmatically.

Example:
    log = StrategyLogger("S007")
    cid = log.cycle_start(now="2024-05-10 10:45", in_window=True, direction="up",
                          context={"rh": 18771, "rl": 18729, "mid": 18750})
    log.position("S007:2024-05-10:0", "open", side="buy", entry=18768.8,
                 sl=18742.7, tp=18805.9, is_add=False)
    log.order("S007:2024-05-10:0", "place_market",
              request={"side": "buy", "sl": 18742.7, "tp": 18805.9, "lot": 0.01},
              result={"order_id": 123})
    log.position("S007:2024-05-10:0", "close", reason="target", exit=18805.9, R=+1.4)
    log.cycle_end(cid, actions=1)
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOCK = threading.Lock()
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _safe(name: str) -> str:
    return _SAFE.sub("_", str(name)).strip("_") or "unnamed"


class StrategyLogger:
    """One logger per strategy. Thread-safe append writes; flushes every record."""

    def __init__(self, strategy: str, log_root: str | Path = "reports/logs",
                 console: bool = True, level: int = logging.DEBUG,
                 max_bytes: int = 5_000_000, backups: int = 10):
        self.strategy = strategy
        self.dir = Path(log_root) / strategy
        self.pos_dir = self.dir / "positions"
        self.pos_dir.mkdir(parents=True, exist_ok=True)
        self._cycle_seq = 0

        # human-readable rotating text log
        self.log = logging.getLogger(f"strategy.{strategy}")
        self.log.setLevel(level)
        self.log.propagate = False
        if not self.log.handlers:
            fmt = logging.Formatter(
                "%(asctime)s | %(levelname)-7s | " + strategy + " | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S")
            fh = RotatingFileHandler(self.dir / f"{strategy}.log",
                                     maxBytes=max_bytes, backupCount=backups)
            fh.setFormatter(fmt)
            self.log.addHandler(fh)
            if console:
                ch = logging.StreamHandler()
                ch.setFormatter(fmt)
                self.log.addHandler(ch)

    # ---------- low-level structured write ----------

    def _jsonl(self, path: Path, record: dict) -> None:
        line = json.dumps(record, default=str, ensure_ascii=False)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def event(self, kind: str, cycle: str | None = None, level: int = logging.INFO,
              text: str | None = None, **fields) -> dict:
        """Write one structured event to events-<date>.jsonl (+ text log)."""
        rec = dict(ts=_now_iso(), strategy=self.strategy, cycle=cycle, kind=kind, **fields)
        day = datetime.now().strftime("%Y-%m-%d")
        self._jsonl(self.dir / f"events-{day}.jsonl", rec)
        msg = text if text is not None else f"{kind} " + " ".join(
            f"{k}={v}" for k, v in fields.items() if k not in ("context",))
        self.log.log(level, (f"[{cycle}] " if cycle else "") + msg)
        return rec

    # ---------- cycle lifecycle ----------

    def cycle_start(self, **fields) -> str:
        self._cycle_seq += 1
        cid = datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{self._cycle_seq:04d}"
        rec = dict(ts=_now_iso(), strategy=self.strategy, cycle=cid,
                   kind="cycle_start", **fields)
        day = datetime.now().strftime("%Y-%m-%d")
        self._jsonl(self.dir / f"cycles-{day}.jsonl", rec)
        self.event("cycle_start", cycle=cid, level=logging.INFO,
                   text="cycle start " + " ".join(
                       f"{k}={v}" for k, v in fields.items() if k != "context"),
                   **fields)
        return cid

    def cycle_end(self, cycle: str, **fields) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        self._jsonl(self.dir / f"cycles-{day}.jsonl",
                    dict(ts=_now_iso(), strategy=self.strategy, cycle=cycle,
                         kind="cycle_end", **fields))
        self.event("cycle_end", cycle=cycle, text="cycle end " + " ".join(
            f"{k}={v}" for k, v in fields.items()), **fields)

    # ---------- per-position logging ----------

    def position(self, label: str, action: str, cycle: str | None = None, **fields) -> None:
        """Append one lifecycle event to positions/<label>.jsonl AND the event log."""
        rec = dict(ts=_now_iso(), strategy=self.strategy, cycle=cycle,
                   label=label, action=action, **fields)
        self._jsonl(self.pos_dir / f"{_safe(label)}.jsonl", rec)
        self.event("position", cycle=cycle, level=logging.INFO,
                   text=f"position {action} [{label}] " + " ".join(
                       f"{k}={v}" for k, v in fields.items()),
                   label=label, action=action, **fields)

    def label_was_closed(self, label: str) -> bool:
        """True if this label's position log already recorded a 'close' action.

        Idempotency guard for stateless-per-cycle bots (see S007's
        bot/s007_paper.py::live): each label is emitted once per day by the
        strategy engine and should open, then close, exactly once. The only
        broker-side source of truth for "is this open" is a fresh reconcile
        each cycle -- but a real stop-out can beat the M1 bar the engine is
        reasoning from to the punch, so "not in the broker's open positions"
        does NOT always mean "never opened". Checking our own append-only
        log (which the broker call that closed it already wrote to) catches
        that case: if we already saw this label close, never re-place it,
        even if a lagging bar still "wants" it open (bug found 2026-07-21,
        see decisions-log.md).
        """
        path = self.pos_dir / f"{_safe(label)}.jsonl"
        if not path.exists():
            return False
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("action") == "close":
                    return True
        return False

    def order(self, label: str, op: str, cycle: str | None = None,
              request: dict | None = None, result=None, error=None) -> None:
        """Log a broker order attempt with its request and result/error, per-position."""
        ok = error is None
        rec = dict(ts=_now_iso(), strategy=self.strategy, cycle=cycle, label=label,
                   action="order", op=op, ok=ok,
                   request=request, result=str(result) if result is not None else None,
                   error=str(error) if error is not None else None)
        self._jsonl(self.pos_dir / f"{_safe(label)}.jsonl", rec)
        self.event("order", cycle=cycle, level=logging.INFO if ok else logging.ERROR,
                   text=f"order {op} [{label}] ok={ok}" + (f" error={error}" if error else ""),
                   label=label, op=op, ok=ok, request=request,
                   result=str(result) if result is not None else None,
                   error=str(error) if error is not None else None)

    # ---------- passthrough ----------

    def debug(self, msg, **kw): self.log.debug(msg)
    def info(self, msg, **kw): self.log.info(msg)
    def warning(self, msg, **kw): self.log.warning(msg)

    def error(self, msg, exc: BaseException | None = None, cycle: str | None = None) -> None:
        self.event("error", cycle=cycle, level=logging.ERROR, text=str(msg),
                   error=repr(exc) if exc else None)
        if exc is not None:
            self.log.exception(msg)
        else:
            self.log.error(msg)
