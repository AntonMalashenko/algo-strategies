//+------------------------------------------------------------------+
//| Scripts/AlgoTrading/S021_PlaceToday.mq5                          |
//| Manual assist: show today's S021 numbers at the current moment,  |
//| and optionally place the pair of resting stops by hand.          |
//|                                                                  |
//| For the day the expert cannot take itself -- it was restarted    |
//| late, the orders were cancelled by hand, the terminal was down   |
//| over the open. The expert refuses to re-enter such a day on      |
//| purpose (Runtime.mqh case 6), and that refusal stays untouched:  |
//| this script is the explicit human override, run on demand.       |
//|                                                                  |
//| Everything is read from the same code the expert uses -- levels  |
//| from Levels.mqh, lot from Sizing.mqh, the already-touched test   |
//| from the same bars -- so the numbers cannot drift from the EA's  |
//| or from the backtest's.                                          |
//|                                                                  |
//| Orders are placed with the EXPERT'S magic and comment, so the    |
//| running expert adopts them on its next cycle and manages them    |
//| normally: OCO cancel of the sibling on a fill, the attached SL,  |
//| the cutoff cancel, and the time exit (Runtime.mqh cases 3-5).    |
//| Nothing here needs a second magic number, which would hide the   |
//| trade from the daily risk cap and the account guard.             |
//|                                                                  |
//| InpPlace is false by default: the script only prints.            |
//+------------------------------------------------------------------+
#property copyright "AlgoTrading (Anton Malashenko)"
#property version   "1.00"
#property script_show_inputs

#include <Strategies/S021_ORB/Levels.mqh>
#include <AlgoCore/GeneratedCore.mqh>
#include <AlgoCore/Sizing.mqh>
#include <AlgoCore/TradeOps.mqh>

input group "What to do"
input bool         InpPlace            = false; // Place the orders (false = show the numbers only)

input group "Must match the running expert"
input double       InpRiskPct          = S021_DEFAULT_RISK_PCT; // Risk per trade, % of balance
input long         InpMagic            = 21021; // Magic number
input ENUM_TZ_RULE InpServerTzRule     = TZ_EET_US_DST; // Broker server timezone rule
input int          InpServerFixedHours = 0;     // ... fixed offset hours (TZ_FIXED only)
input int          InpHistoryDays      = 45;    // M1 lookback for ADR14, calendar days

datetime ServerToStrategyClock(const datetime server_time)
  {
   return S021UtcToClock(ClockLocalToUtc(InpServerTzRule,InpServerFixedHours,server_time));
  }

datetime StrategyClockToServer(const datetime clock_time)
  {
   return ClockUtcToLocal(InpServerTzRule,InpServerFixedHours,S021ClockToUtc(clock_time));
  }

//--- The expert's own guard: a level touched since the open can no longer be
//--- reproduced by a resting stop, so the day must stay skipped.
bool LevelTouchedSinceOpen(const datetime day,const datetime now_server,
                           const S021Levels &levels,double &max_high,double &min_low)
  {
   datetime anchor_server=StrategyClockToServer(day+S021_SESSION_OPEN_MINUTE*CLOCK_SECONDS_PER_MINUTE);
   MqlRates rates[];
   int copied=CopyRates(_Symbol,PERIOD_M1,anchor_server,now_server,rates);
   max_high=0.0;
   min_low=0.0;
   for(int i=0;i<copied;i++)
     {
      max_high=(i==0) ? rates[i].high : MathMax(max_high,rates[i].high);
      min_low=(i==0) ? rates[i].low : MathMin(min_low,rates[i].low);
     }
   double ask=SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   double bid=SymbolInfoDouble(_Symbol,SYMBOL_BID);
   bool touched=(copied>0 && (max_high>=levels.upper || min_low<=levels.lower));
   return touched || ask>=levels.upper || bid<=levels.lower;
  }

bool ComputeTodayLevels(const datetime day,const datetime now_server,S021Levels &levels)
  {
   datetime anchor_server=StrategyClockToServer(day+S021_SESSION_OPEN_MINUTE*CLOCK_SECONDS_PER_MINUTE);
   int anchor_shift=iBarShift(_Symbol,PERIOD_M1,anchor_server,true);
   if(anchor_shift<0)
     {
      PrintFormat("no M1 bar at the %s anchor -- the engine rule is no trade on such a day",
                  ClockIsoDateTime(anchor_server));
      return false;
     }
   double open_price=iOpen(_Symbol,PERIOD_M1,anchor_shift);

   MqlRates rates[];
   datetime from_server=(datetime)((long)now_server-(long)InpHistoryDays*CLOCK_SECONDS_PER_DAY);
   int copied=CopyRates(_Symbol,PERIOD_M1,from_server,now_server,rates);
   if(copied<=0)
     {
      PrintFormat("no M1 history (error %d). Scroll the chart back (Home), then rerun",GetLastError());
      return false;
     }

   datetime clock_times[];
   double opens[];
   double highs[];
   double lows[];
   ArrayResize(clock_times,copied);
   ArrayResize(opens,copied);
   ArrayResize(highs,copied);
   ArrayResize(lows,copied);
   for(int i=0;i<copied;i++)
     {
      clock_times[i]=ServerToStrategyClock(rates[i].time);
      opens[i]=rates[i].open;
      highs[i]=rates[i].high;
      lows[i]=rates[i].low;
     }
   DailySession sessions[];
   SessionWindow window=S021Window();
   int session_count=SessionsBuild(clock_times,opens,highs,lows,copied,window,sessions);
   if(!S021ComputeLevels(sessions,session_count,day,open_price,levels))
     {
      PrintFormat("ADR14 is not computable: %d valid sessions of %d required, %d bars loaded",
                  levels.sessions_used,S021_ADR_WINDOW,copied);
      return false;
     }
   return true;
  }

void PlaceLeg(CTradeOps &ops,const bool is_long,const datetime day,const double lots,
              const S021Levels &levels)
  {
   double raw_price=is_long ? levels.upper : levels.lower;
   double raw_sl=is_long ? raw_price-levels.stop_distance : raw_price+levels.stop_distance;
   double price=SizingNormalizePrice(_Symbol,raw_price);
   double stop_loss=SizingNormalizePrice(_Symbol,raw_sl);
   TradeResult result;
   ops.PlaceStop(is_long,lots,price,stop_loss,S021OrderComment(day,is_long),result);
   PrintFormat("%s stop %.2f sl %.2f lot %.2f -> %s (order %I64u, %s)",
               is_long ? "BUY " : "SELL",price,stop_loss,lots,
               result.ok ? "OK" : "FAILED",result.ticket,result.message);
  }

void OnStart()
  {
   datetime now_server=TimeTradeServer();
   datetime now_clock=ServerToStrategyClock(now_server);
   datetime today=ClockDayStart(now_clock);
   int minute=ClockMinuteOfDay(now_clock);

   PrintFormat("--- S021 manual assist, %s --------------------------------",_Symbol);
   PrintFormat("now: %s server / %s New York (strategy clock)",
               ClockIsoDateTime(now_server),ClockIsoDateTime(now_clock));

   if(minute<S021_SESSION_OPEN_MINUTE || minute>S021_ENTRY_CUTOFF_MINUTE)
     {
      PrintFormat("outside the entry window (%02d:%02d-%02d:%02d New York) -- nothing to place",
                  S021_SESSION_OPEN_MINUTE/60,S021_SESSION_OPEN_MINUTE%60,
                  S021_ENTRY_CUTOFF_MINUTE/60,S021_ENTRY_CUTOFF_MINUTE%60);
      return;
     }

   OwnPosition positions[];
   int position_count=TradeOpsCollectPositions(_Symbol,InpMagic,positions);
   OwnOrder orders[];
   int order_count=TradeOpsCollectStopOrders(_Symbol,InpMagic,orders);
   if(position_count>0 || order_count>0)
     {
      PrintFormat("the expert already holds %d position(s) and %d order(s) with magic %I64d "
                  "-- it is on this day, nothing to do here",position_count,order_count,InpMagic);
      return;
     }

   S021Levels levels;
   if(!ComputeTodayLevels(today,now_server,levels))
      return;

   PrintFormat("levels: O %.2f  ADR14 %.3f  U %.2f  L %.2f  stop %.3f  (%d sessions)",
               levels.open_price,levels.adr,levels.upper,levels.lower,
               levels.stop_distance,levels.sessions_used);

   double balance=AccountInfoDouble(ACCOUNT_BALANCE);
   double risk_amount=balance*InpRiskPct/ALGO_PCT;
   double money_per_point=SizingMoneyPerPointPerLot(_Symbol);
   double volume_min=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN);
   double raw_lots=SizingLotsForRisk(risk_amount,levels.stop_distance,money_per_point,volume_min);
   double lots=SizingNormalizeVolumeForSymbol(_Symbol,raw_lots);
   double real_risk=lots*levels.stop_distance*money_per_point;
   PrintFormat("size: balance %.2f  risk %.2f%% = %.2f  lot %.4f -> %.2f  real risk %.2f (%.3f%%)",
               balance,InpRiskPct,risk_amount,raw_lots,lots,real_risk,
               (balance>0.0) ? real_risk/balance*ALGO_PCT : 0.0);

   double max_high=0.0;
   double min_low=0.0;
   if(LevelTouchedSinceOpen(today,now_server,levels,max_high,min_low))
     {
      PrintFormat("REFUSING: a level was already touched since the open "
                  "(high %.2f / low %.2f vs U %.2f / L %.2f). A resting stop can no longer "
                  "reproduce that entry -- the day stays skipped, same as the expert decides",
                  max_high,min_low,levels.upper,levels.lower);
      return;
     }

   if(!InpPlace)
     {
      Print("dry run: nothing placed. Rerun with InpPlace=true to place these two orders");
      return;
     }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
     {
      Print("trading is not allowed for this script -- enable algo trading and rerun");
      return;
     }

   CTradeOps ops;
   ops.Init(_Symbol,InpMagic);
   PlaceLeg(ops,true,today,lots,levels);
   PlaceLeg(ops,false,today,lots,levels);
   Print("placed with the expert's magic and comment -- it adopts them on its next cycle "
         "(OCO cancel, stop loss, cutoff cancel and the time exit are its job from now on)");
  }
//+------------------------------------------------------------------+
