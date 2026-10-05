//+------------------------------------------------------------------+
//| AlgoCore/Sizing.mqh                                              |
//| Equal-dollar-risk position sizing -- MQL5 port of bot/risk.py's   |
//| lots_for_risk() plus the volume normalisation the cTrader bot    |
//| does in CTraderS007._volume_from_lots (clamp, then round to step).|
//|                                                                  |
//| Units: `stop_distance_points` is a PRICE distance (index points), |
//| not MT5 _Point ticks -- same meaning as in the Python bot.       |
//| money_per_point_per_lot = account-currency P&L of a 1.0 price    |
//| move at 1.0 lot, from the broker's own symbol metadata.           |
//+------------------------------------------------------------------+
#ifndef ALGOCORE_SIZING_MQH
#define ALGOCORE_SIZING_MQH

#include "GeneratedCore.mqh"   // ALGO_MIN_STOP_POINTS (generated from bot/risk.py)

// bot/risk.py::lots_for_risk -- "risk_amount, or the minimum lot if that
// doesn't fit". Degenerate inputs fall back to min_lot, exactly like Python.
double SizingLotsForRisk(const double risk_amount,const double stop_distance_points,
                         const double money_per_point_per_lot,const double min_lot)
  {
   if(!MathIsValidNumber(stop_distance_points) || stop_distance_points<ALGO_MIN_STOP_POINTS
      || !MathIsValidNumber(money_per_point_per_lot) || money_per_point_per_lot<=0.0
      || !MathIsValidNumber(risk_amount) || risk_amount<=0.0)
      return min_lot;
   double lots=risk_amount/(stop_distance_points*money_per_point_per_lot);
   return MathMax(lots,min_lot);
  }

// Clamp to [volume_min, volume_max], then round to a volume_step multiple
// (same order of operations as CTraderS007._volume_from_lots).
double SizingNormalizeVolume(const double lots,const double volume_min,
                             const double volume_step,const double volume_max)
  {
   double step=(volume_step>0.0) ? volume_step : volume_min;
   double clamped=MathMax(volume_min,MathMin(volume_max,lots));
   if(step<=0.0)
      return clamped;
   double rounded=MathRound(clamped/step)*step;
   int step_digits=(int)MathMax(0,MathCeil(-MathLog10(step)));
   return NormalizeDouble(rounded,step_digits);
  }

double SizingNormalizeVolumeForSymbol(const string symbol,const double lots)
  {
   return SizingNormalizeVolume(lots,
                                SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN),
                                SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP),
                                SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX));
  }

// Account-currency value of a 1.0 price move at 1.0 lot. Uses the LOSS-side
// tick value when the broker provides it (that is the side a stop pays).
double SizingMoneyPerPointPerLot(const string symbol)
  {
   double tick_size=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE);
   double tick_value=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tick_value<=0.0)
      tick_value=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_VALUE);
   if(tick_size<=0.0 || tick_value<=0.0)
      return 0.0;
   return tick_value/tick_size;
  }

// Price rounded to the symbol's tick grid and digits.
double SizingNormalizePrice(const string symbol,const double price)
  {
   double tick_size=SymbolInfoDouble(symbol,SYMBOL_TRADE_TICK_SIZE);
   int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
   if(tick_size<=0.0)
      return NormalizeDouble(price,digits);
   return NormalizeDouble(MathRound(price/tick_size)*tick_size,digits);
  }

#endif // ALGOCORE_SIZING_MQH
//+------------------------------------------------------------------+
