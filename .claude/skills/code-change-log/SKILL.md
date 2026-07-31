---
name: code-change-log
description: >-
  Read and write the repo's per-component change log under
  .claude/change-log/ (one JSONL file per package -- bot.jsonl, webapp.jsonl,
  utils.jsonl, ... -- and one per strategy -- ger40_lonfra.jsonl,
  fvg_mtf.jsonl, ...). READ the relevant file(s) BEFORE starting non-trivial
  work on a package or strategy's code (e.g. before touching bot/, read
  bot.jsonl; before touching strategies/ger40_lonfra/ or bot/s007_*, read
  BOTH bot.jsonl and ger40_lonfra.jsonl). WRITE an entry to the relevant
  file(s) after finishing a logical change, right before your closing
  summary. Also use this whenever the user asks "what changed and when",
  "log this fix", "when did we last touch X", or wants change history for a
  package/strategy.
---

# code-change-log — per-package/per-strategy change history

`.claude/change-log/` holds one append-only JSONL file per **component**:
one per package (`bot.jsonl`, `webapp.jsonl`, `utils.jsonl`, `scripts.jsonl`,
`configs.jsonl`, `backtest.jsonl`) and one per **strategy** (named after its
module under `strategies/`: `ger40_lonfra.jsonl`, `donchian.jsonl`,
`fvg_mtf.jsonl`, `fx_carry.jsonl`, `funding_carry.jsonl`,
`crypto_mtf.jsonl`, ...), plus `repo.jsonl` for cross-cutting/meta changes
(README, requirements.txt, CLAUDE.md, AGENTS.md, pyproject.toml, `.claude/`
itself). A change that spans a package AND a strategy (the common case for
`bot/s007_*.py`) gets the **same entry appended to both files** — this is
intentional duplication, not an error, so each file is readable standalone.

This is separate from two things that look similar but aren't:
- `reports/logs/` — runtime/trading logs the STRATEGIES produce while
  running; nothing to do with code-change history.
- git history — commits are user-triggered, often batch several unrelated
  edits together or never happen at all for a given session. This log
  exists to record changes even before/without a commit, and to carry the
  chat/session id a commit message doesn't.

## Read FIRST, before working on code

Before starting non-trivial work on a file, resolve which component(s) it
belongs to (see "Resolving components" below) and read the tail of each
matching `.claude/change-log/<component>.jsonl` — recent decisions, known
gaps, and prior fixes in that area often explain *why* the code looks the
way it does, or flag something already identified but not yet fixed.

- Touching only `bot/ctrader.py` (shared, no strategy-specific name) → read
  `bot.jsonl`.
- Touching `strategies/ger40_lonfra/engine.py` → read `ger40_lonfra.jsonl`.
- Touching `bot/s007_paper.py` (bot package AND the ger40_lonfra/S007
  strategy) → read **both** `bot.jsonl` and `ger40_lonfra.jsonl`.
- Touching `webapp/models.py` → read `webapp.jsonl`.

```bash
tail -20 .claude/change-log/bot.jsonl .claude/change-log/ger40_lonfra.jsonl 2>/dev/null \
  | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line or line.startswith('==>'):
        print(line); continue
    r = json.loads(line)
    print(r['ts'], '-', r['summary'][:200])
"
```

## Resolving components

**Package** = the top-level directory of the file, stripping a leading
`tests/` (tests mirror packages 1:1: `tests/bot/`, `tests/webapp/`,
`tests/utils/`, `tests/scripts/`, `tests/configs/`, `tests/backtest/` map to
`bot`, `webapp`, `utils`, `scripts`, `configs`, `backtest`).
`strategies/` is NOT a package component — every file under it resolves to a
strategy component instead (see below). Anything else at repo root
(`README.md`, `requirements.txt`, `CLAUDE.md`, `AGENTS.md`, `pyproject.toml`,
`.claude/*`) → component `repo`.

**Strategy** = the canonical name is the file/folder name under
`strategies/` that the code belongs to, even when the touched file lives
elsewhere (`bot/`, `scripts/`, `backtest/`, `tests/`, `docs/`). Known
mappings as of this writing (extend this list when a new strategy shows up
— do not leave it stale):

| touched path/filename contains          | strategy component |
|------------------------------------------|---------------------|
| `strategies/ger40_lonfra/`, `s007`, `ger40` | `ger40_lonfra`     |
| `strategies/donchian.py`, `donchian`      | `donchian`          |
| `strategies/fvg_mtf.py`, `fvg`            | `fvg_mtf`           |
| `strategies/fx_carry.py`, `fx_carry`      | `fx_carry`          |
| `strategies/funding_carry.py`, `funding_carry` | `funding_carry` |
| `strategies/crypto_mtf/`, `crypto_mtf`    | `crypto_mtf`        |
| `s009`, `bybit` (no clearer match yet)    | `bybit`             |

If a file matches none of these and isn't under `strategies/`, it has no
strategy component (package-only, or `repo`). If a NEW strategy appears
under `strategies/<name>` (file or folder), its component is `<name>` —
add a row to the table above so this stays discoverable.

A single file resolves to **at most one package** and **at most one
strategy** component; a change usually touches several files, so take the
union across all touched files for that change's full component set.

## Writing an entry

One JSON object per line, UTF-8, English `summary` (per `AGENTS.md`'s
language policy). Append the **identical** line to every resolved
component's file:

```json
{"ts": "2026-07-31T11:28:33", "session_id": "6a1ebc5a-051e-4392-a48d-d3e092405330", "components": ["bot", "utils", "ger40_lonfra"], "files": ["bot/s007_paper.py", "utils/trade_logger.py"], "summary": "Fixed reopen-after-broker-stop race: decide() now backfills the close via label_was_opened() the same cycle it notices the label missing from broker positions."}
```

Fields:
- `ts` — local timestamp at the moment you write the entry (after the
  change is done, not before).
- `session_id` — this chat's session id (see below for how to find it).
- `components` — the full resolved set for this change (same list in every
  copy of the entry, so a reader in one file knows where else to look).
- `files` — repo-relative paths that matter; skip incidental
  whitespace-only touches.
- `summary` — 1-3 sentences, past tense, English. State WHAT changed and WHY
  (the bug, the request, the design reason) like a commit body — not a diff
  dump, not "edited X.py".

### Finding the session id

There's no direct tool call for "what is my session id". Derive it from the
transcript directory: the current session's transcript is always the most
recently modified `*.jsonl` file directly under this project's Claude folder:

```bash
ls -t "$HOME/.claude/projects/-Users-Anton-Malashenko-Trading-algo/"*.jsonl 2>/dev/null \
  | head -1 | xargs -I{} basename {} .jsonl
```

(The project folder name is this repo's absolute path with `/` replaced by
`-`; adjust if this skill is copied into a different repo.)

### Appending

```bash
SESSION_ID=$(ls -t "$HOME/.claude/projects/-Users-Anton-Malashenko-Trading-algo/"*.jsonl \
  | head -1 | xargs -I{} basename {} .jsonl)

python3 -c "
import json, datetime
components = ['bot', 'ger40_lonfra']   # <- resolved for this change
entry = dict(
    ts=datetime.datetime.now().isoformat(timespec='seconds'),
    session_id='$SESSION_ID',
    components=components,
    files=['bot/s007_paper.py'],
    summary='...',
)
line = json.dumps(entry, ensure_ascii=False)
for c in components:
    open(f'.claude/change-log/{c}.jsonl', 'a').write(line + '\n')
"
```

## When to log

- After finishing one coherent change — a fix, a feature, a refactor — even
  if it spanned several files/components. One entry (duplicated across its
  resolved files), not one per Edit call.
- Skip: pure exploration/reads with no edits, changes fully reverted within
  the same turn.
- Log once you're confident the change is final for this turn — right before
  your closing summary to the user, not mid-edit.
