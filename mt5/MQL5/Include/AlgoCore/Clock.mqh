//+------------------------------------------------------------------+
//| AlgoCore/Clock.mqh                                               |
//| Server time <-> UTC <-> fixed-offset strategy clocks.            |
//|                                                                  |
//| Every MT5 bar and TimeCurrent()/TimeTradeServer() reading is in  |
//| the BROKER SERVER's local time, which differs per broker and     |
//| usually switches with DST (most prop/FX servers run UTC+2 / +3,  |
//| switching together with the US so that server midnight is the NY |
//| 17:00 rollover). Strategies in this repo define their sessions on |
//| their own clocks (S021: America/New_York exchange local time,     |
//| TZ_EST_US_DST -- see strategies/orb_intraday/config.py), so all   |
//| strategy decisions are made on UTC-derived clocks, never on raw   |
//| server time.                                                      |
//|                                                                  |
//| The server's DST behaviour cannot be observed for PAST bars, and |
//| TimeGMT() is meaningless inside the Strategy Tester (it returns  |
//| server time there), so the server timezone is always configured  |
//| as an explicit rule (ENUM_TZ_RULE). Live, ClockObservedServer-   |
//| OffsetSeconds() lets the caller verify the rule against reality. |
//|                                                                  |
//| Python mirror (kept in sync, cross-checked against zoneinfo by   |
//| tests/mt5/test_clock.py): mt5/tools/clock.py                     |
//+------------------------------------------------------------------+
#ifndef ALGOCORE_CLOCK_MQH
#define ALGOCORE_CLOCK_MQH

#define CLOCK_SECONDS_PER_MINUTE   60
#define CLOCK_SECONDS_PER_HOUR     3600
#define CLOCK_SECONDS_PER_DAY      86400
#define CLOCK_MINUTES_PER_DAY      1440
#define CLOCK_OFFSET_ROUNDING_SEC  1800   // observed server offsets are rounded to 30 min

// DST transition instants, in UTC hours (rules valid from 2007 on).
#define CLOCK_US_DST_START_UTC_HOUR 7     // 2nd Sunday of March, 02:00 EST
#define CLOCK_US_DST_END_UTC_HOUR   6     // 1st Sunday of November, 02:00 EDT
#define CLOCK_EU_DST_UTC_HOUR       1     // last Sunday of March / October, 01:00 UTC

#define CLOCK_SUNDAY 0                    // MqlDateTime.day_of_week

enum ENUM_TZ_RULE
  {
   TZ_UTC        = 0, // UTC, no DST
   TZ_FIXED      = 1, // fixed offset (FixedHours parameter)
   TZ_EET_US_DST = 2, // UTC+2, UTC+3 while US DST (typical MT5 "NY close" server)
   TZ_EET_EU_DST = 3, // UTC+2, UTC+3 while EU DST (Europe/Athens-like)
   TZ_CET_EU_DST = 4, // UTC+1, UTC+2 while EU DST (Europe/Prague-like, FTMO day reset)
   TZ_EST_US_DST = 5  // UTC-5, UTC-4 while US DST (America/New_York exchange local time)
  };

//--- calendar helpers ------------------------------------------------
datetime ClockMakeTime(const int year,const int month,const int day,
                       const int hour,const int minute)
  {
   MqlDateTime parts;
   parts.year=year;
   parts.mon=month;
   parts.day=day;
   parts.hour=hour;
   parts.min=minute;
   parts.sec=0;
   parts.day_of_week=0;
   parts.day_of_year=0;
   return StructToTime(parts);
  }

int ClockDaysInMonth(const int year,const int month)
  {
   int next_year=(month==12) ? year+1 : year;
   int next_month=(month==12) ? 1 : month+1;
   datetime first_of_next=ClockMakeTime(next_year,next_month,1,0,0);
   MqlDateTime last;
   TimeToStruct(first_of_next-CLOCK_SECONDS_PER_DAY,last);
   return last.day;
  }

// Day-of-month of the nth (1-based) occurrence of `weekday` (0 = Sunday).
int ClockNthWeekday(const int year,const int month,const int weekday,const int nth)
  {
   MqlDateTime first;
   TimeToStruct(ClockMakeTime(year,month,1,0,0),first);
   int delta=(weekday-first.day_of_week+7)%7;
   return 1+delta+7*(nth-1);
  }

// Day-of-month of the last occurrence of `weekday` (0 = Sunday).
int ClockLastWeekday(const int year,const int month,const int weekday)
  {
   int days=ClockDaysInMonth(year,month);
   MqlDateTime last;
   TimeToStruct(ClockMakeTime(year,month,days,0,0),last);
   int delta=(last.day_of_week-weekday+7)%7;
   return days-delta;
  }

int ClockYearOf(const datetime t)
  {
   MqlDateTime parts;
   TimeToStruct(t,parts);
   return parts.year;
  }

//--- DST predicates (argument is a UTC instant) -----------------------
bool ClockIsUsDst(const datetime utc)
  {
   int year=ClockYearOf(utc);
   datetime start=ClockMakeTime(year,3,ClockNthWeekday(year,3,CLOCK_SUNDAY,2),
                                CLOCK_US_DST_START_UTC_HOUR,0);
   datetime stop=ClockMakeTime(year,11,ClockNthWeekday(year,11,CLOCK_SUNDAY,1),
                               CLOCK_US_DST_END_UTC_HOUR,0);
   return utc>=start && utc<stop;
  }

bool ClockIsEuDst(const datetime utc)
  {
   int year=ClockYearOf(utc);
   datetime start=ClockMakeTime(year,3,ClockLastWeekday(year,3,CLOCK_SUNDAY),
                                CLOCK_EU_DST_UTC_HOUR,0);
   datetime stop=ClockMakeTime(year,10,ClockLastWeekday(year,10,CLOCK_SUNDAY),
                               CLOCK_EU_DST_UTC_HOUR,0);
   return utc>=start && utc<stop;
  }

//--- rule -> offset ---------------------------------------------------
int ClockStandardOffsetSeconds(const ENUM_TZ_RULE rule,const int fixed_hours)
  {
   switch(rule)
     {
      case TZ_UTC:        return 0;
      case TZ_FIXED:      return fixed_hours*CLOCK_SECONDS_PER_HOUR;
      case TZ_EET_US_DST: return 2*CLOCK_SECONDS_PER_HOUR;
      case TZ_EET_EU_DST: return 2*CLOCK_SECONDS_PER_HOUR;
      case TZ_CET_EU_DST: return 1*CLOCK_SECONDS_PER_HOUR;
      case TZ_EST_US_DST: return -5*CLOCK_SECONDS_PER_HOUR;
     }
   return 0;
  }

// Offset (local - UTC) in seconds that `rule` has at the UTC instant `utc`.
int ClockOffsetSecondsAtUtc(const ENUM_TZ_RULE rule,const int fixed_hours,const datetime utc)
  {
   int standard=ClockStandardOffsetSeconds(rule,fixed_hours);
   switch(rule)
     {
      case TZ_EET_US_DST:
      case TZ_EST_US_DST:
         return ClockIsUsDst(utc) ? standard+CLOCK_SECONDS_PER_HOUR : standard;
      case TZ_EET_EU_DST:
      case TZ_CET_EU_DST:
         return ClockIsEuDst(utc) ? standard+CLOCK_SECONDS_PER_HOUR : standard;
      default:
         return standard;
     }
  }

datetime ClockUtcToLocal(const ENUM_TZ_RULE rule,const int fixed_hours,const datetime utc)
  {
   return utc+ClockOffsetSecondsAtUtc(rule,fixed_hours,utc);
  }

// Local wall time -> UTC. Unambiguous everywhere except the DST hour itself,
// which for every rule here falls on a weekend (no bars).
datetime ClockLocalToUtc(const ENUM_TZ_RULE rule,const int fixed_hours,const datetime local)
  {
   int guess=ClockOffsetSecondsAtUtc(rule,fixed_hours,
                                     local-ClockStandardOffsetSeconds(rule,fixed_hours));
   datetime utc=local-guess;
   int check=ClockOffsetSecondsAtUtc(rule,fixed_hours,utc);
   if(check!=guess)
      utc=local-check;
   return utc;
  }

//--- day / minute helpers on any clock ---------------------------------
datetime ClockDayStart(const datetime t)
  {
   return (datetime)((long)t-((long)t%CLOCK_SECONDS_PER_DAY));
  }

int ClockMinuteOfDay(const datetime t)
  {
   return (int)(((long)t%CLOCK_SECONDS_PER_DAY)/CLOCK_SECONDS_PER_MINUTE);
  }

// UTC instant of the most recent local midnight under `rule`.
datetime ClockLocalMidnightUtc(const ENUM_TZ_RULE rule,const int fixed_hours,const datetime utc)
  {
   datetime local=ClockUtcToLocal(rule,fixed_hours,utc);
   return ClockLocalToUtc(rule,fixed_hours,ClockDayStart(local));
  }

// "HH:MM" -> minute of day; -1 when empty or malformed.
int ClockParseHhMm(const string text)
  {
   string trimmed=text;
   StringTrimLeft(trimmed);
   StringTrimRight(trimmed);
   if(StringLen(trimmed)!=5 || StringGetCharacter(trimmed,2)!=':')
      return -1;
   int hours=(int)StringToInteger(StringSubstr(trimmed,0,2));
   int minutes=(int)StringToInteger(StringSubstr(trimmed,3,2));
   if(hours<0 || hours>23 || minutes<0 || minutes>59)
      return -1;
   return hours*60+minutes;
  }

// "YYYY-MM-DD" for a datetime on any clock.
string ClockIsoDate(const datetime t)
  {
   MqlDateTime parts;
   TimeToStruct(t,parts);
   return StringFormat("%04d-%02d-%02d",parts.year,parts.mon,parts.day);
  }

// "YYYY-MM-DDTHH:MM:SS" for a datetime on any clock.
string ClockIsoDateTime(const datetime t)
  {
   MqlDateTime parts;
   TimeToStruct(t,parts);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02d",
                       parts.year,parts.mon,parts.day,parts.hour,parts.min,parts.sec);
  }

//--- live-only: what the terminal currently reports ---------------------
// Server offset observed right now (live only -- TimeGMT() is not real
// inside the Strategy Tester). Rounded to CLOCK_OFFSET_ROUNDING_SEC.
int ClockObservedServerOffsetSeconds()
  {
   long diff=(long)TimeTradeServer()-(long)TimeGMT();
   long rounded=(long)MathRound((double)diff/CLOCK_OFFSET_ROUNDING_SEC)*CLOCK_OFFSET_ROUNDING_SEC;
   return (int)rounded;
  }

string ClockRuleName(const ENUM_TZ_RULE rule)
  {
   switch(rule)
     {
      case TZ_UTC:        return "UTC";
      case TZ_FIXED:      return "FIXED";
      case TZ_EET_US_DST: return "EET_US_DST";
      case TZ_EET_EU_DST: return "EET_EU_DST";
      case TZ_CET_EU_DST: return "CET_EU_DST";
     }
   return "UNKNOWN";
  }

#endif // ALGOCORE_CLOCK_MQH
//+------------------------------------------------------------------+
