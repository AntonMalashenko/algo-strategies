//+------------------------------------------------------------------+
//| AlgoCore/InstanceLock.mqh                                        |
//| One running copy per (login, symbol, magic).                     |
//|                                                                  |
//| Two copies of the same expert on the same account and symbol are |
//| indistinguishable to every reconcile path we have: both claim    |
//| the same magic number and the same position labels, both place   |
//| their own pair of resting stops, and both append to the same     |
//| JSONL log. Observed live on 2026-10-06 (ALGODEV-61): charts M1   |
//| and H1 each placed a full long/short pair, so a breakout would   |
//| have filled 3x the intended lot, and one of the four order       |
//| records was lost to the concurrent writes.                       |
//|                                                                  |
//| The lock is a terminal global variable holding the chart id of   |
//| the owner. It is temporary, so it never survives a terminal      |
//| restart, and a lock left behind by a crashed expert is taken     |
//| over as soon as its chart is gone -- the expert can always be    |
//| restarted, it just cannot be doubled.                            |
//+------------------------------------------------------------------+
#ifndef ALGOCORE_INSTANCELOCK_MQH
#define ALGOCORE_INSTANCELOCK_MQH

string InstanceLockName(const string prefix,const string symbol,const long magic)
  {
   return StringFormat("%s:%I64d:%s:%I64d",prefix,
                       AccountInfoInteger(ACCOUNT_LOGIN),symbol,magic);
  }

//--- Returns false when another live chart already owns the name.
bool InstanceLockAcquire(const string name)
  {
   if(MQLInfoInteger(MQL_TESTER))
      return true;
   if(GlobalVariableCheck(name))
     {
      const long owner=(long)GlobalVariableGet(name);
      if(owner!=ChartID() && ChartSymbol(owner)!="")
         return false;
     }
   else
      if(!GlobalVariableTemp(name))
         return false;
   return GlobalVariableSet(name,(double)ChartID())>0;
  }

void InstanceLockRelease(const string name)
  {
   if(MQLInfoInteger(MQL_TESTER))
      return;
   if(GlobalVariableCheck(name) && (long)GlobalVariableGet(name)==ChartID())
      GlobalVariableDel(name);
  }

#endif // ALGOCORE_INSTANCELOCK_MQH
