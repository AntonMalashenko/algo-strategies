# Experiments log (S004 FVG bounce) — moved

Moved to Confluence on 2026-08-30: the E0–E11 log now lives in the
"S004 — H4 FVG bounce, тихие часы" page
(https://anton-mal-slb.atlassian.net/wiki/spaces/~5c584b39876a6c0cdbaee8fc/pages/10944530),
"Журнал экспериментов" section. The Russian research narrative also exists in
the Claude Project (`claude/experiments-log.md`).

Code-level invariant kept from the original file: every engine change ships
as an opt-in flag, defaults reproduce the previous behaviour exactly
(regression-tested — base config reproduces saved EURUSD trades 1811/1811,
r identical to 1e-12).
