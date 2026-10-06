//+------------------------------------------------------------------+
//| Experts/AlgoTrading/S021_ORB.mq5                                 |
//| S021 -- Opening Range Breakout on Nasdaq 100 (ALGODEV-61).       |
//|                                                                  |
//| Thin shell: inputs + MT5 event wiring. All behaviour lives in    |
//| Include/Strategies/S021_ORB/Runtime.mqh, all strategy constants  |
//| in the generated Include/Strategies/S021_ORB/Params.mqh (from    |
//| strategies/orb_intraday/config.py::ORB_BASE -- never edit them   |
//| here). Inputs below are runtime / prop-firm concerns only.       |
//|                                                                  |
//| Attach to ONE chart of the Nasdaq 100 symbol (any timeframe).    |
//| Docs: mt5/README.md, Project doc                                 |
//| claude/prompt-s021-mt5-ea-implementation.md                      |
//+------------------------------------------------------------------+
#property copyright "AlgoTrading (Anton Malashenko)"
#property version   "1.00"
#property description "S021 ORB Nasdaq 100: two resting stops at O +/- 0.2*ADR14, SL 0.75*ADR14, time exit."

#include <Strategies/S021_ORB/Runtime.mqh>

input group "Risk"
input double                  InpRiskPct            = S021_DEFAULT_RISK_PCT; // Risk per trade, % of balance
input double                  InpDailyRiskCapPct    = 0.0;   // Strategy daily risk cap, % (0 = off)
input double                  InpMaxRealRiskWarnPct = 0.0;   // Warn if min-lot risk exceeds this % (0 = off; never blocks)

input group "Account guard (own limits, tighter than the firm's)"
input double                  InpInitialBalance     = 0.0;   // Initial balance (0 = first deposit in history)
input double                  InpDailyGuardPct      = 0.0;   // Daily loss guard, % of initial (0 = off)
input double                  InpMaxGuardPct        = 0.0;   // Max loss guard, % of initial (0 = off)
input ENUM_TZ_RULE            InpDayResetRule       = TZ_FIXED; // Firm's day boundary timezone
input int                     InpDayResetFixedHours = 3;     // ... fixed offset hours (FundingPips: UTC+3)

input group "Clock"
input ENUM_TZ_RULE            InpServerTzRule       = TZ_EET_US_DST; // Broker server timezone rule
input int                     InpServerFixedHours   = 0;     // ... fixed offset hours (TZ_FIXED only)
input bool                    InpVerifyServerOffset = true;  // Live: block entries if the rule disagrees with the terminal

input group "Exits"
input int                     InpExitBufferMin      = 0;     // Time exit N minutes before 15:59 New York
input string                  InpForceExitUtc       = "";    // Firm auto-close workaround, "HH:MM" UTC (empty = off)
input ENUM_DOUBLE_FILL_POLICY InpDoubleFillPolicy   = DOUBLE_FILL_CLOSE_BOTH; // If both stops fill

input group "Runtime"
input long                    InpMagic              = 21021; // Magic number
input int                     InpHistoryDays        = 45;    // M1 lookback for ADR14, calendar days
input bool                    InpLogToCommon        = true;  // Write logs to the shared Common/Files folder
input bool                    InpWriteDaysCsv       = true;  // Per-day CSV (parity input)
input int                     InpTimerSeconds       = 1;     // Safety-net reconcile period, s (raise only for long tester runs)

CS021Runtime g_runtime;

int OnInit()
  {
   S021Settings settings;
   settings.symbol=_Symbol;
   settings.magic=InpMagic;
   settings.strategy_name=StringFormat("%s-mt5-acct%I64d",S021_MAGIC_PREFIX,
                                       AccountInfoInteger(ACCOUNT_LOGIN));
   settings.risk_pct=InpRiskPct;
   settings.server_rule=InpServerTzRule;
   settings.server_fixed_hours=InpServerFixedHours;
   settings.verify_server_offset=InpVerifyServerOffset;
   settings.exit_buffer_min=InpExitBufferMin;
   settings.force_exit_utc_minute=ClockParseHhMm(InpForceExitUtc);
   settings.daily_risk_cap_pct=InpDailyRiskCapPct;
   settings.max_real_risk_pct=InpMaxRealRiskWarnPct;
   settings.double_fill_policy=InpDoubleFillPolicy;
   settings.history_calendar_days=InpHistoryDays;
   settings.log_to_common=InpLogToCommon;
   settings.write_days_csv=InpWriteDaysCsv;
   settings.limits.initial_balance=InpInitialBalance;
   settings.limits.daily_loss_pct=InpDailyGuardPct;
   settings.limits.max_loss_pct=InpMaxGuardPct;
   settings.limits.day_reset_rule=InpDayResetRule;
   settings.limits.day_reset_fixed_hours=InpDayResetFixedHours;

   if(InpRiskPct<=0.0 || InpHistoryDays<S021_ADR_WINDOW || InpTimerSeconds<1)
      return INIT_PARAMETERS_INCORRECT;
   if(StringLen(InpForceExitUtc)>0 && settings.force_exit_utc_minute<0)
      return INIT_PARAMETERS_INCORRECT;
   if(!g_runtime.Init(settings))
      return INIT_FAILED;
   EventSetTimer(InpTimerSeconds);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   g_runtime.Deinit(reason);
  }

void OnTick()
  {
   g_runtime.OnTickEvent();
  }

void OnTimer()
  {
   g_runtime.OnTimerTick();
  }

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   g_runtime.OnTradeTx(trans);
  }
//+------------------------------------------------------------------+
