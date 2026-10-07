//+------------------------------------------------------------------+
//| Scripts/AlgoTrading/ExportM1.mq5                                 |
//| Exports the broker's own M1 bars to CSV (strategy-agnostic).     |
//| Used for parity checks: the Python backtest engine is re-run on  |
//| exactly the bars the EA saw (mt5/tools/s021_parity.py), and for  |
//| recording a broker's contract spec next to the data.             |
//|                                                                  |
//| Output: <Common>/Files/AlgoTrading/exports/<symbol>_M1_<server>  |
//|         .csv  + a matching _spec.json                            |
//| Times are written in SERVER time ("YYYY.MM.DD HH:MM"); the       |
//| Python side converts them with the same timezone rule the EA     |
//| uses (mt5/tools/clock.py).                                       |
//+------------------------------------------------------------------+
#property copyright "AlgoTrading (Anton Malashenko)"
#property version   "1.00"
#property script_show_inputs

#define EXPORT_DIR          "AlgoTrading/exports"
#define EXPORT_CHUNK_BARS   50000
#define EXPORT_SECONDS_PER_DAY 86400

input string InpSymbol = "";    // Symbol, or a comma-separated list (empty = chart symbol)
input int    InpDays   = 800;   // Calendar days back from now

string SafeName(const string text)
  {
   string out=text;
   StringReplace(out," ","_");
   StringReplace(out,"/","_");
   StringReplace(out,"\\","_");
   StringReplace(out,":","_");
   return out;
  }

bool ExportSymbol(const string symbol)
  {
   if(!SymbolSelect(symbol,true))
     {
      PrintFormat("ExportM1: symbol %s not available",symbol);
      return false;
     }
   datetime to_server=TimeTradeServer();
   datetime from_server=(datetime)((long)to_server-(long)InpDays*EXPORT_SECONDS_PER_DAY);
   string server=SafeName(AccountInfoString(ACCOUNT_SERVER));
   string base=EXPORT_DIR+"/"+SafeName(symbol)+"_M1_"+server;
   FolderCreate("AlgoTrading",FILE_COMMON);
   FolderCreate(EXPORT_DIR,FILE_COMMON);

   int handle=FileOpen(base+".csv",FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(handle==INVALID_HANDLE)
     {
      PrintFormat("ExportM1: cannot open output (error %d)",GetLastError());
      return false;
     }
   FileWriteString(handle,"time_server,open,high,low,close,tick_volume,spread\n");
   int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);

   // Walk BACKWARDS in fixed-size chunks (CopyRates by start time + count
   // returns the `count` bars at or before that time), so the export is not
   // capped by the terminal's "Max bars in chart" the way a single date-range
   // request is; then write everything oldest first.
   MqlRates all_rates[];
   int total=0;
   datetime chunk_to=to_server;
   while(true)
     {
      MqlRates rates[];
      int copied=CopyRates(symbol,PERIOD_M1,chunk_to,EXPORT_CHUNK_BARS,rates);
      if(copied<=0)
         break;
      int keep=0;
      for(int i=0;i<copied;i++)
         if(rates[i].time>=from_server)
            keep++;
      if(keep>0)
        {
         MqlRates merged[];
         ArrayResize(merged,keep+total);
         int first_kept=copied-keep;
         for(int i=0;i<keep;i++)
            merged[i]=rates[first_kept+i];
         for(int i=0;i<total;i++)
            merged[keep+i]=all_rates[i];
         ArraySwap(all_rates,merged);
         total+=keep;
        }
      if(keep<copied || copied<EXPORT_CHUNK_BARS)
         break;                               // reached from_server, or no older history
      chunk_to=rates[0].time-60;
     }
   long written=0;
   for(int i=0;i<total;i++)
     {
      FileWriteString(handle,StringFormat("%s,%s,%s,%s,%s,%I64d,%d\n",
                      TimeToString(all_rates[i].time,TIME_DATE|TIME_MINUTES),
                      DoubleToString(all_rates[i].open,digits),
                      DoubleToString(all_rates[i].high,digits),
                      DoubleToString(all_rates[i].low,digits),
                      DoubleToString(all_rates[i].close,digits),
                      all_rates[i].tick_volume,all_rates[i].spread));
      written++;
     }
   FileClose(handle);

   int spec=FileOpen(base+"_spec.json",FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(spec!=INVALID_HANDLE)
     {
      FileWriteString(spec,StringFormat(
         "{\"symbol\":\"%s\",\"server\":\"%s\",\"company\":\"%s\",\"bars\":%I64d,"
         "\"first_requested_server\":\"%s\",\"exported_at_server\":\"%s\","
         "\"digits\":%d,\"contract_size\":%.4f,\"tick_size\":%.8f,\"tick_value\":%.8f,"
         "\"volume_min\":%.4f,\"volume_step\":%.4f,\"volume_max\":%.4f,"
         "\"terminal_max_bars\":%d}\n",
         symbol,AccountInfoString(ACCOUNT_SERVER),AccountInfoString(ACCOUNT_COMPANY),written,
         TimeToString(from_server,TIME_DATE|TIME_MINUTES),
         TimeToString(to_server,TIME_DATE|TIME_MINUTES),digits,
         SymbolInfoDouble(symbol,SYMBOL_TRADE_CONTRACT_SIZE),
         SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE),
         SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE),
         SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN),
         SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP),
         SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX),
         (int)TerminalInfoInteger(TERMINAL_MAXBARS)));
      FileClose(spec);
     }
   PrintFormat("ExportM1: %I64d bars of %s written to <Common>/Files/%s.csv",written,symbol,base);
   return true;
  }

// A parity check needs every symbol the strategy trades, on the same export
// run; doing them one script launch at a time is seven chances to miss one.
void OnStart()
  {
   string list=(StringLen(InpSymbol)>0) ? InpSymbol : _Symbol;
   string symbols[];
   int count=StringSplit(list,',',symbols);
   if(count<=0)
     {
      Print("ExportM1: no symbol to export");
      return;
     }
   int done=0;
   for(int i=0;i<count;i++)
     {
      StringTrimLeft(symbols[i]);
      StringTrimRight(symbols[i]);
      if(StringLen(symbols[i])==0)
         continue;
      if(ExportSymbol(symbols[i]))
         done++;
     }
   PrintFormat("ExportM1: %d of %d symbols exported",done,count);
  }
//+------------------------------------------------------------------+
