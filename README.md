# algo

Algo-trading strategy research: engines, backtests, a live/paper bot, and a
multi-account web control panel. Primary markets: futures / forex / indices.

## Structure

- `strategies/` — strategy implementations (signals, entry/exit rules). A
  single-file strategy is `strategies/<name>.py` (`donchian.py`, `fvg_mtf.py`,
  `fx_carry.py`); a strategy with tunables/variants is a package
  (`strategies/ger40_lonfra/` — S007, `config/data/structure/setups/engine`).
- `backtest/` — backtest runners per strategy (`run_donchian.py`, `run_fvg.py`,
  `run_carry.py`).
- `bot/` — the live/paper trading runtime: the shared cTrader adapter
  (`ctrader.py`) and config (`config.py`), plus per-strategy live glue
  (`s007_config.py` / `s007_signals.py` / `ctrader_s007.py` / `s007_paper.py`
  for S007). See `bot/S007_README.md` for the full S007 bot guide.
- `webapp/` — multi-user / multi-account web control panel (FastAPI + SQLite)
  that manages bot accounts and credentials, decoupled from the trading
  runner. See `webapp/README.md` for the full guide.
- `utils/` — shared, strategy-agnostic helpers: `trade_logger.py`
  (`StrategyLogger` — structured per-strategy, per-position logging),
  `data.py`, `metrics.py`, `report.py`.
- `scripts/` — one-off / scheduled data-fetching and conversion scripts
  (histdata, Dukascopy, FRED rates, G10 spot, index data).
- `configs/` — shared config module (currently a stub, reserved for
  cross-strategy configuration as it's needed).
- `data/raw/` — raw historical data grouped by instrument folder, e.g.
  `data/raw/EURUSD/EURUSDd1.csv` (not in git).
- `data/processed/` — prepared datasets (not in git).
- `reports/` — generated reports, plots, and structured logs (not in git).
  Live/paper logs land under `reports/logs/<STRATEGY>/` (see `utils/trade_logger.py`).
- `notebooks/` — research notebooks.
- `tests/` — pytest test suite.
- `docs/` — in-repo technical docs (e.g. `EXPERIMENTS.md`, `STRATEGY_S004.md`).
- `.claude/skills/` — repo-scoped Claude skills documenting this codebase's
  conventions (architecture, strategy lifecycle, modifiers, logging, webapp).

Strategy **passports, specs, the strategy registry, and the decisions/experiments
logs** are not in this repo — they live in the AlgoTrading Claude Project (the
research narrative track). This repo is the code track; see
`.claude/skills/strategy-lifecycle/SKILL.md` for how the two stay in sync.

## Environment

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` covers the core research/backtest stack (pandas, numpy,
matplotlib, vectorbt, quantstats, pytest, ...). Two components need extra,
separately-installed dependencies — see their sections below:

- the live/paper bot needs `ctrader-open-api` (+ `python-dotenv`, already
  pulled in transitively by the bot config loaders);
- the web control panel needs `fastapi uvicorn jinja2 python-multipart
  itsdangerous sqlalchemy cryptography`.

Broker credentials and app secrets live in `<repo>/.env` (gitignored), read by
both `bot/` and `webapp/`:

```
CTRADER_CLIENT_ID=...
CTRADER_CLIENT_SECRET=...
CTRADER_ACCESS_TOKEN=...
CTRADER_ACCOUNT_ID=...              # numeric ctidTraderAccountId of the DEMO account
CTRADER_HOST=demo.ctraderapi.com    # optional, defaults to demo
APP_SECRET_KEY=...                  # required only for webapp — encrypts secrets at rest
```

## Running the components

### Backtests

Each validated strategy has its own runner in `backtest/`:

```bash
python -m backtest.run_donchian     # S001-S003
python -m backtest.run_fvg          # S004
python -m backtest.run_carry        # S005
```

S007 (`strategies/ger40_lonfra/`) is validated (regression, walk-forward, real-
spread costs, prop-firm simulation — see the strategy passport) but its
backtest/validation scripts are not yet mirrored into this repo's `backtest/`;
migrating them in is a pending reorg task. Until then, results and methodology
are documented in the Claude Project (`strategy-passport-S007.md`).

### S007 live/paper bot (direct path)

The bot trades straight off `.env` credentials — no web panel or database
needed. Full guide: `bot/S007_README.md`.

```bash
pip install ctrader-open-api python-dotenv pandas numpy

python -m bot.s007_paper --accounts   # list demo accounts for this access token
python -m bot.s007_paper --check      # auth handshake + resolve the GER40 symbol
python -m bot.s007_paper --dry-run --at "2024-05-10 10:45"   # offline sanity check, no broker
python -m bot.s007_paper --live       # one live reconcile cycle
```

Run `--live` every minute during the trading session (schedule with cron —
see `bot/S007_README.md` for the exact line). The bot is stateless: it rebuilds
the day's state from recent M1 bars each cycle, so a missed/restarted minute is
harmless. Current config (`bot/s007_config.py`): preset `WORKING_S007`
(recommended champion — see `.claude/skills/strategy-modifiers/SKILL.md`),
first-run sizing is a fixed lot (`FIXED_LOT`), not `RISK_PCT` yet — calibrate
the symbol's tick value against the broker before switching to percent-risk
sizing.

**Before leaving it unattended:** do one manually-watched `--live` cycle and
confirm in the cTrader UI that the SL sits exactly at the 0.5 level, TP is at
the target, and the lot size is right — see the "First-run checklist" in
`bot/S007_README.md`.

### Web control panel (webapp)

Multi-user / multi-account dashboard for managing bot accounts and
credentials. The UI only edits DB state; a separate runner process trades.
Full guide: `webapp/README.md`.

```bash
pip install sqlalchemy cryptography fastapi uvicorn jinja2 python-multipart itsdangerous

python -m webapp.cli init-db
python -m webapp.cli create-user --username <name> --password *** --admin \
    --access-token <ctrader_oauth_token>
python -m webapp.cli add-account --username <name> --ctid <ctidTraderAccountId> \
    --label demo1 --preset WORKING_S007 --risk 0.25 --enable

APP_SECRET_KEY=... uvicorn webapp.app:app --host 0.0.0.0 --port 8000   # the UI
python -m webapp.runner --dry --at "2024-05-10 10:45"                  # offline pipeline test
python -m webapp.runner                                                # one live cycle, all enabled accounts
```

Schedule `webapp.runner` on cron the same way as the direct bot path (every
minute during the session) if you switch to the multi-account setup; the UI
(`uvicorn`) can run continuously and independently since it never trades.

### Tests

```bash
pytest
```

### Data scripts

`scripts/` has no single entrypoint — each script fetches or converts data for
one strategy/source (`fetch_histdata.py`, `fetch_rates.py`, `fetch_g10_spot.py`,
`convert_dukascopy.py`, `convert_histdata*.py`, `download_*.py`,
`sort_raw_data.py`, `refresh_monthly.py`, `build_carry_ranks.py`). Run the one
relevant to the data you need, e.g. `python -m scripts.fetch_histdata`.

## Logging

Any bot logs through the shared `utils/trade_logger.StrategyLogger` — one
logger per strategy (or per strategy+account), every position in its own file
under `reports/logs/<STRATEGY>/positions/<label>.jsonl`, plus per-cycle and
per-event JSONL streams and a human-readable rotating text log. See
`.claude/skills/strategy-logging/SKILL.md` and the "Logging" section of
`bot/S007_README.md` for the full layout and how to read it when debugging.

## Strategy lifecycle

A strategy moves `idea → prototype → backtested → validated → paper → live →
archived`, gated by: regression / no-look-ahead → walk-forward (all years
positive, true OOS) → real-cost survival → deployment-reality simulation (e.g.
prop-firm rules). Modifiers are always default-off config flags + named
presets layered on a frozen baseline — the baseline is never edited in place.
Full detail: `.claude/skills/strategy-lifecycle/SKILL.md` and
`.claude/skills/strategy-modifiers/SKILL.md`. The strategy registry, passports,
and decision/experiment logs live in the AlgoTrading Claude Project, not in
this repo.

## Conventions

Everything committed to this repo (code, comments, docstrings, commit
messages, in-repo docs) is written in English — see `AGENTS.md`. Chat with the
maintainer stays in Russian; that's a separate axis from what's in the repo.
