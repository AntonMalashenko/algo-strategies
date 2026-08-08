"""Generic cross-cycle persistent state for a strategy that needs one --
e.g. S009's daily target book/equity/last-booked-day. A "state store" is
any object with `.load() -> dict` and `.save(dict) -> None`; no formal
Protocol class, just a shared shape two implementations satisfy:

  - FileStateStore (here) -- a single JSON file, used by the single-account
    CLI paths (e.g. bot/s009_paper.py's `--once`/`--loop`/`--status`) that
    have no DB session and shouldn't need one.
  - webapp/state_store.py::DBStateStore -- backed by the `strategy_state`
    DB table, used by the DB-driven multi-account runner
    (webapp/runner.py), since a file would not survive an ephemeral
    container and would collide across accounts anyway.

Kept here (utils/), not in bot/, so any strategy can use it without pulling
in another strategy's module, and kept out of webapp/ so bot/ never gains a
dependency on webapp/db (webapp depends on bot/utils, never the reverse).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable


class FileStateStore:
    """`state.json`-style single-file store. `default_factory` supplies the
    value `.load()` returns when the file does not exist yet (first-ever
    run) -- callers decide their own default shape, this class does not
    guess one."""

    def __init__(self, path: Path, default_factory: Callable[[], dict]):
        self.path = Path(path)
        self._default_factory = default_factory

    def load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return self._default_factory()

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2))
