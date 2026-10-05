//+------------------------------------------------------------------+
//| AlgoCore/Sessions.mqh                                            |
//| Daily session statistics from M1 bars on a strategy clock, and   |
//| the causal average-session-range (ADR) with a data-gap guard.    |
//|                                                                  |
//| Port of strategies/orb_intraday/engine.py::compute_daily_sessions |
//| (+ _day_session) and ::compute_adr14 -- generic over the session  |
//| window, validity threshold and window length, so any strategy    |
//| with a fixed intraday session can reuse it.                      |
//|                                                                  |
//| Inputs are plain arrays (bar times already converted to the      |
//| strategy clock, oldest first), so the functions are pure and are |
//| exercised by Scripts/AlgoTrading/S021_SelfTest.mq5 on fixtures   |
//| generated from the Python engine.                                |
//+------------------------------------------------------------------+
#ifndef ALGOCORE_SESSIONS_MQH
#define ALGOCORE_SESSIONS_MQH

#include "Clock.mqh"

struct SessionWindow
  {
   int               open_minute;      // minute of day, inclusive (S021: 09:30 -> 570)
   int               close_minute;     // minute of day, inclusive (S021: 15:59 -> 959)
   int               min_bars;         // day invalid below this many in-session M1 bars
  };

struct DailySession
  {
   datetime          day;              // strategy-clock midnight
   double            open;             // open of the bar exactly at open_minute
   double            high;
   double            low;
   double            range;            // high - low over the session
   int               bars;
  };

// Appends one session row if [first, last] (inclusive bar indices of one
// clock day) forms a valid session -- engine.py::_day_session semantics:
// the first in-session bar must sit exactly on open_minute and there must
// be at least min_bars in-session bars.
bool SessionsEvaluateDay(const datetime &clock_times[],const double &opens[],
                         const double &highs[],const double &lows[],
                         const int first,const int last,const SessionWindow &window,
                         DailySession &session)
  {
   int count=0;
   bool anchored=false;
   double high=0.0;
   double low=0.0;
   double open=0.0;
   for(int i=first;i<=last;i++)
     {
      int minute=ClockMinuteOfDay(clock_times[i]);
      if(minute<window.open_minute || minute>window.close_minute)
         continue;
      if(count==0)
        {
         if(minute!=window.open_minute)
            return false;                 // session does not start on the anchor bar
         anchored=true;
         open=opens[i];
         high=highs[i];
         low=lows[i];
        }
      else
        {
         high=MathMax(high,highs[i]);
         low=MathMin(low,lows[i]);
        }
      count++;
     }
   if(!anchored || count<window.min_bars)
      return false;
   session.day=ClockDayStart(clock_times[first]);
   session.open=open;
   session.high=high;
   session.low=low;
   session.range=high-low;
   session.bars=count;
   return true;
  }

// All valid sessions in the bars, oldest first. Returns the count.
int SessionsBuild(const datetime &clock_times[],const double &opens[],
                  const double &highs[],const double &lows[],const int bar_count,
                  const SessionWindow &window,DailySession &out[])
  {
   ArrayResize(out,0);
   int found=0;
   int first=0;
   while(first<bar_count)
     {
      datetime day=ClockDayStart(clock_times[first]);
      int last=first;
      while(last+1<bar_count && ClockDayStart(clock_times[last+1])==day)
         last++;
      DailySession session;
      if(SessionsEvaluateDay(clock_times,opens,highs,lows,first,last,window,session))
        {
         ArrayResize(out,found+1);
         out[found]=session;
         found++;
        }
      first=last+1;
     }
   return found;
  }

// Causal average range for the day that FOLLOWS sessions[0..prior_count-1]
// (engine.py::compute_adr14 for the next row): the mean of the last
// `adr_window` ranges, rejected (false) when there are fewer than
// adr_window prior sessions or when those sessions span more than
// max_gap_days_per_session * adr_window calendar days.
bool SessionsAverageRange(const DailySession &sessions[],const int prior_count,
                          const int adr_window,const double max_gap_days_per_session,
                          double &average_range)
  {
   average_range=0.0;
   if(adr_window<=0 || prior_count<adr_window)
      return false;
   int first=prior_count-adr_window;
   int last=prior_count-1;
   long span_seconds=(long)sessions[last].day-(long)sessions[first].day;
   long span_days=span_seconds/CLOCK_SECONDS_PER_DAY;
   if((double)span_days>max_gap_days_per_session*adr_window)
      return false;
   double total=0.0;
   for(int i=first;i<=last;i++)
      total+=sessions[i].range;
   average_range=total/adr_window;
   return true;
  }

// Number of sessions strictly before `day` (sessions sorted oldest first).
int SessionsCountBefore(const DailySession &sessions[],const int count,const datetime day)
  {
   int prior=0;
   while(prior<count && sessions[prior].day<day)
      prior++;
   return prior;
  }

#endif // ALGOCORE_SESSIONS_MQH
//+------------------------------------------------------------------+
