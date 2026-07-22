#!/bin/bash
cd ~/Trading/algo
source .venv/bin/activate

# Single atomic time snapshot: decides TRADE vs SLEEP <secs> in one Python
# call. Two separate `date`/python invocations (bash reads the hour, then a
# later python call reads its own now()) can straddle the 10:00:00 boundary
# and disagree -- observed in practice: bash said "still before 10:00" while
# python, invoked a fraction of a second later, saw "past 10:00" and rolled
# the target to tomorrow, sleeping through the whole session.
window_decision() {
  python3 -c "
import datetime
now = datetime.datetime.now()
in_window = now.weekday() < 5 and 10 <= now.hour < 17
if in_window:
    print('TRADE')
else:
    target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    while target.weekday() >= 5:  # Sat=5, Sun=6
        target += datetime.timedelta(days=1)
    print(f'SLEEP {int((target - now).total_seconds())}')
"
}

while true; do
  now_hm=$(date +%H:%M)
  decision=$(window_decision)

  if [ "${decision%% *}" = "SLEEP" ]; then
    secs=${decision#SLEEP }
    echo "[$now_hm] outside session window, sleeping ${secs}s until next window"
    sleep "$secs"
    continue
  fi

  echo "=== [$now_hm] cycle ==="
  out=$(python -m bot.s007_paper --live 2>&1)
  echo "$out"

  if echo "$out" | grep -q "STATUS day_done=True .* actions=0"; then
    secs_decision=$(window_decision)
    # day is done -- if we're still nominally "in window" by the clock,
    # force a wait until tomorrow rather than re-polling every minute for
    # the rest of today.
    if [ "${secs_decision%% *}" = "SLEEP" ]; then
      secs=${secs_decision#SLEEP }
    else
      secs=$(python3 -c "
import datetime
now = datetime.datetime.now()
target = (now + datetime.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
while target.weekday() >= 5:
    target += datetime.timedelta(days=1)
print(int((target - now).total_seconds()))
")
    fi
    echo "[$now_hm] day done, nothing left to close -- sleeping ${secs}s until next window"
    sleep "$secs"
  else
    sleep 60
  fi
done
