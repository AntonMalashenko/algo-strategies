//+------------------------------------------------------------------+
//| Scripts/AlgoTrading/S004_SizingProbe.mq5                         |
//| Diagnostic: every real S004 entry overnight 2026-10-08/09 sized  |
//| at the broker's absolute minimum lot (0.01) regardless of symbol |
//| or stop distance -- the signature of AlgoCore/Sizing.mqh's       |
//| degenerate-input fallback in LotsFor(), not a scaling error (a   |
//| scaling bug would still vary lot size symbol to symbol).         |
//|                                                                  |
//| This prints the exact inputs AlgoCore/Sizing.mqh::LotsFor() uses |
//| for each S004_SYMBOLS pair, at the current market, so the dead   |
//| branch (stop too small / no tick value / no risk amount) can be  |
//| read off directly instead of guessed from retcodes. Read-only:   |
//| it places nothing and touches no running EA's state.             |
//+------------------------------------------------------------------+
#property copyright "AlgoTrading (Anton Malashenko)"
#property version   "1.00"
#property script_show_inputs

#include <AlgoCore/GeneratedCore.mqh>
#include <AlgoCore/Sizing.mqh>
#include <Strategies/S004_FVG/Params.mqh>

input double InpRiskPct = S004_DEFAULT_RISK_PCT; // Risk per trade, % of balance (must match the live preset)
input double InpAssumedStopPips = 15.0;          // A representative stop distance, for symbols with no open S004 zone right now

void OnStart()
  {
   double balance=AccountInfoDouble(ACCOUNT_BALANCE);
   double risk_amount=balance*InpRiskPct/100.0;
   PrintFormat("balance=%.2f risk_pct=%.3f%% -> risk_amount=%.2f",balance,InpRiskPct,risk_amount);

   string symbols[];
   int count=StringSplit(S004_SYMBOLS,',',symbols);
   for(int i=0;i<count;i++)
     {
      string symbol=symbols[i];
      if(!SymbolSelect(symbol,true))
        {
         PrintFormat("%s: SymbolSelect failed",symbol);
         continue;
        }
      double point=SymbolInfoDouble(symbol,SYMBOL_POINT);
      double tick_size=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE);
      double tick_value_loss=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE_LOSS);
      double tick_value=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE);
      double money_per_point_per_lot=SizingMoneyPerPointPerLotForPointStep(
                                        SizingMoneyPerPointPerLot(symbol),point); // fixed 2026-10-09
      double ask=SymbolInfoDouble(symbol,SYMBOL_ASK);
      double bid=SymbolInfoDouble(symbol,SYMBOL_BID);
      double volume_min=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN);
      double volume_step=SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP);
      double volume_max=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX);

      double assumed_distance_price=InpAssumedStopPips*point*10.0; // S004_PIP_POINTS=10
      double stop_distance_points=(point>0.0) ? assumed_distance_price/point : 0.0;
      double lots=SizingLotsForRisk(risk_amount,stop_distance_points,money_per_point_per_lot,volume_min);
      double normalized=SizingNormalizeVolume(lots,volume_min,volume_step,volume_max);

      PrintFormat("%s: point=%.5f tick_size=%.5f tick_value_loss=%.5f tick_value=%.5f "
                  "money_per_point_per_lot=%.5f ask=%.5f bid=%.5f "
                  "volume(min/step/max)=%.2f/%.2f/%.2f -> stop_pts=%.1f raw_lots=%.4f normalized=%.4f",
                  symbol,point,tick_size,tick_value_loss,tick_value,money_per_point_per_lot,
                  ask,bid,volume_min,volume_step,volume_max,
                  stop_distance_points,lots,normalized);
     }
  }
//+------------------------------------------------------------------+
