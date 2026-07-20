#!/bin/bash
cd ~/Trading/algo
source .venv/bin/activate

# Seconds until the next session window (10:00 EET on the next weekday,
# skipping Sat/Sun). Used both to skip idle time outside 10:00-16:59 and to
# skip the rest of today once the strategy reports day_done with nothing
# left to close.
next_window_sleep_seconds() {
  python3 -c "
import datetime
now = datetime.datetime.now()
target = now.replace(hour=10, minute=0, second=0, microsecond=0)
if now >= target:
    target += datetime.timedelta(days=1)
while target.weekday() >= 5:  # Sat=5, Sun=6
    target += datetime.timedelta(days=1)
print(int((target - now).total_seconds()))
"
}

while true; do
  now_hm=$(date +%H:%M)
  now_h=$(date +%H)
  now_dow=$(date +%u)   # 1=Mon..7=Sun

  if [ "$now_dow" -ge 6 ] || [ "$now_h" -lt 10 ] || [ "$now_h" -ge 17 ]; then
    secs=$(next_window_sleep_seconds)
    echo "[$now_hm] outside session window, sleeping ${secs}s until next window"
    sleep "$secs"
    continue
  fi

  echo "=== [$now_hm] cycle ==="
  out=$(python -m bot.s007_paper --live 2>&1)
  echo "$out"

  if echo "$out" | grep -q "STATUS day_done=True .* actions=0"; then
    secs=$(next_window_sleep_seconds)
    echo "[$now_hm] day done, nothing left to close -- sleeping ${secs}s until next window"
    sleep "$secs"
  else
    sleep 60
  fi
done
