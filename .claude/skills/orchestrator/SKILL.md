---
name: orchestrator
description: >-
  The entry point and router for EVERY task in this AlgoTrading project. It holds the
  catalog of all project skills and when to use each, and defines the start-to-finish
  workflow: at the start of a task, route it to the right skill(s); apply them; verify; then
  answer. Use this skill first, on every task — new strategy, backtest, rule/modifier,
  bot code, logging, the web app, docs, or a plain question — even when the request seems to
  fit one skill directly, because routing through here is what keeps the work consistent and
  the response in the agreed format. It also fixes the answer style: detailed as always, but
  plain-language for the financial/strategy side (for a non-specialist reader) and full
  senior-Python depth on the engineering side. Read this before doing anything else so the
  correct skills are selected and applied instead of ad-hoc work.
---

# orchestrator — route every task, apply the right skills, then answer

This is the project's control loop. Every task starts here: figure out what the task is,
pull in the skill(s) that own the relevant code and conventions, follow them, verify, and
deliver the answer in the agreed format. The point is consistency — no task in this repo is
done "off the map."

## The loop: start → apply → finish

**1. Start — classify and route.** Read the request and match it to the skill catalog below.
Most real tasks touch more than one skill (e.g. "add a rule and wire it into the bot" =
`strategy-modifiers` + `code-architecture` + `strategy-logging`). Name to yourself which
skills apply before writing anything.

**2. Apply — follow the selected skills.** Read/apply each matched skill and honor its rules.
Where skills interact, respect the ordering their own bodies imply — e.g. research before
building (per `strategy-lifecycle`), base-untouched + verdict-in-comment for any variant
(`strategy-modifiers`), shared-base + named-constants for code (`code-architecture`), and
**never edit a skill without Anton's explicit consent** (the governance rule in
`code-architecture`).

**3. Finish — verify, then answer.** Run the verification the task calls for (regression /
no-look-ahead after engine changes, a dry runner cycle for the bot, TestClient for the UI,
re-reading a doc you wrote). Sync the docs on the same task if a skill requires it. Then
respond in the format below. Close the loop: if the work changed code an owned skill
documents, flag the drift and propose the skill update for consent — don't apply it silently.

## Skill catalog — what each owns and when to use it

- **`strategy-lifecycle`** — the end-to-end flow of creating/researching/validating/managing
  a strategy: the two-track system (code in repo, research docs in the Claude Project), the
  `idea→…→archived` status lifecycle, the validation gates (regression → walk-forward →
  costs → deployment). Use for: new strategy, "is there an edge", backtest, walk-forward,
  promote/archive, registry/passport/spec/logs updates.
- **`strategy-modifiers`** — variants as default-off config flags + named `.with_()` presets,
  protecting the frozen baseline, recording each modifier's verdict in the code, keeping docs
  synced, and keeping the most profitable configs ranked and labeled. Use for: "test this
  modification", "base untouched", "add a preset", "which config is most profitable / best
  for prop".
- **`code-architecture`** — clean code, the shared base layer (`bot/` adapter+config+signals,
  `utils/` logger+data+metrics) reused by all strategies, the no-magic-values / named-
  constants rule, and the skill↔code governance rule (update the owning skill only with
  Anton's consent). Use for: any bot code add/refactor, wiring a strategy into shared
  engines, touching the cTrader adapter / utils / config, introducing any constant.
- **`strategy-logging`** — the global `StrategyLogger` (`utils/trade_logger.py`): per-strategy
  group, one file per position, cycle correlation ids, the `reports/logs/` layout. Use for:
  adding logging, tracing a trade, debugging "why did it do that", reading the logs.
- **`webapp`** — the multi-user / multi-account control panel (`webapp/`): FastAPI UI,
  `User/Account/Position` models, Fernet-encrypted creds, PBKDF2 auth, and the decoupled
  runner. Use for: the dashboard, login, accounts, credential storage, the DB, the runner.
- **`trading-analysis`** — on-demand diagnosis of a strategy's live/paper trading
  behavior from its logs (+ read-only broker check): is it doing the right thing right
  now, and if it holds no position, is that a bug or expected. Strategy-agnostic (S007,
  S009, future ones). Use for: "проанализируй торговлю <NNN/name>", "why are there no
  positions", "is this a bug or normal", auditing a session or a wider date range.
- **`orchestrator`** (this) — routing + response format. Always first.

If a task matches no domain skill (a plain factual question, a one-off calculation), still
run the loop: route → note "no domain skill applies" → answer in the format below.

## Response format — two registers, always detailed

Answer thoroughly, as before — but pitch each part to its right reader. The maintainer is a
**senior Python developer** who also wants the **financial/strategy side kept plain enough
for a non-specialist**. So:

- **Financial / strategy / results content → plain, accessible language.** Explain the edge,
  the backtest result, the risk, the "why" of a rule, and what a number means for real money
  in terms an ordinary person could follow. Expand jargon on first use (R, expectancy,
  drawdown, walk-forward, spread) in a few words. Lead with the takeaway, then the detail.
  This is the side to simplify.
- **Engineering / Python / architecture content → full senior depth, no dumbing down.** Real
  code, exact APIs and file paths, precise types, the actual trade-offs, edge cases,
  verification done. Assume fluency; don't pad with basics.
- **Overall:** detailed and complete (not terse), Russian in chat, English for anything that
  lands in the repo (`AGENTS.md`). Structure long answers so the plain-language summary is
  reachable without reading the code depth, and the code depth is there for when it's wanted.

Rule of thumb: if a sentence is about *money, risk, or whether the strategy works*, write it
so Anton could read it aloud to someone non-technical. If it's about *how the code works*,
write it for a senior engineer. Both in the same answer, clearly separated.

## Why route everything through here

One place decides which conventions apply, so a strategy change doesn't skip the gates, a
code change doesn't skip named-constants or the shared base, a modifier doesn't quietly edit
the baseline, and a skill doesn't drift from its code without consent. It also guarantees the
answer always comes back in the format above instead of drifting task to task.
