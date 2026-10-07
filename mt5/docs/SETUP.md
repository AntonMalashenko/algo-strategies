# MT5 expert advisors: installation, setup and daily use

How to install the MQL5 code from `mt5/` into a MetaTrader 5 terminal on macOS,
Windows or Linux. It also covers the S021 tests (self-test, offline tester run,
parity check) and how to set the EA up on a prop account. Layout and design
rules: [`mt5/README.md`](../README.md). Ticket: ALGODEV-61.

---

## TL;DR: what you run day to day

| When | macOS / Linux | Windows (PowerShell) |
|---|---|---|
| after `git pull` or any code change | `mt5/tools/deploy.sh` | `powershell -ExecutionPolicy Bypass -File mt5\tools\deploy.ps1` |
| the same, with another strategy live on that terminal | `mt5/tools/deploy.sh S004` | `... deploy.ps1 S004` |
| while developing (auto-deploy on save) | `mt5/tools/deploy.sh --watch` | `... deploy.ps1 -Watch` |

Named programs (`S004`, `s004_fvg`, or a full path under `MQL5`) narrow steps 3
and 5 to that subset. This matters because step 5 overwrites the `.ex5` a
running expert was loaded from: deploying everything onto a terminal that is
trading would swap the binary under the live EA. Steps 1-4 are safe regardless
-- the sources are mirrored with the existing `.ex5` files kept.

`deploy` does everything that does not need a mouse:

1. regenerates the strategy constants from Python (`gen_params`);
2. runs `tests/mt5`;
3. regenerates the self-test fixtures and the tester history when they are missing **or
   stale** (older than `s021_fixtures.py`, `clock.py`, or the strategy's `config.py` /
   `engine.py` -- the expectations bake in the session clock and the strategy params, so a
   stale fixture keeps asserting the previous behaviour);
4. copies sources, presets and fixtures into the terminal;
5. compiles every EA and script with MetaEditor, and stops with a non-zero exit code on any compile error.

Afterwards, in MT5: **Navigator → right click → Refresh**.

Things you do **once**, not on every update:

- installing the terminal;
- creating the offline symbol;
- tester settings (the terminal remembers them);
- attaching the EA to a chart.

> After `deploy` overwrites the `.ex5` from outside, MT5 keeps running the already-loaded
> copy — Navigator → Refresh updates the list, not the attached expert. To pick up a new
> build on a live chart, drag the EA onto it again (or remove and re-attach). The new
> instance logs a fresh `init`; if there is none, the old build is still running.

| Change | What to repeat |
|---|---|
| Any `.mq5` / `.mqh` / preset / Python config change | `deploy` (or let `--watch` do it), then Refresh in MT5 |
| `strategies/orb_intraday/config.py` (`ORB_BASE`) changed | same. `gen_params` refuses to export a config with a modifier enabled, see `mt5/README.md` |
| New histdata / you want a longer tester window | regenerate the history (section 6.1), rerun `ImportM1CustomSymbol` |
| New terminal, new machine | the whole OS section below |

---

## 1. What gets installed where

`deploy` copies only our own folders; the terminal's stock files are never touched.

| Repo | Terminal (`<MQL5>` = data folder `MQL5`, MT5 → File → Open Data Folder) |
|---|---|
| `mt5/MQL5/Include/AlgoCore` | `<MQL5>/Include/AlgoCore` |
| `mt5/MQL5/Include/Strategies` | `<MQL5>/Include/Strategies` |
| `mt5/MQL5/Experts/AlgoTrading` | `<MQL5>/Experts/AlgoTrading` |
| `mt5/MQL5/Scripts/AlgoTrading` | `<MQL5>/Scripts/AlgoTrading` |
| `mt5/MQL5/Presets/AlgoTrading/*.set` | `<MQL5>/Presets` and `<MQL5>/Profiles/Tester` |
| `mt5/MQL5/Files/AlgoTrading/{fixtures,e2e}` (generated, git-ignored) | `<Common>/Files/AlgoTrading/{fixtures,e2e}` |

The EA writes its logs to `<Common>/Files/AlgoTrading/logs/S021-mt5-acct<login>/`:

- `events-YYYY-MM-DD.jsonl`
- `positions/<label>.jsonl`
- `heartbeat.json`
- `S021-mt5-acct<login>_days.csv`

`<Common>` per OS:

| OS | `<Common>/Files` |
|---|---|
| macOS | `~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files` |
| Windows | `%APPDATA%\MetaQuotes\Terminal\Common\Files` |
| Linux | `~/.mt5/drive_c/users/<you>/AppData/Roaming/MetaQuotes/Terminal/Common/Files` |

Copy mode is the default because the macOS (Wine) build of MT5 does not list Expert Advisors that sit behind a symlink. Scripts behind a symlink do show up, EAs do not, neither in the Navigator nor in the Strategy Tester. `install_mac.sh --link` is still available for terminals that follow symlinks.

---

## 2. macOS (Apple Silicon or Intel)

### 2.1 One-time setup

1. **Install MetaTrader 5.** Download the macOS build from metatrader5.com (Download → macOS) and install it. Launch it once so it creates its Wine prefix.
2. **Log in to any account.** The Strategy Tester refuses to start without one. For tests only, a demo is enough: File → Open an Account → MetaQuotes-Demo.
3. **Repo + Python** (once per machine):
   ```bash
   cd ~/Trading/algo
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Deploy:**
   ```bash
   mt5/tools/deploy.sh
   ```
   - **Expected:** steps 1/5 … 5/5. Step 5 should print `ok … 0 errors` for each `.mq5`.
   - **If step 5 says it cannot compile from the command line:** the bundled Wine binary was not found. Either point `MT5_WINE` at it (`find "/Applications/MetaTrader 5.app" -name 'wine*' -type f`), or open MetaEditor (F4) and press **F7** on each file in `Experts/AlgoTrading` and `Scripts/AlgoTrading`.
5. **Refresh MT5.** Navigator → right click → **Refresh**. Expect **Expert Advisors → AlgoTrading → S021_ORB** and **Scripts → AlgoTrading → ExportM1, ImportM1CustomSymbol, S021_SelfTest**.
6. **Self-test** (section 5).

### 2.2 macOS quirks

- **The Mac Terminal app** is Applications → Utilities → Terminal (or Cmd+Space → "Terminal"). The VS Code terminal works too.
- **The EA is missing from the Navigator or the tester list** (you deployed with `--link` earlier): run `mt5/tools/deploy.sh` (copy mode), then quit MT5 completely with **Cmd+Q** and start it again.
- **The "Select expert…" drop-down on the tester's *Parameters* tab** loads saved parameter sets. It is *not* the EA selector. The EA is chosen on the **Settings** tab.
- **Keep the Mac awake while trading.** System Settings → Battery/Energy → *Prevent automatic sleeping when the display is off*, or run `caffeinate -dimsu &`. Add MetaTrader 5 to **Login Items**. Prop firms forbid VPS/VPN, so the terminal runs on this Mac.

---

## 3. Windows

### 3.1 One-time setup

1. **Install MetaTrader 5** from metatrader5.com (or the prop firm's own MT5 build) and launch it once. Log in to any account (see 2.2 above for why).
2. **Install Python 3.11+** from python.org, with "Add to PATH" checked, then:
   ```powershell
   cd $HOME\Trading\algo
   python -m venv .venv; .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. **Deploy:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File mt5\tools\deploy.ps1
   ```
   - **Data folder:** taken from the most recently used `%APPDATA%\MetaQuotes\Terminal\<hash>`. With several terminals installed, pass it explicitly: `-DataDir "<File → Open Data Folder path>"`.
   - **MetaEditor64.exe:** found through `<DataDir>\origin.txt`. For a portable install, the data folder *is* the install folder.
4. **Refresh and self-test.** Navigator → Refresh, then the self-test (section 5).

### 3.2 Windows notes

- Symlinks are not needed. If you want edit-in-repo without redeploying, create directory junctions (`mklink /J`) instead of copies. Junctions are left out of `deploy.ps1` on purpose: the copy is simpler and always works.
- **Keep the PC from sleeping** (Settings → Power), and put MT5 in **Startup** (`shell:startup`).

---

## 4. Linux (Wine)

### 4.1 One-time setup

1. **Install MT5 with MetaQuotes' official Linux installer**, which sets up Wine and the terminal in the prefix `~/.mt5`. Run the installer script from metatrader5.com (Download → Linux) and follow its prompts. Start the terminal once and log in to an account.
2. **Repo + Python:** same as macOS step 3.
3. **Deploy:**
   ```bash
   MT5_WINEPREFIX=~/.mt5 mt5/tools/deploy.sh
   ```
   - `MT5_WINE` defaults to `wine64` or `wine` on `PATH`.
   - **On a headless box** (no X server) MetaEditor needs a virtual display. Create a wrapper once:
     ```bash
     printf '#!/bin/bash\nexec xvfb-run -a wine "$@"\n' > ~/bin/xwine && chmod +x ~/bin/xwine
     MT5_WINE=~/bin/xwine MT5_WINEPREFIX=~/.mt5 mt5/tools/deploy.sh
     ```
     This is how `deploy.sh` was verified headless: MetaEditor build 6235 compiled all four programs with `0 errors, 0 warnings`, and the same Wine terminal ran S021_SelfTest: 9828 passed.
   - **If autodetection of the data folder fails,** pass `--mql5 "$HOME/.mt5/drive_c/Program Files/MetaTrader 5/MQL5" --common "$HOME/.mt5/drive_c/users/$USER/AppData/Roaming/MetaQuotes/Terminal/Common/Files"`.

---

## 5. Self-test (any OS, ~1 minute)

1. Open any chart. Drag **Navigator → Scripts → AlgoTrading → S021_SelfTest** onto it and press OK.
2. Toolbox (Ctrl/Cmd+T) → **Experts** tab:
   ```
   levels fixture: 87 days compared, 72 with levels
   S021 self-test: 12172 passed, 0 failed -- OK
   ```
   The result is also written to `<Common>/Files/AlgoTrading/selftest/S021_selftest.json`.

**What it checks:**

- server-time rules against real timezones (6968 instants, including the strategy's own
  `EST_US_DST` New York clock);
- O / ADR14 / U / L on 72 days of real history against the Python engine (including both 2025 DST ends and the Thanksgiving half day);
- sizing and the account guard against the pytest cases.

Run it after any change to `AlgoCore` or `Strategies/S021_ORB`. If it fails on roughly the
DST half of the fixture window and passes on the other half, suspect stale fixtures before
suspecting the code: re-run `deploy.sh`, which now regenerates them on staleness.

---

## 6. Offline backtest in the Strategy Tester + parity with Python

This runs the EA on **our own** Nasdaq history (histdata, the same bars the Python backtest uses), so EA and engine can be compared day by day. It needs no broker data.

### 6.1 History → custom symbol (once, or when you want a new window)

1. **History.** `deploy` generates it if it is missing (2025-01-02 … 2026-09-30). For a different window:
   ```bash
   python -m mt5.tools.s021_fixtures --start 2024-01-02 --end 2026-09-30 \
       --out mt5/MQL5/Files/AlgoTrading/e2e && mt5/tools/deploy.sh --skip-tests
   ```
2. **Custom symbol.** Drag **Scripts → AlgoTrading → ImportM1CustomSymbol** onto any chart and set its inputs:
   - `InpCsvPath` = `AlgoTrading/e2e/s021_m1.csv` (not the default `fixtures/…`, which is the short self-test file);
   - `InpSymbol` = `NSXUSD_HD`.

   Expect `ImportM1: 587932 bars imported into custom symbol NSXUSD_HD`. A preceding `some symbol properties were rejected, error 5307/5308` is harmless.

3. **Drop the tester's own copy of the history.** The Strategy Tester does not read
   the custom symbol directly: on the first run it snapshots it into
   `<terminal>/Tester/bases/<server>/history/<symbol>/*.hcs` and keeps reusing that
   snapshot. After *any* re-import the snapshot is stale, so delete it (and the run
   cache) before testing again:

   ```bash
   # macOS; Windows: <terminal data folder>\Tester\{bases,cache}
   T=~/Library/Application\ Support/net.metaquotes.wine.metatrader5/drive_c/Program\ Files/MetaTrader\ 5
   rm -rf "$T/Tester/bases" "$T/Tester/cache"
   ```

   Skipping this is what made the 2026-10-05 parity run fail on 304 days: the symbol
   already held the correct bars, but the tester replayed a snapshot taken before the
   `tick_volume` fix. **Sanity check in the tester log: `ticks` must be ≈ 4 × `bars`.**

### 6.2 Tester settings (remembered by the terminal after the first run)

Open the tester with **View → Strategy Tester** (Ctrl/Cmd+R), or right-click S021_ORB in the Navigator → **Test**.

**Settings** tab:

| Field | Value |
|---|---|
| Expert | `AlgoTrading\S021_ORB.ex5` |
| Symbol / period | `NSXUSD_HD` / `M1` |
| Date | custom, `2025.03.01` – `2026.09.30` (≥ 45 days after the history start, for ADR14) |
| Forward | No |
| Delays | Zero latency, ideal execution |
| Modelling | **1 minute OHLC** (the custom symbol has no real ticks; "history quality 0%" is expected) |
| Deposit | 10000 USD, any leverage |
| Optimization | Disabled |

**Inputs** tab: right click → **Load** → `S021_tester_offline.set`, then **Start**. The preset sets:

- `ServerTzRule = EET_US_DST`, because the history was stamped with that rule;
- `VerifyServerOffset = false`;
- `TimerSeconds = 60`, which is much faster in the tester;
- `RiskPct = 0.5`.

Reference run (2026-10-05, macOS, 0.5% risk, 2025.03.01–2026.09.30): 2132464 ticks /
533116 bars, 365 trades, net +$1444.23, profit factor 1.30, win rate 58.4%, max balance
drawdown 3.11%.

#### Running the import and the tester headless (macOS/Linux)

Everything above can also be driven from the shell, which is what CI-style reruns use.
The terminal must be closed first (one instance locks the data folder), and the wine user
has to be `user`, otherwise `FILE_COMMON` resolves to an empty
`drive_c/users/$USER/...` tree and the import fails with `error 5004`:

```bash
P=~/Library/Application\ Support/net.metaquotes.wine.metatrader5
W="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64"

printf '[StartUp]\nScript=AlgoTrading\\ImportM1CustomSymbol\nSymbol=EURUSD\nPeriod=M1\nShutdownTerminal=1\n' \
    > "$P/drive_c/algotrading_import.ini"

cd "$P/drive_c/Program Files/MetaTrader 5"
USER=user USERNAME=user WINEPREFIX="$P" WINEDEBUG=-all "$W" terminal64.exe '/config:C:\algotrading_import.ini'
```

The tester is the same call with a `[Tester]` config (`Expert=AlgoTrading\S021_ORB`,
`ExpertParameters=S021_tester_offline.set`, `Symbol=NSXUSD_HD`, `Period=M1`, `Model=1`,
`FromDate`/`ToDate`, `Deposit=10000`, `Optimization=0`, `ShutdownTerminal=1`). Do not pass
`/portable`: the macOS app does not, and adding it changes the data folder. Results land in
`<terminal>/Tester/logs/` and in the EA's own `_days.csv`.

### 6.3 Parity check

Copy the EA's per-day file into the repo (`data/raw` is git-ignored) and run the comparison:

```bash
# macOS (Linux: replace the prefix with ~/.mt5/drive_c/users/$USER/...)
mkdir -p data/raw/mt5
cp ~/Library/Application\ Support/net.metaquotes.wine.metatrader5/drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/Common/Files/AlgoTrading/logs/S021-mt5-acct*/S021-mt5-acct*_days.csv data/raw/mt5/
python -m mt5.tools.s021_parity \
  --bars mt5/MQL5/Files/AlgoTrading/e2e/s021_m1.csv \
  --ea-days data/raw/mt5/S021-mt5-acct<login>_days.csv \
  --rule EET_US_DST --out reports/s021_mt5_parity.csv
```

On Windows the source folder is `%APPDATA%\MetaQuotes\Terminal\Common\Files\AlgoTrading\logs\`.

Exit code 0 means parity holds. Levels must match to 1e-6; entries must match day by day. Known, explained categories are not failures:

| Category | Why |
|---|---|
| `engine_skip_both_in_bar` | both levels inside one M1 bar: the engine skips the day, the tester's ticks decide |
| `live_only_short_session` | half days: the engine needs ≥ 350 session bars, live cannot know that in advance |
| `ea_missed_entry` | a level was touched before the EA could place its stops |
| `ea_no_levels` | no session to anchor on: weekends/holidays, the ADR14 warm-up, histdata gaps |
| `both_no_trade` | neither side triggered |

Result on 2026-10-05 (histdata 2025.03.01–2026.09.30, 574 EA days): **exit code 0** —
326 `match`, 198 `ea_no_levels`, 39 `live_only_short_session`, 11 `both_no_trade`, no
failures. On the 326 traded days entry price, direction, entry minute and `exit_reason`
are identical. Mean R per trade: EA +0.0834 vs engine gross +0.0793 (engine net +0.0685,
i.e. the engine's modelled spread/commission, which the custom symbol does not have). The
remaining +0.004 R comes only from intrabar timing of the time exit: the EA closes on the
first tick of the exit minute (= the bar open), the engine on that bar's close.

---

## 7. Live / prop account

1. **Log in to the account** (File → Login to Trade Account). Put the Nasdaq 100 symbol in Market Watch (`US100`, `USTEC`, `NAS100`, …) and open its chart (any timeframe, M1 recommended).
2. **Attach the EA.** Drag **S021_ORB** onto that chart.
   - **Common** tab: tick **Allow Algo Trading**.
   - **Inputs** tab: **Load** the preset for the firm, then set `InpRiskPct` to the risk chosen for this account.

   | Preset | Use for |
   |---|---|
   | `S021_fundingpips_10k_eval.set` | FundingPips 2-Step $10K, evaluation phases |
   | `S021_fundingpips_10k_master_summer.set` | FundingPips Master, US summer time (Mar–Nov): `ForceExitUtc = 20:44`, because the firm auto-closes at 20:45–21:00 UTC. Use the eval preset in winter. |
   | `S021_ftmo_10k.set` | FTMO $10K (day reset = Prague midnight) |

   All presets use a guard of 4.5% daily / 9% max, tighter than the firms' 5% / 10%. The guard blocks *new* entries only, it never force-closes positions.
3. **Turn on Algo Trading** with the toolbar button (it must be green). The EA icon in the chart corner must not be grey.
4. **Check the `init` event** in the Experts tab (or in `events-*.jsonl`):

   | Field | Expected |
   |---|---|
   | `server_offset_expected_s` == `server_offset_observed_s` | equal. Otherwise the EA logs `halted` and refuses new entries: fix `InpServerTzRule`. |
   | `margin_mode` | `hedging` |
   | `volume_min`, `volume_step`, `money_per_point_per_lot`, `contract_size` | the broker's real spec; keep it for the records |

5. **Daily timeline.** S021 is anchored on the **New York** clock, so the UTC readings
   move with US DST (which starts and ends on different dates than EU DST):

   | Event | New York | UTC, US DST (Mar–Nov) | UTC, standard time |
   |---|---|---|---|
   | levels + two stop orders | 09:30 | 13:30 | 14:30 |
   | unfilled orders cancelled | 14:30 | 18:30 | 19:30 |
   | time exit | 15:59 | 19:59 | 20:59 |

   In Kyiv the first event normally reads **16:30** in both seasons, because Ukraine and
   the US change clocks in the same direction. It reads 15:30 during the two weeks a year
   when the two DST calendars disagree (roughly 8–29 March and 25 October – 1 November).
   The broker server clock (`EET_US_DST`) is a constant 7 hours ahead of New York all year,
   so on the chart the levels always appear at 16:30 server time.

   Look for `levels`, `size`, two `place_stop`, then on a fill `fill` and `cancel_sibling` with `cancel_latency_ms`.
6. **Keep the machine running:** see the OS sections. `heartbeat.json` is refreshed every 60 s and can be monitored.
7. **Several accounts** need one terminal instance per account: a terminal can only be logged in to one account. That is not covered by these scripts yet.
8. **One chart only.** A second copy of the EA on the same account+symbol+magic refuses to start (`AlgoCore/InstanceLock.mqh`). Without that lock two copies each place their own pair on the same levels, so a breakout fills the combined lot, their OCO cancels act on each other's orders, and they overwrite each other's JSONL records — all three were observed live on 2026-10-06.

---

## 7.1 Taking a day the EA refuses (manual assist)

The EA will not re-enter a day whose orders already came and went — it was restarted
late, the orders were cancelled by hand, the terminal was down over the open
(`Runtime.mqh` case 6, logged as `day_resolved`). That refusal is deliberate and is not
being relaxed. `Scripts/AlgoTrading/S021_PlaceToday.mq5` is the explicit human override.

Run it from the Navigator on the **chart of the traded symbol**, with `InpRiskPct`,
`InpMagic`, `InpServerTzRule` and `InpHistoryDays` matching the running EA. It prints to
the Experts tab:

```
now: 2026.10.06 16:57:12 server / 2026.10.06 09:57:12 New York (strategy clock)
levels: O 31264.50  ADR14 334.279  U 31331.36  L 31197.64  stop 250.709  (30 sessions)
size: balance 10000.00  risk 1.00% = 100.00  lot 0.3988 -> 0.40  real risk 100.28 (1.003%)
dry run: nothing placed. Rerun with InpPlace=true to place these two orders
```

`InpPlace` is `false` by default, so the first run only shows the numbers. The script
refuses on its own when it is outside the 09:30–14:29 New York entry window, when the EA
already holds orders or positions for the day, when ADR14 is not computable, and — the
important one — when a level was already touched since the open, which is the same test
the EA applies before entering (`Runtime.mqh:363`).

Orders go out with the **EA's magic and comment**, so the running EA adopts them on its
next cycle and manages them exactly like its own: OCO cancel of the sibling on a fill, the
attached stop loss, the cutoff cancel, the time exit. Do not work around the refusal with
a second magic number instead — a second magic hides the trade from the daily risk cap and
the account guard, which count per magic.

---

## 7.2 Trading the same day by hand on another account

A second account (another broker, a prop platform, a web terminal) can follow S021
without running the EA there. Quotes do **not** need to be synchronised, and matching
them against a reference feed is in fact the one way to get this wrong.

The reason is the shape of the rule: only the session open `O` is a broker-specific
price. The bands are `O ± k_range*ADR14` and the stop is `stop_adr_mult*ADR14` — pure
distances off ADR14, the mean daily high-low of the same underlying index. Two brokers
can quote the index hundreds of points apart (dividend adjustments, financing) and still
agree on its daily range to within a fraction of a percent. So the open is read on the
platform you actually trade, and ADR14 is carried over from the EA.

`python -m mt5.tools.s021_manual --open <your 09:30 New York open>` does the arithmetic.
It reads ADR14 from the newest `levels` event in the EA's log, which the EA writes at
09:30:10 New York every session, so the number is current by construction; it says so
loudly when the newest event is not today's. `--adr <value>` covers the EA being down.

```
S021 manual levels -- 2026-10-07 New York
ADR14 334.279   (EA log, session 2026-10-06)

  your open  O       31264.50
  buy stop   U       31331.36   stop loss 31080.65
  sell stop  L       31197.64   stop loss 31448.35
  stop distance        250.71
```

Position size is deliberately not printed: contract units and value per point differ per
platform, and a wrong size is a worse failure than no size.

### Fully standalone, on the broker's own feed

`s021_manual.py` still needs MT5 running somewhere for ADR14, and the open typed in.
`python -m tools.s021_capital` removes both: started any time before the open, it
works out when the New York open is, waits for it, pulls the history and the open from
Capital.com's REST API and prints the same block. Credentials live in `.env`
(`CAPITAL_API_KEY`, `CAPITAL_IDENTIFIER`, `CAPITAL_PASSWORD`, `CAPITAL_DEMO`) — see
`.env.example`. `--search nasdaq` lists the epics if `US100` is not the right one.

`CAPITAL_DEMO=1` selects the demo endpoint, but an API key only authenticates against an
environment where the account actually exists: a key generated on the live platform
answers `HTTP 401 {"errorCode":"error.null.accountId"}` on the demo host. Set
`CAPITAL_DEMO=0` in that case — the tool issues no trading calls either way.

Measured against the IC Markets USTEC export over 12 sessions (2026-09..10), Capital.com's
`US100` is effectively the same instrument: opens differ by −1.3 points on average (worst
8.9), session ranges by 3.6 points on ~322. ADR14 for 2026-10-06 came out 337.1 here
against the EA's 334.3, a 0.9% gap — well inside the noise that matters for a 0.20×ADR14
band.

The open **must** come from the feed the orders will sit on, which is why this tool
talks to the broker rather than to a free index feed. Measured over 493 sessions
(2024-10..2026-10, `^NDX` against the IC Markets USTEC export): the index open sits
between −34 and +41 points of the broker's open at the 5–95% range, versus a band
half-width of ~67 points, because the cash index is still stale at 09:30:00 while its
constituents open one by one. ADR14 does carry across feeds — the index understates the
CFD's daily range by a stable 2.2% (σ 1.0%) — but the open does not.

### To the phone

`--telegram` (on either tool) also pushes the plan to a Telegram bot, with every order
price in its own tap-to-copy monospace span. One-time setup: create the bot with
@BotFather, put its token in `.env` as `TELEGRAM_BOT_TOKEN`, say Start to the bot, then

```bash
python -m utils.telegram      # prints the TELEGRAM_CHAT_ID line, then sends a test message
```

Without those two variables the tools print a one-line notice and carry on, so Telegram
stays optional. The sender is `utils/telegram.py`, shared and not S021-specific — one bot
is enough for every strategy, since each message names its own.

What the EA does for free and a human has to do here: **cancel the opposite stop order
the moment one fills** (there is no OCO across two independent orders, and both filling
means double risk in opposite directions), cancel both if neither filled by the 14:29 New
York cutoff, and close by the clock at 15:59 New York — no retail platform closes on a
schedule.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Invalid account` on login | wrong password or an expired demo. Reset the password in the broker's cabinet, or File → Open an Account to create a fresh demo from inside MT5 |
| Strategy Tester: "tester not started because the account is not specified" | log in to any account (a demo is fine) |
| EA not in the Navigator / tester list | deployed with `--link` on macOS. Redeploy in copy mode, then Cmd+Q and restart MT5 |
| `deploy` step 5: "no compiler log" | MetaEditor could not be started through Wine. Set `MT5_WINE` / `MT5_WINEPREFIX`, or compile with F7 |
| Compile `FAIL` with `cannot open file <AlgoCore/...>` | `Include/AlgoCore` or `Include/Strategies` missing in the terminal. Rerun `deploy` |
| `ImportM1: 114365 bars` instead of 587932 | `InpCsvPath` left at the default `fixtures/…`. Rerun with `AlgoTrading/e2e/s021_m1.csv` |
| `ImportM1: cannot open <Common>/Files/… (error 5004)` | only when the terminal is started from the shell: wine mapped a different user, so `FILE_COMMON` points at an empty tree. Run it with `USER=user USERNAME=user` |
| Tester report: `ticks` == `bars`, EA sees O=H=L=C bars | the tester is replaying a stale snapshot of the custom symbol (or a pre-`tick_volume=4` import). Delete `<terminal>/Tester/bases` and `<terminal>/Tester/cache`, then rerun — see §6.1 |
| Parity fails only outside the window you last imported | same stale tester snapshot: the snapshot is refreshed per history chunk, so a partial re-import "fixes" only its own date range |
| Self-test fails on roughly half the days, passing on the rest | stale fixtures: the expectations on disk were generated under a different session clock or different strategy params, and only the days affected by the change fail. Rerun `deploy.sh` (step 3 regenerates on staleness), do not "fix" the code |
| Self-test suddenly reports more checks than the docs say | expected after a new `ENUM_TZ_RULE` is added: the clock fixture gains ~1419 cross-checks per rule |
| `halted` event at start | the server timezone rule disagrees with the terminal clock. Check the broker's server time and set `InpServerTzRule` |
| `missed_entry` warning | a level was already touched before the EA could place the stops (late start or a gap). The day is skipped by design: no market-order substitute |
| Expert log: `refusing to start: another chart already runs this expert` | the single-instance lock did its job — the EA is attached to a second chart on the same account+symbol+magic. Remove the duplicate, then re-attach. Two copies place two independent order pairs on the same levels, multiplying the risk, and overwrite each other's JSONL records |
| `insufficient_adr_history` | fewer than 14 valid sessions in the M1 history. Scroll the chart back (Home key), or set Tools → Options → Charts → Max bars = Unlimited |
| `oco_double_fill` error | both stops filled before the sibling was cancelled. Both legs are closed (`DoubleFillPolicy`), see ALGODEV-60 |
