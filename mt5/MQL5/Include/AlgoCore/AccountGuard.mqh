//+------------------------------------------------------------------+
//| AlgoCore/AccountGuard.mqh                                        |
//| Account-level loss guard -- MQL5 port of bot/account_guard.py     |
//| ("variant A", ALGODEV-55): it only BLOCKS NEW RISK, open         |
//| positions are never force-closed by it.                          |
//|                                                                  |
//|   daily: (day_start_balance - balance) + open_risk + new_risk    |
//|            <= daily_loss_pct % of initial_balance                |
//|   max:   balance - open_risk - new_risk                          |
//|            >= initial_balance - max_loss_pct % of initial_balance |
//|                                                                  |
//| day_start_balance = balance - realised P&L since the firm's day  |
//| boundary, rebuilt from the deal history (survives restarts, no   |
//| stored state) -- same reconstruction as the Python module.       |
//+------------------------------------------------------------------+
#ifndef ALGOCORE_ACCOUNTGUARD_MQH
#define ALGOCORE_ACCOUNTGUARD_MQH

#include "Clock.mqh"
#include "GeneratedCore.mqh"   // ALGO_PCT, ALGO_REASON_DAILY, ALGO_REASON_MAX

struct AccountLimits
  {
   double            initial_balance;   // <= 0 -> guard off
   double            daily_loss_pct;    // <= 0 -> daily check off
   double            max_loss_pct;      // <= 0 -> max check off
   ENUM_TZ_RULE      day_reset_rule;    // firm's day boundary (FundingPips UTC+3, FTMO CE(S)T)
   int               day_reset_fixed_hours;
  };

struct GuardVerdict
  {
   bool              allowed;
   string            reason;
   double            day_start_balance;
   double            realized_today;
   double            worst_day_loss;
   double            daily_budget;
   double            worst_balance;
   double            max_loss_floor;
  };

void AccountGuardResetVerdict(GuardVerdict &verdict)
  {
   verdict.allowed=true;
   verdict.reason="";
   verdict.day_start_balance=0.0;
   verdict.realized_today=0.0;
   verdict.worst_day_loss=0.0;
   verdict.daily_budget=0.0;
   verdict.worst_balance=0.0;
   verdict.max_loss_floor=0.0;
  }

bool AccountGuardActive(const AccountLimits &limits)
  {
   return limits.initial_balance>0.0 && (limits.daily_loss_pct>0.0 || limits.max_loss_pct>0.0);
  }

// Pure check (no terminal I/O) -- bot/account_guard.py::check_new_risk.
void AccountGuardCheck(const AccountLimits &limits,const double balance,
                       const double realized_today,const double open_risk,
                       const double new_risk,GuardVerdict &verdict)
  {
   AccountGuardResetVerdict(verdict);
   if(!AccountGuardActive(limits))
      return;
   double initial=limits.initial_balance;
   verdict.realized_today=realized_today;
   verdict.day_start_balance=balance-realized_today;

   if(limits.daily_loss_pct>0.0)
     {
      verdict.daily_budget=initial*limits.daily_loss_pct/ALGO_PCT;
      verdict.worst_day_loss=(verdict.day_start_balance-balance)+open_risk+new_risk;
      if(verdict.worst_day_loss>verdict.daily_budget)
        {
         verdict.allowed=false;
         verdict.reason=ALGO_REASON_DAILY;
         return;
        }
     }
   if(limits.max_loss_pct>0.0)
     {
      verdict.max_loss_floor=initial-initial*limits.max_loss_pct/ALGO_PCT;
      verdict.worst_balance=balance-open_risk-new_risk;
      if(verdict.worst_balance<verdict.max_loss_floor)
        {
         verdict.allowed=false;
         verdict.reason=ALGO_REASON_MAX;
         return;
        }
     }
  }

// Realised P&L (profit + swap + commission + fee) of every trade deal on the
// account since `since_server` (server time) -- all symbols and strategies,
// like realized_pnl_since() over the broker's deal history. Entry-side
// commissions are included (MT5 books them on the opening deal).
double AccountGuardRealizedSince(const datetime since_server)
  {
   if(!HistorySelect(since_server,TimeTradeServer()+CLOCK_SECONDS_PER_DAY))
      return 0.0;
   double total=0.0;
   int deals=HistoryDealsTotal();
   for(int i=0;i<deals;i++)
     {
      ulong ticket=HistoryDealGetTicket(i);
      if(ticket==0)
         continue;
      long deal_type=HistoryDealGetInteger(ticket,DEAL_TYPE);
      if(deal_type!=DEAL_TYPE_BUY && deal_type!=DEAL_TYPE_SELL)
         continue;
      total+=HistoryDealGetDouble(ticket,DEAL_PROFIT)
             +HistoryDealGetDouble(ticket,DEAL_SWAP)
             +HistoryDealGetDouble(ticket,DEAL_COMMISSION)
             +HistoryDealGetDouble(ticket,DEAL_FEE);
     }
   return total;
  }

// Server time of the firm's most recent day boundary.
datetime AccountGuardDayStartServer(const AccountLimits &limits,const ENUM_TZ_RULE server_rule,
                                    const int server_fixed_hours)
  {
   datetime now_utc=ClockLocalToUtc(server_rule,server_fixed_hours,TimeTradeServer());
   datetime midnight_utc=ClockLocalMidnightUtc(limits.day_reset_rule,limits.day_reset_fixed_hours,
                                               now_utc);
   return ClockUtcToLocal(server_rule,server_fixed_hours,midnight_utc);
  }

// Earliest balance-type deal (the account's initial deposit) -- used when the
// caller configured no explicit initial balance. 0 when none is found.
double AccountGuardFirstDeposit()
  {
   if(!HistorySelect(0,TimeTradeServer()+CLOCK_SECONDS_PER_DAY))
      return 0.0;
   int deals=HistoryDealsTotal();
   for(int i=0;i<deals;i++)
     {
      ulong ticket=HistoryDealGetTicket(i);
      if(ticket==0)
         continue;
      if(HistoryDealGetInteger(ticket,DEAL_TYPE)==DEAL_TYPE_BALANCE)
        {
         double amount=HistoryDealGetDouble(ticket,DEAL_PROFIT);
         if(amount>0.0)
            return amount;
        }
     }
   return 0.0;
  }

#endif // ALGOCORE_ACCOUNTGUARD_MQH
//+------------------------------------------------------------------+
