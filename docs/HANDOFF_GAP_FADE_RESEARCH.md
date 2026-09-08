# Handoff: intraday gap-fade research — moved

Moved to Confluence on 2026-08-30: "S019 — интрадей gap-fade на US-акциях"
(page renamed 2026-09-01 when the S019 number was assigned; former title
"Кандидат — intraday gap-fade на US-акциях (до регистрации)")
(https://anton-mal-slb.atlassian.net/wiki/spaces/~5c584b39876a6c0cdbaee8fc/pages/11042817).
That page is the source of truth for the research narrative (E1–E5 findings,
frozen candidate wording, caveats, next gates).

Repo artifacts referenced by it: `scripts/fetch_stock_universe.py`,
`scripts/fetch_earnings_dates.py`, `scripts/fetch_stock_minute_polygon.py`,
`backtest/run_gap_*.py` (all reproducible; data git-ignored).

Update 2026-08-30: decision fork resolved as **variant A (freeze + paper)**.
Frozen rules and gates: `docs/STRATEGY_GAP_FADE.md`. Implementation:
`strategies/gap_fade.py`, `bot/gap_fade_paper.py`; log at
`reports/paper/gap_fade_paper_log.csv`. The Confluence page should be
synced with this decision.

