//+------------------------------------------------------------------+
//| Experts/AlgoTrading/S004_FVG.mq5                                 |
//| S004-intraday -- H4 FVG bounce in the Asia session, 7 FX pairs   |
//| (ALGODEV-62).                                                    |
//|                                                                  |
//| Thin shell: inputs + MT5 event wiring. The decisions live in     |
//| Include/Strategies/S004_FVG/Engine.mqh (a bar-for-bar port of    |
//| strategies/fvg_mtf.py, proven by S004_SelfTest), the broker side |
//| in Include/Strategies/S004_FVG/Runtime.mqh, and every strategy   |
//| constant in the generated Params.mqh (from                       |
//| strategies/s004_config.py::S004_INTRADAY -- never edit them      |
//| here).                                                           |
//|                                                                  |
//| Attach to ONE chart, any symbol, any timeframe: the expert reads |
//| all S004_SYMBOLS itself and counts the daily cap across them.    |
//| A second copy on the same account+magic refuses to start.        |
//|                                                                  |
//| The two dials below are meant to be tuned (see                   |
//| backtest/run_s004_risk_grid.py), but their product is what the   |
//| account's survival is made of: a full stop costs exactly the     |
//| risk, so the worst planned day is cap x risk. The expert refuses |
//| a pair beyond S004_DAILY_RISK_BUDGET_PCT.                        |
//|                                                                  |
//| That product is also the ONLY risk control here: the account     |
//| guard ships OFF on purpose, see the input group below.           |
//| Docs: mt5/README.md                                              |
//+------------------------------------------------------------------+
#property copyright "AlgoTrading (Anton Malashenko)"
#property version   "1.00"
#property description "S004-intraday: H4 FVG bounce, Asia session, 7 FX pairs, flat by 22:45 server."

#include <Strategies/S004_FVG/Runtime.mqh>
#include <AlgoCore/InstanceLock.mqh>

input group "Sizing (tunable; cap x risk must stay within the daily budget)"
input double       InpRiskPct         = S004_DEFAULT_RISK_PCT;            // Risk per trade, % of balance
input int          InpMaxTradesPerDay = S004_DEFAULT_MAX_TRADES_PER_DAY;  // Max REAL entries per day, all pairs together
input double       InpEntryShiftPips  = S004_DEFAULT_ENTRY_SHIFT_PIPS;    // Entry limit offset from the zone edge toward the bounce, pips (0 = on the edge)

// OFF BY DEFAULT, AND THAT IS THE DECISION, not an oversight (2026-10-07,
// measured by backtest/run_s004_guard_modes.py).
//
// The DAILY limit needs no guard: cap x risk already is a hard planned worst
// day, and both legs on the account are sized so their sum clears the firm's.
//
// The OVERALL limit is the interesting one, and every guard for it loses money,
// because any rule that stops sizing near the limit is ABSORBING -- with no
// trades the equity cannot move, the room never reopens, and the account is
// finished without ever breaching anything. A breach is the opposite: it costs
// one entry fee and the challenge restarts. Over 3 years, $10k, FundingPips-like
// 8/5, median payout: no guard +23,859 at 6.98 breaches; stop-at-one-trade-left
// +827; shrink-the-size-to-fit +4,723 -- and the guarded accounts spend 70-90%
// of their days dead. Paying the fees is the cheap side of that trade.
//
// This flips if a breach ever stops being cheap: a higher entry fee, a limit on
// retries, or a funded account large enough that losing the status costs more
// than the restart. Then re-run the script before turning these back on.
input group "Account guard (off: see the note above before enabling)"
input double       InpInitialBalance     = 0.0;      // Initial balance (0 = first deposit in history)
input double       InpDailyGuardPct      = 0.0;      // Daily loss guard, % of initial (0 = off, deliberate)
input double       InpMaxGuardPct        = 0.0;      // Max loss guard, % of initial (0 = off, deliberate)
input ENUM_TZ_RULE InpDayResetRule       = TZ_FIXED; // Firm's day boundary timezone
input int          InpDayResetFixedHours = 3;        // ... fixed offset hours (FundingPips: UTC+3)

input group "Clock"
input ENUM_TZ_RULE InpServerTzRule     = S004_CLOCK_TZ_RULE; // Broker server timezone rule
input int          InpServerFixedHours = 0;                  // ... fixed offset hours (TZ_FIXED only)
input bool         InpVerifyServerOffset = true;             // Live: halt if the rule disagrees with the terminal

input group "Runtime"
input long         InpMagic        = 4004;  // Magic number
input int          InpWarmupBars   = 1000;  // Closed M15 bars replayed into each engine at start
input bool         InpTradeEnabled = true;  // false = shadow mode: engines and logs run, no orders
input bool         InpWriteTradesCsv = true; // Append every closed engine trade to <strategy>_trades.csv
input bool         InpLogToCommon  = true;  // Write logs to the shared Common/Files folder
input int          InpTimerSeconds = 5;     // Poll period, s

CS004Runtime g_runtime;
string       g_lock_name="";

int OnInit()
  {
   if(InpRiskPct<=0.0 || InpMaxTradesPerDay<1 || InpTimerSeconds<1)
      return INIT_PARAMETERS_INCORRECT;
   // The shift is backtested only between 0 and S004_MAX_ENTRY_SHIFT_PIPS
   // (strategies/s004_config.py::ENTRY_SHIFT_PIPS holds the evidence). Past
   // it the stop-to-entry distance changes the strategy, untested.
   if(InpEntryShiftPips<0.0 || InpEntryShiftPips>S004_MAX_ENTRY_SHIFT_PIPS)
     {
      PrintFormat("refusing to start: InpEntryShiftPips=%.2f is outside the tested range 0..%.2f pips.",
                  InpEntryShiftPips,S004_MAX_ENTRY_SHIFT_PIPS);
      return INIT_PARAMETERS_INCORRECT;
     }
   // The one input combination that can quietly kill the account: the firm's
   // daily limit is counted in money, not in trades, so a cap x risk beyond
   // S004's share of it means a normal losing day is a breach.
   if(InpMaxTradesPerDay*InpRiskPct>S004_DAILY_RISK_BUDGET_PCT)
     {
      PrintFormat("refusing to start: %d trades x %.2f%% plans a %.2f%% worst day, over S004's "
                  "%.2f%% budget. Lower the cap or the risk (backtest/run_s004_risk_grid.py "
                  "shows what each costs).",
                  InpMaxTradesPerDay,InpRiskPct,InpMaxTradesPerDay*InpRiskPct,
                  S004_DAILY_RISK_BUDGET_PCT);
      return INIT_PARAMETERS_INCORRECT;
     }

   S004Settings settings;
   settings.magic=InpMagic;
   settings.strategy_name=StringFormat("%s-mt5-acct%I64d",S004_MAGIC_PREFIX,
                                       AccountInfoInteger(ACCOUNT_LOGIN));
   settings.risk_pct=InpRiskPct;
   settings.max_trades_per_day=InpMaxTradesPerDay;
   settings.entry_shift_pips=InpEntryShiftPips;
   settings.server_rule=InpServerTzRule;
   settings.server_fixed_hours=InpServerFixedHours;
   settings.verify_server_offset=InpVerifyServerOffset;
   settings.warmup_bars=InpWarmupBars;
   settings.trade_enabled=InpTradeEnabled;
   settings.write_trades_csv=InpWriteTradesCsv;
   settings.log_to_common=InpLogToCommon;
   settings.limits.initial_balance=InpInitialBalance;
   settings.limits.daily_loss_pct=InpDailyGuardPct;
   settings.limits.max_loss_pct=InpMaxGuardPct;
   settings.limits.day_reset_rule=InpDayResetRule;
   settings.limits.day_reset_fixed_hours=InpDayResetFixedHours;

   // The lock is per account+magic, not per symbol: this expert owns all seven
   // pairs, so a second copy anywhere would double every entry.
   g_lock_name=InstanceLockName(S004_MAGIC_PREFIX,"portfolio",InpMagic);
   if(!InstanceLockAcquire(g_lock_name))
     {
      PrintFormat("[%s] refusing to start: another chart already runs this expert with magic "
                  "%I64d. Two copies place two sets of limits on the same zones -- remove the "
                  "duplicate, then re-attach.",settings.strategy_name,InpMagic);
      g_lock_name="";
      return INIT_FAILED;
     }
   if(!g_runtime.Init(settings))
     {
      PrintFormat("init failed: %s",g_runtime.HaltReason());
      return INIT_FAILED;
     }
   EventSetTimer(InpTimerSeconds);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(StringLen(g_lock_name)>0)
     {
      InstanceLockRelease(g_lock_name);
      g_lock_name="";
     }
   g_runtime.Deinit(reason);
  }

void OnTick()
  {
   g_runtime.Poll();
  }

void OnTimer()
  {
   g_runtime.Poll();
  }
//+------------------------------------------------------------------+
