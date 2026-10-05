//+------------------------------------------------------------------+
//| AlgoCore/JsonLog.mqh                                             |
//| Structured JSONL logging for MQL5 strategies, record-compatible  |
//| with the Python StrategyLogger (utils/trade_logger.py):          |
//|                                                                  |
//|   <root>/<strategy>/events-YYYY-MM-DD.jsonl   every event        |
//|   <root>/<strategy>/positions/<label>.jsonl   per-position life  |
//|                                                                  |
//| Every record carries ts (terminal-local ISO), ts_utc, strategy,  |
//| cycle and kind (events) or label+action (positions), plus the    |
//| caller's fields -- the same keys the Python logger writes, so a  |
//| reader/importer can treat both sources alike. `level` is added   |
//| only for WARNING/ERROR records (the Python logger keeps the      |
//| level in its text log only).                                     |
//|                                                                  |
//| Files are written under the terminal's Files sandbox (or the     |
//| shared Common/Files folder when `use_common` is true -- which is |
//| what makes Strategy Tester output easy to find: tester agents    |
//| each have their own sandbox, but share Common).                  |
//+------------------------------------------------------------------+
#ifndef ALGOCORE_JSONLOG_MQH
#define ALGOCORE_JSONLOG_MQH

#include "Clock.mqh"

#define JSONLOG_DEFAULT_DIGITS 6

enum ENUM_ALGO_LOG_LEVEL
  {
   ALGO_LOG_INFO    = 0,
   ALGO_LOG_WARNING = 1,
   ALGO_LOG_ERROR   = 2
  };

//--- JSON value helpers -------------------------------------------------
string JsonEscape(const string text)
  {
   string out=text;
   StringReplace(out,"\\","\\\\");
   StringReplace(out,"\"","\\\"");
   StringReplace(out,"\n","\\n");
   StringReplace(out,"\r","\\r");
   StringReplace(out,"\t","\\t");
   return out;
  }

string JsonNumber(const double value,const int digits=JSONLOG_DEFAULT_DIGITS)
  {
   if(!MathIsValidNumber(value))
      return "null";
   return DoubleToString(value,digits);
  }

// Builder for the body of a JSON object ("k":v,"k2":v2 -- no braces), so
// callers can pass a field set into the logger in one string.
class CJsonFields
  {
private:
   string            m_body;

   void              AppendRaw(const string key,const string raw_value)
     {
      if(StringLen(m_body)>0)
         m_body+=",";
      m_body+="\""+JsonEscape(key)+"\":"+raw_value;
     }

public:
                     CJsonFields(void) { m_body=""; }
   void              Clear(void)                                         { m_body=""; }
   void              Str(const string key,const string value)            { AppendRaw(key,"\""+JsonEscape(value)+"\""); }
   void              Num(const string key,const double value,const int digits=JSONLOG_DEFAULT_DIGITS)
                                                                         { AppendRaw(key,JsonNumber(value,digits)); }
   void              Int(const string key,const long value)              { AppendRaw(key,IntegerToString(value)); }
   void              Bool(const string key,const bool value)             { AppendRaw(key,value ? "true" : "false"); }
   void              Null(const string key)                              { AppendRaw(key,"null"); }
   void              Object(const string key,const string object_body)   { AppendRaw(key,"{"+object_body+"}"); }
   string            Body(void) const                                    { return m_body; }
  };

// Python's utils/trade_logger.py::_safe: runs of characters outside
// [A-Za-z0-9._-] collapse into one '_', leading/trailing '_' stripped.
string JsonLogSafeName(const string name)
  {
   string out="";
   bool last_was_sep=false;
   int length=StringLen(name);
   for(int i=0;i<length;i++)
     {
      ushort ch=StringGetCharacter(name,i);
      bool ok=(ch>='A' && ch<='Z') || (ch>='a' && ch<='z') || (ch>='0' && ch<='9')
              || ch=='.' || ch=='_' || ch=='-';
      if(ok)
        {
         out+=ShortToString(ch);
         last_was_sep=(ch=='_');
        }
      else
         if(!last_was_sep)
           {
            out+="_";
            last_was_sep=true;
           }
     }
   while(StringLen(out)>0 && StringGetCharacter(out,0)=='_')
      out=StringSubstr(out,1);
   while(StringLen(out)>0 && StringGetCharacter(out,StringLen(out)-1)=='_')
      out=StringSubstr(out,0,StringLen(out)-1);
   if(StringLen(out)==0)
      out="unnamed";
   return out;
  }

//--- logger ---------------------------------------------------------------
class CJsonLog
  {
private:
   string            m_strategy;
   string            m_dir;            // <root>/<strategy>
   bool              m_common;
   ENUM_TZ_RULE      m_server_rule;
   int               m_server_fixed_hours;
   long              m_cycle_seq;

   int               FileFlags(void) const
     {
      int flags=FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE;
      if(m_common)
         flags|=FILE_COMMON;
      return flags;
     }

   void              EnsureFolder(const string path) const
     {
      // FolderCreate is called on every prefix so nested folders work on
      // every terminal build; an already-existing folder is not an error.
      string parts[];
      int count=StringSplit(path,'/',parts);
      string prefix="";
      for(int i=0;i<count;i++)
        {
         if(StringLen(parts[i])==0)
            continue;
         prefix=(StringLen(prefix)==0) ? parts[i] : prefix+"/"+parts[i];
         FolderCreate(prefix,m_common ? FILE_COMMON : 0);
        }
     }

   bool              AppendLine(const string path,const string line) const
     {
      int handle=FileOpen(path,FileFlags());
      if(handle==INVALID_HANDLE)
        {
         PrintFormat("JsonLog: cannot open %s (error %d)",path,GetLastError());
         return false;
        }
      FileSeek(handle,0,SEEK_END);
      FileWriteString(handle,line+"\n");
      FileClose(handle);
      return true;
     }

   string            NowUtcIso(void) const
     {
      datetime utc=ClockLocalToUtc(m_server_rule,m_server_fixed_hours,TimeTradeServer());
      return ClockIsoDateTime(utc);
     }

   string            Head(const string cycle) const
     {
      string head="\"ts\":\""+ClockIsoDateTime(TimeLocal())+"\""
                  +",\"ts_utc\":\""+NowUtcIso()+"\""
                  +",\"strategy\":\""+JsonEscape(m_strategy)+"\""
                  +",\"cycle\":"+(StringLen(cycle)>0 ? "\""+JsonEscape(cycle)+"\"" : "null");
      return head;
     }

   string            LevelField(const ENUM_ALGO_LOG_LEVEL level) const
     {
      if(level==ALGO_LOG_WARNING)
         return ",\"level\":\"WARNING\"";
      if(level==ALGO_LOG_ERROR)
         return ",\"level\":\"ERROR\"";
      return "";
     }

   string            Tail(const string fields_body) const
     {
      return (StringLen(fields_body)>0) ? ","+fields_body : "";
     }

public:
                     CJsonLog(void)
     {
      m_strategy="";
      m_dir="";
      m_common=true;
      m_server_rule=TZ_UTC;
      m_server_fixed_hours=0;
      m_cycle_seq=0;
     }

   void              Init(const string root,const string strategy,const bool use_common,
                          const ENUM_TZ_RULE server_rule,const int server_fixed_hours)
     {
      m_strategy=strategy;
      m_dir=root+"/"+strategy;
      m_common=use_common;
      m_server_rule=server_rule;
      m_server_fixed_hours=server_fixed_hours;
      EnsureFolder(m_dir+"/positions");
     }

   string            Strategy(void) const { return m_strategy; }
   string            Dir(void) const      { return m_dir; }
   bool              UsesCommon(void) const { return m_common; }

   // Correlation id, same shape as the Python logger's: YYYYMMDD-HHMMSS-NNNN.
   string            NewCycleId(void)
     {
      m_cycle_seq++;
      MqlDateTime parts;
      TimeToStruct(TimeLocal(),parts);
      return StringFormat("%04d%02d%02d-%02d%02d%02d-%04d",parts.year,parts.mon,parts.day,
                          parts.hour,parts.min,parts.sec,(int)(m_cycle_seq%10000));
     }

   void              Event(const string kind,const string cycle,const string fields_body,
                           const ENUM_ALGO_LOG_LEVEL level=ALGO_LOG_INFO)
     {
      string line="{"+Head(cycle)+",\"kind\":\""+JsonEscape(kind)+"\""
                  +LevelField(level)+Tail(fields_body)+"}";
      AppendLine(m_dir+"/events-"+ClockIsoDate(TimeLocal())+".jsonl",line);
      string text=StringFormat("[%s] %s %s",m_strategy,kind,fields_body);
      if(level==ALGO_LOG_INFO)
         Print(text);
      else
         Print((level==ALGO_LOG_WARNING ? "WARNING " : "ERROR ")+text);
     }

   // One lifecycle record in positions/<label>.jsonl AND an event "position".
   void              Position(const string label,const string action,const string cycle,
                              const string fields_body,const ENUM_ALGO_LOG_LEVEL level=ALGO_LOG_INFO)
     {
      string record="{"+Head(cycle)+",\"label\":\""+JsonEscape(label)+"\""
                    +",\"action\":\""+JsonEscape(action)+"\""+LevelField(level)
                    +Tail(fields_body)+"}";
      AppendLine(m_dir+"/positions/"+JsonLogSafeName(label)+".jsonl",record);
      string event_body="\"label\":\""+JsonEscape(label)+"\",\"action\":\""+JsonEscape(action)+"\""
                        +Tail(fields_body);
      Event("position",cycle,event_body,level);
     }

   // A broker request with its outcome, per position (Python: logger.order).
   void              Order(const string label,const string op,const string cycle,const bool ok,
                           const string request_body,const string result,const string error)
     {
      CJsonFields fields;
      fields.Str("op",op);
      fields.Bool("ok",ok);
      fields.Object("request",request_body);
      if(StringLen(result)>0)
         fields.Str("result",result);
      else
         fields.Null("result");
      if(StringLen(error)>0)
         fields.Str("error",error);
      else
         fields.Null("error");
      string record="{"+Head(cycle)+",\"label\":\""+JsonEscape(label)+"\",\"action\":\"order\""
                    +(ok ? "" : LevelField(ALGO_LOG_ERROR))+","+fields.Body()+"}";
      AppendLine(m_dir+"/positions/"+JsonLogSafeName(label)+".jsonl",record);
      Event("order",cycle,"\"label\":\""+JsonEscape(label)+"\","+fields.Body(),
            ok ? ALGO_LOG_INFO : ALGO_LOG_ERROR);
     }

   // Overwrites <dir>/<name> with one line (heartbeat-style status files).
   void              WriteStatus(const string name,const string fields_body) const
     {
      int flags=FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ;
      if(m_common)
         flags|=FILE_COMMON;
      int handle=FileOpen(m_dir+"/"+name,flags);
      if(handle==INVALID_HANDLE)
         return;
      FileWriteString(handle,"{"+Head("")+Tail(fields_body)+"}\n");
      FileClose(handle);
     }
  };

#endif // ALGOCORE_JSONLOG_MQH
//+------------------------------------------------------------------+
