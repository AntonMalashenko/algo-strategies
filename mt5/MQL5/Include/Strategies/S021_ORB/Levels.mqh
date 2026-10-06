//+------------------------------------------------------------------+
//| Strategies/S021_ORB/Levels.mqh                                   |
//| S021 (ORB, Nasdaq 100) daily levels -- pure functions.           |
//|                                                                  |
//| Mirrors bot/orb_signals.py::_levels_for_today on top of the      |
//| backtest engine's session/ADR math (AlgoCore/Sessions.mqh):      |
//|   ADR14 = mean session range of the 14 previous valid sessions   |
//|   U/L   = O +/- k_range * ADR14                                  |
//|   stop  = stop_adr_mult * ADR14 from each order's own price      |
//| All constants come from the generated Params.mqh (ORB_BASE).     |
//+------------------------------------------------------------------+
#ifndef S021_LEVELS_MQH
#define S021_LEVELS_MQH

#include "Params.mqh"
#include <AlgoCore/Clock.mqh>
#include <AlgoCore/Sessions.mqh>

struct S021Levels
  {
   datetime          day;              // strategy-clock (NY exchange local) midnight
   double            open_price;       // O: open of the 09:30 NY M1 bar
   double            upper;            // U
   double            lower;            // L
   double            adr;              // ADR14
   double            stop_distance;    // stop_adr_mult * ADR14
   int               sessions_used;    // valid prior sessions available
  };

SessionWindow S021Window()
  {
   SessionWindow window;
   window.open_minute=S021_SESSION_OPEN_MINUTE;
   window.close_minute=S021_SESSION_CLOSE_MINUTE;
   window.min_bars=S021_MIN_SESSION_BARS;
   return window;
  }

// The strategy clock is the exchange's own local time, DST-aware -- see
// Params.mqh (S021_CLOCK_TZ_RULE) and engine.py's module docstring for the
// measurements that established it. Anchoring on a fixed UTC-5 clock instead
// (as this EA did before ALGODEV-61) enters one hour late every US summer.
datetime S021UtcToClock(const datetime utc)
  {
   return ClockUtcToLocal(S021_CLOCK_TZ_RULE,0,utc);
  }

datetime S021ClockToUtc(const datetime clock_time)
  {
   return ClockLocalToUtc(S021_CLOCK_TZ_RULE,0,clock_time);
  }

// Levels for `day`, given every valid session known (any order of days is
// fine as long as `sessions` is sorted oldest first) and today's anchor
// open. Only sessions STRICTLY before `day` are used -- the look-ahead guard
// lives here, like _valid_prior_sessions in the Python bot. Returns false
// when ADR14 is not computable (too few sessions, or the gap guard).
bool S021ComputeLevels(const DailySession &sessions[],const int session_count,
                       const datetime day,const double open_price,S021Levels &levels)
  {
   int prior=SessionsCountBefore(sessions,session_count,day);
   levels.day=day;
   levels.open_price=open_price;
   levels.sessions_used=prior;
   levels.adr=0.0;
   levels.upper=0.0;
   levels.lower=0.0;
   levels.stop_distance=0.0;
   double adr=0.0;
   if(!SessionsAverageRange(sessions,prior,S021_ADR_WINDOW,S021_MAX_GAP_DAYS_PER_SESSION,adr))
      return false;
   levels.adr=adr;
   levels.upper=open_price+S021_K_RANGE*adr;
   levels.lower=open_price-S021_K_RANGE*adr;
   levels.stop_distance=S021_STOP_ADR_MULT*adr;
   return true;
  }

// Day labels shared with the Python bot (bot/orb_signals.py::_today_label):
// "S021:YYYY-MM-DD:long|short", so per-position log files line up.
string S021Label(const datetime day,const bool is_long)
  {
   return S021_MAGIC_PREFIX+":"+ClockIsoDate(day)+":"+(is_long ? "long" : "short");
  }

// Broker order comment (<= 31 chars): "S021|YYYY-MM-DD|L|S".
string S021OrderCommentPrefix(const datetime day)
  {
   return S021_MAGIC_PREFIX+"|"+ClockIsoDate(day)+"|";
  }

string S021OrderComment(const datetime day,const bool is_long)
  {
   return S021OrderCommentPrefix(day)+(is_long ? "L" : "S");
  }

#endif // S021_LEVELS_MQH
//+------------------------------------------------------------------+
