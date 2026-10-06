//+------------------------------------------------------------------+
//| Strategies/S021_ORB/Runtime.mqh                                  |
//| S021 live/tester runtime: one idempotent Reconcile() that reads  |
//| the broker state fresh every time and acts on it -- the MQL5     |
//| counterpart of bot/orb_signals.py::decide() (cases 1-7), driven  |
//| by events instead of a once-a-minute cron:                       |
//|                                                                  |
//|   OnTradeTransaction (a fill)  -> Reconcile immediately, so the  |
//|                                   sibling stop is cancelled in   |
//|                                   milliseconds (ALGODEV-60)      |
//|   OnTimer (1 s) / OnTick       -> Reconcile (entry, cutoff,      |
//|                                   time exit, safety net)         |
//|                                                                  |
//| State is rebuilt from positions / orders / history on every call |
//| (magic + symbol + the day encoded in order comments and open     |
//| times), so a terminal restart mid-day neither re-enters nor      |
//| loses the time exit. In-memory DayState only de-duplicates logs  |
//| and caches the day's levels.                                     |
//|                                                                  |
//| Strategy rules are NOT configurable here: they come from the     |
//| generated Params.mqh (= strategies/orb_intraday/config.py ORB_BASE).|
//| Inputs are runtime/firm concerns only (risk %, guards, clocks).  |
//+------------------------------------------------------------------+
#ifndef S021_RUNTIME_MQH
#define S021_RUNTIME_MQH

#include "Params.mqh"
#include "Levels.mqh"
#include <AlgoCore/Clock.mqh>
#include <AlgoCore/JsonLog.mqh>
#include <AlgoCore/Sessions.mqh>
#include <AlgoCore/Sizing.mqh>
#include <AlgoCore/AccountGuard.mqh>
#include <AlgoCore/TradeOps.mqh>

#define S021_LOG_ROOT                "AlgoTrading/logs"
#define S021_DAYS_CSV_SUFFIX         "_days.csv"
#define S021_DAYS_CSV_HEADER_KEY     "day"          // first column; also the header line's prefix
#define S021_HEARTBEAT_FILE          "heartbeat.json"
#define S021_HEARTBEAT_SECONDS       60
#define S021_LEVELS_RETRY_SECONDS    10   // history not synced yet -> retry this often
#define S021_MAX_REASON_SLOTS        8    // pending close reasons tracked at once
#define S021_USEC_PER_MSEC           1000.0
#define S021_PCT                     100.0

enum ENUM_DOUBLE_FILL_POLICY
  {
   DOUBLE_FILL_CLOSE_BOTH   = 0, // close both legs (current cTrader bot behaviour)
   DOUBLE_FILL_CLOSE_SECOND = 1  // keep the first fill, close the later one
  };

struct S021Settings
  {
   string            symbol;
   long              magic;
   string            strategy_name;          // log group, e.g. "S021-mt5-acct123"
   double            risk_pct;               // % of balance risked per trade
   ENUM_TZ_RULE      server_rule;
   int               server_fixed_hours;
   bool              verify_server_offset;   // live only: refuse to trade on a rule mismatch
   int               exit_buffer_min;        // time exit this many minutes before session close
   int               force_exit_utc_minute;  // -1 = off; firm auto-close workaround
   double            daily_risk_cap_pct;     // <= 0 = off (bot: AccountStrategy.daily_risk_cap_pct)
   double            max_real_risk_pct;      // <= 0 = off; WARN only, never blocks
   ENUM_DOUBLE_FILL_POLICY double_fill_policy;
   int               history_calendar_days;  // M1 lookback for ADR14
   bool              log_to_common;
   bool              write_days_csv;
   AccountLimits     limits;
  };

struct S021DayState
  {
   datetime          day;
   bool              levels_ready;
   bool              levels_final;           // computed, or definitively impossible today
   datetime          next_levels_attempt;
   S021Levels        levels;
   string            status;                 // days-CSV status
   bool              warned_history;
   bool              entry_attempted;
   bool              resolved_logged;
   ulong             open_logged_ticket_long;
   ulong             open_logged_ticket_short;
   string            direction;
   string            entry_time_utc;
   double            entry_price;
   double            lots;
   string            exit_reason;
   string            exit_time_utc;
   double            exit_price;
   double            profit;
  };

class CS021Runtime
  {
private:
   S021Settings      m_cfg;
   CJsonLog          m_log;
   CTradeOps         m_ops;
   S021DayState      m_day;
   bool              m_halted;
   string            m_halt_reason;
   datetime          m_last_heartbeat;
   ulong             m_fill_detected_us;     // GetMicrosecondCount() at the last fill event
   ulong             m_reason_position[S021_MAX_REASON_SLOTS];
   string            m_reason_text[S021_MAX_REASON_SLOTS];
   int               m_days_csv_rows;

   //--- clocks ----------------------------------------------------------
   datetime          ServerToUtc(const datetime server_time) const
     {
      return ClockLocalToUtc(m_cfg.server_rule,m_cfg.server_fixed_hours,server_time);
     }
   datetime          UtcToServer(const datetime utc) const
     {
      return ClockUtcToLocal(m_cfg.server_rule,m_cfg.server_fixed_hours,utc);
     }
   datetime          ServerToClock(const datetime server_time) const
     {
      return S021UtcToClock(ServerToUtc(server_time));
     }
   datetime          ClockToServer(const datetime clock_time) const
     {
      return UtcToServer(S021ClockToUtc(clock_time));
     }

   //--- day state ---------------------------------------------------------
   void              ResetDay(const datetime day)
     {
      m_day.day=day;
      m_day.levels_ready=false;
      m_day.levels_final=false;
      m_day.next_levels_attempt=0;
      m_day.levels.day=day;
      m_day.levels.open_price=0.0;
      m_day.levels.upper=0.0;
      m_day.levels.lower=0.0;
      m_day.levels.adr=0.0;
      m_day.levels.stop_distance=0.0;
      m_day.levels.sessions_used=0;
      m_day.status="";
      m_day.warned_history=false;
      m_day.entry_attempted=false;
      m_day.resolved_logged=false;
      m_day.open_logged_ticket_long=0;
      m_day.open_logged_ticket_short=0;
      m_day.direction="";
      m_day.entry_time_utc="";
      m_day.entry_price=0.0;
      m_day.lots=0.0;
      m_day.exit_reason="";
      m_day.exit_time_utc="";
      m_day.exit_price=0.0;
      m_day.profit=0.0;
     }

   void              RollDay(const datetime today)
     {
      if(m_day.day==today)
         return;
      FlushDayRow();
      ResetDay(today);
     }

   //--- days CSV (parity input for mt5/tools/s021_parity.py) ---------------
   string            DaysCsvPath(void) const
     {
      return m_log.Dir()+"/"+m_cfg.strategy_name+S021_DAYS_CSV_SUFFIX;
     }

   void              FlushDayRow(void)
     {
      if(!m_cfg.write_days_csv || m_day.day==0 || StringLen(m_day.status)==0)
         return;
      int common=m_cfg.log_to_common ? FILE_COMMON : 0;
      string path=DaysCsvPath();
      // One row per day, always the latest state: Deinit() flushes too, so a
      // reattach/reinit cycle (OnDeinit+OnInit, reason 3 on every properties
      // dialog) would otherwise append a duplicate row for the same date and
      // break mt5/tools/s021_parity.py, which keys days by date. Rewrite the
      // file without this day's earlier row instead of appending blindly.
      string prefix=ClockIsoDate(m_day.day)+",";
      string kept[];
      int kept_count=0;
      int handle=FileOpen(path,FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|common);
      if(handle!=INVALID_HANDLE)
        {
         while(!FileIsEnding(handle))
           {
            string line=FileReadString(handle);
            if(StringLen(line)==0)
               continue;
            if(StringFind(line,prefix)==0 || StringFind(line,S021_DAYS_CSV_HEADER_KEY)==0)
               continue;
            ArrayResize(kept,kept_count+1);
            kept[kept_count]=line;
            kept_count++;
           }
         FileClose(handle);
        }
      handle=FileOpen(path,FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|common);
      if(handle==INVALID_HANDLE)
         return;
      FileWriteString(handle,S021_DAYS_CSV_HEADER_KEY+",status,open,adr,upper,lower,"
                      "stop_distance,sessions,direction,entry_time_utc,entry_price,lots,"
                      "exit_reason,exit_time_utc,exit_price,profit\n");
      for(int i=0;i<kept_count;i++)
         FileWriteString(handle,kept[i]+"\n");
      int digits=(int)SymbolInfoInteger(m_cfg.symbol,SYMBOL_DIGITS);
      string row=StringFormat("%s,%s,%s,%s,%s,%s,%s,%d,%s,%s,%s,%s,%s,%s,%s,%s\n",
                              ClockIsoDate(m_day.day),m_day.status,
                              DoubleToString(m_day.levels.open_price,digits),
                              DoubleToString(m_day.levels.adr,8),
                              DoubleToString(m_day.levels.upper,8),
                              DoubleToString(m_day.levels.lower,8),
                              DoubleToString(m_day.levels.stop_distance,8),
                              m_day.levels.sessions_used,
                              m_day.direction,m_day.entry_time_utc,
                              DoubleToString(m_day.entry_price,digits),
                              DoubleToString(m_day.lots,4),
                              m_day.exit_reason,m_day.exit_time_utc,
                              DoubleToString(m_day.exit_price,digits),
                              DoubleToString(m_day.profit,2));
      FileWriteString(handle,row);
      FileClose(handle);
      m_days_csv_rows=kept_count+1;
     }

   void              SetStatus(const string status)
     {
      // "traded" is final; anything else may still be upgraded by a later step.
      if(m_day.status=="traded")
         return;
      m_day.status=status;
     }

   //--- close-reason bookkeeping (deal events arrive after the call) -------
   void              RememberCloseReason(const ulong position_ticket,const string reason)
     {
      for(int i=0;i<S021_MAX_REASON_SLOTS;i++)
         if(m_reason_position[i]==0 || m_reason_position[i]==position_ticket)
           {
            m_reason_position[i]=position_ticket;
            m_reason_text[i]=reason;
            return;
           }
      m_reason_position[0]=position_ticket;     // table full: overwrite the oldest slot
      m_reason_text[0]=reason;
     }

   string            TakeCloseReason(const ulong position_ticket)
     {
      for(int i=0;i<S021_MAX_REASON_SLOTS;i++)
         if(m_reason_position[i]==position_ticket)
           {
            m_reason_position[i]=0;
            string reason=m_reason_text[i];
            m_reason_text[i]="";
            return reason;
           }
      return "";
     }

   //--- levels --------------------------------------------------------------
   // Loads M1 history, rebuilds sessions and today's anchor. Returns true once
   // the day's levels are final (ready, or definitively not available).
   void              TryComputeLevels(const datetime now_server,const int minute,const string cycle)
     {
      if(m_day.levels_final || now_server<m_day.next_levels_attempt)
         return;
      m_day.next_levels_attempt=now_server+S021_LEVELS_RETRY_SECONDS;

      datetime anchor_clock=m_day.day+S021_SESSION_OPEN_MINUTE*CLOCK_SECONDS_PER_MINUTE;
      datetime anchor_server=ClockToServer(anchor_clock);
      int anchor_shift=iBarShift(m_cfg.symbol,PERIOD_M1,anchor_server,true);
      if(anchor_shift<0)
        {
         if(minute==S021_SESSION_OPEN_MINUTE)
            return;                         // no tick in the anchor minute yet -- wait
         m_day.levels_final=true;           // engine rule: no bar exactly at 09:30 -> no trade
         SetStatus("no_anchor");
         CJsonFields fields;
         fields.Str("day",ClockIsoDate(m_day.day));
         fields.Str("anchor_server",ClockIsoDateTime(anchor_server));
         m_log.Event("no_anchor",cycle,fields.Body());
         return;
        }
      double open_price=iOpen(m_cfg.symbol,PERIOD_M1,anchor_shift);

      MqlRates rates[];
      datetime from_server=(datetime)((long)now_server-(long)m_cfg.history_calendar_days*CLOCK_SECONDS_PER_DAY);
      int copied=CopyRates(m_cfg.symbol,PERIOD_M1,from_server,now_server,rates);
      if(copied<=0)
        {
         if(!m_day.warned_history)
           {
            m_day.warned_history=true;
            CJsonFields fields;
            fields.Int("error",GetLastError());
            fields.Int("history_calendar_days",m_cfg.history_calendar_days);
            m_log.Event("history_not_ready",cycle,fields.Body(),ALGO_LOG_WARNING);
           }
         return;                            // retried after S021_LEVELS_RETRY_SECONDS
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
         clock_times[i]=ServerToClock(rates[i].time);
         opens[i]=rates[i].open;
         highs[i]=rates[i].high;
         lows[i]=rates[i].low;
        }
      DailySession sessions[];
      SessionWindow window=S021Window();
      int session_count=SessionsBuild(clock_times,opens,highs,lows,copied,window,sessions);

      S021Levels levels;
      if(!S021ComputeLevels(sessions,session_count,m_day.day,open_price,levels))
        {
         m_day.levels.sessions_used=levels.sessions_used;
         if(!m_day.warned_history)
           {
            m_day.warned_history=true;
            CJsonFields fields;
            fields.Int("sessions_available",levels.sessions_used);
            fields.Int("adr_window",S021_ADR_WINDOW);
            fields.Int("bars_loaded",copied);
            fields.Str("text","session-open anchor is valid but ADR14 is not computable "
                       "(too few valid prior sessions, or the gap guard)");
            m_log.Event("insufficient_adr_history",cycle,fields.Body(),ALGO_LOG_WARNING);
           }
         SetStatus("insufficient_history");
         if(minute>S021_ENTRY_CUTOFF_MINUTE)
            m_day.levels_final=true;
         return;
        }
      m_day.levels=levels;
      m_day.levels_ready=true;
      m_day.levels_final=true;
      CJsonFields fields;
      fields.Str("day",ClockIsoDate(levels.day));
      fields.Num("O",levels.open_price);
      fields.Num("ADR14",levels.adr);
      fields.Num("U",levels.upper);
      fields.Num("L",levels.lower);
      fields.Num("stop_dist",levels.stop_distance);
      fields.Int("sessions_available",levels.sessions_used);
      m_log.Event("levels",cycle,fields.Body());
     }

   // True when today's bars since the anchor already touched a level -- the
   // backtest would have entered earlier, a resting stop can no longer
   // reproduce that (the cTrader bot's broker would reject it).
   bool              LevelAlreadyTouched(const datetime now_server,double &max_high,double &min_low)
     {
      datetime anchor_server=ClockToServer(m_day.day+S021_SESSION_OPEN_MINUTE*CLOCK_SECONDS_PER_MINUTE);
      MqlRates rates[];
      int copied=CopyRates(m_cfg.symbol,PERIOD_M1,anchor_server,now_server,rates);
      max_high=0.0;
      min_low=0.0;
      for(int i=0;i<copied;i++)
        {
         max_high=(i==0) ? rates[i].high : MathMax(max_high,rates[i].high);
         min_low=(i==0) ? rates[i].low : MathMin(min_low,rates[i].low);
        }
      double ask=SymbolInfoDouble(m_cfg.symbol,SYMBOL_ASK);
      double bid=SymbolInfoDouble(m_cfg.symbol,SYMBOL_BID);
      bool touched=(copied>0 && (max_high>=m_day.levels.upper || min_low<=m_day.levels.lower));
      return touched || ask>=m_day.levels.upper || bid<=m_day.levels.lower;
     }

   //--- actions ---------------------------------------------------------------
   void              ClosePosition(const OwnPosition &position,const string label,
                                   const string reason,const string cycle,
                                   const ENUM_ALGO_LOG_LEVEL level=ALGO_LOG_INFO)
     {
      RememberCloseReason(position.ticket,reason);
      TradeResult result;
      m_ops.Close(position.ticket,result);
      CJsonFields request;
      request.Int("position_id",(long)position.ticket);
      request.Str("reason",reason);
      m_log.Order(label,"close_position",cycle,result.ok,request.Body(),result.message,
                  result.ok ? "" : result.message);
      if(!result.ok)
         TakeCloseReason(position.ticket);
      else
         if(level!=ALGO_LOG_INFO)
           {
            CJsonFields fields;
            fields.Str("label",label);
            fields.Str("reason",reason);
            m_log.Event(reason,cycle,fields.Body(),level);
           }
     }

   void              CancelOrder(const OwnOrder &order,const string label,const string reason,
                                 const string cycle)
     {
      TradeResult result;
      ulong started_us=GetMicrosecondCount();
      m_ops.Cancel(order.ticket,result);
      CJsonFields request;
      request.Int("order_id",(long)order.ticket);
      request.Str("reason",reason);
      m_log.Order(label,"cancel_order",cycle,result.ok,request.Body(),result.message,
                  result.ok ? "" : result.message);
      CJsonFields fields;
      fields.Str("label",label);
      fields.Str("reason",reason);
      fields.Bool("ok",result.ok);
      fields.Num("cancel_call_ms",(double)(GetMicrosecondCount()-started_us)/S021_USEC_PER_MSEC,1);
      if(reason=="opposite_leg_filled" && m_fill_detected_us>0)
         fields.Num("cancel_latency_ms",
                    (double)(GetMicrosecondCount()-m_fill_detected_us)/S021_USEC_PER_MSEC,1);
      m_log.Event(reason=="opposite_leg_filled" ? "cancel_sibling" :
                  (reason=="entry_cutoff_passed" ? "cancel_unfilled" : "cancel_order"),
                  cycle,fields.Body(),reason=="stale_order" ? ALGO_LOG_WARNING : ALGO_LOG_INFO);
     }

   void              LogOpenOnce(const OwnPosition &position,const bool is_long,const string cycle,
                                 const bool recovered)
     {
      ulong logged=is_long ? m_day.open_logged_ticket_long : m_day.open_logged_ticket_short;
      if(logged==position.ticket)
         return;
      if(is_long)
         m_day.open_logged_ticket_long=position.ticket;
      else
         m_day.open_logged_ticket_short=position.ticket;
      CJsonFields fields;
      fields.Str("side",is_long ? "buy" : "sell");
      fields.Num("entry",position.price_open);
      fields.Num("sl",position.stop_loss);
      fields.Null("tp");
      fields.Bool("is_add",false);
      fields.Num("volume_lots",position.volume,4);
      fields.Int("position_id",(long)position.ticket);
      if(recovered)
         fields.Bool("recovered",true);
      m_log.Position(S021Label(m_day.day,is_long),"open",cycle,fields.Body());
      m_day.direction=is_long ? "long" : "short";
      m_day.entry_time_utc=ClockIsoDateTime(ServerToUtc(position.time_server));
      m_day.entry_price=position.price_open;
      m_day.lots=position.volume;
      SetStatus("traded");
     }

   bool              AfterExitTime(const int clock_minute,const datetime now_utc) const
     {
      if(clock_minute>=S021_SESSION_CLOSE_MINUTE-m_cfg.exit_buffer_min)
         return true;
      if(m_cfg.force_exit_utc_minute>=0
         && ClockMinuteOfDay(now_utc)>=m_cfg.force_exit_utc_minute
         && clock_minute>=S021_SESSION_OPEN_MINUTE)
         return true;
      return false;
     }

   void              TryEnter(const datetime now_server,const int minute,const string cycle)
     {
      if(m_day.entry_attempted)
         return;
      if(!m_day.levels_ready)
         return;
      m_day.entry_attempted=true;

      double max_high=0.0;
      double min_low=0.0;
      if(LevelAlreadyTouched(now_server,max_high,min_low))
        {
         SetStatus("missed_entry");
         CJsonFields fields;
         fields.Num("U",m_day.levels.upper);
         fields.Num("L",m_day.levels.lower);
         fields.Num("high_since_open",max_high);
         fields.Num("low_since_open",min_low);
         fields.Num("ask",SymbolInfoDouble(m_cfg.symbol,SYMBOL_ASK));
         fields.Num("bid",SymbolInfoDouble(m_cfg.symbol,SYMBOL_BID));
         fields.Str("text","a level was already touched before orders could be placed -- "
                    "day skipped (no market-order substitute, execution stays stop-order only)");
         m_log.Event("missed_entry",cycle,fields.Body(),ALGO_LOG_WARNING);
         return;
        }

      double balance=AccountInfoDouble(ACCOUNT_BALANCE);
      double risk_amount=balance*m_cfg.risk_pct/S021_PCT;
      double money_per_point=SizingMoneyPerPointPerLot(m_cfg.symbol);
      double volume_min=SymbolInfoDouble(m_cfg.symbol,SYMBOL_VOLUME_MIN);
      double raw_lots=SizingLotsForRisk(risk_amount,m_day.levels.stop_distance,money_per_point,
                                        volume_min);
      double lots=SizingNormalizeVolumeForSymbol(m_cfg.symbol,raw_lots);
      double new_risk=lots*m_day.levels.stop_distance*money_per_point;
      double real_risk_pct=(balance>0.0) ? new_risk/balance*S021_PCT : 0.0;
      string long_label=S021Label(m_day.day,true);
      string short_label=S021Label(m_day.day,false);

      CJsonFields size;
      size.Str("long_label",long_label);
      size.Str("short_label",short_label);
      size.Num("U",m_day.levels.upper);
      size.Num("L",m_day.levels.lower);
      size.Num("stop_dist",m_day.levels.stop_distance);
      size.Num("lot_raw",raw_lots,6);
      size.Num("lot",lots,4);
      size.Num("risk_amount",risk_amount,2);
      size.Num("real_risk",new_risk,2);
      size.Num("real_risk_pct",real_risk_pct,3);
      size.Num("money_per_point_per_lot",money_per_point,6);
      size.Num("volume_min",volume_min,4);
      m_log.Event("size",cycle,size.Body());

      if(m_cfg.max_real_risk_pct>0.0 && real_risk_pct>m_cfg.max_real_risk_pct)
        {
         CJsonFields warn;
         warn.Num("real_risk_pct",real_risk_pct,3);
         warn.Num("max_real_risk_pct",m_cfg.max_real_risk_pct,3);
         warn.Str("text","minimum-lot / rounding risk exceeds the configured warning level "
                  "(informational only -- the trade is NOT blocked)");
         m_log.Event("real_risk_above_warning",cycle,warn.Body(),ALGO_LOG_WARNING);
        }

      if(m_cfg.daily_risk_cap_pct>0.0)
        {
         double cap_base=(m_cfg.limits.initial_balance>0.0) ? m_cfg.limits.initial_balance : balance;
         double risk_cap=cap_base*m_cfg.daily_risk_cap_pct/S021_PCT;
         if(new_risk>risk_cap)
           {
            SetStatus("skip_risk_cap");
            CJsonFields skip;
            skip.Str("label",long_label);
            skip.Num("new_risk",new_risk,2);
            skip.Num("risk_cap",risk_cap,2);
            m_log.Event("skip_risk_cap",cycle,skip.Body());
            return;
           }
        }

      if(AccountGuardActive(m_cfg.limits))
        {
         datetime day_start=AccountGuardDayStartServer(m_cfg.limits,m_cfg.server_rule,
                                                       m_cfg.server_fixed_hours);
         double realized=AccountGuardRealizedSince(day_start);
         GuardVerdict verdict;
         AccountGuardCheck(m_cfg.limits,balance,realized,0.0,new_risk,verdict);
         if(!verdict.allowed)
           {
            SetStatus("skip_account_guard");
            CJsonFields skip;
            skip.Str("label",long_label);
            skip.Str("reason",verdict.reason);
            skip.Num("balance",balance,2);
            skip.Num("day_start_balance",verdict.day_start_balance,2);
            skip.Num("realized_today",verdict.realized_today,2);
            skip.Num("new_risk",new_risk,2);
            skip.Num("worst_day_loss",verdict.worst_day_loss,2);
            skip.Num("daily_budget",verdict.daily_budget,2);
            skip.Num("worst_balance",verdict.worst_balance,2);
            skip.Num("max_loss_floor",verdict.max_loss_floor,2);
            m_log.Event("skip_account_guard",cycle,skip.Body());
            return;
           }
        }

      if(m_halted)
        {
         SetStatus("halted");
         CJsonFields fields;
         fields.Str("reason",m_halt_reason);
         m_log.Event("entry_blocked_halted",cycle,fields.Body(),ALGO_LOG_ERROR);
         return;
        }

      PlaceLeg(true,lots,long_label,cycle);
      PlaceLeg(false,lots,short_label,cycle);
      SetStatus("no_fill");
     }

   void              PlaceLeg(const bool is_long,const double lots,const string label,
                              const string cycle)
     {
      double raw_price=is_long ? m_day.levels.upper : m_day.levels.lower;
      double raw_sl=is_long ? raw_price-m_day.levels.stop_distance
                    : raw_price+m_day.levels.stop_distance;
      double price=SizingNormalizePrice(m_cfg.symbol,raw_price);
      double stop_loss=SizingNormalizePrice(m_cfg.symbol,raw_sl);
      TradeResult result;
      m_ops.PlaceStop(is_long,lots,price,stop_loss,S021OrderComment(m_day.day,is_long),result);
      CJsonFields request;
      request.Str("side",is_long ? "buy" : "sell");
      request.Num("stop",price);
      request.Num("sl",stop_loss);
      request.Num("lot",lots,4);
      request.Num("stop_raw",raw_price,8);
      m_log.Order(label,"place_stop",cycle,result.ok,request.Body(),
                  StringFormat("order=%I64u %s",result.ticket,result.message),
                  result.ok ? "" : result.message);
      if(!result.ok)
         SetStatus("placement_failed");
     }

   //--- the reconcile ----------------------------------------------------------
   void              ReconcileInternal(const string trigger)
     {
      datetime now_server=TimeTradeServer();
      datetime now_utc=ServerToUtc(now_server);
      datetime now_clock=S021UtcToClock(now_utc);
      datetime today=ClockDayStart(now_clock);
      int minute=ClockMinuteOfDay(now_clock);
      RollDay(today);
      string cycle=m_log.NewCycleId();

      OwnPosition positions[];
      int position_count=TradeOpsCollectPositions(m_cfg.symbol,m_cfg.magic,positions);
      OwnOrder orders[];
      int order_count=TradeOpsCollectStopOrders(m_cfg.symbol,m_cfg.magic,orders);

      int long_index=-1;
      int short_index=-1;
      for(int i=0;i<position_count;i++)
        {
         datetime position_day=ClockDayStart(ServerToClock(positions[i].time_server));
         if(position_day<today)
           {
            // A previous session's position survived its time exit (terminal off,
            // connection lost...) -- close it now, loudly (cTrader bot incident 2026-10-01).
            ClosePosition(positions[i],S021Label(position_day,positions[i].is_buy),
                          "late_time_exit",cycle,ALGO_LOG_WARNING);
            continue;
           }
         if(positions[i].is_buy)
            long_index=(long_index<0) ? i : long_index;
         else
            short_index=(short_index<0) ? i : short_index;
        }

      int today_orders=0;
      for(int i=0;i<order_count;i++)
        {
         datetime order_day=ClockDayStart(ServerToClock(orders[i].setup_server));
         if(order_day<today)
           {
            CancelOrder(orders[i],S021Label(order_day,orders[i].is_buy),"stale_order",cycle);
            continue;
           }
         today_orders++;
        }

      //--- case 7: both legs filled -> execution anomaly ---------------------
      if(long_index>=0 && short_index>=0)
        {
         LogOpenOnce(positions[long_index],true,cycle,false);
         LogOpenOnce(positions[short_index],false,cycle,false);
         CJsonFields fields;
         fields.Str("policy",m_cfg.double_fill_policy==DOUBLE_FILL_CLOSE_BOTH ? "close_both" : "close_second");
         fields.Str("long_open_server",ClockIsoDateTime(positions[long_index].time_server));
         fields.Str("short_open_server",ClockIsoDateTime(positions[short_index].time_server));
         m_log.Event("oco_double_fill",cycle,fields.Body(),ALGO_LOG_ERROR);
         bool long_is_second=(positions[long_index].time_server>positions[short_index].time_server)
                             || (positions[long_index].time_server==positions[short_index].time_server
                                 && positions[long_index].ticket>positions[short_index].ticket);
         if(m_cfg.double_fill_policy==DOUBLE_FILL_CLOSE_BOTH || long_is_second)
            ClosePosition(positions[long_index],S021Label(today,true),"both_filled_anomaly",cycle);
         if(m_cfg.double_fill_policy==DOUBLE_FILL_CLOSE_BOTH || !long_is_second)
            ClosePosition(positions[short_index],S021Label(today,false),"both_filled_anomaly",cycle);
         return;
        }

      //--- case 3/4: one leg filled -> cancel sibling, time exit -------------
      if(long_index>=0 || short_index>=0)
        {
         bool is_long=(long_index>=0);
         int index=is_long ? long_index : short_index;
         for(int i=0;i<order_count;i++)
           {
            if(ClockDayStart(ServerToClock(orders[i].setup_server))<today)
               continue;
            CancelOrder(orders[i],S021Label(today,orders[i].is_buy),"opposite_leg_filled",cycle);
           }
         m_fill_detected_us=0;
         LogOpenOnce(positions[index],is_long,cycle,trigger=="init");
         if(AfterExitTime(minute,now_utc))
           {
            bool forced=(minute<S021_SESSION_CLOSE_MINUTE-m_cfg.exit_buffer_min);
            ClosePosition(positions[index],S021Label(today,is_long),forced ? "force_exit" : "time",cycle);
           }
         return;
        }

      //--- case 5: pending, unfilled -> wait, or cancel at the cutoff ---------
      if(today_orders>0)
        {
         if(minute>S021_ENTRY_CUTOFF_MINUTE)
           {
            for(int i=0;i<order_count;i++)
              {
               if(ClockDayStart(ServerToClock(orders[i].setup_server))<today)
                  continue;
               CancelOrder(orders[i],S021Label(today,orders[i].is_buy),"entry_cutoff_passed",cycle);
              }
            SetStatus("no_fill");
           }
         return;
        }

      //--- nothing open or pending: case 6 / today already handled -----------
      if(minute<S021_SESSION_OPEN_MINUTE || minute>S021_ENTRY_CUTOFF_MINUTE)
         return;
      datetime day_start_server=ClockToServer(today);
      if(TradeOpsHistoryHasOrder(m_cfg.symbol,m_cfg.magic,S021OrderCommentPrefix(today),
                                 day_start_server))
        {
         if(!m_day.resolved_logged)
           {
            m_day.resolved_logged=true;
            m_day.entry_attempted=true;
            CJsonFields fields;
            fields.Str("day",ClockIsoDate(today));
            fields.Str("text","today's orders were already placed and are gone "
                       "(filled+closed, or cancelled) -- no re-entry (sec 3)");
            m_log.Event("day_resolved",cycle,fields.Body());
           }
         return;
        }

      //--- case 1/2: levels, then a fresh entry --------------------------------
      TryComputeLevels(now_server,minute,cycle);
      TryEnter(now_server,minute,cycle);
     }

public:
                     CS021Runtime(void)
     {
      m_halted=false;
      m_halt_reason="";
      m_last_heartbeat=0;
      m_fill_detected_us=0;
      m_days_csv_rows=0;
      for(int i=0;i<S021_MAX_REASON_SLOTS;i++)
        {
         m_reason_position[i]=0;
         m_reason_text[i]="";
        }
      ResetDay(0);
     }

   bool              Init(const S021Settings &settings)
     {
      m_cfg=settings;
      if(!SymbolSelect(m_cfg.symbol,true))
        {
         PrintFormat("S021: symbol %s not available",m_cfg.symbol);
         return false;
        }
      m_log.Init(S021_LOG_ROOT,m_cfg.strategy_name,m_cfg.log_to_common,
                 m_cfg.server_rule,m_cfg.server_fixed_hours);
      m_ops.Init(m_cfg.symbol,m_cfg.magic);

      if(m_cfg.limits.initial_balance<=0.0 && (m_cfg.limits.daily_loss_pct>0.0
                                                || m_cfg.limits.max_loss_pct>0.0))
         m_cfg.limits.initial_balance=AccountGuardFirstDeposit();

      bool tester=(bool)MQLInfoInteger(MQL_TESTER);
      int observed=tester ? 0 : ClockObservedServerOffsetSeconds();
      int expected=ClockOffsetSecondsAtUtc(m_cfg.server_rule,m_cfg.server_fixed_hours,TimeGMT());
      if(!tester && m_cfg.verify_server_offset && observed!=expected)
        {
         m_halted=true;
         m_halt_reason=StringFormat("server offset mismatch: rule %s gives %d s, terminal shows %d s",
                                    ClockRuleName(m_cfg.server_rule),expected,observed);
        }

      CJsonFields spec;
      spec.Str("symbol",m_cfg.symbol);
      spec.Int("login",AccountInfoInteger(ACCOUNT_LOGIN));
      spec.Str("server",AccountInfoString(ACCOUNT_SERVER));
      spec.Str("company",AccountInfoString(ACCOUNT_COMPANY));
      spec.Str("currency",AccountInfoString(ACCOUNT_CURRENCY));
      spec.Str("margin_mode",AccountInfoInteger(ACCOUNT_MARGIN_MODE)==ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
               ? "hedging" : "netting_or_exchange");
      spec.Num("contract_size",SymbolInfoDouble(m_cfg.symbol,SYMBOL_TRADE_CONTRACT_SIZE),4);
      spec.Num("tick_size",SymbolInfoDouble(m_cfg.symbol,SYMBOL_TRADE_TICK_SIZE),8);
      spec.Num("tick_value",SymbolInfoDouble(m_cfg.symbol,SYMBOL_TRADE_TICK_VALUE),8);
      spec.Num("tick_value_loss",SymbolInfoDouble(m_cfg.symbol,SYMBOL_TRADE_TICK_VALUE_LOSS),8);
      spec.Num("money_per_point_per_lot",SizingMoneyPerPointPerLot(m_cfg.symbol),6);
      spec.Num("volume_min",SymbolInfoDouble(m_cfg.symbol,SYMBOL_VOLUME_MIN),4);
      spec.Num("volume_step",SymbolInfoDouble(m_cfg.symbol,SYMBOL_VOLUME_STEP),4);
      spec.Num("volume_max",SymbolInfoDouble(m_cfg.symbol,SYMBOL_VOLUME_MAX),4);
      spec.Int("digits",SymbolInfoInteger(m_cfg.symbol,SYMBOL_DIGITS));
      spec.Int("stops_level_points",SymbolInfoInteger(m_cfg.symbol,SYMBOL_TRADE_STOPS_LEVEL));
      spec.Int("freeze_level_points",SymbolInfoInteger(m_cfg.symbol,SYMBOL_TRADE_FREEZE_LEVEL));
      spec.Int("terminal_max_bars",TerminalInfoInteger(TERMINAL_MAXBARS));
      spec.Str("server_tz_rule",ClockRuleName(m_cfg.server_rule));
      spec.Int("server_offset_expected_s",expected);
      spec.Int("server_offset_observed_s",observed);
      spec.Bool("tester",tester);
      spec.Num("risk_pct",m_cfg.risk_pct,3);
      spec.Num("initial_balance",m_cfg.limits.initial_balance,2);
      spec.Num("daily_guard_pct",m_cfg.limits.daily_loss_pct,2);
      spec.Num("max_guard_pct",m_cfg.limits.max_loss_pct,2);
      spec.Int("exit_buffer_min",m_cfg.exit_buffer_min);
      spec.Int("force_exit_utc_minute",m_cfg.force_exit_utc_minute);
      spec.Str("params_source",S021_PARAMS_SOURCE);
      m_log.Event("init",m_log.NewCycleId(),spec.Body());
      if(m_halted)
        {
         CJsonFields fields;
         fields.Str("reason",m_halt_reason);
         m_log.Event("halted",m_log.NewCycleId(),fields.Body(),ALGO_LOG_ERROR);
         Alert("S021: ",m_halt_reason," -- new entries are disabled. Fix ServerTzRule.");
        }
      ReconcileInternal("init");
      return true;
     }

   void              Deinit(const int reason)
     {
      FlushDayRow();
      CJsonFields fields;
      fields.Int("reason",reason);
      fields.Int("days_csv_rows",m_days_csv_rows);
      m_log.Event("deinit",m_log.NewCycleId(),fields.Body());
     }

   void              OnTimerTick(void)
     {
      ReconcileInternal("timer");
      datetime now=TimeLocal();
      if(now-m_last_heartbeat>=S021_HEARTBEAT_SECONDS)
        {
         m_last_heartbeat=now;
         CJsonFields fields;
         fields.Int("login",AccountInfoInteger(ACCOUNT_LOGIN));
         fields.Str("symbol",m_cfg.symbol);
         fields.Bool("connected",(bool)TerminalInfoInteger(TERMINAL_CONNECTED));
         fields.Bool("terminal_trade_allowed",(bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED));
         fields.Bool("ea_trade_allowed",(bool)MQLInfoInteger(MQL_TRADE_ALLOWED));
         fields.Bool("halted",m_halted);
         fields.Str("server_time",ClockIsoDateTime(TimeTradeServer()));
         m_log.WriteStatus(S021_HEARTBEAT_FILE,fields.Body());
        }
     }

   void              OnTickEvent(void)
     {
      ReconcileInternal("tick");
     }

   void              OnTradeTx(const MqlTradeTransaction &trans)
     {
      if(trans.type!=TRADE_TRANSACTION_DEAL_ADD || trans.deal==0)
         return;
      if(!HistoryDealSelect(trans.deal))
         return;
      if(HistoryDealGetString(trans.deal,DEAL_SYMBOL)!=m_cfg.symbol
         || HistoryDealGetInteger(trans.deal,DEAL_MAGIC)!=m_cfg.magic)
         return;
      long entry=HistoryDealGetInteger(trans.deal,DEAL_ENTRY);
      long deal_type=HistoryDealGetInteger(trans.deal,DEAL_TYPE);
      datetime deal_server=(datetime)HistoryDealGetInteger(trans.deal,DEAL_TIME);
      ulong position_id=(ulong)HistoryDealGetInteger(trans.deal,DEAL_POSITION_ID);
      double price=HistoryDealGetDouble(trans.deal,DEAL_PRICE);
      datetime deal_day=ClockDayStart(ServerToClock(deal_server));
      string cycle=m_log.NewCycleId();

      if(entry==DEAL_ENTRY_IN)
        {
         m_fill_detected_us=GetMicrosecondCount();
         CJsonFields fields;
         fields.Int("deal",(long)trans.deal);
         fields.Int("position_id",(long)position_id);
         fields.Num("price",price);
         fields.Str("deal_time_server",ClockIsoDateTime(deal_server));
         m_log.Event("fill",cycle,fields.Body());
         ReconcileInternal("fill");          // cancels the sibling right now
         return;
        }

      if(entry==DEAL_ENTRY_OUT || entry==DEAL_ENTRY_OUT_BY)
        {
         // a SELL deal closes a long, a BUY deal closes a short
         bool was_long=(deal_type==DEAL_TYPE_SELL);
         long deal_reason=HistoryDealGetInteger(trans.deal,DEAL_REASON);
         string reason=TakeCloseReason(position_id);
         if(StringLen(reason)==0)
           {
            if(deal_reason==DEAL_REASON_SL)
               reason="stop";
            else
               if(deal_reason==DEAL_REASON_TP)
                  reason="tp";
               else
                  if(deal_reason==DEAL_REASON_SO)
                     reason="stop_out";
                  else
                     if(deal_reason==DEAL_REASON_CLIENT || deal_reason==DEAL_REASON_MOBILE
                        || deal_reason==DEAL_REASON_WEB)
                        reason="manual";
                     else
                        reason="broker_side";
           }
         double profit=HistoryDealGetDouble(trans.deal,DEAL_PROFIT)
                       +HistoryDealGetDouble(trans.deal,DEAL_SWAP)
                       +HistoryDealGetDouble(trans.deal,DEAL_COMMISSION)
                       +HistoryDealGetDouble(trans.deal,DEAL_FEE);
         // the position's own day = the day its entry deal belongs to
         datetime position_day=deal_day;
         if(HistorySelectByPosition(position_id))
           {
            int deals=HistoryDealsTotal();
            for(int i=0;i<deals;i++)
              {
               ulong ticket=HistoryDealGetTicket(i);
               if(ticket>0 && HistoryDealGetInteger(ticket,DEAL_ENTRY)==DEAL_ENTRY_IN)
                 {
                  position_day=ClockDayStart(ServerToClock((datetime)HistoryDealGetInteger(ticket,DEAL_TIME)));
                  break;
                 }
              }
           }
         CJsonFields fields;
         fields.Str("reason",reason);
         fields.Num("exit",price);
         fields.Num("pnl",profit,2);
         fields.Int("position_id",(long)position_id);
         fields.Int("deal",(long)trans.deal);
         m_log.Position(S021Label(position_day,was_long),"close",cycle,fields.Body());
         if(position_day==m_day.day)
           {
            m_day.exit_reason=reason;
            m_day.exit_time_utc=ClockIsoDateTime(ServerToUtc(deal_server));
            m_day.exit_price=price;
            m_day.profit+=profit;
           }
        }
     }
  };

#endif // S021_RUNTIME_MQH
//+------------------------------------------------------------------+
