//+------------------------------------------------------------------+
//| AlgoCore/TradeOps.mqh                                            |
//| Thin, strategy-agnostic trade plumbing over CTrade: snapshot a   |
//| strategy's own positions/orders (by magic + symbol), place a     |
//| resting stop order with an attached SL, cancel, close. Each call |
//| returns a TradeResult the caller logs as-is.                     |
//+------------------------------------------------------------------+
#ifndef ALGOCORE_TRADEOPS_MQH
#define ALGOCORE_TRADEOPS_MQH

#include <Trade/Trade.mqh>
#include "Clock.mqh"

#define TRADEOPS_DEVIATION_POINTS 50   // max slippage for market closes, in _Point units
#define TRADEOPS_KIND_STOP        1    // bit flags for the pending-order collectors:
#define TRADEOPS_KIND_LIMIT       2    // S021 rests on stops, S004 rests on limits

struct OwnPosition
  {
   ulong             ticket;
   bool              is_buy;
   double            price_open;
   double            stop_loss;
   double            volume;
   datetime          time_server;      // POSITION_TIME
   string            comment;
  };

struct OwnOrder
  {
   ulong             ticket;
   bool              is_buy;
   double            price;
   double            stop_loss;
   double            take_profit;
   double            volume;
   datetime          setup_server;     // ORDER_TIME_SETUP
   string            comment;
  };

struct TradeResult
  {
   bool              ok;
   uint              retcode;
   ulong             ticket;           // order ticket (place) or deal ticket (close)
   double            price;
   string            message;
  };

void TradeOpsResetResult(TradeResult &result)
  {
   result.ok=false;
   result.retcode=0;
   result.ticket=0;
   result.price=0.0;
   result.message="";
  }

int TradeOpsCollectPositions(const string symbol,const long magic,OwnPosition &out[])
  {
   ArrayResize(out,0);
   int found=0;
   int total=PositionsTotal();
   for(int i=0;i<total;i++)
     {
      ulong ticket=PositionGetTicket(i);
      if(ticket==0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL)!=symbol || PositionGetInteger(POSITION_MAGIC)!=magic)
         continue;
      OwnPosition position;
      position.ticket=ticket;
      position.is_buy=(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY);
      position.price_open=PositionGetDouble(POSITION_PRICE_OPEN);
      position.stop_loss=PositionGetDouble(POSITION_SL);
      position.volume=PositionGetDouble(POSITION_VOLUME);
      position.time_server=(datetime)PositionGetInteger(POSITION_TIME);
      position.comment=PositionGetString(POSITION_COMMENT);
      ArrayResize(out,found+1);
      out[found]=position;
      found++;
     }
   return found;
  }

bool TradeOpsOrderTypeMatches(const long order_type,const int kinds)
  {
   if((kinds & TRADEOPS_KIND_STOP)!=0
      && (order_type==ORDER_TYPE_BUY_STOP || order_type==ORDER_TYPE_SELL_STOP))
      return true;
   if((kinds & TRADEOPS_KIND_LIMIT)!=0
      && (order_type==ORDER_TYPE_BUY_LIMIT || order_type==ORDER_TYPE_SELL_LIMIT))
      return true;
   return false;
  }

bool TradeOpsOrderIsBuy(const long order_type)
  {
   return order_type==ORDER_TYPE_BUY_STOP || order_type==ORDER_TYPE_BUY_LIMIT;
  }

int TradeOpsCollectOrders(const string symbol,const long magic,const int kinds,OwnOrder &out[])
  {
   ArrayResize(out,0);
   int found=0;
   int total=OrdersTotal();
   for(int i=0;i<total;i++)
     {
      ulong ticket=OrderGetTicket(i);
      if(ticket==0 || !OrderSelect(ticket))
         continue;
      if(OrderGetString(ORDER_SYMBOL)!=symbol || OrderGetInteger(ORDER_MAGIC)!=magic)
         continue;
      long order_type=OrderGetInteger(ORDER_TYPE);
      if(!TradeOpsOrderTypeMatches(order_type,kinds))
         continue;
      OwnOrder order;
      order.ticket=ticket;
      order.is_buy=TradeOpsOrderIsBuy(order_type);
      order.price=OrderGetDouble(ORDER_PRICE_OPEN);
      order.stop_loss=OrderGetDouble(ORDER_SL);
      order.take_profit=OrderGetDouble(ORDER_TP);
      order.volume=OrderGetDouble(ORDER_VOLUME_CURRENT);
      order.setup_server=(datetime)OrderGetInteger(ORDER_TIME_SETUP);
      order.comment=OrderGetString(ORDER_COMMENT);
      ArrayResize(out,found+1);
      out[found]=order;
      found++;
     }
   return found;
  }

int TradeOpsCollectStopOrders(const string symbol,const long magic,OwnOrder &out[])
  {
   return TradeOpsCollectOrders(symbol,magic,TRADEOPS_KIND_STOP,out);
  }

int TradeOpsCollectLimitOrders(const string symbol,const long magic,OwnOrder &out[])
  {
   return TradeOpsCollectOrders(symbol,magic,TRADEOPS_KIND_LIMIT,out);
  }

// True if the history since `from_server` holds any stop order of ours whose
// comment starts with `comment_prefix` (placed earlier, then filled or
// cancelled) -- the "today already handled" check that survives restarts.
bool TradeOpsHistoryHasOrder(const string symbol,const long magic,const string comment_prefix,
                             const datetime from_server,const int kinds=TRADEOPS_KIND_STOP)
  {
   if(!HistorySelect(from_server,TimeTradeServer()+CLOCK_SECONDS_PER_DAY))
      return false;
   int total=HistoryOrdersTotal();
   for(int i=0;i<total;i++)
     {
      ulong ticket=HistoryOrderGetTicket(i);
      if(ticket==0)
         continue;
      if(HistoryOrderGetString(ticket,ORDER_SYMBOL)!=symbol
         || HistoryOrderGetInteger(ticket,ORDER_MAGIC)!=magic)
         continue;
      long order_type=HistoryOrderGetInteger(ticket,ORDER_TYPE);
      if(!TradeOpsOrderTypeMatches(order_type,kinds))
         continue;
      long state=HistoryOrderGetInteger(ticket,ORDER_STATE);
      if(state==ORDER_STATE_REJECTED)
         continue;                        // never became a live order -- may retry
      if(StringFind(HistoryOrderGetString(ticket,ORDER_COMMENT),comment_prefix)==0)
         return true;
     }
   return false;
  }

class CTradeOps
  {
private:
   CTrade            m_trade;
   string            m_symbol;

   void              Fill(TradeResult &result,const bool sent)
     {
      result.retcode=m_trade.ResultRetcode();
      result.ok=sent && (result.retcode==TRADE_RETCODE_DONE
                         || result.retcode==TRADE_RETCODE_PLACED
                         || result.retcode==TRADE_RETCODE_DONE_PARTIAL);
      result.price=m_trade.ResultPrice();
      result.message=StringFormat("retcode=%u %s",result.retcode,m_trade.ResultRetcodeDescription());
     }

public:
   void              Init(const string symbol,const long magic)
     {
      m_symbol=symbol;
      m_trade.SetExpertMagicNumber((ulong)magic);
      m_trade.SetDeviationInPoints(TRADEOPS_DEVIATION_POINTS);
      m_trade.SetTypeFillingBySymbol(symbol);
      m_trade.SetAsyncMode(false);
      m_trade.LogLevel(LOG_LEVEL_ERRORS);
     }

   // Resting BUY STOP / SELL STOP with an attached SL and no TP, good till
   // cancelled (the strategy cancels it itself at its cutoff).
   void              PlaceStop(const bool is_buy,const double volume,const double price,
                               const double stop_loss,const string comment,TradeResult &result)
     {
      TradeOpsResetResult(result);
      bool sent=is_buy
                ? m_trade.BuyStop(volume,price,m_symbol,stop_loss,0.0,ORDER_TIME_GTC,0,comment)
                : m_trade.SellStop(volume,price,m_symbol,stop_loss,0.0,ORDER_TIME_GTC,0,comment);
      Fill(result,sent);
      result.ticket=m_trade.ResultOrder();
     }

   // Resting BUY LIMIT / SELL LIMIT with an attached SL and TP, good till
   // cancelled. A limit is how a strategy whose backtest fills inside the bar
   // (S004 enters at the zone edge) reproduces that fill live: the price is
   // parked in advance, so a touch fills at the modelled level instead of at
   // the close of the bar the EA noticed it on.
   void              PlaceLimit(const bool is_buy,const double volume,const double price,
                                const double stop_loss,const double take_profit,
                                const string comment,TradeResult &result)
     {
      TradeOpsResetResult(result);
      bool sent=is_buy
                ? m_trade.BuyLimit(volume,price,m_symbol,stop_loss,take_profit,ORDER_TIME_GTC,0,comment)
                : m_trade.SellLimit(volume,price,m_symbol,stop_loss,take_profit,ORDER_TIME_GTC,0,comment);
      Fill(result,sent);
      result.ticket=m_trade.ResultOrder();
     }

   void              Cancel(const ulong order_ticket,TradeResult &result)
     {
      TradeOpsResetResult(result);
      bool sent=m_trade.OrderDelete(order_ticket);
      Fill(result,sent);
      result.ticket=order_ticket;
     }

   void              Close(const ulong position_ticket,TradeResult &result)
     {
      TradeOpsResetResult(result);
      bool sent=m_trade.PositionClose(position_ticket);
      Fill(result,sent);
      result.ticket=m_trade.ResultDeal();
     }
  };

#endif // ALGOCORE_TRADEOPS_MQH
//+------------------------------------------------------------------+
