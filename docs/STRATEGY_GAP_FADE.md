# Strategy passport: S019 — Gap Fade

Status: **FROZEN 2026-08-30; paper phase pending (not started yet)** (variant
A of the E5 decision fork -- see `docs/HANDOFF_GAP_FADE_RESEARCH.md` for the
full research trail, experiments E1-E5). Registry S-number: **S019**,
assigned 2026-09-01. Tracking task: **ALGODEV-35** (In Progress).

## Frozen rules (do not tune during paper)

All times US/Eastern, regular session. Code: `strategies/gap_fade.py`.

| Element | Rule |
|---|---|
| Universe | 64 liquid US large caps (`scripts/fetch_stock_universe.py`) |
| Setup | Official open 2-5% below yesterday's close |
| Exclusion | Earnings announcement within the last 3 calendar days (or unknown) |
| Regime | SPY 20-day realized vol (through yesterday) < 25% annualized |
| Liquidity | Trailing 20-day median dollar volume >= $50M |
| Selection | Max 4 trades/day, largest \|gap\| first |
| Entry | Long, first bar at/after 09:45 |
| Stop | -1% from entry, intrabar |
| Exit | 12:00 bar close if not stopped |
| Cost assumption | 6 bps round-trip |

## Expectations (from research, honestly discounted)

- Research subsample (2024-09..2026-08, 643 events): +17.9 bps/trade net,
  win 46.7%, t=3.05.
- Expected LIVE-ish performance after survivorship haircut: assume
  ~+8-12 bps/trade until the paper log says otherwise.
- Loss per trade hard-capped ~-1.06% gross of position (stop + gap-through
  allowance). At 0.25% account risk/trade, worst observed day was -1.06% of
  account.
- Trade frequency: highly uneven -- median 1 signal/day when active, but
  multi-signal cluster days carry much of the P&L.

## Paper procedure

Daily after the US close (or catch-up later -- the runner is idempotent):

```bash
python -m bot.gap_fade_paper
```

Log: `reports/paper/gap_fade_paper_log.csv`. Refresh earnings cache monthly:

```bash
python -m scripts.fetch_earnings_dates
```

## Gate to proceed (decided in advance)

Evaluate after **100 traded events** (expect ~3-6 months):

- PROCEED to sizing/live discussion if: mean net > +5 bps AND no
  procedural failures (missed earnings exclusions, broken data days).
- KILL if: mean net < 0, OR any single day worse than -2.5x the modeled
  worst day at reference sizing, OR the stop-rate exceeds 60% (entry
  timing no longer working).
- Otherwise: extend paper by 50 events, once.

## Kill criteria during paper (operational)

- Earnings cache older than 45 days -> pause until refreshed.
- Polygon minute data unavailable/mismatched for >3 sessions -> pause,
  investigate data source.

## Known unresolved risks (accepted into paper)

1. Survivorship bias in the research universe (current large caps only).
2. Cluster-day selection (largest-gap-first) was chosen on thin evidence.
3. 2-year minute-data validation window only; 2011-style sudden-vol regimes
   untested at minute resolution.
4. No live execution slippage measurement yet -- paper log IS the bar-price
   baseline a future live phase will be compared against.


