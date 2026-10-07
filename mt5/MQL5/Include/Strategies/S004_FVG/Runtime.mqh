//+------------------------------------------------------------------+
//| Strategies/S004_FVG/Runtime.mqh                                  |
//| S004-intraday live runtime: the broker-facing half of the EA     |
//| (ALGODEV-62 phase C). Engine.mqh decides, this places orders.    |
//|                                                                  |
//| One EA instance drives ALL S004_SYMBOLS from a single chart:     |
//| every symbol owns a CS004Engine fed its own closed M15 bars, and |
//| the daily cap is counted across the whole portfolio, exactly as  |
//| backtest/run_s004_intraday.py does.                              |
//|                                                                  |
//| The three things that make this more than a thin wrapper:        |
//|                                                                  |
//| 1. RESTING LIMITS. The backtest fills at the near edge of the    |
//|    zone INSIDE the bar, so a live entry has to be parked before  |
//|    that bar opens -- entering at the close of the bar the touch  |
//|    was noticed on would be a different (worse) price. After each |
//|    bar the runtime parks one limit per symbol on the zone the    |
//|    engine would take next, and cancels it when that zone dies.   |
//|    A limit that gaps through fills BETTER than its price, which  |
//|    is what the engine models too (entry = min(open, near edge)), |
//|    so the fill is then re-stopped/re-targeted off the real fill. |
//|                                                                  |
//| 2. VIRTUAL TRADES. The engine trades round the clock and the     |
//|    backtest filters by entry hour and daily cap AFTERWARDS, so a |
//|    trade outside the Asia window or past the cap still consumes  |
//|    its zone and holds the one-position-per-symbol lock. Those    |
//|    stay VIRTUAL here: the engine opens and closes them, no order |
//|    is ever sent. Skipping them in the engine instead would free  |
//|    the symbol earlier than the backtest does and the live trade  |
//|    list would drift. A limit is therefore parked only when a     |
//|    fill on the NEXT bar would actually be taken.                 |
//|                                                                  |
//| 3. RESTART = REPLAY. There is no persisted state: on init every  |
//|    engine is replayed over the last InpWarmupBars closed M15     |
//|    bars, which rebuilds the same zones, the same lock and the    |
//|    same open position the running instance had. Open positions   |
//|    are then re-adopted by magic+symbol.                          |
//|                                                                  |
//| Two clocks. Everything the STRATEGY decides -- the Asia window,  |
//| the 22:45 cutoff, the day the cap is counted in -- runs on the   |
//| session clock (S004_CLOCK_TZ_RULE, the EET/EEST the backtest     |
//| bars are stamped in), so bar times are converted on the way into |
//| the engine. The broker's own clock is only where bars and        |
//| positions are read from. A broker whose server rule differs from |
//| the session rule (EET_US_DST is the common one) would otherwise  |
//| shift the whole session by an hour for the weeks the two DST     |
//| calendars disagree -- that is the ALGODEV-61 bug, not a shortcut.|
//|                                                                  |
//| Parity input. Every closed engine trade is appended to           |
//| <strategy>_trades.csv with the status that says whether an order |
//| backed it; mt5/tools/s004_parity.py diffs that file against the  |
//| Python engine re-run on the broker's own bars (phase D).         |
//+------------------------------------------------------------------+
#ifndef STRATEGIES_S004_FVG_RUNTIME_MQH
#define STRATEGIES_S004_FVG_RUNTIME_MQH

#include "Engine.mqh"
#include <AlgoCore/Clock.mqh>
#include <AlgoCore/JsonLog.mqh>
#include <AlgoCore/Sizing.mqh>
#include <AlgoCore/AccountGuard.mqh>
#include <AlgoCore/TradeOps.mqh>

#define S004_LOG_ROOT             "AlgoTrading/logs"
#define S004_MAX_SYMBOLS          16
#define S004_HEARTBEAT_SECONDS    60
#define S004_HEARTBEAT_FILE       "heartbeat.json"
#define S004_PCT                  100.0
#define S004_MIN_WARMUP_BARS      400     // >= 4 H4 bars + the 96-bar zone window, with room
#define S004_PIP_POINTS           10      // an FX pip is ten points on a 3/5-digit feed
#define S004_PRICE_DIGITS         6       // log prices at tick resolution, not lot resolution
#define S004_CSV_PRICE_DIGITS     8       // the parity CSV: same precision as the Python fixtures
#define S004_CSV_R_DIGITS         6
#define S004_CSV_LOT_DIGITS       4
#define S004_TRADES_CSV_SUFFIX    "_trades.csv"
#define S004_TRADES_CSV_HEADER    "symbol,time_in,time_out,dir,entry,sl,tp,exit,r,exit_reason," \
                                  "hour,status,fill,lots,ticket,pip,cost"

// What the live layer did with a trade the engine took. The first two are the
// backtest's own "not taken" reasons and must match it exactly; the rest are
// live-only outcomes mt5/tools/s004_parity.py reports but does not fail on.
#define S004_STATUS_TAKEN         "taken"           // a real position mirrored the engine
#define S004_STATUS_SHADOW        "shadow"          // would be taken, InpTradeEnabled=false
#define S004_STATUS_MISSED_FILL   "missed_fill"     // would be taken, no position came back
#define S004_STATUS_VIRTUAL_HOUR  "virtual_hour"    // outside the Asia entry window
#define S004_STATUS_VIRTUAL_CAP   "virtual_cap"     // the daily cap was already spent
#define S004_STATUS_VIRTUAL_HALT  "virtual_halt"    // the account guard had halted trading

struct S004Settings
  {
   long              magic;
   string            strategy_name;        // log group, e.g. "S004-mt5-acct123"
   double            risk_pct;             // % of balance risked per trade (EA input)
   int               max_trades_per_day;   // portfolio-wide cap on REAL entries (EA input)
   ENUM_TZ_RULE      server_rule;          // the broker clock the session hours are on
   int               server_fixed_hours;
   int               warmup_bars;          // closed M15 bars replayed into each engine at init
   bool              trade_enabled;        // false: engines and logs run, no order is ever sent
   bool              write_trades_csv;     // the mt5/tools/s004_parity.py input
   bool              log_to_common;
   AccountLimits     limits;
  };

// One symbol: its engine, its resting limit and the real position (if any)
// mirroring the engine's.
struct S004Slot
  {
   string            symbol;
   double            point;
   double            pip;
   datetime          last_bar;             // last closed M15 bar fed to the engine, SERVER clock
   ulong             limit_ticket;         // resting entry limit, 0 = none
   int               limit_zone;           // the zone that limit belongs to, -1 = none
   double            limit_price;
   ulong             position_ticket;      // real position mirroring the engine's, 0 = virtual
   datetime          position_day;         // session-clock day the entry was counted in
   string            position_status;      // S004_STATUS_* of the engine position now open
   double            position_fill;        // the real fill price, 0 when no order backed it
   double            position_lots;
  };

//+------------------------------------------------------------------+
//| The portfolio runtime.                                           |
//+------------------------------------------------------------------+
class CS004Runtime
  {
private:
   S004Settings      m_cfg;
   CJsonLog          m_log;
   CTradeOps         m_ops;
   CS004Engine      *m_engine[S004_MAX_SYMBOLS];
   S004Slot          m_slot[S004_MAX_SYMBOLS];
   int               m_count;
   datetime          m_day;                // current server day (00:00 of it)
   int               m_taken_today;        // REAL entries counted today, portfolio-wide
   bool              m_halted;
   string            m_halt_reason;
   datetime          m_last_heartbeat;

   //--- small helpers ---------------------------------------------------
   static datetime   DayOf(const datetime stamp)
     {
      return (datetime)(((long)stamp / CLOCK_SECONDS_PER_DAY) * CLOCK_SECONDS_PER_DAY);
     }

   // Broker clock -> the clock the strategy is defined on. Both hops are
   // DST-aware, so the Asia window stays put even in the weeks where the US
   // and the EU have already/not yet switched.
   datetime          ServerToClock(const datetime server_time) const
     {
      datetime utc=ClockLocalToUtc(m_cfg.server_rule,m_cfg.server_fixed_hours,server_time);
      return ClockUtcToLocal(S004_CLOCK_TZ_RULE,0,utc);
     }

   static bool       InSession(const datetime bar_time)
     {
      int hour=CS004Engine::HourOf(bar_time);
      return hour>=S004_SESSION_FIRST_HOUR && hour<=S004_SESSION_LAST_HOUR;
     }

   string            Label(const int i,const datetime bar_time) const
     {
      return StringFormat("%s-%s-%s",S004_MAGIC_PREFIX,m_slot[i].symbol,
                          TimeToString(bar_time,TIME_DATE|TIME_MINUTES));
     }

   double            SpreadPrice(const int i) const
     {
      double ask=SymbolInfoDouble(m_slot[i].symbol,SYMBOL_ASK);
      double bid=SymbolInfoDouble(m_slot[i].symbol,SYMBOL_BID);
      double spread=ask-bid;
      if(spread>0.0)
         return spread;
      return (double)SymbolInfoInteger(m_slot[i].symbol,SYMBOL_SPREAD)*m_slot[i].point;
     }

   void              RollDay(const datetime bar_time)
     {
      datetime day=DayOf(bar_time);
      if(day==m_day)
         return;
      m_day=day;
      m_taken_today=0;
     }

   //--- sizing ----------------------------------------------------------
   // Rule 2 (cost_inclusive_sizing): size on (stop distance + spread), so a
   // full stop costs exactly risk_pct of the account and the planned worst day
   // is exactly max_trades_per_day * risk_pct.
   double            LotsFor(const int i,const double entry,const double stop_loss) const
     {
      double distance=MathAbs(entry-stop_loss);
      if(S004_COST_INCLUSIVE_SIZING)
         distance+=SpreadPrice(i);
      if(distance<=0.0 || m_slot[i].point<=0.0)
         return 0.0;
      double risk_amount=AccountInfoDouble(ACCOUNT_BALANCE)*m_cfg.risk_pct/S004_PCT;
      double lots=SizingLotsForRisk(risk_amount,distance/m_slot[i].point,
                                    SizingMoneyPerPointPerLot(m_slot[i].symbol),
                                    SymbolInfoDouble(m_slot[i].symbol,SYMBOL_VOLUME_MIN));
      return SizingNormalizeVolumeForSymbol(m_slot[i].symbol,lots);
     }

   // OwnPosition carries no take profit, and the retarget check needs one.
   static double     PositionTakeProfit(const ulong ticket)
     {
      if(!PositionSelectByTicket(ticket))
         return 0.0;
      return PositionGetDouble(POSITION_TP);
     }

   //--- guards ----------------------------------------------------------
   bool              GuardBlocks(const string cycle)
     {
      if(m_halted)
         return true;
      if(!AccountGuardActive(m_cfg.limits))
         return false;
      datetime day_start=AccountGuardDayStartServer(m_cfg.limits,m_cfg.server_rule,
                                                    m_cfg.server_fixed_hours);
      GuardVerdict verdict;
      AccountGuardCheck(m_cfg.limits,AccountInfoDouble(ACCOUNT_BALANCE),
                        AccountGuardRealizedSince(day_start),0.0,0.0,verdict);
      if(verdict.allowed)
         return false;
      m_halted=true;
      m_halt_reason=verdict.reason;
      CJsonFields fields;
      fields.Str("reason",verdict.reason);
      fields.Num("equity",AccountInfoDouble(ACCOUNT_EQUITY));
      m_log.Event("account_guard_halt",cycle,fields.Body(),ALGO_LOG_ERROR);
      return true;
     }

   //--- resting limits ---------------------------------------------------
   void              CancelLimit(const int i,const string cycle,const string reason)
     {
      if(m_slot[i].limit_ticket==0)
         return;
      TradeResult result;
      m_ops.Init(m_slot[i].symbol,m_cfg.magic);
      m_ops.Cancel(m_slot[i].limit_ticket,result);
      CJsonFields request;
      request.Str("symbol",m_slot[i].symbol);
      request.Str("reason",reason);
      request.Int("ticket",(long)m_slot[i].limit_ticket);
      m_log.Order(Label(i,ServerToClock(m_slot[i].last_bar)),"cancel_limit",cycle,result.ok,
                  request.Body(),result.message,IntegerToString(result.retcode));
      m_slot[i].limit_ticket=0;
      m_slot[i].limit_zone=-1;
      m_slot[i].limit_price=0.0;
     }

   // The zone the engine will try next: active, alive, not in a trade, and with
   // a positive risk (the engine kills a zone whose entry is not beyond its own
   // stop). Returns the zone index or -1.
   int               NextZone(const int i) const
     {
      CS004Engine *engine=m_engine[i];
      for(int k=0; k<engine.ActiveCount(); k++)
        {
         int index=engine.ActiveZone(k);
         S004Zone zone;
         engine.GetZone(index,zone);
         if(zone.dead || zone.in_trade)
            continue;
         double entry=engine.NearEdgeOf(index);
         double stop=engine.StopFor(index);
         if((entry-stop)*zone.dir<=0.0)
            continue;
         return index;
        }
      return -1;
     }

   // Park (or re-park) the entry limit for the bar that is about to open.
   // `next_bar_server` is on the broker clock; the session test is not.
   void              ParkLimit(const int i,const datetime next_bar_server,const string cycle)
     {
      CS004Engine *engine=m_engine[i];
      datetime next_bar=ServerToClock(next_bar_server);
      bool wanted=m_cfg.trade_enabled && !m_halted && !engine.HasPosition()
                  && InSession(next_bar) && m_taken_today<m_cfg.max_trades_per_day;
      int index=wanted ? NextZone(i) : -1;
      if(index<0)
        {
         CancelLimit(i,cycle,wanted ? "no_zone" : "not_taken");
         return;
        }
      S004Zone zone;
      engine.GetZone(index,zone);
      double entry=SizingNormalizePrice(m_slot[i].symbol,engine.NearEdgeOf(index));
      double stop=SizingNormalizePrice(m_slot[i].symbol,engine.StopFor(index));
      double target=SizingNormalizePrice(m_slot[i].symbol,
                                         entry+zone.dir*S004_RR*(entry-stop)*zone.dir);
      if(m_slot[i].limit_ticket!=0 && m_slot[i].limit_zone==index
         && MathAbs(m_slot[i].limit_price-entry)<m_slot[i].point/2.0)
         return;                                  // already parked where it belongs
      CancelLimit(i,cycle,"reprice");
      double lots=LotsFor(i,entry,stop);
      if(lots<=0.0)
         return;
      TradeResult result;
      m_ops.Init(m_slot[i].symbol,m_cfg.magic);
      m_ops.PlaceLimit(zone.dir==S004_DIR_LONG,lots,entry,stop,target,
                       Label(i,next_bar),result);
      CJsonFields request;
      request.Str("symbol",m_slot[i].symbol);
      request.Str("side",zone.dir==S004_DIR_LONG ? "buy" : "sell");
      request.Num("price",entry,S004_PRICE_DIGITS);
      request.Num("sl",stop,S004_PRICE_DIGITS);
      request.Num("tp",target,S004_PRICE_DIGITS);
      request.Num("lots",lots);
      request.Int("zone",index);
      m_log.Order(Label(i,next_bar),"place_limit",cycle,result.ok,request.Body(),
                  result.message,IntegerToString(result.retcode));
      if(!result.ok)
         return;
      m_slot[i].limit_ticket=result.ticket;
      m_slot[i].limit_zone=index;
      m_slot[i].limit_price=entry;
     }

   //--- adopting / flattening what the limit produced ---------------------
   bool              FindPosition(const int i,OwnPosition &out) const
     {
      OwnPosition positions[];
      int found=TradeOpsCollectPositions(m_slot[i].symbol,m_cfg.magic,positions);
      if(found<=0)
         return false;
      out=positions[0];
      return true;
     }

   void              CloseReal(const int i,const string cycle,const string reason)
     {
      if(m_slot[i].position_ticket==0)
         return;
      TradeResult result;
      m_ops.Init(m_slot[i].symbol,m_cfg.magic);
      m_ops.Close(m_slot[i].position_ticket,result);
      CJsonFields request;
      request.Str("symbol",m_slot[i].symbol);
      request.Str("reason",reason);
      request.Int("ticket",(long)m_slot[i].position_ticket);
      m_log.Order(Label(i,ServerToClock(m_slot[i].last_bar)),"close_position",cycle,result.ok,
                  request.Body(),result.message,IntegerToString(result.retcode));
      if(result.ok)
         m_slot[i].position_ticket=0;
     }

   // The engine opened on this bar. Decide whether the backtest would have
   // TAKEN this trade, and make the broker agree with that decision.
   void              OnEngineOpen(const int i,const S004Bar &bar,const string cycle)
     {
      CS004Engine *engine=m_engine[i];
      S004Position pos;
      engine.GetPosition(pos);
      bool in_session=InSession(pos.time_in);
      bool has_slot=m_taken_today<m_cfg.max_trades_per_day;
      // What the BACKTEST would do -- deliberately free of trade_enabled, so a
      // shadow run (InpTradeEnabled=false) spends the daily cap exactly as a
      // live one does and mt5/tools/s004_parity.py can compare the two.
      bool taken=in_session && has_slot && !m_halted;
      OwnPosition live;
      bool filled=FindPosition(i,live);

      CJsonFields fields;
      fields.Str("symbol",m_slot[i].symbol);
      fields.Str("side",pos.dir==S004_DIR_LONG ? "buy" : "sell");
      fields.Num("engine_entry",pos.entry,S004_PRICE_DIGITS);
      fields.Num("sl",pos.sl,S004_PRICE_DIGITS);
      fields.Num("tp",pos.tp,S004_PRICE_DIGITS);
      fields.Int("hour",CS004Engine::HourOf(pos.time_in));
      fields.Bool("in_session",in_session);
      fields.Bool("taken",taken);
      fields.Int("taken_today",m_taken_today);
      fields.Bool("filled",filled);

      m_slot[i].limit_ticket=0;          // it either filled or is about to be re-parked
      m_slot[i].limit_zone=-1;
      m_slot[i].position_ticket=0;
      m_slot[i].position_fill=0.0;
      m_slot[i].position_lots=0.0;
      if(!taken)
        {
         // Virtual: the engine holds the symbol, the account does not. A limit
         // that filled anyway (the cap ran out while it rested) is flattened.
         m_slot[i].position_status=!in_session ? S004_STATUS_VIRTUAL_HOUR
                                   : (m_halted ? S004_STATUS_VIRTUAL_HALT : S004_STATUS_VIRTUAL_CAP);
         fields.Str("status",m_slot[i].position_status);
         if(filled)
           {
            m_slot[i].position_ticket=live.ticket;
            CloseReal(i,cycle,"not_taken");
           }
         m_log.Position(Label(i,pos.time_in),"open_virtual",cycle,fields.Body());
         return;
        }
      // The slot is spent the moment the backtest counts the trade, whether or
      // not an order ends up backing it: not counting a missed fill would let
      // the day take an entry the backtest marked as over the cap.
      m_taken_today++;
      m_slot[i].position_day=DayOf(pos.time_in);
      if(!m_cfg.trade_enabled)
        {
         m_slot[i].position_status=S004_STATUS_SHADOW;
         fields.Str("status",S004_STATUS_SHADOW);
         m_log.Position(Label(i,pos.time_in),"open_shadow",cycle,fields.Body());
         return;
        }
      if(!filled)
        {
         // The engine says the near edge was touched but no fill came back:
         // the limit was never parked (a restart mid-bar), or the broker
         // rejected it. Never chase with a market order -- the backtest's
         // price is gone; let this one be virtual and log it loudly.
         m_slot[i].position_status=S004_STATUS_MISSED_FILL;
         fields.Str("status",S004_STATUS_MISSED_FILL);
         m_log.Event("missed_fill",cycle,fields.Body(),ALGO_LOG_WARNING);
         return;
        }
      m_slot[i].position_ticket=live.ticket;
      m_slot[i].position_status=S004_STATUS_TAKEN;
      m_slot[i].position_fill=live.price_open;
      m_slot[i].position_lots=live.volume;
      // A gap through the limit fills better than the parked price, and the
      // engine derives both the stop and the target from the ACTUAL fill.
      double sl=SizingNormalizePrice(m_slot[i].symbol,pos.sl);
      double tp=SizingNormalizePrice(m_slot[i].symbol,pos.tp);
      if(MathAbs(live.stop_loss-sl)>=m_slot[i].point/2.0
         || MathAbs(PositionTakeProfit(live.ticket)-tp)>=m_slot[i].point/2.0)
        {
         TradeResult result;
         m_ops.Init(m_slot[i].symbol,m_cfg.magic);
         m_ops.Modify(live.ticket,sl,tp,result);
         CJsonFields request;
         request.Num("fill",live.price_open,S004_PRICE_DIGITS);
         request.Num("sl",sl,S004_PRICE_DIGITS);
         request.Num("tp",tp,S004_PRICE_DIGITS);
         m_log.Order(Label(i,pos.time_in),"retarget_fill",cycle,result.ok,
                     request.Body(),result.message,IntegerToString(result.retcode));
        }
      fields.Str("status",S004_STATUS_TAKEN);
      fields.Num("fill",live.price_open,S004_PRICE_DIGITS);
      fields.Num("lots",live.volume);
      m_log.Position(Label(i,pos.time_in),"open",cycle,fields.Body());
     }

   // The engine closed on this bar. SL and TP are attached to the position, so
   // the broker has usually done it already; the 22:45 cutoff never is.
   void              OnEngineClose(const int i,const S004Bar &bar,const string cycle)
     {
      S004Trade trade;
      m_engine[i].GetLastTrade(trade);
      CJsonFields fields;
      fields.Str("symbol",m_slot[i].symbol);
      fields.Str("reason",trade.exit_reason);
      fields.Num("exit",trade.exit,S004_PRICE_DIGITS);
      fields.Num("r",trade.r);
      fields.Str("status",m_slot[i].position_status);
      fields.Bool("virtual",m_slot[i].position_ticket==0);
      m_log.Position(Label(i,trade.time_in),"close",cycle,fields.Body());
      FlushTradeRow(i,trade);
      m_slot[i].position_status="";
      if(m_slot[i].position_ticket==0)
         return;
      OwnPosition live;
      if(FindPosition(i,live))
         CloseReal(i,cycle,trade.exit_reason);   // cutoff, or an SL/TP that did not trigger
      else
         m_slot[i].position_ticket=0;            // the broker closed it on the attached SL/TP
     }

   //--- parity input ------------------------------------------------------
   string            TradesCsvPath(void) const
     {
      return m_log.Dir()+"/"+m_cfg.strategy_name+S004_TRADES_CSV_SUFFIX;
     }

   // A tester pass must start from an empty file: the CSV is append-only (see
   // FlushTradeRow) and the common folder survives between passes, so a second
   // shadow run would otherwise hand s004_parity.py every trade twice.
   void              ResetTradesCsvForTester(void) const
     {
      if(!m_cfg.write_trades_csv || !MQLInfoInteger(MQL_TESTER))
         return;
      FileDelete(TradesCsvPath(),m_cfg.log_to_common ? FILE_COMMON : 0);
     }

   // One line per closed engine trade, append-only: a trade is final when it
   // closes, and the warmup replay is silent, so a restart cannot duplicate a
   // row the way S021's per-day file could.
   void              FlushTradeRow(const int i,const S004Trade &trade)
     {
      if(!m_cfg.write_trades_csv)
         return;
      int common=m_cfg.log_to_common ? FILE_COMMON : 0;
      string path=TradesCsvPath();
      bool fresh=!FileIsExist(path,common);
      int handle=FileOpen(path,FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ
                          |FILE_SHARE_WRITE|common);
      if(handle==INVALID_HANDLE)
         return;
      if(fresh)
         FileWriteString(handle,S004_TRADES_CSV_HEADER+"\n");
      FileSeek(handle,0,SEEK_END);
      string row=StringFormat("%s,%s,%s,%d,%s,%s,%s,%s,%s,%s,%d,%s,%s,%s,%I64u,%s,%s\n",
                              m_slot[i].symbol,
                              TimeToString(trade.time_in,TIME_DATE|TIME_MINUTES),
                              TimeToString(trade.time_out,TIME_DATE|TIME_MINUTES),
                              trade.dir,
                              DoubleToString(trade.entry,S004_CSV_PRICE_DIGITS),
                              DoubleToString(trade.sl,S004_CSV_PRICE_DIGITS),
                              DoubleToString(trade.tp,S004_CSV_PRICE_DIGITS),
                              DoubleToString(trade.exit,S004_CSV_PRICE_DIGITS),
                              DoubleToString(trade.r,S004_CSV_R_DIGITS),
                              trade.exit_reason,trade.hour,
                              m_slot[i].position_status,
                              DoubleToString(m_slot[i].position_fill,S004_CSV_PRICE_DIGITS),
                              DoubleToString(m_slot[i].position_lots,S004_CSV_LOT_DIGITS),
                              m_slot[i].position_ticket,
                              DoubleToString(m_engine[i].Pip(),S004_CSV_PRICE_DIGITS),
                              DoubleToString(m_engine[i].Cost(),S004_CSV_PRICE_DIGITS));
      FileWriteString(handle,row);
      FileClose(handle);
     }

   //--- bar feed ----------------------------------------------------------
   // The oldest bar any symbol still owes the engine; 0 when all are current.
   datetime          NextPendingBar(void) const
     {
      datetime oldest=0;
      for(int i=0; i<m_count; i++)
        {
         datetime next=NextBarFor(i);
         if(next==0)
            continue;
         if(oldest==0 || next<oldest)
            oldest=next;
        }
      return oldest;
     }

   datetime          NextBarFor(const int i) const
     {
      datetime closed=(datetime)iTime(m_slot[i].symbol,PERIOD_M15,1);
      if(closed==0 || m_slot[i].last_bar>=closed)
         return 0;
      if(m_slot[i].last_bar==0)
         return closed;
      datetime next=(datetime)(m_slot[i].last_bar+S004_M15_SECONDS);
      return (next<=closed) ? next : 0;
     }

   bool              LoadBar(const int i,const datetime bar_time,S004Bar &out) const
     {
      MqlRates rates[];
      if(CopyRates(m_slot[i].symbol,PERIOD_M15,bar_time,1,rates)<1)
         return false;
      if(rates[0].time!=bar_time)
         return false;                  // a hole in the feed: that bar never existed
      out.time=rates[0].time;
      out.open=rates[0].open;
      out.high=rates[0].high;
      out.low=rates[0].low;
      out.close=rates[0].close;
      return true;
     }

   void              FeedBar(const int i,const datetime bar_time,const string cycle)
     {
      S004Bar bar;
      if(!LoadBar(i,bar_time,bar))
        {
         m_slot[i].last_bar=bar_time;   // nothing traded on this symbol in that slot
         return;
        }
      m_slot[i].last_bar=bar.time;      // server clock: that is what iTime returns
      bar.time=ServerToClock(bar.time); // the engine runs on the session clock
      RollDay(bar.time);
      ENUM_S004_EVENT event=m_engine[i].Feed(bar);
      if(event==S004_EVENT_OPEN || event==S004_EVENT_OPEN_AND_CLOSE)
         OnEngineOpen(i,bar,cycle);
      if(event==S004_EVENT_CLOSE || event==S004_EVENT_OPEN_AND_CLOSE)
         OnEngineClose(i,bar,cycle);
     }

   void              Heartbeat(void)
     {
      datetime now=TimeTradeServer();
      if(now-m_last_heartbeat<S004_HEARTBEAT_SECONDS)
         return;
      m_last_heartbeat=now;
      CJsonFields fields;
      fields.Int("symbols",m_count);
      fields.Int("taken_today",m_taken_today);
      fields.Bool("halted",m_halted);
      fields.Num("equity",AccountInfoDouble(ACCOUNT_EQUITY));
      m_log.WriteStatus(S004_HEARTBEAT_FILE,fields.Body());
     }

public:
                     CS004Runtime(void)
     {
      m_count=0;
      m_day=0;
      m_taken_today=0;
      m_halted=false;
      m_halt_reason="";
      m_last_heartbeat=0;
      for(int i=0; i<S004_MAX_SYMBOLS; i++)
         m_engine[i]=NULL;
     }

                    ~CS004Runtime(void)
     {
      for(int i=0; i<S004_MAX_SYMBOLS; i++)
         if(m_engine[i]!=NULL)
           {
            delete m_engine[i];
            m_engine[i]=NULL;
           }
     }

   string            HaltReason(void) const { return m_halt_reason; }
   int               SymbolCount(void) const { return m_count; }
   int               TakenToday(void) const { return m_taken_today; }

   // Symbols come from Params.mqh (S004_SYMBOLS) and are sorted, because ties
   // on the same bar are resolved by symbol name in the backtest
   // (sort_values(["time_in", "symbol"])) and the daily cap depends on it.
   bool              Init(const S004Settings &settings)
     {
      m_cfg=settings;
      m_log.Init(S004_LOG_ROOT,m_cfg.strategy_name,m_cfg.log_to_common,
                 m_cfg.server_rule,m_cfg.server_fixed_hours);
      string names[];
      int count=StringSplit(S004_SYMBOLS,',',names);
      if(count<=0 || count>S004_MAX_SYMBOLS)
        {
         m_halted=true;
         m_halt_reason=StringFormat("S004_SYMBOLS has %d entries",count);
         return false;
        }
      ArraySort(names);
      string cycle=m_log.NewCycleId();
      int warmup=MathMax(m_cfg.warmup_bars,S004_MIN_WARMUP_BARS);
      for(int i=0; i<count; i++)
        {
         string symbol=names[i];
         if(!SymbolSelect(symbol,true))
           {
            CJsonFields fields;
            fields.Str("symbol",symbol);
            m_log.Event("symbol_unavailable",cycle,fields.Body(),ALGO_LOG_ERROR);
            m_halted=true;
            m_halt_reason="symbol unavailable: "+symbol;
            return false;
           }
         m_slot[i].symbol=symbol;
         m_slot[i].point=SymbolInfoDouble(symbol,SYMBOL_POINT);
         m_slot[i].pip=m_slot[i].point*S004_PIP_POINTS;
         m_slot[i].last_bar=0;
         m_slot[i].limit_ticket=0;
         m_slot[i].limit_zone=-1;
         m_slot[i].limit_price=0.0;
         m_slot[i].position_ticket=0;
         m_slot[i].position_day=0;
         m_slot[i].position_status="";
         m_slot[i].position_fill=0.0;
         m_slot[i].position_lots=0.0;
         m_engine[i]=new CS004Engine();
         m_engine[i].Configure(symbol,m_slot[i].pip,SpreadPrice(i));
        }
      m_count=count;
      ResetTradesCsvForTester();
      Warmup(warmup,cycle);
      AdoptPositions(cycle);
      CJsonFields fields;
      fields.Str("params",S004_PARAMS_SOURCE);
      fields.Int("symbols",m_count);
      fields.Int("warmup_bars",warmup);
      fields.Num("risk_pct",m_cfg.risk_pct);
      fields.Int("cap",m_cfg.max_trades_per_day);
      fields.Bool("trade_enabled",m_cfg.trade_enabled);
      fields.Str("trades_csv",m_cfg.write_trades_csv ? TradesCsvPath() : "");
      m_log.Event("init",cycle,fields.Body());
      return true;
     }

   // Replay history into every engine WITHOUT touching the account: zones, the
   // one-position lock and any position still open are all rebuilt from bars.
   void              Warmup(const int bars,const string cycle)
     {
      for(int i=0; i<m_count; i++)
        {
         MqlRates rates[];
         int copied=CopyRates(m_slot[i].symbol,PERIOD_M15,1,bars,rates);
         if(copied<=0)
           {
            CJsonFields fields;
            fields.Str("symbol",m_slot[i].symbol);
            m_log.Event("warmup_no_history",cycle,fields.Body(),ALGO_LOG_WARNING);
            continue;
           }
         for(int k=0; k<copied; k++)
           {
            S004Bar bar;
            bar.time=ServerToClock(rates[k].time);
            bar.open=rates[k].open;
            bar.high=rates[k].high;
            bar.low=rates[k].low;
            bar.close=rates[k].close;
            RollDay(bar.time);
            m_engine[i].Feed(bar);               // silent: no orders, no logs
            m_slot[i].last_bar=rates[k].time;
           }
        }
      m_taken_today=0;    // the replay's entries are history, not today's budget
     }

   // Re-adopt whatever this magic still holds, so a restart neither orphans a
   // position nor double-counts the day's budget.
   void              AdoptPositions(const string cycle)
     {
      for(int i=0; i<m_count; i++)
        {
         OwnPosition live;
         if(!FindPosition(i,live))
            continue;
         m_slot[i].position_ticket=live.ticket;
         m_slot[i].position_day=DayOf(ServerToClock(live.time_server));
         m_slot[i].position_status=S004_STATUS_TAKEN;
         m_slot[i].position_fill=live.price_open;
         m_slot[i].position_lots=live.volume;
         if(m_slot[i].position_day==m_day)
            m_taken_today++;
         CJsonFields fields;
         fields.Str("symbol",m_slot[i].symbol);
         fields.Int("ticket",(long)live.ticket);
         fields.Num("entry",live.price_open,S004_PRICE_DIGITS);
         fields.Bool("engine_agrees",m_engine[i].HasPosition());
         m_log.Event("adopt_position",cycle,fields.Body(),
                     m_engine[i].HasPosition() ? ALGO_LOG_INFO : ALGO_LOG_WARNING);
        }
      // Orders outlive a restart too; the next park cancels what it cannot use.
      for(int i=0; i<m_count; i++)
        {
         OwnOrder orders[];
         if(TradeOpsCollectLimitOrders(m_slot[i].symbol,m_cfg.magic,orders)>0)
           {
            m_slot[i].limit_ticket=orders[0].ticket;
            m_slot[i].limit_price=orders[0].price;
            m_slot[i].limit_zone=-1;             // unknown: forces a re-price
           }
        }
     }

   void              Deinit(const int reason)
     {
      CJsonFields fields;
      fields.Int("reason",reason);
      fields.Int("taken_today",m_taken_today);
      // Resting limits are deliberately LEFT in place: a chart reload or a
      // parameter change must not drop an entry the engine is still waiting
      // for; AdoptPositions picks them back up.
      m_log.Event("deinit",m_log.NewCycleId(),fields.Body());
     }

   // Drive everything. Idempotent and cheap when no bar has closed.
   void              Poll(void)
     {
      if(m_count<=0)
         return;
      Heartbeat();
      datetime bar_time=NextPendingBar();
      if(bar_time==0)
         return;
      string cycle=m_log.NewCycleId();
      GuardBlocks(cycle);
      while(bar_time!=0)
        {
         // all symbols for the same bar, in name order: that is the backtest's
         // tie-break (sort_values(["time_in", "symbol"])) and it decides which
         // symbol gets the last slot of the daily cap
         for(int i=0; i<m_count; i++)
            if(NextBarFor(i)==bar_time)
               FeedBar(i,bar_time,cycle);
         datetime next_bar=(datetime)(bar_time+S004_M15_SECONDS);
         for(int i=0; i<m_count; i++)
            if(m_slot[i].last_bar==bar_time)
               ParkLimit(i,next_bar,cycle);
         bar_time=NextPendingBar();
        }
     }
  };

#endif // STRATEGIES_S004_FVG_RUNTIME_MQH
//+------------------------------------------------------------------+
