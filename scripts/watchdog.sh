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
LOG="/Users/cagri/Desktop/amazon-tracker/logs/loop.out.log"
WLOG="/Users/cagri/Desktop/amazon-tracker/logs/watchdog.log"
CHECK_INTERVAL="${CHECK_INTERVAL:-900}"   # 15 min

mkdir -p "$(dirname "$WLOG")"
echo "[$(date)] watchdog started (interval ${CHECK_INTERVAL}s, pid $$)" >> "$WLOG"

# Match the actual python process only (Python binary running the script), so we
# never self-match a shell whose command line merely contains the script name.
# Slot 1 (or single-key mode). When KEEPA_N_SLOTS=2 is set, this watchdog
# only guards slot 1; loop2.sh / com.amazontracker.loop2.plist handles slot 2.
SLOT="${KEEPA_API_KEY_SLOT:-1}"
N_SLOTS="${KEEPA_N_SLOTS:-1}"

running() {
  # Match slot-1 python loop (no KEEPA_API_KEY_SLOT=2 in env)
  ps ax -o pid,command | grep -i "[p]ython.*keepa_api_offers.py --loop" \
    | grep -v "SLOT=2" >/dev/null 2>&1
}

while true; do
  if ! running; then
    echo "[$(date)] slot-${SLOT} loop is DOWN — restarting" >> "$WLOG"
    cd "$PIPELINE" && KEEPA_API_KEY_SLOT=$SLOT KEEPA_N_SLOTS=$N_SLOTS \
      nohup /usr/bin/python3 keepa_api_offers.py --loop >> "$LOG" 2>&1 &
    sleep 5
    if running; then
      echo "[$(date)] restart OK" >> "$WLOG"
    else
      echo "[$(date)] restart FAILED — check $LOG" >> "$WLOG"
    fi
  fi
  sleep "$CHECK_INTERVAL"
done
