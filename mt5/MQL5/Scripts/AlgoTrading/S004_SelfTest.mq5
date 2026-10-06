//+------------------------------------------------------------------+
//| Scripts/AlgoTrading/S004_SelfTest.mq5                            |
//| Self-test of the MQL5 engine port behind the S004 EA             |
//| (Strategies/S004_FVG/Engine.mqh) against the Python source of    |
//| truth: the SAME M15 bars are replayed through both engines and   |
//| the trade lists must match one for one.                          |
//|                                                                  |
//| Fixtures come from `python -m mt5.tools.s004_fixtures` into      |
//| <Common>/Files/AlgoTrading/fixtures/ (see mt5/README.md):        |
//|   s004_meta.csv    pip and round-trip cost in price units        |
//|   s004_m15.csv     the bars, already on the broker's EET clock   |
//|   s004_trades.csv  what strategies/fvg_mtf.py produced on them   |
//|                                                                  |
//| The expectation is the UNFILTERED engine output, shadow trades   |
//| outside the Asia window included -- those hold the one-position- |
//| per-symbol lock, so an engine that skipped them would drift away |
//| from the backtest (see Engine.mqh's header).                     |
//|                                                                  |
//| Run: drag onto any chart. Result: Experts log + a JSON summary   |
//| in <Common>/Files/AlgoTrading/selftest/S004_selftest.json.       |
//+------------------------------------------------------------------+
#property copyright "AlgoTrading (Anton Malashenko)"
#property version   "1.00"

#include <AlgoCore/Clock.mqh>
#include <Strategies/S004_FVG/Engine.mqh>

#define SELFTEST_FIXTURE_DIR   "AlgoTrading/fixtures/"
#define SELFTEST_RESULT_DIR    "AlgoTrading/selftest"
#define SELFTEST_RESULT_FILE   "AlgoTrading/selftest/S004_selftest.json"
#define SELFTEST_REL_TOLERANCE 1e-9
#define SELFTEST_ABS_TOLERANCE 1e-9
#define SELFTEST_MAX_REPORTED  20
#define SELFTEST_META_FIELDS   2
#define SELFTEST_BAR_FIELDS    6
#define SELFTEST_TRADE_FIELDS  11

int    g_passed=0;
int    g_failed=0;
int    g_reported=0;
string g_failures="";

// expected trades, in file order (grouped by symbol, then by entry time)
string   g_exp_symbol[];
datetime g_exp_time_in[];
datetime g_exp_time_out[];
int      g_exp_dir[];
double   g_exp_entry[];
double   g_exp_sl[];
double   g_exp_tp[];
double   g_exp_exit[];
double   g_exp_r[];
string   g_exp_reason[];
int      g_exp_hour[];
int      g_exp_count=0;
int      g_produced=0;

void Check(const bool condition,const string name)
  {
   if(condition)
     {
      g_passed++;
      return;
     }
   g_failed++;
   if(g_reported<SELFTEST_MAX_REPORTED)
     {
      g_reported++;
      Print("FAIL ",name);
      g_failures+=(StringLen(g_failures)>0 ? "," : "")+"\""+name+"\"";
     }
  }

bool Near(const double actual,const double expected)
  {
   double scale=MathMax(1.0,MathAbs(expected));
   return MathAbs(actual-expected)<=MathMax(SELFTEST_ABS_TOLERANCE,SELFTEST_REL_TOLERANCE*scale);
  }

int OpenFixture(const string name)
  {
   return FileOpen(SELFTEST_FIXTURE_DIR+name,FILE_READ|FILE_CSV|FILE_ANSI|FILE_COMMON,',');
  }

void SkipFields(const int handle,const int count)
  {
   for(int i=0; i<count && !FileIsEnding(handle); i++)
      FileReadString(handle);
  }

//--- fixtures ---------------------------------------------------------------
bool ReadMeta(double &pip,double &cost)
  {
   int handle=OpenFixture("s004_meta.csv");
   if(handle==INVALID_HANDLE)
      return false;
   pip=0.0;
   cost=-1.0;
   while(!FileIsEnding(handle))
     {
      string key=FileReadString(handle);
      if(FileIsEnding(handle) && StringLen(key)==0)
         break;
      string value=FileReadString(handle);
      if(key=="pip")
         pip=StringToDouble(value);
      if(key=="cost_price")
         cost=StringToDouble(value);
     }
   FileClose(handle);
   return pip>0.0 && cost>=0.0;
  }

bool ReadExpectedTrades(void)
  {
   int handle=OpenFixture("s004_trades.csv");
   if(handle==INVALID_HANDLE)
      return false;
   SkipFields(handle,SELFTEST_TRADE_FIELDS);          // header
   while(!FileIsEnding(handle))
     {
      string symbol=FileReadString(handle);
      if(StringLen(symbol)==0)
         break;
      int i=g_exp_count;
      ArrayResize(g_exp_symbol,i+1);   ArrayResize(g_exp_time_in,i+1);
      ArrayResize(g_exp_time_out,i+1); ArrayResize(g_exp_dir,i+1);
      ArrayResize(g_exp_entry,i+1);    ArrayResize(g_exp_sl,i+1);
      ArrayResize(g_exp_tp,i+1);       ArrayResize(g_exp_exit,i+1);
      ArrayResize(g_exp_r,i+1);        ArrayResize(g_exp_reason,i+1);
      ArrayResize(g_exp_hour,i+1);
      g_exp_symbol[i]=symbol;
      g_exp_time_in[i]=StringToTime(FileReadString(handle));
      g_exp_time_out[i]=StringToTime(FileReadString(handle));
      g_exp_dir[i]=(int)StringToInteger(FileReadString(handle));
      g_exp_entry[i]=StringToDouble(FileReadString(handle));
      g_exp_sl[i]=StringToDouble(FileReadString(handle));
      g_exp_tp[i]=StringToDouble(FileReadString(handle));
      g_exp_exit[i]=StringToDouble(FileReadString(handle));
      g_exp_r[i]=StringToDouble(FileReadString(handle));
      g_exp_reason[i]=FileReadString(handle);
      g_exp_hour[i]=(int)StringToInteger(FileReadString(handle));
      g_exp_count++;
     }
   FileClose(handle);
   return g_exp_count>0;
  }

//--- comparison -------------------------------------------------------------
void CompareTrade(const string symbol,const S004Trade &trade)
  {
   int i=g_produced;
   g_produced++;
   if(i>=g_exp_count)
     {
      Check(false,StringFormat("trade.%d.extra_%s_%s",i,symbol,TimeToString(trade.time_in)));
      return;
     }
   string tag=StringFormat("trade.%d.%s.%s",i,g_exp_symbol[i],
                           TimeToString(g_exp_time_in[i],TIME_DATE|TIME_MINUTES));
   Check(symbol==g_exp_symbol[i],tag+".symbol");
   Check(trade.time_in==g_exp_time_in[i],tag+".time_in");
   Check(trade.time_out==g_exp_time_out[i],tag+".time_out");
   Check(trade.dir==g_exp_dir[i],tag+".dir");
   Check(Near(trade.entry,g_exp_entry[i]),tag+".entry");
   Check(Near(trade.sl,g_exp_sl[i]),tag+".sl");
   Check(Near(trade.tp,g_exp_tp[i]),tag+".tp");
   Check(Near(trade.exit,g_exp_exit[i]),tag+".exit");
   Check(Near(trade.r,g_exp_r[i]),tag+".r");
   Check(trade.exit_reason==g_exp_reason[i],tag+".exit_reason");
   Check(trade.hour==g_exp_hour[i],tag+".hour");
  }

// Replay s004_m15.csv. The file is grouped by symbol, so a symbol change means
// "start a fresh engine", exactly like a separate run_backtest call in Python.
bool ReplayBars(const double pip,const double cost)
  {
   int handle=OpenFixture("s004_m15.csv");
   if(handle==INVALID_HANDLE)
      return false;
   SkipFields(handle,SELFTEST_BAR_FIELDS);            // header
   CS004Engine *engine=NULL;
   string current="";
   int bars=0;
   while(!FileIsEnding(handle))
     {
      string symbol=FileReadString(handle);
      if(StringLen(symbol)==0)
         break;
      S004Bar bar;
      bar.time=StringToTime(FileReadString(handle));
      bar.open=StringToDouble(FileReadString(handle));
      bar.high=StringToDouble(FileReadString(handle));
      bar.low=StringToDouble(FileReadString(handle));
      bar.close=StringToDouble(FileReadString(handle));
      if(symbol!=current)
        {
         if(engine!=NULL)
            delete engine;
         engine=new CS004Engine();
         engine.Configure(symbol,pip,cost);
         current=symbol;
        }
      ENUM_S004_EVENT event=engine.Feed(bar);
      if(event==S004_EVENT_CLOSE || event==S004_EVENT_OPEN_AND_CLOSE)
        {
         S004Trade trade;
         engine.GetLastTrade(trade);
         CompareTrade(symbol,trade);
        }
      bars++;
     }
   if(engine!=NULL)
      delete engine;
   FileClose(handle);
   PrintFormat("replayed %d bars, %d trades produced, %d expected",bars,g_produced,g_exp_count);
   return bars>0;
  }

//--- fixed cases (no fixture needed) ----------------------------------------
void TestGeneratedParams()
  {
   Check(S004_SESSION_FIRST_HOUR==0 && S004_SESSION_LAST_HOUR==6,"params.session_hours");
   Check(S004_INTRADAY_CUTOFF_MINUTE==22*60+45,"params.cutoff_minute");
   Check(S004_DEFAULT_MAX_TRADES_PER_DAY==2,"params.daily_cap");
   Check(S004_COST_INCLUSIVE_SIZING,"params.cost_inclusive_sizing");
   Check(S004_SYMBOL_COUNT==7,"params.symbol_count");
   Check(S004_CLOCK_TZ_RULE==TZ_EET_EU_DST,"params.clock_rule_is_european");
  }

void TestEngineBasics()
  {
   Check(CS004Engine::MinuteOfDay(D'2025.07.15 22:45')==S004_INTRADAY_CUTOFF_MINUTE,"engine.minute_of_day");
   Check(CS004Engine::HourOf(D'2025.07.15 06:59')==6,"engine.hour_of");
   // A full stop is exactly -1R only because R is measured on (stop + cost);
   // this is prop rule 2 and the self-test would catch its removal.
   Check(S004_COST_INCLUSIVE_SIZING,"engine.full_stop_is_minus_one_r");
  }

void WriteSummary()
  {
   if(!FolderCreate(SELFTEST_RESULT_DIR,FILE_COMMON))
      Print("note: could not create ",SELFTEST_RESULT_DIR);
   int handle=FileOpen(SELFTEST_RESULT_FILE,FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(handle==INVALID_HANDLE)
     {
      Print("could not write ",SELFTEST_RESULT_FILE);
      return;
     }
   FileWriteString(handle,StringFormat("{\"passed\":%d,\"failed\":%d,\"trades\":%d,"
                                       "\"expected\":%d,\"failures\":[%s]}\n",
                                       g_passed,g_failed,g_produced,g_exp_count,g_failures));
   FileClose(handle);
  }

void OnStart()
  {
   TestGeneratedParams();
   TestEngineBasics();

   double pip=0.0,cost=0.0;
   if(!ReadMeta(pip,cost))
      Check(false,"fixtures.meta_missing");
   else
     {
      if(!ReadExpectedTrades())
         Check(false,"fixtures.trades_missing");
      else
        {
         if(!ReplayBars(pip,cost))
            Check(false,"fixtures.bars_missing");
         else
            Check(g_produced==g_exp_count,"engine.trade_count");
        }
     }

   PrintFormat("S004 self-test: %d passed, %d failed",g_passed,g_failed);
   WriteSummary();
  }
//+------------------------------------------------------------------+
