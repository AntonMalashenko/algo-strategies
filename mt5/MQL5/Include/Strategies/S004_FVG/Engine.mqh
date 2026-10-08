//+------------------------------------------------------------------+
//| Strategies/S004_FVG/Engine.mqh                                   |
//| Bar-for-bar port of strategies/fvg_mtf.py::run_backtest for the  |
//| S004-intraday preset (ALGODEV-62 phase C).                       |
//|                                                                  |
//| This header is PURE: it owns no orders, reads no account and     |
//| calls nothing that touches the market. It is fed closed M15 bars |
//| and reproduces the Python engine's state machine exactly, so the |
//| parity tool can diff it against the backtest bar by bar.         |
//|                                                                  |
//| Scope -- the preset's frozen settings only (mode="base",          |
//| stop="zone", max_reentries=0, no partial/breakeven/time-stop/    |
//| trend/FTA option). Under those the Python code collapses a lot:  |
//|   * a zone arms and enters on the SAME bar, and a zone that      |
//|     cannot produce a valid risk dies, so the "armed" state never |
//|     survives a bar and the shift/fvg15/ob branches are dead code;|
//|   * after any exit the zone is consumed (_release with           |
//|     max_reentries=0 always kills it), so there is no wait_exit.  |
//| Zone state is therefore just {dir, top, bot, avail, dead,        |
//| in_trade}.                                                       |
//|                                                                  |
//| THE SHADOW-TRADE RULE. The backtest filters entry hours AFTER    |
//| the run (backtest/run_s004_intraday.py: trades[hour in           |
//| entry_hours]), so the engine also opens positions outside the    |
//| Asia window. Those trades are not part of S004's economics, but  |
//| they DO consume their zone and hold the one-position-per-symbol  |
//| lock until they exit. The EA therefore keeps them VIRTUAL: this  |
//| engine opens and closes them like any other trade, and the       |
//| runtime simply does not mirror them with a real order. Dropping  |
//| them instead would free the symbol earlier than the backtest     |
//| does and the live trade list would drift away from it.           |
//+------------------------------------------------------------------+
#ifndef STRATEGIES_S004_FVG_ENGINE_MQH
#define STRATEGIES_S004_FVG_ENGINE_MQH

#include <Strategies/S004_FVG/Params.mqh>

#define S004_H4_SECONDS      14400     // resample_h4("4h"), aligned to 00:00
#define S004_M15_SECONDS     900
#define S004_FVG_PATTERN_BARS 3        // gap between H4 bar i and bar i+2
#define S004_DIR_LONG        1
#define S004_DIR_SHORT      (-1)

// What the engine did on the bar it was just stepped over.
enum ENUM_S004_EVENT
  {
   S004_EVENT_NONE  = 0,
   S004_EVENT_OPEN  = 1,
   S004_EVENT_CLOSE = 2,
   S004_EVENT_OPEN_AND_CLOSE = 3    // opened and stopped/cut on the same bar
  };

// Exit reasons, spelled exactly like fvg_mtf.py's exit_reason so the parity
// tool can compare them as strings.
#define S004_EXIT_SL       "sl"
#define S004_EXIT_TP       "tp"
#define S004_EXIT_INTRADAY "intraday"

struct S004Bar
  {
   datetime          time;          // bar OPEN time, broker clock (= the Python index)
   double            open;
   double            high;
   double            low;
   double            close;
  };

struct S004Zone
  {
   int               dir;           // +1 bullish gap, -1 bearish
   double            top;
   double            bot;
   datetime          avail;         // close of the 3rd H4 bar: tradable from here
   bool              dead;
   bool              in_trade;
  };

struct S004Position
  {
   bool              active;
   int               dir;
   double            entry;
   double            sl;
   double            tp;
   double            risk;          // (entry - sl) * dir, price units
   double            r_denom;       // risk + cost: a full stop is exactly -1R
   datetime          time_in;
   int               zone;          // index into m_zones
   bool              opened_this_bar;
  };

struct S004Trade
  {
   datetime          time_in;
   datetime          time_out;
   int               dir;
   double            entry;
   double            sl;            // as opened (sl0 in Python)
   double            tp;
   double            exit;
   double            r;
   string            exit_reason;
   int               hour;          // entry hour on the broker clock
  };

//+------------------------------------------------------------------+
//| One symbol's engine state.                                       |
//+------------------------------------------------------------------+
class CS004Engine
  {
private:
   string            m_symbol;
   double            m_pip;          // price units per pip
   double            m_buffer;       // BUFFER_PIPS in price units
   double            m_cost;         // round-trip cost in price units (R denominator)
   double            m_shift;        // entry limit offset from the near edge, toward the bounce, price units

   S004Bar           m_h4[];         // completed H4 bars, oldest first
   S004Zone          m_zones[];      // every zone ever created, oldest first
   int               m_next_zone;    // next zone to activate (zones are avail-ordered)
   int               m_active[];     // indices into m_zones, activation order
   S004Position      m_pos;
   datetime          m_last_bar;     // last M15 bar time stepped over

   ENUM_S004_EVENT   m_event;
   S004Trade         m_last_trade;

   //--- H4 aggregation ------------------------------------------------
   static datetime   H4Bucket(const datetime bar_time)
     {
      return (datetime)(((long)bar_time / S004_H4_SECONDS) * S004_H4_SECONDS);
     }

   // Fold one M15 bar into the H4 series; returns true if a H4 bar CLOSED.
   bool              FoldIntoH4(const S004Bar &bar)
     {
      datetime bucket=H4Bucket(bar.time);
      int last=ArraySize(m_h4)-1;
      if(last>=0 && m_h4[last].time==bucket)
        {
         m_h4[last].high=MathMax(m_h4[last].high,bar.high);
         m_h4[last].low=MathMin(m_h4[last].low,bar.low);
         m_h4[last].close=bar.close;
         return false;
        }
      S004Bar h4;
      h4.time=bucket;
      h4.open=bar.open;
      h4.high=bar.high;
      h4.low=bar.low;
      h4.close=bar.close;
      ArrayResize(m_h4,last+2);
      m_h4[last+1]=h4;
      return last>=0;                 // the previous bucket is now complete
     }

   // fvg_mtf.find_h4_fvg on the last three completed H4 bars. The zone becomes
   // tradable at the close of the third bar, which is exactly now.
   void              DetectZone(void)
     {
      int n=ArraySize(m_h4);
      if(n<S004_FVG_PATTERN_BARS+1)
         return;
      int i=n-1-S004_FVG_PATTERN_BARS;   // the completed triple is i, i+1, i+2
      S004Zone zone;
      zone.dead=false;
      zone.in_trade=false;
      zone.avail=(datetime)(m_h4[i+2].time+S004_H4_SECONDS);
      if(m_h4[i+2].low>m_h4[i].high)
        {
         zone.dir=S004_DIR_LONG;
         zone.top=m_h4[i+2].low;
         zone.bot=m_h4[i].high;
        }
      else
         if(m_h4[i+2].high<m_h4[i].low)
           {
            zone.dir=S004_DIR_SHORT;
            zone.top=m_h4[i].low;
            zone.bot=m_h4[i+2].high;
           }
         else
            return;
      int count=ArraySize(m_zones);
      ArrayResize(m_zones,count+1);
      m_zones[count]=zone;
     }

   //--- position bookkeeping ------------------------------------------
   void              RecordExit(const datetime bar_time,const double exit_price,
                                const double pnl,const string reason)
     {
      m_last_trade.time_in=m_pos.time_in;
      m_last_trade.time_out=bar_time;
      m_last_trade.dir=m_pos.dir;
      m_last_trade.entry=m_pos.entry;
      m_last_trade.sl=m_pos.sl;
      m_last_trade.tp=m_pos.tp;
      m_last_trade.exit=exit_price;
      m_last_trade.r=pnl/m_pos.r_denom;
      m_last_trade.exit_reason=reason;
      m_last_trade.hour=HourOf(m_pos.time_in);
      m_zones[m_pos.zone].in_trade=false;
      m_zones[m_pos.zone].dead=true;   // max_reentries=0: a traded zone is consumed
      m_pos.active=false;
      m_event=(m_event==S004_EVENT_OPEN ? S004_EVENT_OPEN_AND_CLOSE : S004_EVENT_CLOSE);
     }

   // fvg_mtf._open for stop="zone": NaN-free, returns false when risk <= 0.
   bool              OpenAt(const int zone_index,const double entry,const datetime bar_time)
     {
      S004Zone zone=m_zones[zone_index];
      double sl=(zone.dir==S004_DIR_LONG) ? zone.bot-m_buffer : zone.top+m_buffer;
      double risk=(entry-sl)*zone.dir;
      if(risk<=0.0)
         return false;
      m_pos.active=true;
      m_pos.dir=zone.dir;
      m_pos.entry=entry;
      m_pos.sl=sl;
      m_pos.tp=entry+zone.dir*S004_RR*risk;
      m_pos.risk=risk;
      m_pos.r_denom=S004_COST_INCLUSIVE_SIZING ? risk+m_cost : risk;
      m_pos.time_in=bar_time;
      m_pos.zone=zone_index;
      m_pos.opened_this_bar=true;
      m_zones[zone_index].in_trade=true;
      m_event=S004_EVENT_OPEN;
      return true;
     }

   void              DropActive(void)
     {
      int kept=0;
      for(int i=0; i<ArraySize(m_active); i++)
         if(!m_zones[m_active[i]].dead)
            m_active[kept++]=m_active[i];
      ArrayResize(m_active,kept);
     }

public:
                     CS004Engine(void) { m_next_zone=0; m_last_bar=0; m_event=S004_EVENT_NONE; m_pos.active=false; }

   // `shift_pips` is fvg_mtf's entry_shift_pips: the limit sits that far from the
   // zone's near edge toward the bounce (0 = parked exactly on the edge).
   void              Configure(const string symbol,const double pip,const double cost_price,
                               const double shift_pips)
     {
      m_symbol=symbol;
      m_pip=pip;
      m_buffer=S004_BUFFER_PIPS*pip;
      m_cost=cost_price;
      m_shift=shift_pips*pip;
     }

   static int        HourOf(const datetime stamp)
     {
      MqlDateTime parts;
      TimeToStruct(stamp,parts);
      return parts.hour;
     }

   static int        MinuteOfDay(const datetime stamp)
     {
      MqlDateTime parts;
      TimeToStruct(stamp,parts);
      return parts.hour*60+parts.min;
     }

   //--- one closed M15 bar, exactly one iteration of the Python loop ---
   ENUM_S004_EVENT   Step(const S004Bar &bar)
     {
      m_event=S004_EVENT_NONE;
      m_pos.opened_this_bar=false;
      m_last_bar=bar.time;

      // 1. activate zones whose H4 bar has closed
      while(m_next_zone<ArraySize(m_zones) && m_zones[m_next_zone].avail<=bar.time)
        {
         int count=ArraySize(m_active);
         ArrayResize(m_active,count+1);
         m_active[count]=m_next_zone;
         m_next_zone++;
        }

      // 2. manage the open position -- pessimistic, SL before anything else
      if(m_pos.active)
        {
         ManageOpen(bar);
         return m_event;               // no new signals while managing
        }

      // 3. zone lifecycle and entries
      TryEnter(bar);
      DropActive();

      // 4. same-bar stop-out / cutoff for a position opened on this bar
      if(m_pos.active && m_pos.opened_this_bar)
         ManageOpenedThisBar(bar);
      return m_event;
     }

   void              ManageOpen(const S004Bar &bar)
     {
      int dir=m_pos.dir;
      bool hit_sl=(dir==S004_DIR_LONG) ? bar.low<=m_pos.sl : bar.high>=m_pos.sl;
      if(hit_sl)
        {
         RecordExit(bar.time,m_pos.sl,(m_pos.sl-m_pos.entry)*dir-m_cost,S004_EXIT_SL);
         return;
        }
      bool hit_tp=(dir==S004_DIR_LONG) ? bar.high>=m_pos.tp : bar.low<=m_pos.tp;
      if(hit_tp)
        {
         RecordExit(bar.time,m_pos.tp,(m_pos.tp-m_pos.entry)*dir-m_cost,S004_EXIT_TP);
         return;
        }
      if(MinuteOfDay(bar.time)>=S004_INTRADAY_CUTOFF_MINUTE)
         RecordExit(bar.time,bar.close,(bar.close-m_pos.entry)*dir-m_cost,S004_EXIT_INTRADAY);
     }

   void              ManageOpenedThisBar(const S004Bar &bar)
     {
      int dir=m_pos.dir;
      bool hit_sl=(dir==S004_DIR_LONG) ? bar.low<=m_pos.sl : bar.high>=m_pos.sl;
      if(hit_sl)
        {
         RecordExit(bar.time,m_pos.sl,(m_pos.sl-m_pos.entry)*dir-m_cost,S004_EXIT_SL);
         return;
        }
      if(MinuteOfDay(bar.time)>=S004_INTRADAY_CUTOFF_MINUTE)
         RecordExit(bar.time,bar.close,(bar.close-m_pos.entry)*dir-m_cost,S004_EXIT_INTRADAY);
     }

   void              TryEnter(const S004Bar &bar)
     {
      for(int i=0; i<ArraySize(m_active); i++)
        {
         int index=m_active[i];
         if(m_zones[index].dead || m_zones[index].in_trade)
            continue;
         int dir=m_zones[index].dir;
         double far_edge=(dir==S004_DIR_LONG) ? m_zones[index].bot : m_zones[index].top;
         if((dir==S004_DIR_LONG && bar.close<far_edge) || (dir==S004_DIR_SHORT && bar.close>far_edge))
           {
            m_zones[index].dead=true;  // closed beyond the far edge: invalidated
            continue;
           }
         double fill_edge=EntryEdgeOf(index);
         bool touched=(dir==S004_DIR_LONG) ? bar.low<=fill_edge : bar.high>=fill_edge;
         if(!touched)
            continue;
         // base mode: the limit fills at its price on the first touch, or at
         // the open when the bar gapped past it
         double entry=(dir==S004_DIR_LONG) ? MathMin(bar.open,fill_edge) : MathMax(bar.open,fill_edge);
         if(!OpenAt(index,entry,bar.time))
            m_zones[index].dead=true;  // no valid risk: the zone is consumed
         break;                        // one entry attempt per bar, like Python's break
        }
     }

   //--- accessors for the live layer -----------------------------------
   double            NearEdgeOf(const int zone_index) const
     {
      return (m_zones[zone_index].dir==S004_DIR_LONG) ? m_zones[zone_index].top : m_zones[zone_index].bot;
     }

   // The limit's price: the near edge moved `m_shift` toward the bounce (up for a
   // long, down for a short). This -- not NearEdgeOf -- is what the live layer parks.
   double            EntryEdgeOf(const int zone_index) const
     {
      return NearEdgeOf(zone_index)+m_zones[zone_index].dir*m_shift;
     }

   double            StopFor(const int zone_index) const
     {
      return (m_zones[zone_index].dir==S004_DIR_LONG)
             ? m_zones[zone_index].bot-m_buffer : m_zones[zone_index].top+m_buffer;
     }

   string            Symbol(void) const { return m_symbol; }
   // Pip and Cost are what the Python engine takes as `pip` and
   // `spread_pips * pip`: they move the buffered stop and the R denominator, so
   // mt5/tools/s004_parity.py re-runs the backtest with these exact values
   // instead of the modelled ones -- hence the columns in the trades CSV.
   double            Pip(void)    const { return m_pip; }
   double            Cost(void)   const { return m_cost; }
   double            Shift(void)  const { return m_shift; }
   datetime          LastBar(void)const { return m_last_bar; }
   bool              HasPosition(void) const { return m_pos.active; }
   bool              OpenedThisBar(void) const { return m_pos.opened_this_bar; }
   int               ActiveCount(void) const { return ArraySize(m_active); }
   int               ActiveZone(const int i) const { return m_active[i]; }
   int               ZoneCount(void) const { return ArraySize(m_zones); }
   int               H4Count(void) const { return ArraySize(m_h4); }
   void              GetZone(const int index,S004Zone &out) const { out=m_zones[index]; }
   void              GetPosition(S004Position &out) const { out=m_pos; }
   void              GetLastTrade(S004Trade &out) const { out=m_last_trade; }

   // Feed one closed M15 bar: fold it into H4 first (a H4 close can create a
   // zone that the SAME M15 bar then activates, exactly like the Python loop,
   // where zones carry avail = H4 close and activation is `avail <= bar_time`).
   ENUM_S004_EVENT   Feed(const S004Bar &bar)
     {
      if(FoldIntoH4(bar))
         DetectZone();
      return Step(bar);
     }
  };

#endif // STRATEGIES_S004_FVG_ENGINE_MQH
//+------------------------------------------------------------------+
