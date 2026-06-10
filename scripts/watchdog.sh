#!/bin/bash
# Collector watchdog — keeps keepa_api_offers.py --loop alive so a crash can't
# silently waste Keepa tokens (an idle balance caps at 300, forfeiting refill).
#
# Checks every CHECK_INTERVAL seconds; if the loop isn't running, restarts it.
# Collector ONLY — it never touches the public dashboard/tunnel.
#
# Started by the Login Item (~/start_amazon_tracker.command) and runnable by hand:
#   nohup bash /Users/cagri/Desktop/amazon-tracker/scripts/watchdog.sh &

PIPELINE="/Users/cagri/Desktop/amazon-tracker/pipeline"
CHECK_INTERVAL="${CHECK_INTERVAL:-900}"   # 15 min

# One watchdog instance per key slot. When KEEPA_N_SLOTS=2 is set, run this
# script twice: once plain (slot 1) and once with KEEPA_API_KEY_SLOT=2.
SLOT="${KEEPA_API_KEY_SLOT:-1}"
# Default 2: dual-key is live. A loop launched without sharding thinks it owns
# the whole catalog and re-fetches the other slot's ASINs (~50% token waste).
N_SLOTS="${KEEPA_N_SLOTS:-2}"

# Per-slot logs so the two loops' output doesn't interleave
if [ "$SLOT" = "2" ]; then
  LOG="/Users/cagri/Desktop/amazon-tracker/logs/loop2.out.log"
else
  LOG="/Users/cagri/Desktop/amazon-tracker/logs/loop.out.log"
fi
WLOG="/Users/cagri/Desktop/amazon-tracker/logs/watchdog.log"

mkdir -p "$(dirname "$WLOG")"
echo "[$(date)] watchdog started (slot ${SLOT}, interval ${CHECK_INTERVAL}s, pid $$)" >> "$WLOG"

running() {
  # Match THIS slot's loop by its --slot argv. (Env vars never show in ps, so
  # the old `grep -v SLOT=2` could not tell the two loops apart.)
  ps ax -o pid,command | grep -i "[p]ython.*keepa_api_offers.py --loop --slot ${SLOT}" \
    >/dev/null 2>&1
}

while true; do
  if ! running; then
    echo "[$(date)] slot-${SLOT} loop is DOWN — restarting" >> "$WLOG"
    cd "$PIPELINE" && \
      nohup /usr/bin/python3 keepa_api_offers.py --loop --slot "$SLOT" --n-slots "$N_SLOTS" \
      >> "$LOG" 2>&1 &
    sleep 5
    if running; then
      echo "[$(date)] restart OK" >> "$WLOG"
    else
      echo "[$(date)] restart FAILED — check $LOG" >> "$WLOG"
    fi
  fi
  sleep "$CHECK_INTERVAL"
done
