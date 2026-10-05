//+------------------------------------------------------------------+
//| Scripts/AlgoTrading/ImportM1CustomSymbol.mq5                     |
//| Builds an offline CUSTOM symbol from an M1 CSV, so any EA can be |
//| run in the Strategy Tester on our own data without a broker:     |
//|                                                                  |
//|   - histdata-derived bars (mt5/tools/s021_fixtures.py s021_m1.csv)|
//|   - a broker export (ExportM1.mq5 output) for reproducible runs  |
//|                                                                  |
//| Input CSV: <Common>/Files/<InpCsvPath>, header line first, then  |
//| time_server,open,high,low,close[,...] with "YYYY.MM.DD HH:MM"    |
//| times already in the server timezone the EA will be told about.  |
//| Contract: CFD, USD, $1 per 1.0 price move per 1.0 lot by default |
//| (InpContractSize), volume min/step 0.01.                         |
//+------------------------------------------------------------------+
#property copyright "AlgoTrading (Anton Malashenko)"
#property version   "1.00"
#property script_show_inputs

#define IMPORT_BATCH_BARS 100000

input string InpCsvPath      = "AlgoTrading/fixtures/s021_m1.csv"; // CSV under <Common>/Files
input string InpSymbol       = "NSXUSD_HD";                        // custom symbol name
input int    InpDigits       = 3;                                  // price digits
input double InpContractSize = 1.0;                                // $ per 1.0 move per lot
input double InpVolumeMin    = 0.01;
input double InpVolumeStep   = 0.01;
input double InpVolumeMax    = 100.0;
input int    InpSpreadPoints = 0;                                  // fixed spread, points

bool ConfigureSymbol(const string name)
  {
   if(!SymbolInfoInteger(name,SYMBOL_CUSTOM))
     {
      if(!CustomSymbolCreate(name,"AlgoTrading"))
        {
         PrintFormat("ImportM1: CustomSymbolCreate(%s) failed, error %d",name,GetLastError());
         return false;
        }
     }
   double point=MathPow(10.0,-InpDigits);
   bool ok=true;
   ok&=CustomSymbolSetInteger(name,SYMBOL_DIGITS,InpDigits);
   ok&=CustomSymbolSetDouble(name,SYMBOL_POINT,point);
   ok&=CustomSymbolSetDouble(name,SYMBOL_TRADE_TICK_SIZE,point);
   ok&=CustomSymbolSetDouble(name,SYMBOL_TRADE_CONTRACT_SIZE,InpContractSize);
   ok&=CustomSymbolSetDouble(name,SYMBOL_TRADE_TICK_VALUE,point*InpContractSize);
   ok&=CustomSymbolSetDouble(name,SYMBOL_TRADE_TICK_VALUE_PROFIT,point*InpContractSize);
   ok&=CustomSymbolSetDouble(name,SYMBOL_TRADE_TICK_VALUE_LOSS,point*InpContractSize);
   ok&=CustomSymbolSetDouble(name,SYMBOL_VOLUME_MIN,InpVolumeMin);
   ok&=CustomSymbolSetDouble(name,SYMBOL_VOLUME_STEP,InpVolumeStep);
   ok&=CustomSymbolSetDouble(name,SYMBOL_VOLUME_MAX,InpVolumeMax);
   ok&=CustomSymbolSetInteger(name,SYMBOL_TRADE_CALC_MODE,SYMBOL_CALC_MODE_CFD);
   ok&=CustomSymbolSetInteger(name,SYMBOL_TRADE_MODE,SYMBOL_TRADE_MODE_FULL);
   ok&=CustomSymbolSetInteger(name,SYMBOL_SPREAD,InpSpreadPoints);
   ok&=CustomSymbolSetInteger(name,SYMBOL_SPREAD_FLOAT,false);
   ok&=CustomSymbolSetInteger(name,SYMBOL_TRADE_STOPS_LEVEL,0);
   ok&=CustomSymbolSetString(name,SYMBOL_CURRENCY_BASE,"USD");
   ok&=CustomSymbolSetString(name,SYMBOL_CURRENCY_PROFIT,"USD");
   ok&=CustomSymbolSetString(name,SYMBOL_CURRENCY_MARGIN,"USD");
   if(!ok)
      PrintFormat("ImportM1: some symbol properties were rejected, error %d",GetLastError());
   return true;
  }

bool Flush(const string name,MqlRates &batch[],const int count)
  {
   if(count==0)
      return true;
   MqlRates chunk[];
   ArrayResize(chunk,count);
   for(int i=0;i<count;i++)
      chunk[i]=batch[i];
   int updated=CustomRatesUpdate(name,chunk);
   if(updated<0)
     {
      PrintFormat("ImportM1: CustomRatesUpdate failed, error %d",GetLastError());
      return false;
     }
   return true;
  }

void OnStart()
  {
   if(!ConfigureSymbol(InpSymbol))
      return;
   int handle=FileOpen(InpCsvPath,FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(handle==INVALID_HANDLE)
     {
      PrintFormat("ImportM1: cannot open <Common>/Files/%s (error %d)",InpCsvPath,GetLastError());
      return;
     }
   FileReadString(handle);                  // header
   MqlRates batch[];
   ArrayResize(batch,IMPORT_BATCH_BARS);
   int in_batch=0;
   long total=0;
   while(!FileIsEnding(handle))
     {
      string line=FileReadString(handle);
      string fields[];
      if(StringSplit(line,',',fields)<5)
         continue;
      MqlRates bar;
      bar.time=StringToTime(fields[0]);
      bar.open=StringToDouble(fields[1]);
      bar.high=StringToDouble(fields[2]);
      bar.low=StringToDouble(fields[3]);
      bar.close=StringToDouble(fields[4]);
      bar.tick_volume=1;
      bar.real_volume=0;
      bar.spread=InpSpreadPoints;
      batch[in_batch++]=bar;
      total++;
      if(in_batch==IMPORT_BATCH_BARS)
        {
         if(!Flush(InpSymbol,batch,in_batch))
            break;
         in_batch=0;
        }
     }
   Flush(InpSymbol,batch,in_batch);
   FileClose(handle);
   SymbolSelect(InpSymbol,true);
   PrintFormat("ImportM1: %I64d bars imported into custom symbol %s",total,InpSymbol);
  }
//+------------------------------------------------------------------+
